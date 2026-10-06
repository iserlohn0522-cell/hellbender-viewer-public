"""Pure parsers for the fixed read-only Slurm queries. No I/O."""
from __future__ import annotations
import math
import re
from datetime import datetime

SEP = "\x1f"
UNKNOWN = {"", "unknown", "n/a", "none", "not_set", "(null)", "invalid"}
ACTIVE = {"RUNNING", "PENDING", "COMPLETING", "CONFIGURING", "SUSPENDED", "RESIZING", "REQUEUED"}
TERMINAL = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}

# squeue --format letters, in the order of QUEUE_FIELDS.
QUEUE_FORMAT = ["%i", "%T", "%P", "%j", "%M", "%l", "%C", "%m", "%D", "%R", "%S", "%V", "%e", "%a", "%b", "%Z"]
QUEUE_FIELDS = ("jobid", "state", "partition", "name", "elapsed", "time_limit", "cpus", "memory", "node_count",
                "node_reason", "start", "submitted", "end", "account", "gres", "workdir")
ACCT_FORMAT = ("JobID,JobName%100,State%40,Partition,Elapsed,AllocCPUS,ReqMem,Start,End,ExitCode,Submit,"
               "NodeList%80,AllocTRES%120,Timelimit,Account")
ACCT_FIELDS = ("jobid", "name", "state", "partition", "elapsed", "cpus", "memory", "start", "end", "exit_code",
               "submitted", "nodes", "tres", "time_limit", "account")
NODE_FIELDS = ("node", "partition", "state", "cpus_state", "memory", "alloc_mem", "gres", "gres_used")
SINFO_FORMAT = ("NodeList", "Partition", "StateLong", "CPUsState", "Memory", "AllocMem", "Gres", "GresUsed")
SHARE_FORMAT = ("Account", "User", "RawShares", "NormShares", "RawUsage", "NormUsage", "EffectvUsage",
                "FairShare", "LevelFS", "TRESRunMins")

class ParseError(ValueError):
    pass

def rows(text: str, count: int, delimiter: str = SEP):
    if len(text) > 2_000_000:
        raise ParseError("Response exceeded 2 MB; narrow the query.")
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(delimiter)]
        if len(parts) != count:
            raise ParseError(f"Unexpected response at row {number}: expected {count} fields, got {len(parts)}. Cached data retained.")
        yield parts

def timestamp(value: str) -> str | None:
    if value is None or value.lower() in UNKNOWN:
        return None
    try:
        datetime.fromisoformat(value)
        return value
    except ValueError:
        return None

def normalized_state(value: str) -> str:
    return value.split()[0].rstrip("+") if value.strip() else "UNKNOWN"

def gres_text(value: str) -> str:
    value = value.strip()
    if value.lower() in UNKNOWN:
        return ""
    return ",".join(part.removeprefix("gres/").removeprefix("gres:") for part in value.split(","))

def parse_queue(text: str) -> list[dict]:
    result = []
    for values in rows(text, len(QUEUE_FIELDS)):
        job = dict(zip(QUEUE_FIELDS, values))
        if not re.fullmatch(r"[0-9][0-9_+\[\],%:-]*", job["jobid"]):
            raise ParseError("Unrecognized job identifier; cached queue retained.")
        job["state"] = normalized_state(job["state"])
        job["start"] = timestamp(job["start"])
        job["end"] = timestamp(job["end"])
        job["start_kind"] = "estimate" if job["state"] == "PENDING" else "actual"
        job["gres"] = gres_text(job["gres"])
        if job["workdir"].lower() in UNKNOWN:
            job["workdir"] = ""
        result.append(job)
    return result

def parse_history(text: str) -> list[dict]:
    """sacct --parsable2 rows. A '|' inside a job name is folded back into the name."""
    result = []
    count = len(ACCT_FIELDS)
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) > count:
            extra = len(parts) - count
            parts = [parts[0], "|".join(parts[1:2 + extra])] + parts[2 + extra:]
        if len(parts) != count:
            raise ParseError(f"Unexpected accounting row {number}: expected {count} fields, got {len(parts)}. Cached data retained.")
        job = dict(zip(ACCT_FIELDS, (p.strip() for p in parts)))
        if not re.fullmatch(r"\d+(?:_(?:\d+|\[[0-9,%:\-]+\]))?(?:\+\d+)?", job["jobid"]):
            raise ParseError("Unexpected accounting job ID; cached history retained.")
        job["state"] = normalized_state(job["state"])
        job["start"] = timestamp(job["start"])
        job["end"] = timestamp(job["end"])
        job["start_kind"] = "actual" if job["start"] else "unknown"
        job["tres"] = "" if job["tres"].lower() in UNKNOWN else job["tres"]
        result.append(job)
    return result

def number(value: str) -> float | None:
    value = (value or "").strip()
    if value.lower() in UNKNOWN:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if not math.isnan(parsed) else None

def gres_counts(value: str) -> dict[str, int]:
    """'gpu:L40S:2(S:0-1),gpu:1g.10gb:3(IDX:0-2)' -> {'L40S': 2, '1g.10gb': 3}; untyped GPUs use 'gpu'."""
    counts: dict[str, int] = {}
    text = re.sub(r"\([^)]*\)?", "", value or "")  # also drops a parenthesis cut off by a column width
    for item in text.split(","):
        item = item.strip().removeprefix("gres/")
        fields = item.split(":")
        if len(fields) < 2 or fields[0] != "gpu":
            continue
        kind = ":".join(fields[1:-1]) or "gpu"
        match = re.fullmatch(r"(\d+)", fields[-1].strip())
        if match:
            counts[kind] = counts.get(kind, 0) + int(match[1])
    return counts

def parse_nodes(text: str) -> list[dict]:
    """sinfo --Node -O rows. Every row keeps its partition; a node in two partitions appears twice."""
    result = {}
    for values in rows(text, len(NODE_FIELDS)):
        raw = dict(zip(NODE_FIELDS, values))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", raw["node"]):
            raise ParseError("Unrecognized node name in sinfo output; cached nodes retained.")
        cpus = re.fullmatch(r"(\d+)/(\d+)/(\d+)/(\d+)", raw["cpus_state"])
        node = {"node": raw["node"], "partition": raw["partition"].rstrip("*"),
                "default_partition": raw["partition"].endswith("*"), "state": raw["state"].lower(),
                "cpu_alloc": int(cpus[1]) if cpus else None, "cpu_idle": int(cpus[2]) if cpus else None,
                "cpu_other": int(cpus[3]) if cpus else None, "cpu_total": int(cpus[4]) if cpus else None,
                "mem_mb": number(raw["memory"].rstrip("+")), "mem_alloc_mb": number(raw["alloc_mem"]),
                "gpus": gres_counts(raw["gres"]), "gpus_used": gres_counts(raw["gres_used"])}
        result[(node["node"], node["partition"])] = node
    return list(result.values())

def tres(value: str) -> dict[str, float]:
    """'cpu=40,mem=80G,billing=40' -> numbers; memory-like suffixes are converted to MiB."""
    result = {}
    for item in (value or "").split(","):
        key, _, amount = item.partition("=")
        if not key or not amount:
            continue
        parsed = memory_mb(amount) if re.fullmatch(r"[\d.]+[KMGTP]", amount) else number(amount)
        if parsed is not None:
            result[key.strip()] = parsed
    return result

def parse_share(text: str) -> list[dict]:
    result = []
    for values in rows(text, len(SHARE_FORMAT), "|"):
        account, user, raw_shares, norm_shares, raw_usage, norm_usage, effective, fairshare, level, run_mins = values
        if not account:
            raise ParseError("Fairshare row without an account; cached fairshare retained.")
        level_value = None
        if level.lower() in ("inf", "infinity"):
            level_value = math.inf
        elif level:
            level_value = number(level)
        result.append({"account": account, "user": user, "raw_shares": raw_shares,
                       "norm_shares": number(norm_shares), "raw_usage": number(raw_usage),
                       "norm_usage": number(norm_usage), "effectv_usage": number(effective),
                       "fairshare": number(fairshare), "level_fs": level_value, "tres_run_mins": tres(run_mins)})
    users = [row for row in result if row["user"]]
    return users or result

def duration_seconds(value: str) -> int | None:
    """Slurm durations: D-HH:MM:SS, D-HH, D-HH:MM, HH:MM:SS, MM:SS, or minutes."""
    value = (value or "").strip()
    if value.upper() in ("UNLIMITED", "INFINITE", "NONE", "INVALID", "NOT_SET", "") or value.lower() in UNKNOWN:
        return None
    try:
        if "-" in value:
            days, rest = value.split("-", 1)
            parts = [int(v) for v in rest.split(":")] if rest else [0]
            parts += [0] * (3 - len(parts))
            hours, minutes, seconds = parts[:3]
            days = int(days)
        else:
            parts = [int(v) for v in value.split(":")]
            days = 0
            if len(parts) == 3:
                hours, minutes, seconds = parts
            elif len(parts) == 2:
                hours, minutes, seconds = 0, *parts
            elif len(parts) == 1:
                hours, minutes, seconds = 0, parts[0], 0
            else:
                return None
        if min(days, hours, minutes, seconds) < 0 or minutes > 59 or seconds > 59:
            return None
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    except (ValueError, TypeError):
        return None

elapsed_seconds = duration_seconds

def parse_priority_config(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().startswith("Priority"):
            values[key.strip()] = value.strip()
    if not values:
        raise ParseError("No priority settings returned; cached settings retained.")
    lower = {key.lower(): value for key, value in values.items()}
    get = lambda name: lower.get(name.lower(), "")
    flags = [f.strip().upper() for f in get("PriorityFlags").split(",") if f.strip()]
    return {"values": values, "half_life": duration_seconds(get("PriorityDecayHalfLife")), "flags": flags,
            "fair_tree": "NO_FAIR_TREE" not in flags and "multifactor" in (get("PriorityType") or "multifactor"),
            "weight_fairshare": number(get("PriorityWeightFairshare")), "weight_age": number(get("PriorityWeightAge")),
            "max_age": duration_seconds(get("PriorityMaxAge")), "reset_period": get("PriorityUsageResetPeriod")}

def memory_mb(value: str, cpus: float | None = None) -> float | None:
    """Slurm memory strings: 80G, 4000M, 1T, 80Gn (per node), 4Gc (per CPU)."""
    match = re.fullmatch(r"\s*([\d.]+)\s*([KMGTP]?)([nc]?)\s*", value or "")
    if not match:
        return None
    amount = float(match[1]) * {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 ** 2, "P": 1024 ** 3}[match[2]]
    if match[3] == "c":
        if not cpus:
            return None
        amount *= cpus
    return amount
