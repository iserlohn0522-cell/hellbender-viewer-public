import math
from pathlib import Path
import pytest
from hellbender.parsers import (SEP, ParseError, duration_seconds, gres_counts, memory_mb, parse_history, parse_nodes,
                                parse_priority_config, parse_queue, parse_share, timestamp, tres)

FIXTURES = Path(__file__).parent / "fixtures"

def fixture(name, sep=None):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return text.replace("|", sep) if sep else text

def test_queue_fields_estimates_arrays_and_paths():
    jobs = parse_queue(fixture("squeue.psv", SEP))
    assert len(jobs) == 5
    running, pending, array, gpu_pending, task = jobs
    assert running["start_kind"] == "actual" and running["workdir"] == "/home/user/vasp/run with space"
    assert running["end"] == "2026-10-07T14:50:04" and running["account"] == "lab" and running["gres"] == ""
    assert pending["start"] == "2026-10-08T18:30:00" and pending["start_kind"] == "estimate"
    assert array["jobid"] == "9100003_[0-53%3]" and array["start"] is None and array["gres"] == "gpu:1"
    assert gpu_pending["gres"] == "gpu:l40s:1" and gpu_pending["node_reason"] == "(Resources)"
    assert task["jobid"] == "9100005_4" and task["state"] == "RUNNING"

def test_empty_queue_is_valid_but_errors_are_not_empty_successes():
    assert parse_queue("") == []
    with pytest.raises(ParseError):
        parse_queue("slurm_load_jobs error: Socket timed out on send/recv operation")
    with pytest.raises(ParseError):
        parse_queue(fixture("squeue.psv", SEP).replace("9100001", "bad;id", 1))

def test_accounting_rows_pipes_in_names_and_states():
    jobs = parse_history(fixture("sacct.psv"))
    assert [j["jobid"] for j in jobs][:3] == ["9000001", "9000002", "9000003"]
    assert jobs[1]["name"] == "weird|name" and jobs[1]["state"] == "COMPLETED" and jobs[1]["account"] == "lab"
    assert jobs[2]["state"] == "CANCELLED" and jobs[2]["start"] is None and jobs[2]["tres"] == ""
    assert jobs[3]["tres"].endswith("node=1") and jobs[3]["time_limit"] == "01:00:00"
    assert jobs[4]["jobid"] == "9100003_[5-53%3]" and jobs[4]["state"] == "PENDING"
    assert jobs[5]["end"] is None
    with pytest.raises(ParseError):
        parse_history("1|broken")
    with pytest.raises(ParseError):
        parse_history(fixture("sacct.psv").replace("9000001", "abc", 1))

def test_nodes_from_sinfo_format_with_partitions_states_and_gres():
    nodes = parse_nodes(fixture("sinfo.psv", SEP))
    by = {(n["node"], n["partition"]): n for n in nodes}
    assert len(nodes) == 11
    mig = by[("gpu03", "gpu_requeue")]
    assert mig["gpus"] == {"1g.10gb": 28} and mig["gpus_used"] == {"1g.10gb": 20}
    assert by[("gpu13", "gpu")]["gpus_used"] == {"A100": 3}  # IDX list with a comma
    assert by[("gpu30", "gpu_requeue")]["state"] == "mixed-" and by[("gpu30", "gpu_requeue")]["gpus_used"] == {"H100": 0}
    idle = by[("gpu39", "gpu")]
    assert (idle["cpu_alloc"], idle["cpu_idle"], idle["cpu_total"]) == (0, 64, 64)
    assert idle["mem_mb"] == 502700 and idle["mem_alloc_mb"] == 0
    default = by[("cpu05", "requeue")]
    assert default["default_partition"] and default["gpus"] == {}

def test_nodes_tolerate_padding_and_reject_garbage():
    padded = "\n".join(SEP.join(f"{v:<24}" for v in line.split("|")) for line in fixture("sinfo.psv").splitlines())
    assert parse_nodes(padded) == parse_nodes(fixture("sinfo.psv", SEP))
    with pytest.raises(ParseError):
        parse_nodes("sinfo: error: Unable to contact slurm controller (connect failure)")

@pytest.mark.parametrize("text,expected", [
    ("gpu:L40S:2(S:0-1)", {"L40S": 2}),
    ("gpu:a100:2(IDX:0,2),gpu:1g.10gb:3(IDX:4-6)", {"a100": 2, "1g.10gb": 3}),
    ("gpu:2", {"gpu": 2}), ("gres/gpu:h100:1", {"h100": 1}), ("(null)", {}), ("", {}),
    ("gpu:1g.10gb:28(S:0-1", {"1g.10gb": 28}), ("shard:8,gpu:L40S:1(IDX:0)", {"L40S": 1})])
def test_gres_counts(text, expected):
    assert gres_counts(text) == expected

def test_fairshare_rows_include_infinite_level_and_running_tres():
    rows = parse_share(fixture("sshare.psv"))
    assert [r["account"] for r in rows] == ["rcss-general", "investor-lab"]
    first, second = rows
    assert first["level_fs"] == pytest.approx(0.480145) and first["fairshare"] == pytest.approx(0.4183)
    assert first["tres_run_mins"]["billing"] == 56210 and first["tres_run_mins"]["gres/gpu"] == 408
    assert math.isinf(second["level_fs"]) and second["raw_usage"] == 0

def test_fairshare_classic_algorithm_has_no_level_and_account_rows_are_dropped():
    text = "root||1|1.000000|100|1.0|1.0|0.5||cpu=0\nlab|me|1|0.25|10|0.1|0.1|0.758||cpu=0,billing=0\n"
    rows = parse_share(text)
    assert len(rows) == 1 and rows[0]["user"] == "me" and rows[0]["level_fs"] is None
    with pytest.raises(ParseError):
        parse_share("sshare: error: Problem talking to the database")

def test_priority_config_half_life_flags_and_weights():
    config = parse_priority_config(fixture("config.txt"))
    assert config["half_life"] == 7 * 86400 and config["fair_tree"] and config["flags"] == []
    assert config["weight_fairshare"] == 10000 and config["weight_age"] == 1000
    classic = parse_priority_config("PriorityFlags = NO_FAIR_TREE,CALCULATE_RUNNING\nPriorityDecayHalfLife = 14-0\nPriorityType = priority/multifactor")
    assert not classic["fair_tree"] and classic["half_life"] == 14 * 86400
    assert parse_priority_config("PriorityDecayHalfLife = 00:00:00\nPriorityType=priority/multifactor")["half_life"] == 0
    with pytest.raises(ParseError):
        parse_priority_config("")

@pytest.mark.parametrize("value,expected", [("1-02:03:04", 93784), ("05:30", 330), ("0:00", 0), ("2-00:00:00", 172800),
                                            ("12:00:00", 43200), ("7-0", 604800), ("1-12", 129600), ("30", 1800),
                                            ("UNLIMITED", None), ("1:88:00", None), ("INVALID", None), ("", None)])
def test_durations(value, expected):
    assert duration_seconds(value) == expected

@pytest.mark.parametrize("value,cpus,expected", [("80G", None, 81920), ("4000M", None, 4000), ("80Gn", None, 81920),
                                                 ("4Gc", 4, 16384), ("4Gc", None, None), ("1T", None, 1048576), ("lots", None, None)])
def test_memory(value, cpus, expected):
    assert memory_mb(value, cpus) == expected

def test_tres_and_timestamps():
    assert tres("cpu=40,mem=80G,billing=40,gres/gpu:l40s=1") == {"cpu": 40, "mem": 81920, "billing": 40, "gres/gpu:l40s": 1}
    assert timestamp("Unknown") is None and timestamp("2026-99-02T03:00:00") is None
    assert timestamp("2026-10-02T03:00:00") == "2026-10-02T03:00:00"
