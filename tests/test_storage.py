import json
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest
from hellbender.parsers import SEP, parse_history, parse_queue
from hellbender.storage import Store

FIXTURES = Path(__file__).parent / "fixtures"

def queue():
    return parse_queue((FIXTURES / "squeue.psv").read_text(encoding="utf-8").replace("|", SEP))

def accounting():
    return parse_history((FIXTURES / "sacct.psv").read_text(encoding="utf-8"))

def ok(**sections):
    return {"sections": {name: {"ok": True, "data": data} for name, data in sections.items()}, "since": "2026-10-01T00:00:00",
            "trace": {"plan": list(sections), "bytes": 10, "seconds": 0.5}}

def by_id(store, profile="p"):
    return {j["jobid"]: j for j in store.jobs(profile)}

def test_job_that_started_and_ended_between_refreshes_is_recorded(tmp_path):
    store = Store(tmp_path / "s.db")
    store.apply_refresh("p", ok(queue=queue()[:1]))
    store.apply_refresh("p", ok(queue=queue()[:1], acct=accounting()))
    jobs = by_id(store)
    unseen = jobs["9100005_3"]  # never appeared in any queue snapshot
    assert not unseen["active"] and unseen["state"] == "COMPLETED" and not unseen["queue_absent"]
    assert jobs["9000003"]["state"] == "CANCELLED"
    assert jobs["9100001"]["active"] and jobs["9100001"]["state"] == "RUNNING"  # accounting never overrides the queue
    assert "9100003_[5-53%3]" not in jobs  # non-final accounting rows are ignored

def test_vanished_job_stays_unknown_until_accounting_confirms(tmp_path):
    store = Store(tmp_path / "s.db")
    pending = queue()[1]
    store.apply_refresh("p", ok(queue=[pending]))
    summary = store.apply_refresh("p", ok(queue=[]))
    assert summary["vanished"] == 1
    job = by_id(store)["9100002"]
    assert not job["active"] and job["queue_absent"] and job["state"] == "PENDING"
    final = dict(accounting()[0], jobid="9100002", submitted=pending["submitted"])
    summary = store.apply_refresh("p", ok(acct=[final]))
    job = by_id(store)["9100002"]
    assert summary["resolved"] == 1 and job["state"] == "COMPLETED" and not job["queue_absent"]

def test_grouped_array_summary_is_superseded_not_unknown(tmp_path):
    store = Store(tmp_path / "s.db")
    array = queue()[2]
    store.apply_refresh("p", ok(queue=[array]))
    summary = store.apply_refresh("p", ok(queue=[array | {"jobid": "9100003_[3-53%3]"}]))
    old = by_id(store)["9100003_[0-53%3]"]
    assert summary["vanished"] == 0 and old["superseded"] and not old["active"]

def test_failed_sections_keep_previous_data_and_back_off(tmp_path):
    store = Store(tmp_path / "s.db")
    store.apply_refresh("p", ok(queue=queue(), share=[{"account": "a", "fairshare": .5}]), {"share_point": {"t": "x", "account": "a"}})
    result = ok(queue=queue())
    result["sections"]["share"] = {"ok": False, "error": "sshare: error"}
    result["sections"]["nodes"] = {"ok": False, "error": "timeout"}
    summary = store.apply_refresh("p", result)
    assert summary["failed"] == {"share": "sshare: error", "nodes": "timeout"}
    assert store.snapshot("p", "fairshare")["data"]["rows"][0]["fairshare"] == .5
    sync = store.snapshot("p", "_sync")["data"]
    assert sync["share"]["failures"] == 1 and sync["share"]["retry"] > sync["queue"]["ok_epoch"]
    assert sync["queue"]["failures"] == 0
    events = store.events("p")
    assert events[0]["kind"] == "warning" and "failed=share,nodes" in events[0]["message"]

def test_history_lists_are_capped(tmp_path):
    store = Store(tmp_path / "s.db")
    for index in range(410):
        store.apply_refresh("p", ok(nodes=[]), {"gpu_point": {"gpu|L40S": [index % 3, 6, 0]}})
    points = store.history("p", "gpu_history")
    assert len(points) == 400 and points[-1]["rows"]["gpu|L40S"][0] == 409 % 3

def test_refresh_budget_honours_older_per_category_cooldowns(tmp_path):
    store = Store(tmp_path / "s.db")
    assert store.reserve_read("p", "nodes", 900, clock=1000) == 0
    store.finish_read("p", "nodes", clock=1001)
    assert store.peek_read("p", "refresh", clock=1100) > 0  # old nodes cooldown still pending
    before = store.snapshot("p", "_read_budget")
    assert store.reserve_read("p", "refresh", 300, clock=1100) > 0
    assert store.snapshot("p", "_read_budget") == before  # a rejected click changes nothing
    assert store.reserve_read("p", "refresh", 300, clock=1901) == 0
    assert store.peek_read("p", "refresh", clock=1950) > 0
    store.finish_read("p", "refresh", failed=True, clock=1960)
    assert store.peek_read("p", "refresh", clock=1901 + 600) > 0  # failure backoff doubles the floor

def test_persistence_in_a_fresh_python_process(tmp_path):
    path = tmp_path / "viewer.sqlite3"
    store = Store(path)
    store.apply_refresh("hellbender", ok(queue=queue()))
    store.save_settings({"skin": "Daylight", "watch_gpus": "L40S,H100"})
    code = ("from pathlib import Path; from hellbender.storage import Store; import json,sys; s=Store(Path(sys.argv[1]));"
            "print(json.dumps([s.settings(), len(s.jobs('hellbender'))]))")
    response = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True,
                              cwd=Path(__file__).resolve().parents[1])
    settings, count = json.loads(response.stdout)
    assert settings["skin"] == "Daylight" and settings["watch_gpus"] == "L40S,H100" and count == 5

def test_bad_record_rolls_back_the_whole_merge(tmp_path):
    store = Store(tmp_path / "s.db")
    store.merge_jobs("a", queue()[:1], "queue")
    previous = store.snapshot("a", "queue")
    with pytest.raises(KeyError):
        store.merge_jobs("a", [queue()[0] | {"state": "PENDING"}, {"not_a_job": True}], "queue")
    assert store.jobs("a")[0]["state"] == "RUNNING" and store.jobs("a")[0]["active"]
    assert store.snapshot("a", "queue") == previous

def test_export_never_overwrites_and_newer_schema_is_refused(tmp_path):
    path = tmp_path / "data.sqlite3"
    store = Store(path)
    store.apply_refresh("a", ok(queue=queue()))
    destination = tmp_path / "export.json"
    store.export(destination, "a")
    assert len(json.loads(destination.read_text())["jobs"]) == 5
    with pytest.raises(FileExistsError):
        store.export(destination, "a")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=2")
    with pytest.raises(ValueError, match="newer"):
        Store(path)
