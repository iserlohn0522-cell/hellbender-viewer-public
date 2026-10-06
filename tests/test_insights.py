import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from hellbender import insights as ins
from hellbender.parsers import SEP, parse_nodes, parse_share

FIXTURES = Path(__file__).parent / "fixtures"
SHAPE = {"cpus": 4, "mem_mb": 32 * 1024, "gpus": 1}

def nodes():
    return parse_nodes((FIXTURES / "sinfo.psv").read_text(encoding="utf-8").replace("|", SEP))

@pytest.mark.parametrize("state,expected", [("idle", "ok"), ("mixed", "ok"), ("allocated", "ok"), ("completing", "ok"),
                                            ("mixed-", "planned"), ("planned", "planned"), ("drained", "unavailable"),
                                            ("draining", "unavailable"), ("down*", "unavailable"), ("idle~", "unavailable"),
                                            ("mixed*", "unavailable"), ("reserved", "unavailable"), ("idle$", "unavailable"),
                                            ("maint", "unavailable"), ("inval", "unavailable"), ("", "unavailable")])
def test_node_condition(state, expected):
    assert ins.node_condition(state) == expected

def test_gpu_table_counts_free_usable_held_and_offline():
    rows = {(r["partition"], r["type"]): r for r in ins.gpu_table(nodes(), SHAPE, ["l40s"])}
    l40s = rows[("gpu", "L40S")]
    assert (l40s["total"], l40s["used"], l40s["free"], l40s["usable"], l40s["offline"]) == (6, 1, 3, 3, 2)
    assert l40s["watched"] and [n["node"] for n in l40s["free_nodes"]] == ["gpu38", "gpu39"]
    a100 = rows[("gpu", "A100")]
    assert (a100["free"], a100["usable"]) == (1, 0)  # gpu13 has a free card but only 2 idle CPUs
    h100 = rows[("gpu_requeue", "H100")]
    assert (h100["free"], h100["held"]) == (0, 2)  # planned node: held for a waiting job
    mig = rows[("gpu_requeue", "1g.10gb")]
    assert (mig["free"], mig["usable"]) == (8, 1)
    assert all("gpu" in partition for partition, _ in rows)
    first = ins.gpu_table(nodes(), SHAPE, ["l40s"])[0]
    assert first["type"] == "L40S" and first["partition"] == "gpu"
    assert ins.gpu_point([l40s]) == {"gpu|L40S": [3, 6, 3]}

def test_memory_limits_usable_gpus_and_untyped_usage_maps_to_the_only_type():
    node = {"node": "g1", "partition": "gpu", "state": "mixed", "cpu_idle": 64, "mem_mb": 100_000, "mem_alloc_mb": 80_000,
            "gpus": {"L40S": 2}, "gpus_used": {"gpu": 0}}
    row = ins.gpu_table([node], SHAPE)[0]
    assert (row["free"], row["usable"]) == (2, 0)
    assert ins.gpu_table([node | {"mem_alloc_mb": 0}], SHAPE)[0]["usable"] == 2
    assert ins.gpu_table([node | {"gpus_used": {"gpu": 1}}], SHAPE)[0]["free"] == 1

def test_cpu_table_counts_nodes_that_fit_your_usual_job():
    rows = {r["partition"]: r for r in ins.cpu_table(nodes(), {"cpus": 40, "mem_mb": 80 * 1024, "gpus": 0})}
    assert rows["general"]["fit_nodes"] == 1 and rows["general"]["idle_cpus"] == 128 and rows["general"]["nodes"] == 2
    assert [r["partition"] for r in ins.cpu_table(nodes(), SHAPE)] == ["general", "requeue"]

def test_usual_shape_needs_three_jobs_and_settings_override():
    jobs = [{"partition": "gpu", "cpus": "8", "memory": "64G", "gres": "gpu:l40s:1"} for _ in range(3)]
    assert ins.usual_shape(jobs[:2], True)["cpus"] == 4
    shape = ins.usual_shape(jobs, True)
    assert (shape["cpus"], shape["mem_mb"], shape["gpus"]) == (8, 65536, 1)
    assert ins.usual_shape(jobs, True, cpus=2, mem_gb=16)["mem_mb"] == 16384
    assert ins.shape_text(shape) == "8 核 · 64 GB · 1 卡"

def share_row(**changes):
    row = parse_share((FIXTURES / "sshare.psv").read_text(encoding="utf-8"))[0]
    return row | changes

def test_fair_tree_projection_matches_closed_form_without_running_jobs():
    row = share_row(tres_run_mins={})
    out = ins.fairshare_outlook(row, {"half_life": 7 * 86400, "fair_tree": True})
    u0, eu0, ns = row["raw_usage"], row["effectv_usage"], row["norm_shares"]
    others = u0 / eu0 - u0
    scale = row["level_fs"] / (ns / eu0)
    expected = lambda t: scale * ns * (u0 * 0.5 ** (t / (7 * 86400)) + others) / (u0 * 0.5 ** (t / (7 * 86400)))
    series = dict(out["series"])
    assert out["mode"] == "fair_tree" and out["projectable"] and not out["half_life_assumed"]
    assert series[0] == pytest.approx(row["level_fs"])
    assert series[7 * 86400] == pytest.approx(expected(7 * 86400), rel=1e-6)
    target_usage = scale * ns * others / (1 - scale * ns)  # scale*ns*(u+o)/u = 1
    crossing = 7 * 86400 * math.log2(u0 / target_usage)
    assert abs(out["cross"] - crossing) <= 3600
    assert out["low"][0] == 0

def test_running_jobs_delay_recovery_and_can_push_the_value_down_first():
    calm = ins.fairshare_outlook(share_row(), {"half_life": 7 * 86400})
    busy = ins.fairshare_outlook(share_row(tres_run_mins={"billing": 40 * 2 * 86400 / 60 * 2}), {"half_life": 7 * 86400},
                                 running=[{"cpus": 40, "remaining": 2 * 86400}])
    assert busy["cross"] > calm["cross"]
    assert busy["low"][1] < busy["series"][0][1] and busy["low"][0] > 0
    assert busy["running_billing"] > 0

def test_classic_projection_calibrates_to_reported_factor_and_rises():
    row = share_row(level_fs=None, fairshare=0.25, tres_run_mins={})
    out = ins.fairshare_outlook(row, {"half_life": 7 * 86400, "fair_tree": False})
    assert out["mode"] == "classic" and out["target"] == 0.5
    assert out["series"][0][1] == pytest.approx(0.25)
    assert out["series"][-1][1] > 0.25 and out["cross"] is not None

def test_unprojectable_and_assumed_half_life():
    out = ins.fairshare_outlook(share_row(raw_usage=0), {})
    assert not out["projectable"] and out["half_life_assumed"] and out["half_life"] == 7 * 86400
    above = ins.fairshare_outlook(share_row(level_fs=1.5, tres_run_mins={}), {"half_life": 7 * 86400})
    assert above["cross"] == 0

def test_pick_share_prefers_the_account_your_jobs_use():
    rows = parse_share((FIXTURES / "sshare.psv").read_text(encoding="utf-8"))
    assert ins.pick_share(rows, [{"active": True, "account": "investor-lab"}])["account"] == "investor-lab"
    assert ins.pick_share(rows, [])["account"] == "rcss-general"
    assert ins.pick_share([], []) is None

def test_cluster_times_are_us_central_with_or_without_tzdata(monkeypatch):
    summer = ins.cluster_time("2026-07-01T12:00:00")
    winter = ins.cluster_time("2026-12-01T12:00:00")
    assert summer.utcoffset() == timedelta(hours=-5) and winter.utcoffset() == timedelta(hours=-6)
    assert ins.cluster_string(datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)) == "2026-10-06T12:00:00"
    monkeypatch.setattr(ins, "CLUSTER_TZ", None)
    assert ins.cluster_time("2026-03-08T01:30:00").utcoffset() == timedelta(hours=-6)
    assert ins.cluster_time("2026-03-08T03:30:00").utcoffset() == timedelta(hours=-5)
    assert ins.cluster_time("2026-11-01T01:00:00").utcoffset() == timedelta(hours=-5)
    assert ins.cluster_time("2026-11-01T03:00:00").utcoffset() == timedelta(hours=-6)
    assert ins.cluster_string(datetime(2026, 12, 6, 18, 0, tzinfo=timezone.utc)) == "2026-12-06T12:00:00"
    assert ins.cluster_time("garbage") is None and ins.cluster_time(None) is None

def test_friendly_times():
    now = datetime(2026, 10, 6, 15, 0, tzinfo=timezone(timedelta(hours=-5)))  # a Tuesday
    at = lambda **kw: now + timedelta(**kw)
    assert ins.when_text(at(hours=2), now) == "今天 17:00"
    assert ins.when_text(at(days=1), now) == "明天 15:00"
    assert ins.when_text(at(days=2), now) == "后天 15:00"
    assert ins.when_text(at(days=3), now) == "周五 15:00"
    assert ins.when_text(at(days=-1), now) == "昨天 15:00"
    assert ins.when_text(at(days=-3), now) == "10/3 15:00"
    assert ins.when_text(at(days=9), now) == "10/15 15:00"
    assert ins.relative_text(at(hours=50), now) == "2 天 2 小时后"
    assert ins.relative_text(at(minutes=-90), now) == "1 小时 30 分前"
    assert ins.compact_span(45 * 60) == "45m" and ins.compact_span(5.25 * 3600) == "5.2h" and ins.compact_span(48 * 3600) == "48h"
    assert ins.clock_text(61.2) == "1:02"

def test_job_status_never_invents_a_result():
    assert ins.job_status({"active": True, "state": "PENDING"}) == "PENDING"
    assert ins.job_status({"active": False, "state": "RUNNING", "queue_absent": True}) == "VANISHED"
    assert ins.job_status({"active": False, "state": "COMPLETED", "queue_absent": False}) == "COMPLETED"
    assert ins.job_status({"active": False, "state": "FAILED", "queue_absent": True, "state_confirmed": True}) == "FAILED"
    assert ins.status_text("VANISHED") == "结果未知" and ins.status_tone("FAILED") == "bad"

def test_reasons():
    assert ins.reason_text("(Priority)") == "前面还有优先级更高的作业"
    assert ins.reason_short("(Resources)") == "等资源"
    assert ins.reason_text("(QOSMaxGRESPerUser)") == "达到每人 GPU 上限"
    assert ins.reason_short("(QOSGrpGRESRunMinutes)") == "QOS 上限"
    assert ins.reason_text("(Something new)") == "Something new"

def test_typical_runtime_and_wait_need_three_examples():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    done = [{"active": False, "state": "COMPLETED", "partition": "general", "cpus": "40", "name": f"ads_{i}",
             "elapsed": f"1-{10 + i}:00:00", "start": f"2026-10-0{i + 2}T10:00:00", "submitted": f"2026-10-0{i + 1}T10:00:00"}
            for i in range(3)]
    job = {"active": True, "state": "RUNNING", "partition": "general", "cpus": "40", "name": "ads_new"}
    assert ins.typical_runtime(job, done) == (35 * 3600, 3)
    assert ins.typical_runtime(job, done[:2]) is None
    assert ins.typical_runtime(job | {"cpus": "8"}, done) is None
    assert ins.typical_wait("general", done, now) == (86400, 3)
    assert ins.typical_wait("gpu", done, now) is None

def test_recovery_beyond_the_chart_is_solved_in_closed_form():
    row = {"account": "lab", "norm_shares": 0.0003, "raw_usage": 300_000_000.0, "effectv_usage": 0.015,
           "fairshare": 0.025, "level_fs": 0.02, "tres_run_mins": {}}
    out = ins.fairshare_outlook(row, {"half_life": 14 * 86400, "fair_tree": True})
    others = row["raw_usage"] / row["effectv_usage"] - row["raw_usage"]
    scale = row["level_fs"] / (row["norm_shares"] / row["effectv_usage"])
    goal = scale * row["norm_shares"] * others / (1 - scale * row["norm_shares"])
    expected = 14 * 86400 * math.log2(row["raw_usage"] / goal)
    assert out["cross_beyond"] and abs(out["cross"] - expected) < 3600
    assert 75 * 86400 < out["cross"] < 90 * 86400
    still_running = ins.fairshare_outlook(row | {"tres_run_mins": {"billing": 1e9}}, {"half_life": 14 * 86400},
                                          running=[{"cpus": 40, "remaining": 20 * 86400}])
    assert still_running["cross"] is None and not still_running["cross_beyond"]
