"""Synthetic, offline data shaped like Hellbender output. Never reads the network or SSH settings."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from .collector import PARSERS
from .insights import cluster_string
from .parsers import SEP

CONFIG = """PriorityCalcPeriod      = 00:05:00
PriorityDecayHalfLife   = 7-00:00:00
PriorityFavorSmall      = No
PriorityFlags           =
PriorityMaxAge          = 7-00:00:00
PriorityUsageResetPeriod = NONE
PriorityType            = priority/multifactor
PriorityWeightAge       = 1000
PriorityWeightFairshare = 10000
"""

def hms(minutes: int) -> str:
    minutes = max(0, int(minutes))
    days, rest = divmod(minutes, 1440)
    text = f"{rest // 60:02d}:{rest % 60:02d}:00"
    return f"{days}-{text}" if days else text

class DemoCollector:
    def __init__(self, clock=None):
        self.count = 0
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.base = datetime.fromisoformat(cluster_string(self.clock()))

    def at(self, minutes: float) -> str:
        return (self.base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")

    def refresh(self, plan, since=None) -> dict:
        self.count += 1
        self.base = datetime.fromisoformat(cluster_string(self.clock()))
        texts = {"queue": self.queue_text, "nodes": self.nodes_text, "share": self.share_text,
                 "config": lambda: CONFIG, "acct": self.acct_text}
        sections, size = {}, 0
        for name in plan:
            text = texts[name]()
            size += len(text.encode())
            sections[name] = {"ok": True, "data": PARSERS[name](text), "bytes": len(text.encode())}
        return {"sections": sections, "since": since, "trace": {"plan": list(plan), "seconds": 0.0, "bytes": size, "returncode": 0}}

    def queue_text(self) -> str:
        d = self.count * 3
        at = self.at
        rows = [
            ["1842051", "RUNNING", "gpu", "mace-finetune", hms(312 + d), "12:00:00", "4", "32G", "1", "gpu38",
             at(-312 - d), at(-350 - d), at(408 - d), "demo_lab", "gres/gpu:L40S:1", "/home/demo/ml/mace-finetune"],
            ["1842042", "RUNNING", "general", "ads_CO_Pt111", hms(1570 + d), "2-00:00:00", "40", "80G", "1", "cpu22",
             at(-1570 - d), at(-1810), at(1310 - d), "demo_lab", "N/A", "/home/demo/vasp/Pt111/CO"],
            ["1842090", "PENDING", "general", "ads_OH_Pt111", "0:00", "2-00:00:00", "40", "80G", "1", "(Priority)",
             at(2650), at(-480), at(5530), "demo_lab", "N/A", "/home/demo/vasp/Pt111/OH"],
            ["1842091", "PENDING", "general", "ads_O_Pt111", "0:00", "2-00:00:00", "40", "80G", "1", "(Priority)",
             at(3010), at(-478), at(5890), "demo_lab", "N/A", "/home/demo/vasp/Pt111/O"],
            ["1842092", "PENDING", "general", "ads_H_Pt211", "0:00", "2-00:00:00", "40", "80G", "1", "(Priority)",
             at(4320), at(-470), at(7200), "demo_lab", "N/A", "/home/demo/vasp/Pt211/H"],
            ["1842103_[0-53%3]", "PENDING", "gpu", "screen-batch", "0:00", "1:00:00", "4", "32G", "1", "(Priority)",
             "N/A", at(-95), "N/A", "demo_lab", "gres/gpu:1", "/home/demo/ml/screen"],
            ["1842110", "PENDING", "gpu", "model-eval", "0:00", "2:00:00", "4", "32G", "1", "(Resources)",
             at(38), at(-20), at(158), "demo_lab", "gres/gpu:L40S:1", "/home/demo/ml/model-eval"],
        ]
        return "\n".join(SEP.join(row) for row in rows) + "\n"

    def nodes_text(self) -> str:
        spare = 2 if self.count % 3 else 0
        gpu_nodes = [
            # node, partitions, state, cpus a/i/o/t, mem MB, alloc MB, gres, used
            ("gpu01", "R", "mixed", "43/21/0/64", 502123, 300000, "gpu:A100:4(S:0-1)", "gpu:A100:3(IDX:0-2)"),
            ("gpu02", "R", "allocated", "64/0/0/64", 502123, 480000, "gpu:A100:4(S:0-1)", "gpu:A100:4(IDX:0-3)"),
            ("gpu03", "R", "mixed", "60/4/0/64", 502123, 400000, "gpu:1g.10gb:28(S:0-1)", "gpu:1g.10gb:20(IDX:0-19)"),
            ("gpu04", "R", "mixed", "32/32/0/64", 502123, 250000, "gpu:A100:4(S:0-1)", "gpu:A100:2(IDX:0-1)"),
            ("gpu12", "GR", "mixed", "60/4/0/64", 502123, 420000, "gpu:A100:4(S:0-1)", "gpu:A100:4(IDX:0-3)"),
            ("gpu13", "GR", "mixed", "62/2/0/64", 502123, 410000, "gpu:A100:4(S:0-1)", "gpu:A100:3(IDX:0-2)"),
            ("gpu14", "GR", "allocated", "64/0/0/64", 502123, 500000, "gpu:A100:4(S:0-1)", "gpu:A100:4(IDX:0-3)"),
            ("gpu17", "GR", "mixed", "30/34/0/64", 502123, 300000, "gpu:A100:4(S:0-1)", "gpu:A100:4(IDX:0-3)"),
            ("gpu21", "R", "mixed", "18/18/0/36", 178729, 90000, "gpu:V100-PCIE-32GB:1(S:0-1)", "gpu:V100-PCIE-32GB:1(IDX:0)"),
            ("gpu22", "R", "idle", "0/72/0/72", 178720, 0, "gpu:V100-PCIE-32GB:1(S:0-71)", "gpu:V100-PCIE-32GB:0(IDX:N/A)"),
            ("gpu27", "GR", "mixed", "34/6/0/40", 372257, 200000, "gpu:V100-PCIE-32GB:3(S:0-1)", "gpu:V100-PCIE-32GB:2(IDX:0-1)"),
            ("gpu29", "R", "mixed", "26/38/0/64", 502184, 200000, "gpu:H100:2(S:0-1)", "gpu:H100:1(IDX:0)"),
            ("gpu30", "R", "mixed-", "10/54/0/64", 502700, 80000, "gpu:H100:2(S:0-1)", "gpu:H100:0(IDX:N/A)"),
            ("gpu31", "R", "allocated", "64/0/0/64", 502700, 450000, "gpu:H100:2(S:0-1)", "gpu:H100:2(IDX:0-1)"),
            ("gpu32", "GR", "mixed", "29/35/0/64", 502700, 300000, "gpu:H100:2(S:0-1)", "gpu:H100:2(IDX:0-1)"),
            ("gpu33", "GR", "mixed", "40/24/0/64", 502700, 260000, "gpu:H100:2(S:0-1)", "gpu:H100:1(IDX:0)"),
            ("gpu34", "GR", "mixed", "50/14/0/64", 502700, 380000, "gpu:H100:2(S:0-1)", "gpu:H100:2(IDX:0-1)"),
            ("gpu35", "R", "mixed", "20/44/0/64", 502700, 120000, "gpu:L40S:2(S:0-1)", "gpu:L40S:0(IDX:N/A)"),
            ("gpu36", "R", "mixed", "40/24/0/64", 502700, 200000, "gpu:L40S:2(S:0-1)", "gpu:L40S:1(IDX:0)"),
            ("gpu37", "R", "allocated", "64/0/0/64", 502700, 400000, "gpu:L40S:2(S:0-1)", "gpu:L40S:2(IDX:0-1)"),
            ("gpu38", "GR", "mixed", "36/28/0/64", 502700, 160000, "gpu:L40S:2(S:0-1)", "gpu:L40S:1(IDX:0)"),
            ("gpu39", "GR", "mixed", "12/52/0/64", 502700, 64000, "gpu:L40S:2(S:0-1)", f"gpu:L40S:{2 - spare}(IDX:0-1)"),
            ("gpu40", "GR", "drained", "0/0/64/64", 502184, 0, "gpu:L40S:2(S:0-1)", "gpu:L40S:0(IDX:N/A)"),
            ("gpu41", "R", "allocated", "96/0/0/96", 2050462, 1800000, "gpu:H200:8(S:0-1)", "gpu:H200:8(IDX:0-7)"),
        ]
        cpu_nodes = [
            ("cpu05", "idle", "0/128/0/128", 515000, 0), ("cpu09", "mixed", "40/88/0/128", 515000, 160000),
            ("cpu11", "drained", "0/0/128/128", 515000, 0), ("cpu14", "mixed", "120/8/0/128", 515000, 400000),
            ("cpu22", "mixed", "88/40/0/128", 515000, 300000), ("cpu31", "allocated", "128/0/0/128", 515000, 500000),
            ("cpu40", "mixed", "96/32/0/128", 515000, 420000), ("cpu41", "mixed", "64/64/0/128", 515000, 200000),
        ]
        rows = []
        for node, parts, state, cpus, mem, alloc, gres, used in gpu_nodes:
            names = (["gpu"] if "G" in parts else []) + ["gpu_requeue", "requeue*"]
            for partition in names:
                rows.append([node, partition, state, cpus, str(mem), str(alloc), gres, used])
        for node, state, cpus, mem, alloc in cpu_nodes:
            for partition in ("general", "requeue*"):
                rows.append([node, partition, state, cpus, str(mem), str(alloc), "(null)", "gpu:0"])
        return "\n".join(SEP.join(row) for row in rows) + "\n"

    def share_text(self) -> str:
        level = 0.4801 + 0.004 * self.count
        effective = 0.002841 / level
        return (f"demo_lab|demo|1|0.002841|{41_250_000 - 90_000 * self.count}|0.006100|{effective:.6f}|"
                f"{0.4183 + 0.003 * self.count:.6f}|{level:.6f}|cpu=54032,mem=108064000,energy=0,node=2,billing=56210,"
                "fs/disk=0,vmem=0,pages=0,gres/gpu=408\n")

    def acct_text(self) -> str:
        at = self.at
        rows = [
            ("1841800", "ads_CO_Pt100", "COMPLETED", "general", "1-14:20:11", "40", "80G", at(-6000), at(-3700), "0:0", at(-9000), "cpu14", "billing=40,cpu=40,mem=80G,node=1"),
            ("1841801", "ads_OH_Pt100", "COMPLETED", "general", "1-12:02:40", "40", "80G", at(-5900), at(-3730), "0:0", at(-8990), "cpu22", "billing=40,cpu=40,mem=80G,node=1"),
            ("1841802", "ads_O_Pt100", "COMPLETED", "general", "1-16:45:03", "40", "80G", at(-5600), at(-3195), "0:0", at(-8980), "cpu40", "billing=40,cpu=40,mem=80G,node=1"),
            ("1841803", "ads_H_Pt100", "FAILED", "general", "00:12:09", "40", "80G", at(-5200), at(-5188), "1:0", at(-8970), "cpu41", "billing=40,cpu=40,mem=80G,node=1"),
            ("1841850", "ads_COOH_Pt111", "TIMEOUT", "general", "2-00:00:12", "40", "80G", at(-4000), at(-1120), "0:15", at(-6100), "cpu09", "billing=40,cpu=40,mem=80G,node=1"),
            ("1841900", "mace-train", "COMPLETED", "gpu", "08:12:44", "4", "32G", at(-2400), at(-1907), "0:0", at(-2460), "gpu39", "billing=4,cpu=4,gres/gpu:l40s=1,gres/gpu=1,mem=32G,node=1"),
            ("1841938", "mace-mem-check", "OUT_OF_MEMORY", "gpu", "00:03:18", "4", "4G", at(-1500), at(-1497), "0:125", at(-1530), "gpu38", "billing=4,cpu=4,gres/gpu:l40s=1,gres/gpu=1,mem=4G,node=1"),
            ("1841960_0", "screen-batch", "COMPLETED", "gpu", "00:41:10", "4", "32G", at(-900), at(-859), "0:0", at(-1000), "gpu36", "billing=4,cpu=4,gres/gpu:l40s=1,gres/gpu=1,mem=32G,node=1"),
            ("1841960_1", "screen-batch", "COMPLETED", "gpu", "00:39:55", "4", "32G", at(-880), at(-840), "0:0", at(-1000), "gpu35", "billing=4,cpu=4,gres/gpu:l40s=1,gres/gpu=1,mem=32G,node=1"),
            ("1841960_2", "screen-batch", "CANCELLED by 1000", "gpu", "00:05:00", "4", "32G", at(-845), at(-840), "0:0", at(-1000), "gpu35", "billing=4,cpu=4,gres/gpu:l40s=1,gres/gpu=1,mem=32G,node=1"),
            ("1842070", "quick-check", "COMPLETED", "interactive", "00:04:31", "2", "4G", at(-150), at(-145), "0:0", at(-152), "cpu05", "billing=2,cpu=2,mem=4G,node=1"),
            ("1842042", "ads_CO_Pt111", "RUNNING", "general", "1-02:10:00", "40", "80G", at(-1570), "Unknown", "0:0", at(-1810), "cpu22", "billing=40,cpu=40,mem=80G,node=1"),
        ]
        limits = {"general": "2-00:00:00", "gpu": "12:00:00", "interactive": "04:00:00"}
        return "\n".join("|".join(row + (limits[row[3]], "demo_lab")) for row in rows) + "\n"

    def seed_history(self) -> tuple[list, list]:
        """Earlier observations so the demo charts have something to show."""
        moment = self.clock()
        shares, gpus = [], []
        for step in range(12, 0, -1):
            stamp = (moment - timedelta(hours=12 * step)).isoformat(timespec="seconds")
            level = 0.62 - 0.012 * (12 - step)
            shares.append({"t": stamp, "account": "demo_lab", "fairshare": round(0.52 - 0.009 * (12 - step), 4),
                           "level": round(level, 4), "raw_usage": 32_000_000 + 800_000 * (12 - step),
                           "effectv_usage": 0.002841 / level, "norm_shares": 0.002841})
        pattern = [2, 3, 1, 0, 2, 2, 3, 1, 2, 0, 1, 2, 3, 2, 1, 2]
        for index, free in enumerate(pattern):
            stamp = (moment - timedelta(hours=5 * (len(pattern) - index))).isoformat(timespec="seconds")
            gpus.append({"t": stamp, "rows": {"gpu|L40S": [free, 6, free], "gpu_requeue|L40S": [free + 2, 12, free + 1]}})
        return shares, gpus
