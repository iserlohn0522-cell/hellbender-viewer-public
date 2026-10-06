"""Local interpretation of saved snapshots: free GPUs, fairshare outlook, waits and friendly times.

Pure functions. Nothing here touches the network or the database.
"""
from __future__ import annotations
import math
import re
import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from .parsers import TERMINAL, duration_seconds, gres_counts, memory_mb, number, tres

# ---------------------------------------------------------------- time

try:
    from zoneinfo import ZoneInfo
    CLUSTER_TZ = ZoneInfo("America/Chicago")  # Hellbender prints cluster-local (US Central) times
except Exception:  # pragma: no cover - tzdata missing
    CLUSTER_TZ = None

WEEKDAYS = "一二三四五六日"

def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))

def _central_offset(naive: datetime) -> int:
    """US Central without tzdata: UTC-5 from the 2nd Sunday of March to the 1st Sunday of November."""
    start = datetime.combine(_nth_sunday(naive.year, 3, 2), datetime.min.time()) + timedelta(hours=2)
    end = datetime.combine(_nth_sunday(naive.year, 11, 1), datetime.min.time()) + timedelta(hours=2)
    return -5 if start <= naive < end else -6

def cluster_time(value) -> datetime | None:
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if naive.tzinfo:
        return naive
    zone = CLUSTER_TZ or timezone(timedelta(hours=_central_offset(naive)))
    return naive.replace(tzinfo=zone)

def utc_time(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

def cluster_string(moment: datetime) -> str:
    """Aware moment -> cluster-local 'YYYY-MM-DDTHH:MM:SS' (sacct input)."""
    if CLUSTER_TZ:
        return moment.astimezone(CLUSTER_TZ).strftime("%Y-%m-%dT%H:%M:%S")
    utc = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return (utc + timedelta(hours=_central_offset(utc - timedelta(hours=6)))).strftime("%Y-%m-%dT%H:%M:%S")

def local_now() -> datetime:
    return datetime.now().astimezone()

def when_text(moment: datetime | None, now: datetime | None = None) -> str:
    if moment is None:
        return "—"
    now = now or local_now()
    local = moment.astimezone(now.tzinfo)
    days = (local.date() - now.date()).days
    day = {0: "今天", 1: "明天", 2: "后天", -1: "昨天"}.get(days)
    if day is None:
        day = "周" + WEEKDAYS[local.weekday()] if 2 < days < 7 else f"{local.month}/{local.day}"
    return f"{day} {local:%H:%M}"

def span_text(seconds: float) -> str:
    seconds = abs(int(seconds))
    if seconds < 60:
        return "不到 1 分钟"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {minutes} 分" if minutes and hours < 10 else "")
    days, hours = divmod(hours, 24)
    return f"{days} 天" + (f" {hours} 小时" if hours else "")

def relative_text(moment: datetime, now: datetime | None = None) -> str:
    delta = (moment - (now or local_now())).total_seconds()
    if abs(delta) < 60:
        return "现在"
    return span_text(delta) + ("后" if delta > 0 else "前")

def age_text(stamp, now: datetime | None = None) -> str:
    moment = utc_time(stamp)
    if not moment:
        return "从未"
    seconds = ((now or datetime.now(timezone.utc)) - moment).total_seconds()
    return "刚刚" if seconds < 60 else span_text(seconds) + "前"

def clock_text(seconds: float) -> str:
    seconds = max(0, int(math.ceil(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"

def compact_span(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 100 * 3600:
        return f"{seconds / 3600:.1f}h".replace(".0h", "h")
    return f"{seconds / 86400:.1f}d".replace(".0d", "d")

# ---------------------------------------------------------------- jobs

STATE_TEXT = {"RUNNING": "运行中", "PENDING": "排队中", "COMPLETING": "收尾中", "CONFIGURING": "启动中",
              "SUSPENDED": "已挂起", "REQUEUED": "重新排队", "RESIZING": "调整中", "COMPLETED": "完成",
              "FAILED": "失败", "TIMEOUT": "超时", "CANCELLED": "已取消", "OUT_OF_MEMORY": "内存不足",
              "NODE_FAIL": "节点故障", "PREEMPTED": "被抢占", "BOOT_FAIL": "启动失败", "DEADLINE": "到截止时间",
              "VANISHED": "结果未知", "UNKNOWN": "未知"}
STATE_TONE = {"RUNNING": "good", "COMPLETING": "good", "CONFIGURING": "good", "PENDING": "warn", "REQUEUED": "warn",
              "SUSPENDED": "warn", "COMPLETED": "accent", "CANCELLED": "accent2", "PREEMPTED": "accent2",
              "VANISHED": "muted", "UNKNOWN": "muted"}

def job_status(job: dict) -> str:
    state = job.get("state") or "UNKNOWN"
    if job.get("active"):
        return state
    if state in TERMINAL and (job.get("state_confirmed") or not job.get("queue_absent")):
        return state
    return "VANISHED"

def status_text(code: str) -> str:
    return STATE_TEXT.get(code, code.title())

def status_tone(code: str) -> str:
    return STATE_TONE.get(code, "bad")

def is_grouped(job: dict) -> bool:
    return "[" in job.get("jobid", "")

REASONS = {
    "Priority": "前面还有优先级更高的作业",
    "Resources": "已轮到你，正在等资源空出来",
    "Dependency": "在等所依赖的作业结束",
    "DependencyNeverSatisfied": "依赖永远无法满足，需要处理",
    "JobArrayTaskLimit": "数组并发上限，前面的任务结束后自动开始",
    "BeginTime": "设定了最早开始时间",
    "JobHeldUser": "被你挂起（hold）",
    "JobHeldAdmin": "被管理员挂起",
    "ReqNodeNotAvail": "请求的节点暂不可用（维护或预留）",
    "Reservation": "在等预留时段",
    "PartitionTimeLimit": "申请时长超过分区上限",
    "PartitionNodeLimit": "申请节点数超过分区上限",
    "PartitionDown": "分区已停用",
    "PartitionInactive": "分区未启用",
    "BadConstraints": "约束条件无法满足",
    "NodeDown": "所需节点故障",
    "Licenses": "在等许可证",
    "QOSMaxJobsPerUserLimit": "达到每人同时运行作业数上限",
    "QOSMaxSubmitJobPerUserLimit": "达到每人提交数上限",
    "QOSMaxCpuPerUserLimit": "达到每人 CPU 上限",
    "QOSMaxGRESPerUser": "达到每人 GPU 上限",
    "QOSMaxNodePerUserLimit": "达到每人节点数上限",
    "QOSMaxMemoryPerUser": "达到每人内存上限",
    "AssocGrpCPUMinutesLimit": "账户 CPU 时数已用完",
    "AssocGrpBillingMinutes": "账户计费时数已用完",
    "AssocMaxJobsLimit": "账户同时作业数上限",
    "None": "调度器还没处理到它",
}

def reason_text(raw: str) -> str:
    reason = (raw or "").strip().strip("()").strip()
    if not reason:
        return ""
    key = reason.split(",")[0].strip()
    if key in REASONS:
        return REASONS[key]
    if key.startswith("QOS"):
        return f"QOS 限制（{key}）"
    if key.startswith("Assoc"):
        return f"账户限制（{key}）"
    if "Nodes required for job are DOWN" in reason or "launch failed" in reason:
        return "所需节点故障或启动失败"
    return reason

SHORT_REASONS = {"Priority": "优先级排队", "Resources": "等资源", "Dependency": "等依赖", "JobArrayTaskLimit": "数组并发上限",
                 "BeginTime": "定时开始", "JobHeldUser": "已挂起", "JobHeldAdmin": "管理员挂起", "ReqNodeNotAvail": "节点不可用",
                 "Reservation": "等预留", "DependencyNeverSatisfied": "依赖无法满足"}

def reason_short(raw: str) -> str:
    key = (raw or "").strip().strip("()").split(",")[0].strip()
    if key in SHORT_REASONS:
        return SHORT_REASONS[key]
    if key.startswith("QOS"):
        return "QOS 上限"
    if key.startswith("Assoc"):
        return "账户上限"
    return reason_text(raw)

def is_gpu_partition(name: str) -> bool:
    return "gpu" in (name or "").lower()

def job_gpus(job: dict) -> int:
    counts = gres_counts(job.get("gres", ""))
    if counts:
        return sum(counts.values())
    allocated = tres(job.get("tres", ""))
    return int(allocated.get("gres/gpu", 0))

def job_gpu_label(job: dict) -> str:
    counts = gres_counts(job.get("gres", ""))
    if not counts:
        allocated = tres(job.get("tres", ""))
        counts = {k.split(":", 1)[1]: int(v) for k, v in allocated.items() if k.startswith("gres/gpu:")}
        if not counts and allocated.get("gres/gpu"):
            counts = {"gpu": int(allocated["gres/gpu"])}
    return " + ".join(f"{gpu_label(k)} × {v}" for k, v in counts.items())

def job_cpus(job: dict) -> int | None:
    value = number(str(job.get("cpus", "")))
    return int(value) if value else None

def job_mem_mb(job: dict) -> float | None:
    return memory_mb(job.get("memory", ""), cpus=job_cpus(job))

def walltime(job: dict) -> tuple[int | None, int | None]:
    return duration_seconds(job.get("elapsed", "")), duration_seconds(job.get("time_limit", ""))

def name_stem(name: str) -> str:
    return re.split(r"[_\-.\s]+", (name or "").strip().lower(), maxsplit=1)[0]

def typical_runtime(job: dict, jobs: list[dict]) -> tuple[int, int] | None:
    """Median elapsed of completed jobs with the same name stem, partition and CPU count."""
    stem, cpus = name_stem(job.get("name", "")), job_cpus(job)
    values = []
    for other in jobs:
        if other is job or other.get("active") or job_status(other) != "COMPLETED":
            continue
        if other.get("partition") != job.get("partition") or job_cpus(other) != cpus or name_stem(other.get("name", "")) != stem:
            continue
        seconds = duration_seconds(other.get("elapsed", ""))
        if seconds and seconds >= 60:
            values.append(seconds)
    return (int(statistics.median(values)), len(values)) if len(values) >= 3 else None

def typical_wait(partition: str, jobs: list[dict], now: datetime | None = None, days: int = 14) -> tuple[int, int] | None:
    """Median submit-to-start wait of your jobs that started in this partition recently."""
    now = now or local_now()
    waits = []
    for job in jobs:
        if job.get("partition") != partition or job.get("state") == "PENDING" or job.get("start_kind") == "estimate":
            continue
        start, submitted = cluster_time(job.get("start")), cluster_time(job.get("submitted"))
        if start and submitted and start >= submitted and (now - start).total_seconds() <= days * 86400:
            waits.append((start - submitted).total_seconds())
    return (int(statistics.median(waits)), len(waits)) if len(waits) >= 3 else None

def usual_shape(jobs: list[dict], gpu: bool, cpus: int = 0, mem_gb: float = 0) -> dict:
    sample = [j for j in jobs if is_gpu_partition(j.get("partition", "")) == gpu]
    shape = {"cpus": 4, "mem_mb": 32 * 1024, "gpus": 1} if gpu else {"cpus": 16, "mem_mb": 32 * 1024, "gpus": 0}
    shape["source"] = "默认值"
    cpu_values = [c for j in sample if (c := job_cpus(j))]
    mem_values = [m for j in sample if (m := job_mem_mb(j))]
    if len(cpu_values) >= 3:
        shape["cpus"], shape["source"] = int(statistics.median(cpu_values)), "按你最近的作业"
    if len(mem_values) >= 3:
        shape["mem_mb"] = float(statistics.median(mem_values))
    if gpu:
        gpu_values = [g for j in sample if (g := job_gpus(j))]
        if len(gpu_values) >= 3:
            shape["gpus"] = max(1, int(statistics.median(gpu_values)))
    if cpus and cpus > 0:
        shape["cpus"], shape["source"] = int(cpus), "按设置"
    if mem_gb and mem_gb > 0:
        shape["mem_mb"], shape["source"] = float(mem_gb) * 1024, "按设置"
    return shape

def shape_text(shape: dict) -> str:
    text = f"{shape['cpus']} 核 · {shape['mem_mb'] / 1024:g} GB"
    return text + (f" · {shape['gpus']} 卡" if shape.get("gpus") else "")

# ---------------------------------------------------------------- nodes and GPUs

BAD_FLAGS = set("*~#!%$@^")
UNAVAILABLE_WORDS = ("drain", "down", "fail", "maint", "reserv", "inval", "unk", "future", "block", "power", "reboot")

def node_condition(state: str) -> str:
    """'ok' accepts new work, 'planned' is held for a waiting job, 'unavailable' accepts none."""
    raw = (state or "").strip().lower()
    base = re.split(r"[+*~#!%$@^\-]", raw, maxsplit=1)[0]
    if any(flag in raw for flag in BAD_FLAGS) or any(word in raw for word in UNAVAILABLE_WORDS):
        return "unavailable"
    if base == "planned" or "planned" in raw or raw.endswith("-"):
        return "planned"
    return "ok" if base in ("idle", "mixed", "allocated", "completing", "mix", "alloc", "comp") else "unavailable"

def gpu_label(kind: str) -> str:
    if re.fullmatch(r"\d+g\.\d+gb", kind or ""):
        return f"MIG {kind}"
    return kind or "gpu"

def gpu_short(kind: str) -> str:
    """Compact name for tight spaces: V100-PCIE-32GB -> V100, 1g.10gb -> MIG 1g."""
    if re.fullmatch(r"\d+g\.\d+gb", kind or ""):
        return "MIG " + kind.split(".")[0]
    return re.sub(r"-(PCIE|SXM|NVL|HBM).*$", "", kind or "gpu", flags=re.I)

def free_mem_mb(node: dict) -> float | None:
    if node.get("mem_mb") is None or node.get("mem_alloc_mb") is None:
        return None
    return node["mem_mb"] - node["mem_alloc_mb"]

def fitting_jobs(node: dict, free_gpus: int, shape: dict) -> int:
    jobs = free_gpus // max(1, int(shape.get("gpus") or 1))
    if node.get("cpu_idle") is not None:
        jobs = min(jobs, node["cpu_idle"] // max(1, int(shape["cpus"])))
    memory = free_mem_mb(node)
    if memory is not None:
        jobs = min(jobs, int(memory // max(1.0, shape["mem_mb"])))
    return max(0, jobs)

def partition_rank(name: str) -> tuple:
    order = {"gpu": 0, "general": 0, "gpu_requeue": 1, "requeue": 1, "interactive": 2}
    return order.get(name, 3), name

def gpu_table(nodes: list[dict], shape: dict, watch=()) -> list[dict]:
    """Per GPU partition and type: total, used, free, usable-for-your-shape, held, offline."""
    watched = {w.casefold() for w in watch}
    groups: dict[tuple, dict] = {}
    for node in nodes:
        if not node.get("gpus") or not is_gpu_partition(node.get("partition", "")):
            continue
        condition = node_condition(node.get("state", ""))
        used_map = {k.casefold(): v for k, v in (node.get("gpus_used") or {}).items()}
        for kind, total in node["gpus"].items():
            used = used_map.get(kind.casefold())
            if used is None and len(node["gpus"]) == 1:
                used = used_map.get("gpu")
            used = min(total, used or 0)
            free = total - used
            row = groups.setdefault((node["partition"], kind), {
                "partition": node["partition"], "type": kind, "total": 0, "used": 0, "free": 0, "usable": 0,
                "held": 0, "offline": 0, "free_nodes": [], "watched": kind.casefold() in watched})
            row["total"] += total
            if condition == "unavailable":
                row["offline"] += total
                continue
            row["used"] += used
            if condition == "planned":
                row["held"] += free
                continue
            row["free"] += free
            usable = fitting_jobs(node, free, shape) * max(1, int(shape.get("gpus") or 1))
            row["usable"] += min(usable, free)
            if free:
                row["free_nodes"].append({"node": node["node"], "free": free, "usable": min(usable, free)})
    rows = list(groups.values())
    rows.sort(key=lambda r: (not r["watched"], partition_rank(r["partition"]), -r["usable"], -r["free"], r["type"].casefold()))
    return rows

def gpu_point(rows: list[dict]) -> dict:
    return {f"{r['partition']}|{r['type']}": [r["free"], r["total"], r["usable"]] for r in rows}

def cpu_table(nodes: list[dict], shape: dict) -> list[dict]:
    groups: dict[str, dict] = {}
    for node in nodes:
        if is_gpu_partition(node.get("partition", "")):
            continue
        row = groups.setdefault(node["partition"], {"partition": node["partition"], "nodes": 0, "fit_nodes": 0,
                                                    "idle_nodes": 0, "idle_cpus": 0, "offline": 0})
        row["nodes"] += 1
        if node_condition(node.get("state", "")) != "ok":
            row["offline"] += 1
            continue
        idle = node.get("cpu_idle") or 0
        row["idle_cpus"] += idle
        row["idle_nodes"] += node.get("state", "").startswith("idle")
        memory = free_mem_mb(node)
        if idle >= shape["cpus"] and (memory is None or memory >= shape["mem_mb"]):
            row["fit_nodes"] += 1
    return sorted(groups.values(), key=lambda r: partition_rank(r["partition"]))

# ---------------------------------------------------------------- fairshare

def pick_share(rows: list[dict], jobs: list[dict]) -> dict | None:
    if not rows:
        return None
    accounts = Counter(j.get("account") for j in jobs if j.get("active") and j.get("account"))
    for account, _ in accounts.most_common():
        for row in rows:
            if row["account"] == account:
                return row
    return max(rows, key=lambda r: r.get("raw_usage") or 0)

def running_load(jobs: list[dict]) -> list[dict]:
    load = []
    for job in jobs:
        if not job.get("active") or job.get("state") != "RUNNING":
            continue
        elapsed, limit = walltime(job)
        cpus = job_cpus(job) or 1
        if limit and elapsed is not None:
            load.append({"cpus": cpus, "remaining": max(0, limit - elapsed)})
    return load

def fairshare_outlook(row: dict, config: dict | None = None, running=(), horizon: int = 14 * 86400, step: int = 600) -> dict:
    """Project your own decayed usage forward; other users' usage is held constant (steady state).

    Fair Tree: LevelFS = NormShares / EffectvUsage among siblings; the factor itself is a rank and is
    not projected. Classic: factor = 2^(-EffectvUsage / NormShares). Running jobs are counted as if they
    use their whole time limit (an upper bound); jobs you have not submitted yet are not included.
    """
    config = config or {}
    half = config.get("half_life")
    level, factor = row.get("level_fs"), row.get("fairshare")
    mode = "fair_tree" if level is not None and config.get("fair_tree", True) else "classic"
    result = {"mode": mode, "fairshare": factor, "level": level, "target": 1.0 if mode == "fair_tree" else 0.5,
              "half_life": half if half is not None else 7 * 86400, "half_life_assumed": half is None,
              "series": [], "cross": None, "cross_beyond": False, "low": None, "projectable": False, "running_billing": 0.0}
    half = result["half_life"]
    run = row.get("tres_run_mins") or {}
    remaining_billing = float(run.get("billing") or run.get("cpu") or 0) * 60
    plan = [(job["cpus"], job["remaining"]) for job in running if job["remaining"] > 0]
    weight = sum(c * r for c, r in plan)
    rates = [(c * remaining_billing / weight, end) for c, end in plan] if weight and remaining_billing else []
    result["running_billing"] = remaining_billing if rates else 0.0
    u0, eu0, shares = row.get("raw_usage") or 0, row.get("effectv_usage") or 0, row.get("norm_shares") or 0
    if u0 <= 0 or eu0 <= 0 or shares <= 0:
        return result
    decay = 0.5 ** (step / half) if half and half > 0 else 1.0
    usage, samples, t = float(u0), [], 0
    while t <= horizon:
        if t % 3600 == 0:
            samples.append((t, usage))
        usage = usage * decay + sum(rate * max(0, min(step, end - t)) for rate, end in rates)
        t += step
    if mode == "fair_tree":
        others = max(u0 / eu0 - u0, 0.0)
        def value(u):
            effective = u / (u + others) if u + others > 0 else 0
            return shares / effective if effective > 0 else math.inf
        base = value(u0)
        scale = level / base if level and math.isfinite(level) and base and math.isfinite(base) else 1.0
        series = [(t, value(u) * scale) for t, u in samples]
        # Usage at which scale*shares*(u+others)/u reaches 1.
        goal = scale * shares * others / (1 - scale * shares) if others > 0 and scale * shares < 1 else None
    else:
        scale = -math.log2(factor) / (eu0 / shares) if factor and 0 < factor < 1 else 1.0
        series = [(t, 2 ** (-scale * (eu0 * u / u0) / shares)) for t, u in samples]
        goal = u0 * shares / (scale * eu0)  # usage at which the factor reaches 0.5
    result.update(series=series, projectable=True, low=min(series, key=lambda p: p[1]))
    target = result["target"]
    if series[0][1] >= target:
        result["cross"] = 0
    else:
        result["cross"] = next((t for t, v in series if v >= target), None)
        last_t, last_u = samples[-1]
        busy_after = any(end > last_t for _, end in rates)
        if result["cross"] is None and goal and goal > 0 and half and half > 0 and not busy_after and last_u > goal:
            # Beyond the chart, usage only decays: solve u_last * 2^(-dt/half) = goal.
            result["cross"] = int(last_t + half * math.log2(last_u / goal))
            result["cross_beyond"] = True
    return result

def share_point(row: dict, stamp: str) -> dict:
    return {"t": stamp, "account": row.get("account"), "fairshare": row.get("fairshare"),
            "level": row.get("level_fs") if row.get("level_fs") is None or math.isfinite(row["level_fs"]) else None,
            "raw_usage": row.get("raw_usage"), "effectv_usage": row.get("effectv_usage"), "norm_shares": row.get("norm_shares")}

# ---------------------------------------------------------------- pending

def pending_starts(jobs: list[dict]) -> list[datetime]:
    return sorted(m for j in jobs if j.get("active") and j.get("state") == "PENDING" and (m := cluster_time(j.get("start"))))
