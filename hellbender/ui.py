"""Hellbender Viewer 3: a compact, read-only Tk monitor.

All widget access and database writes stay on the UI thread; the background
thread only runs one Refresh and posts its parsed result back.
"""
from __future__ import annotations
import math
import queue
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from . import __version__
from . import insights as ins
from .collector import Collector, validate_host
from .demo import DemoCollector
from .storage import Store, now
from .tray import create_tray

SKINS = {
    "Neon": dict(bg="#080c14", panel="#0c111c", card="#101828", inset="#0a0f19", text="#d8e0f0", muted="#7c8db0",
                 faint="#34425f", line="#1a2540", accent="#00e8ff", accent2="#ff2cf1", good="#4df08a",
                 warn="#ffb300", bad="#ff4d6a", selected="#0b3346"),
    "Midnight": dict(bg="#101722", panel="#151f2d", card="#1a2637", inset="#111a26", text="#e9f0f8", muted="#94a5bb",
                     faint="#45566d", line="#2a394d", accent="#6ed8cc", accent2="#c9a3ff", good="#8cdeb2",
                     warn="#ffcc80", bad="#ff9d9d", selected="#294657"),
    "Daylight": dict(bg="#eef2f7", panel="#ffffff", card="#ffffff", inset="#f4f7fa", text="#1f2b3d", muted="#5b6b82",
                     faint="#c3ccd8", line="#d9e1eb", accent="#08707c", accent2="#9b3dc2", good="#15784a",
                     warn="#985a00", bad="#b3343b", selected="#d3ecef"),
}
LEGACY_SKINS = {"Forest": "Midnight"}
FLOOR = 300                    # seconds between live refreshes (never lower than the saved interval)
SHARE_EVERY = 1800             # fairshare changes slowly
CONFIG_EVERY = 7 * 86400       # decay half-life and flags almost never change
ACCT_EVERY = 900               # accounting sync; sooner when a tracked job has just left the queue
VIEWS = (("active", "活动"), ("running", "运行"), ("pending", "排队"), ("finished", "已结束"), ("all", "全部"))
RUNNING_STATES = {"RUNNING", "COMPLETING", "CONFIGURING", "RESIZING"}

def mix(color_a: str, color_b: str, weight: float) -> str:
    a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * weight):02x}" for x, y in zip(a, b))

class App:
    def __init__(self, root: tk.Tk, store: Store, demo=False, skin=None, tray_factory=create_tray, collector_factory=None):
        self.root, self.store, self.demo = root, store, demo
        self.settings = store.settings()
        chosen = LEGACY_SKINS.get(skin or self.settings.get("skin"), skin or self.settings.get("skin"))
        self.settings["skin"] = chosen if chosen in SKINS else "Neon"
        try:
            self.settings["interval"] = max(60, min(3600, int(self.settings.get("interval") or 120)))
        except (TypeError, ValueError):
            self.settings["interval"] = 120
        self.profile = "demo" if demo else validate_host(self.settings["host"])
        if collector_factory:
            self.collector_factory = collector_factory
        elif demo:
            shared = DemoCollector()
            self.collector_factory = lambda host: shared
        else:
            self.collector_factory = Collector
        self.messages, self.tray_events = queue.Queue(), queue.Queue()
        self.busy = self.closed = self.hidden = False
        self.jobs, self.items = [], {}
        self.selected = None
        self.sort_state = None
        self.view = tk.StringVar(value="active")
        self.search = tk.StringVar()
        self.cooldown_until = 0.0
        self.last_minute = None
        self.last_day = None
        self.last_tray_check = 0.0
        self.message_text = ""
        self.recolor = []
        root.title(f"Hellbender Viewer {__version__}" + (" · 演示" if demo else ""))
        root.geometry(self.settings.get("geometry") or "1180x760")
        root.minsize(980, 620)
        root.protocol("WM_DELETE_WINDOW", self.close_to_tray)
        root.attributes("-topmost", bool(self.settings.get("topmost")))
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.make_fonts()
        self.c = SKINS[self.settings["skin"]]
        self.build()
        self.apply_skin()
        self.tray = tray_factory(store.path, self.tray_events.put, demo=demo)
        self.normal_geometry = root.geometry().split("+")[0]
        if not demo:
            self.cooldown_until = time.time() + store.peek_read(self.profile, "refresh")
        self.reload()
        if demo and not self.store.snapshot(self.profile, "queue"):
            self.seed_demo()
        elif not self.store.snapshot(self.profile, "queue"):
            self.message("还没有数据。点“刷新”通过 SSH 只读查询一次（每 5 分钟最多一次，不会自动刷新）。")
        root.bind("<Configure>", self.on_resize, add=True)
        root.bind("<Unmap>", self.on_minimize, add=True)
        root.bind("<Control-r>", lambda e: self.refresh())
        root.bind("<F5>", lambda e: self.refresh())
        root.after(100, self.drain)
        root.after(1000, self.tick)
        self.update_header()

    # ------------------------------------------------------------ appearance

    def make_fonts(self):
        families = set(tkfont.families(self.root))
        ui = next((f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI") if f in families), "TkDefaultFont")
        mono = next((f for f in ("Consolas", "Cascadia Mono", "Menlo", "DejaVu Sans Mono") if f in families), "TkFixedFont")
        self.f = {"ui": (ui, 9), "ui_b": (ui, 9, "bold"), "small": (ui, 8), "title": (ui, 12, "bold"),
                  "section": (ui, 8, "bold"), "card": (ui, 9, "bold"), "brand": (mono, 13, "bold"),
                  "big": (mono, 21, "bold"), "mid": (mono, 15, "bold"), "mono": (mono, 9), "mono_b": (mono, 10, "bold")}
        self.measure = {name: tkfont.Font(self.root, font=spec) for name, spec in self.f.items()}

    def clip(self, text, font, width):
        if self.measure[font].measure(text) <= width:
            return text
        while text and self.measure[font].measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def fit(self, head, parts, font, width, joiner=" · "):
        """Longest 'head + parts' that fits; drops whole trailing parts before clipping characters."""
        text = head
        for index, part in enumerate(parts):
            candidate = text + (joiner if index else "") + part
            if self.measure[font].measure(candidate) > width:
                return self.clip(candidate, font, width) if index == 0 else text + " …"
            text = candidate
        return text

    def tinted(self, widget, **roles):
        self.recolor.append((widget, roles))
        widget.configure(**{option: self.c[role] for option, role in roles.items()})
        return widget

    def apply_skin(self):
        c = self.c = SKINS[self.settings["skin"]]
        s, f = self.style, self.f
        self.root.configure(bg=c["bg"])
        s.configure(".", background=c["bg"], foreground=c["text"], font=f["ui"], borderwidth=0, bordercolor=c["line"],
                    lightcolor=c["line"], darkcolor=c["line"], troughcolor=c["panel"], focuscolor=c["accent"],
                    selectbackground=c["selected"], selectforeground=c["text"], insertcolor=c["accent"])
        for name, background in (("TFrame", c["bg"]), ("Panel.TFrame", c["panel"])):
            s.configure(name, background=background)
        s.configure("TLabel", background=c["bg"], foreground=c["text"])
        s.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
        s.configure("Brand.TLabel", background=c["bg"], foreground=c["accent"], font=f["brand"])
        s.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
        s.configure("PanelMuted.TLabel", background=c["panel"], foreground=c["muted"], font=f["small"])
        s.configure("Dialog.TLabel", background=c["bg"], foreground=c["muted"], font=f["small"])
        s.configure("DialogTitle.TLabel", background=c["bg"], foreground=c["accent"], font=f["title"])
        for tone in ("muted", "good", "warn", "bad", "accent"):
            s.configure(f"Msg.{tone}.TLabel", background=c["bg"], foreground=c[tone])
        s.configure("TButton", background=c["card"], foreground=c["text"], padding=(10, 3), borderwidth=1,
                    bordercolor=c["line"], lightcolor=c["card"], darkcolor=c["card"], relief="flat")
        s.map("TButton", background=[("disabled", c["panel"]), ("active", c["selected"])],
              foreground=[("disabled", c["faint"])], bordercolor=[("focus", c["accent"])])
        s.configure("Accent.TButton", foreground=c["accent"], bordercolor=c["accent"], font=f["ui_b"], padding=(14, 3))
        s.map("Accent.TButton", background=[("disabled", c["panel"]), ("active", c["selected"])],
              foreground=[("disabled", c["muted"])], bordercolor=[("disabled", c["line"])])
        s.configure("Filter.Toolbutton", background=c["panel"], foreground=c["muted"], padding=(10, 3), borderwidth=1,
                    bordercolor=c["panel"], lightcolor=c["panel"], darkcolor=c["panel"], font=f["ui"])
        s.map("Filter.Toolbutton", background=[("selected", c["selected"]), ("active", c["card"])],
              foreground=[("selected", c["accent"]), ("active", c["text"])], bordercolor=[("selected", c["accent"])])
        s.configure("TEntry", fieldbackground=c["inset"], foreground=c["text"], insertcolor=c["accent"], padding=4,
                    bordercolor=c["line"], lightcolor=c["inset"], darkcolor=c["inset"])
        s.map("TEntry", bordercolor=[("focus", c["accent"])])
        s.configure("TCombobox", fieldbackground=c["inset"], background=c["card"], foreground=c["text"], arrowcolor=c["accent"],
                    bordercolor=c["line"], lightcolor=c["inset"], darkcolor=c["inset"], padding=3)
        s.map("TCombobox", fieldbackground=[("readonly", c["inset"])], foreground=[("readonly", c["text"])],
              selectbackground=[("readonly", c["inset"])], selectforeground=[("readonly", c["text"])])
        self.root.option_add("*TCombobox*Listbox.background", c["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["selected"])
        s.configure("TCheckbutton", background=c["bg"], foreground=c["text"], indicatorbackground=c["inset"],
                    indicatorforeground=c["accent"])
        s.map("TCheckbutton", background=[("active", c["bg"])], indicatorbackground=[("selected", c["inset"])])
        s.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        s.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"], padding=(16, 5), borderwidth=0,
                    lightcolor=c["bg"], bordercolor=c["bg"], font=f["ui"])
        s.map("TNotebook.Tab", background=[("selected", c["panel"]), ("active", c["card"])],
              foreground=[("selected", c["accent"]), ("active", c["text"])],
              lightcolor=[("selected", c["accent"])], font=[("selected", f["ui_b"])])
        s.configure("Treeview", background=c["panel"], fieldbackground=c["panel"], foreground=c["text"], rowheight=25,
                    borderwidth=0, font=f["ui"])
        s.configure("Treeview.Heading", background=c["inset"], foreground=c["muted"], font=f["small"], relief="flat",
                    padding=(6, 4), borderwidth=0)
        s.map("Treeview", background=[("selected", c["selected"])], foreground=[("selected", c["text"])])
        s.map("Treeview.Heading", background=[("active", c["card"])], foreground=[("active", c["text"])])
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"{orient}.TScrollbar", background=c["line"], troughcolor=c["panel"], arrowcolor=c["muted"],
                        borderwidth=0, bordercolor=c["panel"], lightcolor=c["line"], darkcolor=c["line"], arrowsize=11)
            s.map(f"{orient}.TScrollbar", background=[("active", c["faint"])])
        for widget, roles in self.recolor:
            if widget.winfo_exists():
                widget.configure(**{option: c[role] for option, role in roles.items()})
        for tree in (getattr(self, "tree", None), getattr(self, "gpu_tree", None)):
            if tree:
                for tone in ("good", "warn", "bad", "accent", "accent2", "muted", "text"):
                    tree.tag_configure(tone, foreground=c[tone])
                tree.tag_configure("watch", foreground=c["accent"])
                tree.tag_configure("dim", foreground=c["faint"])
        for text in (getattr(self, "detail", None), getattr(self, "share_text", None)):
            if text:
                self.text_tags(text)
        if hasattr(self, "gpu_rows"):
            self.render_all()

    def text_tags(self, text: tk.Text):
        c, f = self.c, self.f
        text.configure(bg=c["panel"], fg=c["text"], insertbackground=c["accent"], selectbackground=c["selected"],
                       highlightthickness=0)
        text.tag_configure("title", font=f["title"], foreground=c["text"], spacing1=2, spacing3=2)
        text.tag_configure("big", font=f["big"])
        text.tag_configure("sub", font=f["small"], foreground=c["muted"], spacing3=6)
        text.tag_configure("section", font=f["section"], foreground=c["muted"], spacing1=12, spacing3=3)
        text.tag_configure("key", foreground=c["muted"])
        text.tag_configure("val", foreground=c["text"])
        text.tag_configure("note", font=f["small"], foreground=c["muted"], lmargin1=0, spacing1=2)
        text.tag_configure("mono", font=f["mono"], foreground=c["text"])
        text.tag_configure("bar", font=f["mono"], foreground=c["accent"])
        text.tag_configure("barbg", font=f["mono"], foreground=c["faint"])
        for tone in ("good", "warn", "bad", "accent", "accent2", "muted"):
            text.tag_configure(tone, foreground=c[tone])
            text.tag_configure("badge_" + tone, foreground=c[tone], font=f["ui_b"], spacing1=4)

    # ------------------------------------------------------------ layout

    def build(self):
        outer = ttk.Frame(self.root, padding=(12, 9, 12, 7))
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="HELLBENDER", style="Brand.TLabel").pack(side="left")
        self.mode_label = ttk.Label(header, text=self.mode_text(), style="Muted.TLabel")
        self.mode_label.pack(side="left", padx=(10, 0), pady=(3, 0))
        ttk.Button(header, text="⚙ 设置", command=self.settings_dialog).pack(side="right")
        self.refresh_button = ttk.Button(header, text="↻ 刷新", style="Accent.TButton", command=self.refresh, width=11)
        self.refresh_button.pack(side="right", padx=(0, 8))
        self.status_label = ttk.Label(header, style="Muted.TLabel")
        self.status_label.pack(side="right", padx=(0, 12), pady=(3, 0))
        self.tinted(tk.Frame(outer, height=1), bg="accent").pack(fill="x", pady=(8, 10))
        cards = ttk.Frame(outer)
        cards.pack(fill="x")
        self.cards = []
        for index, target in enumerate(("resources", "share", "jobs")):
            cards.columnconfigure(index, weight=1, uniform="card")
            canvas = self.tinted(tk.Canvas(cards, height=118, highlightthickness=0, bd=0, cursor="hand2"), bg="bg")
            canvas.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0 if index == 2 else 5))
            canvas.bind("<Configure>", lambda e: self.draw_cards())
            canvas.bind("<Button-1>", lambda e, t=target: self.show_tab(t))
            self.cards.append(canvas)
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True, pady=(10, 0))
        self.jobs_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.res_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.share_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.notebook.add(self.jobs_tab, text="作业")
        self.notebook.add(self.res_tab, text="空闲资源")
        self.notebook.add(self.share_tab, text="公平分")
        self.build_jobs(self.jobs_tab)
        self.build_resources(self.res_tab)
        self.build_share(self.share_tab)
        self.message_label = ttk.Label(outer, style="Msg.muted.TLabel", anchor="w")
        self.message_label.pack(fill="x", pady=(6, 0))

    def build_jobs(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        bar = ttk.Frame(tab, style="Panel.TFrame", padding=(8, 8, 8, 6))
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.view_buttons = {}
        for value, label in VIEWS:
            button = ttk.Radiobutton(bar, text=label, value=value, variable=self.view, style="Filter.Toolbutton",
                                     command=self.render_jobs, takefocus=False)
            button.pack(side="left", padx=(0, 4))
            self.view_buttons[value] = button
        ttk.Entry(bar, textvariable=self.search, width=22).pack(side="right")
        ttk.Label(bar, text="搜索名称 / 作业号", style="PanelMuted.TLabel").pack(side="right", padx=(0, 6))
        self.search.trace_add("write", lambda *a: self.render_jobs())
        frame = ttk.Frame(tab, style="Panel.TFrame")
        frame.grid(row=1, column=0, rowspan=2, sticky="nsew", padx=(8, 0), pady=(0, 8))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        columns = [("jobid", "作业号", 120, 0), ("name", "名称", 170, 90), ("partition", "分区", 74, 0),
                   ("status", "状态", 62, 0), ("time", "已用 / 时限", 88, 0), ("when", "开始 / 预计开始", 104, 0),
                   ("where", "节点 / 原因", 140, 70)]
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for key, title, width, flexible in columns:
            self.tree.heading(key, text=title, anchor="w", command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, minwidth=flexible or width, stretch=bool(flexible), anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.empty_label = ttk.Label(frame, style="PanelMuted.TLabel")
        self.detail = tk.Text(tab, width=40, wrap="char", relief="flat", bd=0, padx=14, pady=10, cursor="arrow",
                              font=self.f["ui"], tabs=("76p",), height=10)
        self.detail.grid(row=1, column=1, sticky="nsew", padx=8, pady=(0, 4))
        self.text_tags(self.detail)
        buttons = ttk.Frame(tab, style="Panel.TFrame", padding=(8, 0, 8, 8))
        buttons.grid(row=2, column=1, sticky="ew")
        self.copy_id_button = ttk.Button(buttons, text="复制作业号", command=self.copy_id)
        self.copy_id_button.pack(side="left")
        self.copy_dir_button = ttk.Button(buttons, text="复制工作目录", command=self.copy_dir)
        self.copy_dir_button.pack(side="left", padx=(6, 0))

    def build_resources(self, tab):
        top = ttk.Frame(tab, style="Panel.TFrame", padding=(12, 9, 12, 4))
        top.pack(fill="x")
        self.res_scope = ttk.Label(top, style="PanelMuted.TLabel")
        self.res_scope.pack(side="left")
        frame = ttk.Frame(tab, style="Panel.TFrame", padding=(10, 0, 10, 0))
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        columns = [("type", "型号", 140, False), ("partition", "分区", 170, False), ("free", "空闲 / 总数", 92, False),
                   ("usable", "能立即用", 82, False), ("nodes", "空闲卡在哪", 330, True)]
        self.gpu_tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="none", height=8)
        for key, title, width, stretch in columns:
            self.gpu_tree.heading(key, text=title, anchor="w")
            self.gpu_tree.column(key, width=width, minwidth=60, stretch=stretch, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.gpu_tree.yview)
        self.gpu_tree.configure(yscrollcommand=scroll.set)
        self.gpu_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.gpu_history = self.tinted(tk.Canvas(tab, height=86, highlightthickness=0, bd=0), bg="panel")
        self.gpu_history.pack(fill="x", padx=10, pady=(8, 0))
        self.gpu_history.bind("<Configure>", lambda e: self.draw_gpu_history())
        self.cpu_label = ttk.Label(tab, style="Panel.TLabel", justify="left")
        self.cpu_label.pack(fill="x", padx=12, pady=(8, 2))
        self.res_note = ttk.Label(tab, style="PanelMuted.TLabel", justify="left",
                                  text="空闲 = 还没被分配的卡；“能立即用”还要求同一节点剩下的 CPU 和内存够你常用的作业规格。"
                                       "能否马上开始仍取决于优先级、预留和 QOS 上限；gpu_requeue 上的作业可能被投资者抢占后重新排队。")
        self.res_note.pack(fill="x", padx=12, pady=(0, 10))

    def build_share(self, tab):
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)
        self.share_text = tk.Text(tab, width=46, wrap="char", relief="flat", bd=0, padx=16, pady=12, cursor="arrow",
                                  font=self.f["ui"], height=10)
        self.share_text.grid(row=0, column=0, sticky="nsew")
        self.text_tags(self.share_text)
        self.share_chart = self.tinted(tk.Canvas(tab, highlightthickness=0, bd=0), bg="panel")
        self.share_chart.grid(row=0, column=1, sticky="nsew", padx=(0, 12), pady=12)
        self.share_chart.bind("<Configure>", lambda e: self.draw_share_chart())

    def show_tab(self, name):
        self.notebook.select({"jobs": self.jobs_tab, "resources": self.res_tab, "share": self.share_tab}[name])

    def mode_text(self):
        return "// 演示数据，不联网" if self.demo else f"// {self.profile} · 只读"

    # ------------------------------------------------------------ data

    def reload(self):
        self.jobs = self.store.jobs(self.profile)
        self.nodes_snapshot = self.store.snapshot(self.profile, "nodes")
        self.share_snapshot = self.store.snapshot(self.profile, "fairshare")
        self.config_snapshot = self.store.snapshot(self.profile, "priority_config")
        self.queue_snapshot = self.store.snapshot(self.profile, "queue")
        self.sync = (self.store.snapshot(self.profile, "_sync") or {}).get("data", {})
        self.share_history = self.store.history(self.profile, "fairshare_history")
        self.gpu_history_points = self.store.history(self.profile, "gpu_history")
        nodes = self.nodes_snapshot["data"].get("nodes", []) if self.nodes_snapshot else []
        self.gpu_shape = ins.usual_shape(self.jobs, True, self.int_setting("gpu_cpus"), self.float_setting("gpu_mem_gb"))
        self.cpu_shape = ins.usual_shape(self.jobs, False, self.int_setting("cpu_cpus"), self.float_setting("cpu_mem_gb"))
        self.gpu_rows = ins.gpu_table(nodes, self.gpu_shape, self.watch_list())
        self.cpu_rows = ins.cpu_table(nodes, self.cpu_shape)
        rows = self.share_snapshot["data"].get("rows", []) if self.share_snapshot else []
        self.share_row = ins.pick_share(rows, self.jobs)
        config = self.config_snapshot["data"] if self.config_snapshot else {}
        self.outlook = ins.fairshare_outlook(self.share_row, config, ins.running_load(self.jobs)) if self.share_row else None
        self.render_all()

    def render_all(self):
        self.render_jobs()
        self.render_resources()
        self.render_share()
        self.draw_cards()
        self.update_header()
        self.update_tray_tip()

    def int_setting(self, key):
        try:
            return int(float(self.settings.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    def float_setting(self, key):
        try:
            return float(self.settings.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    def watch_list(self):
        return [w.strip() for w in str(self.settings.get("watch_gpus", "")).split(",") if w.strip()]

    @staticmethod
    def key(job):
        return job["jobid"], job["submitted"]

    def visible(self, job, view):
        status = ins.job_status(job)
        if status == "VANISHED" and (job.get("superseded") or ins.is_grouped(job)):
            return False  # an old grouped-array summary; its tasks are tracked individually
        if view == "active":
            return job.get("active")
        if view == "running":
            return job.get("active") and job.get("state") in RUNNING_STATES
        if view == "pending":
            return job.get("active") and not job.get("state") in RUNNING_STATES
        if view == "finished":
            return not job.get("active")
        return True

    def elapsed_now(self, job):
        elapsed, limit = ins.walltime(job)
        if elapsed is None or job.get("state") != "RUNNING" or not job.get("active"):
            return elapsed
        seen = ins.utc_time(job.get("observed"))
        if seen:
            elapsed += max(0, (datetime.now(timezone.utc) - seen).total_seconds())
        return min(elapsed, limit) if limit else elapsed

    def sort_key(self, job):
        status = ins.job_status(job)
        far = datetime.max.replace(tzinfo=timezone.utc)
        if job.get("active"):
            if job.get("state") in RUNNING_STATES:
                start = ins.cluster_time(job.get("start"))
                return (0, -(start.timestamp() if start else 0))
            start = ins.cluster_time(job.get("start"))
            return (1, (start or far).timestamp(), job["jobid"])
        end = ins.cluster_time(job.get("end")) or ins.utc_time(job.get("observed"))
        return (2, -(end.timestamp() if end else 0), status)

    def row_values(self, job):
        status = ins.job_status(job)
        elapsed, limit = ins.walltime(job)
        if job.get("active") and job.get("state") in RUNNING_STATES:
            time_text = f"{ins.compact_span(self.elapsed_now(job))} / {ins.compact_span(limit)}"
            when = ins.when_text(ins.cluster_time(job.get("start")))
            where = job.get("node_reason", "")
        elif job.get("active"):
            time_text = f"— / {ins.compact_span(limit)}"
            start = ins.cluster_time(job.get("start"))
            when = ins.when_text(start) if start else "未估计"
            where = ins.reason_short(job.get("node_reason", ""))
        else:
            time_text = ins.compact_span(elapsed)
            when = ins.when_text(ins.cluster_time(job.get("end"))) if job.get("end") else "—"
            code = job.get("exit_code", "")
            where = f"退出码 {code}" if code and code != "0:0" and status != "COMPLETED" else job.get("nodes") or job.get("node_reason", "")
            if status == "VANISHED":
                time_text, where = "—", "已离开队列，待记账确认"
        return [job["jobid"], job.get("name", ""), job.get("partition", ""), ins.status_text(status), time_text, when, where]

    def render_jobs(self):
        if not hasattr(self, "tree"):
            return
        view, query = self.view.get(), self.search.get().strip().casefold()
        counts = {value: 0 for value, _ in VIEWS}
        shown = []
        for job in self.jobs:
            for value, _ in VIEWS:
                counts[value] += bool(self.visible(job, value))
            if not self.visible(job, view):
                continue
            if query and query not in f"{job['jobid']} {job.get('name', '')} {job.get('partition', '')}".casefold():
                continue
            shown.append(job)
        for value, label in VIEWS:
            self.view_buttons[value].configure(text=f"{label} {counts[value]}" if value != "all" else label)
        if self.sort_state:
            column, descending = self.sort_state
            index = [c for c in self.tree["columns"]].index(column)
            shown.sort(key=lambda j: self.column_sort_value(j, column, index), reverse=descending)
        else:
            shown.sort(key=self.sort_key)
        selected_item = None
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        for number, job in enumerate(shown):
            item = f"j{number}"
            self.items[item] = self.key(job)
            status = ins.job_status(job)
            self.tree.insert("", "end", iid=item, values=self.row_values(job), tags=(ins.status_tone(status),))
            if self.key(job) == self.selected:
                selected_item = item
        if selected_item:
            self.tree.selection_set(selected_item)
            self.tree.see(selected_item)
        elif self.selected and not any(self.key(j) == self.selected for j in shown):
            self.selected = None
        if shown:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="没有符合条件的作业。" if (query or self.jobs) else "还没有作业数据。点右上角“刷新”。")
            self.empty_label.place(relx=0.5, rely=0.45, anchor="center")
        self.render_detail()

    def column_sort_value(self, job, column, index):
        if column == "when":
            moment = ins.cluster_time(job.get("start") if job.get("active") else job.get("end"))
            return moment.timestamp() if moment else float("inf")
        if column == "time":
            return self.elapsed_now(job) or 0
        if column == "jobid":
            digits = "".join(ch if ch.isdigit() else " " for ch in job["jobid"]).split()
            return [int(d) for d in digits]
        return str(self.row_values(job)[index]).casefold()

    def sort_by(self, column):
        descending = self.sort_state == (column, False)
        self.sort_state = (column, descending)
        self.render_jobs()

    def on_select(self, event=None):
        selection = self.tree.selection()
        if selection:
            self.selected = self.items.get(selection[0])
            self.render_detail()

    def selected_job(self):
        return next((job for job in self.jobs if self.key(job) == self.selected), None)

    # ------------------------------------------------------------ job detail

    def kv(self, key, value, tone="val"):
        self.detail.insert("end", key + "\t", "key")
        self.detail.insert("end", value + "\n", tone)

    def note(self, text):
        self.detail.insert("end", text + "\n", "note")

    def render_detail(self):
        if not hasattr(self, "detail"):
            return
        text, job = self.detail, self.selected_job()
        text.configure(state="normal")
        text.delete("1.0", "end")
        if not job:
            text.insert("end", "选一个作业\n", "title")
            text.insert("end", "在左边点一行，这里会显示预计开始、已用时间、资源和工作目录。\n", "sub")
            self.copy_id_button.state(["disabled"])
            self.copy_dir_button.state(["disabled"])
        else:
            status = ins.job_status(job)
            text.insert("end", (job.get("name") or "（无名称）") + "\n", "title")
            account = f" · {job['account']}" if job.get("account") else ""
            text.insert("end", f"{job['jobid']} · {job.get('partition', '')}{account}\n", "sub")
            text.insert("end", "● " + ins.status_text(status) + "\n", "badge_" + ins.status_tone(status) if ins.status_tone(status) != "text" else "val")
            if job.get("active") and job.get("state") in RUNNING_STATES:
                self.detail_running(job)
            elif job.get("active"):
                self.detail_pending(job)
            else:
                self.detail_finished(job, status)
            self.detail_resources(job)
            self.copy_id_button.state(["!disabled"])
            self.copy_dir_button.state(["!disabled"] if job.get("workdir") else ["disabled"])
        text.configure(state="disabled")

    def detail_running(self, job):
        _, limit = ins.walltime(job)
        elapsed = self.elapsed_now(job)
        start = ins.cluster_time(job.get("start"))
        self.detail.insert("end", "时间\n", "section")
        if limit and elapsed is not None:
            ratio = max(0.0, min(1.0, elapsed / limit))
            filled = round(ratio * 18)
            self.detail.insert("end", "█" * filled, "bar")
            self.detail.insert("end", "█" * (18 - filled), "barbg")
            self.detail.insert("end", f"  {ratio * 100:.0f}%\n", "mono")
            self.kv("已用", f"{ins.span_text(elapsed)} / 时限 {ins.span_text(limit)}")
        if start:
            self.kv("开始于", ins.when_text(start))
        end = ins.cluster_time(job.get("end")) or (start + timedelta(seconds=limit) if start and limit else None)
        if end:
            self.kv("时限到", f"{ins.when_text(end)}（{ins.relative_text(end)}）")
        typical = ins.typical_runtime(job, self.jobs)
        if typical and start:
            finish = start + timedelta(seconds=typical[0])
            self.kv("同类作业", f"通常 {ins.span_text(typical[0])}（{typical[1]} 个）")
            self.kv("参考结束", f"约 {ins.when_text(finish)}（{ins.relative_text(finish)}）", "accent")
            self.note("按你同名前缀、同分区、同核数的已完成作业中位用时推算，不是读日志得到的进度。")
        self.kv("节点", job.get("node_reason", "") or "—")

    def detail_pending(self, job):
        start = ins.cluster_time(job.get("start"))
        self.detail.insert("end", "什么时候开始\n", "section")
        if start:
            self.kv("预计开始", f"{ins.when_text(start)}（{ins.relative_text(start)}）", "accent")
            if start < ins.local_now():
                self.note("这个估计已经过时，下次刷新会更新。")
            else:
                self.note("调度器按当前队列给出的估计，会随队列变化。")
        else:
            self.kv("预计开始", "调度器暂未给出")
            wait = ins.typical_wait(job.get("partition", ""), self.jobs)
            if wait:
                self.note(f"参考：你在 {job.get('partition')} 分区近 14 天开始的 {wait[1]} 个作业，从提交到开始的中位等待是 {ins.span_text(wait[0])}。")
        reason = ins.reason_text(job.get("node_reason", ""))
        if reason:
            self.kv("排队原因", reason)
        submitted = ins.cluster_time(job.get("submitted"))
        if submitted:
            self.kv("已排队", ins.span_text((ins.local_now() - submitted).total_seconds()))

    def detail_finished(self, job, status):
        self.detail.insert("end", "结果\n", "section")
        code = job.get("exit_code", "")
        result = ins.status_text(status) + (f"（退出码 {code}）" if code and code != "0:0" else "")
        self.kv("结果", result, ins.status_tone(status))
        if status == "VANISHED":
            self.note("它已经离开队列，但还没有记账记录确认结果；下次刷新会自动查询。")
        elapsed = ins.duration_seconds(job.get("elapsed", ""))
        if elapsed is not None and status != "VANISHED":
            self.kv("用时", ins.span_text(elapsed) if elapsed >= 60 else f"{elapsed} 秒")
        start, end = ins.cluster_time(job.get("start")), ins.cluster_time(job.get("end"))
        if start:
            self.kv("开始", ins.when_text(start))
        if end:
            self.kv("结束", ins.when_text(end))
        if job.get("nodes"):
            self.kv("节点", job["nodes"])

    def detail_resources(self, job):
        self.detail.insert("end", "资源\n", "section")
        parts = []
        if job.get("cpus"):
            parts.append(f"{job['cpus']} 核")
        if job.get("memory"):
            parts.append(job["memory"].rstrip("nc"))
        if job.get("node_count") and job["node_count"] != "1":
            parts.append(f"{job['node_count']} 节点")
        gpu = ins.job_gpu_label(job)
        if gpu:
            parts.append(gpu)
        self.kv("申请", " · ".join(parts) or "—")
        limit = ins.duration_seconds(job.get("time_limit", ""))
        if limit:
            self.kv("时限", ins.span_text(limit))
        submitted = ins.cluster_time(job.get("submitted"))
        if submitted:
            self.kv("提交于", ins.when_text(submitted))
        if job.get("workdir"):
            self.detail.insert("end", "工作目录\n", "section")
            self.detail.insert("end", job["workdir"] + "\n", "mono")

    # ------------------------------------------------------------ resources tab

    def render_resources(self):
        if not hasattr(self, "gpu_tree"):
            return
        self.gpu_tree.delete(*self.gpu_tree.get_children())
        observed = self.nodes_snapshot["observed"] if self.nodes_snapshot else None
        self.res_scope.configure(text=(f"节点数据 {ins.age_text(observed)}读取 · " if observed else "还没有节点数据 · ")
                                 + f"“能立即用”按你常用的 GPU 作业：{ins.shape_text(self.gpu_shape)}（{self.gpu_shape['source']}）")
        for row in self.gpu_rows:
            partition = row["partition"] + ("（可被抢占）" if "requeue" in row["partition"] else "（公共）" if row["partition"] == "gpu" else "")
            nodes = "  ".join(f"{n['node']}×{n['free']}" + ("" if n["usable"] == n["free"] else f"（可用{n['usable']}）")
                              for n in row["free_nodes"][:8]) or "—"
            extra = []
            if row["held"]:
                extra.append(f"另有 {row['held']} 张被预留给排队作业")
            if row["offline"]:
                extra.append(f"{row['offline']} 张在维护/下线")
            if extra:
                nodes += "   (" + "，".join(extra) + ")"
            tag = "watch" if row["watched"] and row["usable"] else "good" if row["usable"] else "text" if row["free"] else "dim"
            self.gpu_tree.insert("", "end", values=[ins.gpu_label(row["type"]), partition, f"{row['free']} / {row['total']}",
                                                    row["usable"], nodes], tags=(tag,))
        if not self.gpu_rows:
            self.gpu_tree.insert("", "end", values=["—", "还没有 GPU 节点数据，刷新一次即可", "", "", ""], tags=("dim",))
        parts = []
        for row in self.cpu_rows[:3]:
            label = row["partition"] + ("（可被抢占）" if "requeue" in row["partition"] else "")
            parts.append(f"{label}：{row['fit_nodes']} 个节点能立即放下，空闲 CPU {row['idle_cpus']:,}")
        self.cpu_label.configure(text=(f"CPU（按你常用的作业：{ins.shape_text(self.cpu_shape)}）  " + "   ·   ".join(parts)) if parts else "")
        self.draw_gpu_history()

    def history_key(self):
        watched = [w.casefold() for w in self.watch_list()]
        for row in self.gpu_rows:
            if row["partition"] == "gpu" and row["type"].casefold() in watched:
                return f"gpu|{row['type']}", row["type"]
        if watched:
            return f"gpu|{self.watch_list()[0]}", self.watch_list()[0]
        return (f"gpu|{self.gpu_rows[0]['type']}", self.gpu_rows[0]["type"]) if self.gpu_rows else (None, None)

    def draw_gpu_history(self):
        canvas, c = self.gpu_history, self.c
        canvas.delete("all")
        w, h = max(300, canvas.winfo_width()), max(60, canvas.winfo_height())
        key, kind = self.history_key()
        points = [p for p in getattr(self, "gpu_history_points", []) if key and key in p.get("rows", {})][-48:]
        if not points:
            canvas.create_text(4, h / 2, anchor="w", fill=c["muted"], font=self.f["small"],
                               text="刷新几次以后，这里会画出你关注的 GPU 每次刷新时的空闲张数，看它是不是经常闲着。")
            return
        frees = [p["rows"][key][0] for p in points]
        total = max(max(p["rows"][key][1] for p in points), 1)
        idle_times = sum(1 for f in frees if f > 0)
        canvas.create_text(4, 10, anchor="w", fill=c["text"], font=self.f["ui_b"],
                           text=f"{ins.gpu_label(kind)}（gpu 公共分区）最近 {len(points)} 次刷新：{idle_times} 次有空闲卡")
        left, right, top, bottom = 4, w - 4, 24, h - 20
        slot = (right - left) / max(len(points), 12)
        for index, free in enumerate(frees):
            x0 = left + index * slot + 1
            x1 = x0 + max(2, slot - 3)
            height = (bottom - top) * free / total
            canvas.create_rectangle(x0, bottom - max(1, height), x1, bottom, fill=c["good"] if free else c["faint"], outline="")
        canvas.create_line(left, bottom + 1, right, bottom + 1, fill=c["line"])
        first, last = ins.utc_time(points[0]["t"]), ins.utc_time(points[-1]["t"])
        if first:
            canvas.create_text(left, h - 2, anchor="sw", fill=c["muted"], font=self.f["small"], text=ins.when_text(first))
        if last and len(points) > 1:
            canvas.create_text(left + slot * len(points), h - 2, anchor="se", fill=c["muted"], font=self.f["small"], text=ins.when_text(last))
        canvas.create_text(right, 10, anchor="e", fill=c["muted"], font=self.f["small"], text=f"柱高 = 空闲张数（共 {total} 张）")

    # ------------------------------------------------------------ fairshare tab

    def share_summary(self):
        """Short (headline, detail, outlook) texts shared by the card and the tab."""
        row, outlook = self.share_row, self.outlook
        if not row:
            return None
        level = row.get("level_fs")
        if outlook and outlook["mode"] == "fair_tree" and level is not None:
            if math.isinf(level):
                detail = "LevelFS ∞ · 最近没有用量"
            elif level >= 1:
                detail = f"LevelFS {level:.3g} · 用量低于份额"
            else:
                detail = f"LevelFS {level:.3g} · 用量约为份额的 {1 / level:.3g} 倍"
        elif row.get("effectv_usage") and row.get("norm_shares"):
            detail = f"用量 / 份额 = {row['effectv_usage'] / row['norm_shares']:.1f}"
        else:
            detail = "还没有足够的数据"
        forecast = ""
        if outlook and outlook["projectable"]:
            days = (outlook["cross"] or 0) / 86400
            if outlook["cross"] == 0:
                forecast = "已在份额线以上，排队相对有利"
            elif outlook["cross"] is not None and not outlook["cross_beyond"]:
                forecast = f"若不再提交，约 {days:.1f} 天后回到份额线"
            elif outlook["cross"] is not None:
                forecast = f"若不再提交，约 {days:.0f} 天后才回到份额线（半衰期 {outlook['half_life'] / 86400:g} 天）"
            else:
                forecast = "按现有数据推算不出何时回到份额线"
        return detail, forecast

    def render_share(self):
        if not hasattr(self, "share_text"):
            return
        text, row, outlook = self.share_text, self.share_row, self.outlook
        text.configure(state="normal")
        text.delete("1.0", "end")
        if not row:
            text.insert("end", "公平分\n", "title")
            failure = self.sync.get("share", {})
            if failure.get("failures"):
                text.insert("end", f"上次读取失败：{failure.get('error', '')}\n", "warn")
            text.insert("end", "还没有读到公平分。刷新一次后会显示（公平分每 30 分钟最多读一次）。\n", "sub")
        else:
            factor = row.get("fairshare")
            observed = self.share_snapshot["observed"] if self.share_snapshot else None
            text.insert("end", "公平分  ", "title")
            text.insert("end", f"{factor:.3f}\n" if factor is not None else "—\n", ("big", self.factor_tone(factor)))
            text.insert("end", f"账户 {row['account']} · {ins.age_text(observed)}读取\n", "sub")
            detail, forecast = self.share_summary()
            text.insert("end", "现在\n", "section")
            text.insert("end", detail + "\n", "val")
            if outlook and outlook["mode"] == "fair_tree":
                text.insert("end", "LevelFS = 你的份额 ÷ 你的用量（和同组用户相比）。大于 1 说明用得比份额少，排名会靠前。\n", "note")
            text.insert("end", "预计\n", "section")
            if outlook and outlook["projectable"]:
                text.insert("end", (forecast or "—") + "\n", "accent")
                if outlook["cross_beyond"] or outlook["cross"] is None:
                    label = "LevelFS" if outlook["mode"] == "fair_tree" else "公平分"
                    text.insert("end", f"图上 14 天后 {label} 约 {outlook['series'][-1][1]:.3g}（现在 {outlook['series'][0][1]:.3g}）。\n", "val")
                low_t, low_v = outlook["low"]
                if outlook["running_billing"] and low_t > 0 and low_v < outlook["series"][0][1] * 0.98:
                    when = ins.local_now() + timedelta(seconds=low_t)
                    label = "LevelFS" if outlook["mode"] == "fair_tree" else "公平分"
                    text.insert("end", f"正在运行的作业若跑满时限，会先把 {label} 压到约 {low_v:.2f}（{ins.when_text(when)}）再回升。\n", "val")
                half = outlook["half_life"]
                source = "假定 Slurm 默认" if outlook["half_life_assumed"] else "集群配置"
                text.insert("end", f"按用量半衰期 {half / 86400:g} 天（{source}）推算；假设其他用户的用量不变，"
                                   "运行中的作业按跑满时限计，不含你今后新提交的作业。只是估算。\n", "note")
            else:
                text.insert("end", "数据不足以推算走势（用量为 0 或缺少份额信息）。\n", "note")
            text.insert("end", "这是什么\n", "section")
            text.insert("end", "公平分（FairShare）是你在所有用户中的排名，0–1，越高越优先；Hellbender 官方文档说它是决定排队先后的最主要因素。"
                               "你跑得越多它越低，历史用量随时间衰减，少提交一阵子就会回升。\n", "note")
            rows = self.share_snapshot["data"].get("rows", []) if self.share_snapshot else []
            if len(rows) > 1:
                text.insert("end", "你的其他账户\n", "section")
                for other in rows:
                    if other is row or other["account"] == row["account"]:
                        continue
                    value = other.get("fairshare")
                    text.insert("end", f"{other['account']}\t公平分 {value:.3f}\n" if value is not None else f"{other['account']}\t—\n", "val")
        text.configure(state="disabled")
        self.draw_share_chart()

    def factor_tone(self, factor):
        if factor is None:
            return "muted"
        return "good" if factor >= 0.5 else "warn" if factor >= 0.2 else "bad"

    def draw_share_chart(self):
        canvas, c = self.share_chart, self.c
        canvas.delete("all")
        w, h = max(320, canvas.winfo_width()), max(200, canvas.winfo_height())
        outlook = self.outlook
        history = [p for p in getattr(self, "share_history", [])
                   if not self.share_row or p.get("account") == self.share_row.get("account")]
        if not outlook and not history:
            canvas.create_text(w / 2, h / 2, fill=c["muted"], font=self.f["ui"], text="刷新一次后，这里会画出公平分的走势和预计。")
            return
        fair_tree = outlook["mode"] == "fair_tree" if outlook else any(p.get("level") is not None for p in history)
        value_of = (lambda p: p.get("level")) if fair_tree else (lambda p: p.get("fairshare"))
        now_utc = datetime.now(timezone.utc)
        past = [(ins.utc_time(p["t"]), value_of(p)) for p in history]
        past = [(t, v) for t, v in past if t and v is not None and (now_utc - t).total_seconds() <= 21 * 86400]
        future = [(now_utc + timedelta(seconds=t), v) for t, v in (outlook or {}).get("series", [])] if outlook and outlook["projectable"] else []
        values = [v for _, v in past + future if v and math.isfinite(v)] + [1.0 if fair_tree else 0.5]
        start = min([t for t, _ in past] + [now_utc - timedelta(days=2)])
        end = max([t for t, _ in future] + [now_utc + timedelta(days=7)])
        left, right, top, bottom = 52, w - 18, 30, h - 52
        if fair_tree:
            low = min(values) / 1.25
            high = max(values) * 1.25
            low, high = max(low, 1 / 64), min(high, 64)
            scale = lambda v: bottom - (math.log2(max(min(v, high), low)) - math.log2(low)) / (math.log2(high) - math.log2(low)) * (bottom - top)
            ticks = [2 ** k for k in range(-6, 7) if low <= 2 ** k <= high]
            title, target = "LevelFS（份额 ÷ 用量，对数刻度）", 1.0
        else:
            low, high = 0.0, 1.0
            scale = lambda v: bottom - (min(max(v, 0), 1)) * (bottom - top)
            ticks = [0, 0.25, 0.5, 0.75, 1.0]
            title, target = "公平分", 0.5
        span = max(1.0, (end - start).total_seconds())
        x_of = lambda t: left + (t - start).total_seconds() / span * (right - left)
        canvas.create_text(left, 12, anchor="w", fill=c["text"], font=self.f["ui_b"], text=title)
        for tick in ticks:
            y = scale(tick)
            canvas.create_line(left, y, right, y, fill=c["line"])
            canvas.create_text(left - 6, y, anchor="e", fill=c["muted"], font=self.f["small"], text=f"{tick:.2g}")
        y_target = scale(target)
        canvas.create_line(left, y_target, right, y_target, fill=c["accent2"], dash=(4, 3))
        canvas.create_text(right, y_target - 8, anchor="e", fill=c["accent2"], font=self.f["small"], text="份额线")
        day = start.astimezone().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        step = max(1, math.ceil(span / 86400 / 9))
        while day < end:
            x = x_of(day)
            canvas.create_line(x, bottom, x, bottom + 4, fill=c["faint"])
            canvas.create_text(x, bottom + 13, fill=c["muted"], font=self.f["small"], text=f"{day.month}/{day.day}")
            day = (day + timedelta(days=step, hours=3)).replace(hour=0)
        x_now = x_of(now_utc)
        canvas.create_line(x_now, top, x_now, bottom, fill=c["faint"], dash=(2, 3))
        canvas.create_text(x_now + 4, bottom - 8, anchor="w", fill=c["muted"], font=self.f["small"], text="现在")
        if len(past) >= 2:
            canvas.create_line(*[coord for t, v in past for coord in (x_of(t), scale(v))], fill=c["accent"], width=2)
        for t, v in past:
            canvas.create_oval(x_of(t) - 2.5, scale(v) - 2.5, x_of(t) + 2.5, scale(v) + 2.5, fill=c["accent"], outline="")
        if len(future) >= 2:
            canvas.create_line(*[coord for t, v in future for coord in (x_of(t), scale(v))], fill=c["accent"], width=2, dash=(6, 4))
            cross = outlook.get("cross")
            if cross and not outlook.get("cross_beyond"):
                t = now_utc + timedelta(seconds=cross)
                canvas.create_oval(x_of(t) - 4, y_target - 4, x_of(t) + 4, y_target + 4, outline=c["accent2"], width=2)
                canvas.create_text(x_of(t), y_target + 14, fill=c["accent2"], font=self.f["small"], text=f"约 {cross / 86400:.1f} 天后")
        canvas.create_text(left, h - 2, anchor="sw", fill=c["muted"], font=self.f["small"], text="实线：你每次刷新时读到的值    虚线：预计（若不再提交新作业）")

    # ------------------------------------------------------------ summary cards

    def draw_cards(self):
        if not hasattr(self, "cards"):
            return
        for canvas, painter in zip(self.cards, (self.paint_gpu_card, self.paint_share_card, self.paint_jobs_card)):
            canvas.delete("all")
            w, h = max(200, canvas.winfo_width()), max(100, canvas.winfo_height())
            canvas.create_rectangle(0, 0, w - 1, h - 1, fill=self.c["card"], outline=self.c["line"])
            painter(canvas, w, h)

    def card_title(self, canvas, w, title, stamp):
        canvas.create_text(12, 14, anchor="w", text=title, fill=self.c["muted"], font=self.f["card"])
        if stamp:
            canvas.create_text(w - 12, 14, anchor="e", text=ins.age_text(stamp), fill=self.c["muted"], font=self.f["small"])

    def paint_gpu_card(self, canvas, w, h):
        c, f = self.c, self.f
        self.card_title(canvas, w, "空闲 GPU · gpu 公共分区", self.nodes_snapshot["observed"] if self.nodes_snapshot else None)
        public = [r for r in self.gpu_rows if r["partition"] == "gpu"]
        if not public:
            canvas.create_text(12, 52, anchor="w", text="还没有节点数据，点“刷新”", fill=c["muted"], font=f["ui"])
            return
        y = 38
        for row in public[:3]:
            name_color = c["accent"] if row["watched"] else c["text"]
            canvas.create_text(12, y, anchor="w", text=ins.gpu_short(row["type"]), fill=name_color, font=f["mono_b"])
            x = 84
            total = row["total"]
            if total <= 16:
                for index in range(total):
                    if index < row["usable"]:
                        color = c["good"]
                    elif index < row["free"]:
                        color = mix(c["good"], c["card"], 0.55)
                    elif index < row["free"] + row["used"] + row["held"]:
                        color = c["faint"]
                    else:
                        color = mix(c["bad"], c["card"], 0.6)
                    canvas.create_rectangle(x + index * 11, y - 5, x + index * 11 + 8, y + 5, fill=color, outline="")
            else:
                width = 170
                canvas.create_rectangle(x, y - 5, x + width, y + 5, fill=c["faint"], outline="")
                if row["free"]:
                    canvas.create_rectangle(x, y - 5, x + width * row["free"] / total, y + 5, fill=c["good"], outline="")
            label = f"空闲 {row['free']} / {total}"
            canvas.create_text(w - 12, y, anchor="e", text=label, fill=c["good"] if row["usable"] else c["muted"], font=f["ui"])
            y += 21
        others = [r for r in self.gpu_rows if r["partition"] != "gpu" and r["free"]]
        if others:
            text = self.fit("gpu_requeue（可被抢占）空闲：", [f"{ins.gpu_short(r['type'])} {r['free']}" for r in others], "small", w - 24)
            canvas.create_text(12, h - 13, anchor="w", text=text, fill=c["muted"], font=f["small"])

    def paint_share_card(self, canvas, w, h):
        c, f = self.c, self.f
        self.card_title(canvas, w, "公平分", self.share_snapshot["observed"] if self.share_snapshot else None)
        row = self.share_row
        if not row:
            failure = self.sync.get("share", {})
            text = "读取失败，稍后自动重试" if failure.get("failures") else "还没有读到，点“刷新”"
            canvas.create_text(12, 52, anchor="w", text=text, fill=c["muted"], font=f["ui"])
            return
        factor = row.get("fairshare")
        value = f"{factor:.3f}" if factor is not None else "—"
        item = canvas.create_text(12, 48, anchor="w", text=value, fill=c[self.factor_tone(factor)], font=f["big"])
        history = [p for p in self.share_history if p.get("account") == row["account"] and p.get("fairshare") is not None]
        if len(history) >= 2 and factor is not None:
            delta = history[-1]["fairshare"] - history[-2]["fairshare"]
            if abs(delta) >= 0.0005:
                arrow, tone = ("▲", "good") if delta > 0 else ("▼", "bad")
                canvas.create_text(canvas.bbox(item)[2] + 8, 52, anchor="w", text=f"{arrow} {abs(delta):.3f}（较上次）", fill=c[tone], font=f["small"])
        detail, forecast = self.share_summary()
        canvas.create_text(12, 78, anchor="w", text=self.clip(detail, "ui", w - 24), fill=c["text"], font=f["ui"])
        if forecast:
            canvas.create_text(12, 99, anchor="w", text=self.clip(forecast, "ui", w - 24), fill=c["accent"], font=f["ui"])

    def paint_jobs_card(self, canvas, w, h):
        c, f = self.c, self.f
        self.card_title(canvas, w, "我的作业", self.queue_snapshot["observed"] if self.queue_snapshot else None)
        active = [j for j in self.jobs if j.get("active")]
        running = sum(j.get("state") in RUNNING_STATES for j in active)
        pending = len(active) - running
        if not self.queue_snapshot:
            canvas.create_text(12, 52, anchor="w", text="还没有作业数据，点“刷新”", fill=c["muted"], font=f["ui"])
            return
        x = 12
        for count, label, tone in ((running, "运行", "good"), (pending, "排队", "warn")):
            item = canvas.create_text(x, 48, anchor="w", text=str(count), fill=c[tone], font=f["big"])
            x = canvas.bbox(item)[2] + 4
            item = canvas.create_text(x, 52, anchor="w", text=label, fill=c["muted"], font=f["ui"])
            x = canvas.bbox(item)[2] + 16
        starts = [m for m in ins.pending_starts(self.jobs) if m > ins.local_now() - timedelta(hours=1)]
        if starts:
            line = f"最早预计开始：{ins.when_text(starts[0])}（{ins.relative_text(starts[0])}）"
        elif pending:
            line = "排队作业暂时都没有调度器估计"
        else:
            line = "没有排队的作业"
        canvas.create_text(12, 78, anchor="w", text=self.clip(line, "ui", w - 24), fill=c["accent"] if starts else c["muted"], font=f["ui"])
        cutoff = ins.local_now() - timedelta(hours=24)
        recent = [j for j in self.jobs if not j.get("active") and (end := ins.cluster_time(j.get("end"))) and end >= cutoff]
        done = sum(ins.job_status(j) == "COMPLETED" for j in recent)
        bad = sum(ins.status_tone(ins.job_status(j)) == "bad" for j in recent)
        unknown = sum(ins.job_status(j) == "VANISHED" and not j.get("superseded") and not ins.is_grouped(j) for j in self.jobs)
        text = f"近 24 小时结束：完成 {done} · 失败/超时 {bad}" + (f" · 结果待确认 {unknown}" if unknown else "")
        canvas.create_text(12, 99, anchor="w", text=self.clip(text, "ui", w - 24), fill=c["warn"] if bad else c["muted"], font=f["ui"])

    # ------------------------------------------------------------ refresh

    def plan(self):
        clock = time.time()
        def due(name, every):
            entry = self.sync.get(name, {})
            return clock >= entry.get("retry", 0) and clock - entry.get("ok_epoch", 0) >= every
        plan = ["queue", "nodes"]
        if due("share", SHARE_EVERY):
            plan.append("share")
            if due("config", CONFIG_EVERY):
                plan.append("config")
        acct = self.sync.get("acct", {})
        last = acct.get("ok_epoch")
        unresolved = [ins.utc_time(j.get("observed")) for j in self.jobs
                      if ins.job_status(j) == "VANISHED" and not j.get("superseded") and not ins.is_grouped(j)]
        unresolved = [t for t in unresolved if t]
        # Only jobs that left the queue since the last sync widen the window; older leftovers do not.
        fresh = [t for t in unresolved if not last or t.timestamp() > last - 60]
        since = None
        if due("acct", ACCT_EVERY) or (fresh and due("acct", 240)):
            plan.append("acct")
            start = clock - 14 * 86400 if not last else last - 2 * 3600
            if fresh:
                start = min(start, min(t.timestamp() for t in fresh) - 2 * 3600)
            start = max(start, clock - 30 * 86400)
            since = ins.cluster_string(datetime.fromtimestamp(start, timezone.utc))
        return plan, since

    def refresh(self):
        if self.closed or self.busy:
            return
        if not self.demo:
            floor = max(FLOOR, self.settings["interval"])
            wait = self.store.reserve_read(self.profile, "refresh", floor)
            if wait:
                self.cooldown_until = time.time() + wait
                self.message(f"本地限速：约 {ins.clock_text(wait)} 后才能再刷新。没有发送任何请求。")
                self.update_header()
                return
            self.cooldown_until = time.time() + floor
        plan, since = self.plan()
        self.busy = True
        self.update_header()
        self.message("正在刷新：一次 SSH 会话，只读查询 " + "、".join({"queue": "作业", "nodes": "节点", "share": "公平分", "config": "优先级设置", "acct": "记账"}[p] for p in plan) + "…")
        profile, collector = self.profile, self.collector_factory(self.profile)
        def work():
            try:
                self.messages.put((True, profile, plan, collector.refresh(plan, since)))
            except Exception as exc:
                self.messages.put((False, profile, plan, str(exc)))
        threading.Thread(target=work, name="hellbender-refresh", daemon=True).start()

    def drain(self):
        if self.closed:
            return
        while not self.tray_events.empty():
            action = self.tray_events.get_nowait()
            if action == "quit":
                self.close()
                return
            if action == "hide":
                self.hide_to_tray()
            elif action in ("restore", "unavailable"):
                self.restore_window()
                if action == "unavailable":
                    self.message(self.tray.last_error, "warn")
        try:
            while True:
                ok, profile, plan, result = self.messages.get_nowait()
                self.busy = False
                self.finish_refresh(ok, profile, plan, result)
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def finish_refresh(self, ok, profile, plan, result):
        queue_ok = ok and result["sections"].get("queue", {}).get("ok")
        if not self.demo:
            self.store.finish_read(profile, "refresh", failed=not queue_ok)
            self.cooldown_until = time.time() + self.store.peek_read(profile, "refresh")
        if profile != self.profile:
            return
        if not ok:
            self.store.event(profile, "error", result)
            self.message(f"{result}  显示的是上次保存的数据。", "bad")
            self.update_header()
            return
        extras = {}
        sections = result["sections"]
        try:
            if sections.get("nodes", {}).get("ok"):
                jobs = self.jobs + [j | {"active": True} for j in sections.get("queue", {}).get("data", [])]
                shape = ins.usual_shape(jobs, True, self.int_setting("gpu_cpus"), self.float_setting("gpu_mem_gb"))
                extras["gpu_point"] = ins.gpu_point(ins.gpu_table(sections["nodes"]["data"], shape, self.watch_list()))
            if sections.get("share", {}).get("ok"):
                jobs = [j | {"active": True} for j in sections.get("queue", {}).get("data", [])] or self.jobs
                row = ins.pick_share(sections["share"]["data"], jobs)
                if row:
                    extras["share_point"] = ins.share_point(row, now())
            summary = self.store.apply_refresh(profile, result, extras)
        except Exception as exc:
            self.message(f"本地保存失败：{exc}。这次结果没有存下来。", "bad")
            self.update_header()
            return
        self.reload()
        names = {"queue": "作业", "nodes": "节点", "share": "公平分", "config": "优先级设置", "acct": "记账"}
        parts = []
        if "queue" in summary["ok"]:
            active = [j for j in self.jobs if j.get("active")]
            parts.append(f"作业 {len(active)} 个")
        if "nodes" in summary["ok"]:
            usable = sum(r["usable"] for r in self.gpu_rows if r["partition"] == "gpu")
            parts.append(f"公共分区能立即用的 GPU {usable} 张")
        if "share" in summary["ok"] and self.share_row and self.share_row.get("fairshare") is not None:
            parts.append(f"公平分 {self.share_row['fairshare']:.3f}")
        if "acct" in summary["ok"]:
            parts.append(f"记账补全 {summary['resolved']} 个已结束作业" if summary["resolved"] else "记账已同步")
        text = "已更新：" + "，".join(parts) if parts else "刷新完成"
        if summary["failed"]:
            failed = "；".join(f"{names.get(k, k)}失败（{str(v)[:80]}）" for k, v in summary["failed"].items())
            self.message(f"{text}。{failed}，保留上次数据，稍后自动重试。", "warn")
        else:
            self.message(text + "。", "good")
        self.update_header()

    def message(self, text, tone="muted"):
        self.message_text = text
        self.message_label.configure(text=text, style=f"Msg.{tone}.TLabel")

    def update_header(self):
        if not hasattr(self, "refresh_button"):
            return
        remaining = self.cooldown_until - time.time()
        if self.busy:
            self.refresh_button.configure(text="刷新中…")
            self.refresh_button.state(["disabled"])
        elif remaining > 0 and not self.demo:
            self.refresh_button.configure(text=f"↻ {ins.clock_text(remaining)}")
            self.refresh_button.state(["disabled"])
        else:
            self.refresh_button.configure(text="↻ 刷新")
            self.refresh_button.state(["!disabled"])
        if self.busy:
            status = "正在读取…"
        elif not self.queue_snapshot:
            status = "还没有数据"
        else:
            status = f"{ins.age_text(self.queue_snapshot['observed'])}更新"
            failed = [k for k, v in self.sync.items() if v.get("failures")]
            if failed:
                status += " · 部分没读到"
        self.status_label.configure(text=status)
        if status != getattr(self, "last_status", None):
            self.last_status = status
            if hasattr(self, "gpu_rows"):  # keep the cards' "N 分钟前" in step with the header
                self.draw_cards()
                self.render_resources_scope()

    def update_tray_tip(self):
        if not hasattr(self, "tray") or not hasattr(self.tray, "set_tooltip"):
            return
        active = [j for j in self.jobs if j.get("active")]
        running = sum(j.get("state") in RUNNING_STATES for j in active)
        tip = f"Hellbender · 运行 {running} · 排队 {len(active) - running}"
        watched = [r for r in self.gpu_rows if r["watched"] and r["partition"] == "gpu"]
        if watched:
            tip += f" · {ins.gpu_label(watched[0]['type'])} 空闲 {watched[0]['free']}"
        self.tray.set_tooltip(tip)

    def tick(self):
        if self.closed:
            return
        self.update_header()
        minute = int(time.time() // 60)
        if minute != self.last_minute:
            self.last_minute = minute
            today = datetime.now().date()
            if today != self.last_day:
                self.last_day = today
                self.render_jobs()
            else:
                self.refresh_running_rows()
                self.render_detail()
            self.draw_cards()
            self.render_resources_scope()
        if self.hidden and time.monotonic() - self.last_tray_check >= 10:
            self.last_tray_check = time.monotonic()
            if not self.tray.ensure_visible():
                self.restore_window()
                self.message(self.tray.last_error, "warn")
        self.root.after(1000, self.tick)

    def refresh_running_rows(self):
        for item, key in self.items.items():
            job = next((j for j in self.jobs if self.key(j) == key), None)
            if job and job.get("active") and job.get("state") in RUNNING_STATES and self.tree.exists(item):
                self.tree.item(item, values=self.row_values(job))

    def render_resources_scope(self):
        observed = self.nodes_snapshot["observed"] if self.nodes_snapshot else None
        if observed:
            self.res_scope.configure(text=f"节点数据 {ins.age_text(observed)}读取 · “能立即用”按你常用的 GPU 作业：{ins.shape_text(self.gpu_shape)}（{self.gpu_shape['source']}）")

    def seed_demo(self):
        shares, gpus = DemoCollector(clock=lambda: datetime.now(timezone.utc)).seed_history()
        self.store.save_snapshot(self.profile, "fairshare_history", shares)
        self.store.save_snapshot(self.profile, "gpu_history", gpus)
        self.refresh()
        self.message("演示模式：数据是合成的，独立数据库，不连接集群。")

    # ------------------------------------------------------------ actions

    def copy_id(self):
        job = self.selected_job()
        if job:
            self.root.clipboard_clear()
            self.root.clipboard_append(job["jobid"])
            self.message(f"已复制作业号 {job['jobid']}")

    def copy_dir(self):
        job = self.selected_job()
        if job and job.get("workdir"):
            self.root.clipboard_clear()
            self.root.clipboard_append(job["workdir"])
            self.message(f"已复制工作目录 {job['workdir']}")

    def settings_dialog(self):
        if self.busy:
            self.message("正在刷新，等它结束再改设置。")
            return
        c = self.c
        dialog = tk.Toplevel(self.root)
        dialog.title("设置")
        dialog.configure(bg=c["bg"])
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(2, weight=1)
        ttk.Label(frame, text="设置（只影响本机）", style="DialogTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
        values = {}
        def entry(row, key, label, width=18, column=0, hint=""):
            ttk.Label(frame, text=label).grid(row=row, column=column, sticky="w", padx=(0, 10), pady=4)
            current = self.settings.get(key, "")
            values[key] = tk.StringVar(value="" if current in (0, "0", None) else str(current))
            ttk.Entry(frame, textvariable=values[key], width=width).grid(row=row, column=column + 1, sticky="w", pady=4)
            if hint:
                ttk.Label(frame, text=hint, style="Dialog.TLabel").grid(row=row, column=column + 2, columnspan=2, sticky="w", padx=(8, 0))
        entry(1, "host", "SSH 别名", hint="用你已经配好的别名（ssh hellbender 能直接登录）")
        ttk.Label(frame, text="外观").grid(row=2, column=0, sticky="w", pady=4)
        skin = tk.StringVar(value=self.settings["skin"])
        ttk.Combobox(frame, textvariable=skin, values=list(SKINS), state="readonly", width=15).grid(row=2, column=1, sticky="w", pady=4)
        entry(3, "watch_gpus", "关注的 GPU", hint="逗号分隔，例如 L40S,H100；会排在最前并高亮")
        ttk.Label(frame, text="常用作业规格（留空 = 按你最近的作业自动推断）", style="Dialog.TLabel").grid(row=4, column=0, columnspan=4, sticky="w", pady=(12, 2))
        entry(5, "gpu_cpus", "GPU 作业 CPU 核数", width=8)
        entry(6, "gpu_mem_gb", "GPU 作业内存 GB", width=8)
        entry(7, "cpu_cpus", "CPU 作业核数", width=8)
        entry(8, "cpu_mem_gb", "CPU 作业内存 GB", width=8)
        topmost = tk.BooleanVar(value=bool(self.settings.get("topmost")))
        ttk.Checkbutton(frame, text="窗口置顶", variable=topmost).grid(row=9, column=0, columnspan=4, sticky="w", pady=(12, 2))
        to_tray = tk.BooleanVar(value=bool(self.settings.get("minimize_to_tray")))
        ttk.Checkbutton(frame, text="最小化时也收进托盘（关闭按钮总是收进托盘）", variable=to_tray,
                        state="normal" if getattr(self.tray, "supported", False) else "disabled").grid(row=10, column=0, columnspan=4, sticky="w")
        ttk.Label(frame, text="刷新只在你点击时发生：一次 SSH 会话，每 5 分钟最多一次；公平分最多每 30 分钟读一次，记账最多每 15 分钟同步一次。\n"
                              f"本地数据：{self.store.path}", style="Dialog.TLabel", justify="left").grid(row=11, column=0, columnspan=4, sticky="w", pady=(12, 0))
        error = tk.StringVar()
        ttk.Label(frame, textvariable=error, style="Msg.bad.TLabel").grid(row=12, column=0, columnspan=4, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=13, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        def save():
            try:
                host = validate_host(values["host"].get().strip())
                numbers = {}
                for key in ("gpu_cpus", "gpu_mem_gb", "cpu_cpus", "cpu_mem_gb"):
                    raw = values[key].get().strip()
                    number = float(raw) if raw else 0
                    if number < 0 or number > 4096:
                        raise ValueError("规格要在 0–4096 之间（留空表示自动）。")
                    numbers[key] = int(number) if key.endswith("cpus") else number
                watch = ",".join(w.strip() for w in values["watch_gpus"].get().split(",") if w.strip())
                self.settings.update(host=host, skin=skin.get(), watch_gpus=watch, topmost=topmost.get(),
                                     minimize_to_tray=to_tray.get(), **numbers)
                if not self.persist_settings():
                    raise ValueError("设置没能保存。")
                new_profile = "demo" if self.demo else host
                if new_profile != self.profile:
                    self.profile, self.selected = new_profile, None
                    self.cooldown_until = time.time() + (0 if self.demo else self.store.peek_read(self.profile, "refresh"))
                self.root.attributes("-topmost", self.settings["topmost"])
                self.mode_label.configure(text=self.mode_text())
                dialog.destroy()
                self.apply_skin()
                self.reload()
                self.message("设置已保存。")
            except Exception as exc:
                error.set(str(exc))
        ttk.Button(buttons, text="导出数据…", command=self.export).pack(side="left")
        ttk.Button(buttons, text="退出程序", command=self.close).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="保存", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=(0, 6))
        dialog.grab_set()

    def persist_settings(self):
        try:
            self.store.save_settings(self.settings)
            return True
        except Exception as exc:
            self.message(f"设置没能保存：{exc}", "bad")
            return False

    def export(self):
        destination = filedialog.asksaveasfilename(parent=self.root, title="导出本机保存的数据", defaultextension=".json",
                                                   initialfile="hellbender-data.json", filetypes=[("JSON", "*.json")])
        if destination:
            try:
                self.store.export(Path(destination), self.profile)
                self.message(f"已导出：{destination}")
            except Exception as exc:
                messagebox.showerror("导出失败", str(exc), parent=self.root)

    # ------------------------------------------------------------ window and tray

    def on_resize(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if self.root.state() == "normal" and self.root.winfo_width() > 100:
            self.normal_geometry = self.root.geometry().split("+")[0]
        width = self.root.winfo_width()
        if width > 100:
            self.res_note.configure(wraplength=width - 60)
            self.cpu_label.configure(wraplength=width - 60)

    def on_minimize(self, event):
        if event.widget is self.root and not self.closed and self.root.state() == "iconic":
            if self.settings.get("minimize_to_tray") and self.tray.available:
                self.root.after_idle(self.hide_to_tray)

    def close_to_tray(self):
        if self.hide_to_tray():
            return
        self.restore_window()
        self.message("没能收进托盘，窗口保持打开；要退出请用 设置 → 退出程序。" + (self.tray.last_error or ""), "warn")

    def hide_to_tray(self):
        if self.closed:
            return False
        if self.root.grab_current():
            self.restore_window()
            self.message("先关掉打开的对话框，再隐藏窗口。")
            return False
        if not self.tray.ensure_visible():
            self.restore_window()
            self.message(self.tray.last_error, "warn")
            return False
        self.hidden = True
        self.root.withdraw()
        if hasattr(self.tray, "explain_hide"):
            self.tray.explain_hide()
        return True

    def restore_window(self):
        if not self.closed:
            self.hidden = False
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()

    def close(self):
        if self.closed:
            return
        if self.hidden:
            self.restore_window()
        self.settings["geometry"] = self.normal_geometry
        if not self.persist_settings():
            messagebox.showwarning("设置没能保存", self.message_text, parent=self.root)
        self.closed = True
        self.tray.stop()
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()
