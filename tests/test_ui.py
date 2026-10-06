"""Real Tk widgets with demo data or scripted collectors. Nothing here can contact a cluster."""
import time
import tkinter as tk
from tkinter import ttk
import pytest
from hellbender import ui
from hellbender.collector import MonitorError
from hellbender.demo import DemoCollector
from hellbender.storage import Store
from hellbender.tray import UnavailableTray

def pump(root, seconds=.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(.01)

def settle(app, root, limit=8):
    end = time.monotonic() + limit
    pump(root, .1)
    while app.busy and time.monotonic() < end:
        pump(root, .05)
    pump(root, .2)
    assert not app.busy

@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    window.errors = []
    window.report_callback_exception = lambda *args: window.errors.append(args)
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass

def make(root, store, **options):
    return ui.App(root, store, tray_factory=lambda *a, **k: UnavailableTray(), **options)

class Scripted:
    """Stands in for Collector: records plans and returns demo-shaped results, optionally failing sections."""
    def __init__(self, fail=(), error=None):
        self.plans, self.fail, self.error, self.demo = [], set(fail), error, DemoCollector()
    def __call__(self, host):
        return self
    def refresh(self, plan, since=None):
        self.plans.append((list(plan), since))
        if self.error:
            raise self.error
        result = self.demo.refresh(plan, since)
        for name in self.fail & set(plan):
            result["sections"][name] = {"ok": False, "error": f"{name}: fake failure"}
        return result

def test_demo_fills_every_view(tmp_path, root):
    app = make(root, Store(tmp_path / "demo.sqlite3"), demo=True)
    settle(app, root)
    assert len(app.tree.get_children()) == 7 and app.view_buttons["finished"].cget("text") == "已结束 11"
    app.tree.selection_set(app.tree.get_children()[0])
    pump(root)
    assert "时间" in app.detail.get("1.0", "end") and app.copy_id_button.instate(["!disabled"])
    pending = next(i for i in app.tree.get_children() if app.tree.set(i, "status") == "排队中")
    app.tree.selection_set(pending)
    pump(root)
    assert "预计开始" in app.detail.get("1.0", "end")
    app.view.set("finished")
    app.render_jobs()
    assert len(app.tree.get_children()) == 11
    assert app.gpu_tree.get_children() and "L40S" in app.gpu_tree.set(app.gpu_tree.get_children()[0], "type")
    assert "公平分" in app.share_text.get("1.0", "end") and "预计" in app.share_text.get("1.0", "end")
    assert all(canvas.find_all() for canvas in app.cards)
    app.search.set("mace")
    assert all("mace" in app.tree.set(i, "name") for i in app.tree.get_children())
    for skin in ui.SKINS:
        app.settings["skin"] = skin
        app.apply_skin()
        pump(root)
    root.geometry("980x620")
    pump(root)
    app.close()
    assert root.errors == []

def test_live_mode_never_reads_at_startup_and_respects_the_cooldown(tmp_path, root):
    scripted = Scripted()
    app = make(root, Store(tmp_path / "live.sqlite3"), collector_factory=scripted)
    pump(root, .3)
    assert scripted.plans == [] and "还没有数据" in app.message_text
    app.refresh()
    settle(app, root)
    assert scripted.plans[0][0] == ["queue", "nodes", "share", "config", "acct"] and scripted.plans[0][1]
    assert app.queue_snapshot and "已更新" in app.message_text
    app.refresh()
    pump(root)
    assert len(scripted.plans) == 1 and "本地限速" in app.message_text and "没有发送任何请求" in app.message_text
    assert app.refresh_button.instate(["disabled"]) and ":" in app.refresh_button.cget("text")
    app.close()
    assert root.errors == []

def test_failed_section_keeps_old_values_and_waits_before_retrying(tmp_path, root, monkeypatch):
    store = Store(tmp_path / "live.sqlite3")
    scripted = Scripted(fail={"share"})
    app = make(root, store, collector_factory=scripted)
    app.refresh()
    settle(app, root)
    assert "公平分失败" in app.message_text and app.share_row is None
    assert len(app.tree.get_children()) == 7
    monkeypatch.setattr(store, "reserve_read", lambda *a, **k: 0)
    app.refresh()
    settle(app, root)
    assert "share" not in scripted.plans[1][0] and "config" not in scripted.plans[1][0]
    app.close()
    assert root.errors == []

def test_ssh_failure_keeps_saved_data(tmp_path, root, monkeypatch):
    store = Store(tmp_path / "live.sqlite3")
    good = Scripted()
    app = make(root, store, collector_factory=good)
    app.refresh()
    settle(app, root)
    app.collector_factory = Scripted(error=MonitorError("SSH 连接失败：Permission denied (publickey).", "ssh_failed"))
    monkeypatch.setattr(store, "reserve_read", lambda *a, **k: 0)
    app.refresh()
    settle(app, root)
    assert "publickey" in app.message_text and "上次保存" in app.message_text
    assert len(app.tree.get_children()) == 7
    app.close()
    assert root.errors == []

def test_vanished_job_triggers_an_early_accounting_check(tmp_path, root, monkeypatch):
    store = Store(tmp_path / "live.sqlite3")
    app = make(root, store, collector_factory=Scripted())
    app.refresh()
    settle(app, root)
    sync = store.snapshot(app.profile, "_sync")["data"]
    sync["acct"]["ok_epoch"] -= 300
    sync["share"]["ok_epoch"] = time.time()
    store.save_snapshot(app.profile, "_sync", sync)
    app.reload()
    assert "acct" not in app.plan()[0]
    job = next(j for j in app.jobs if j.get("active"))
    store.apply_refresh(app.profile, {"sections": {"queue": {"ok": True, "data": [j for j in DemoCollector().refresh(["queue"])["sections"]["queue"]["data"] if j["jobid"] != job["jobid"]]}}})
    app.reload()
    plan, since = app.plan()
    assert "acct" in plan and since
    app.close()

def test_settings_dialog_saves_preferences(tmp_path, root):
    store = Store(tmp_path / "demo.sqlite3")
    app = make(root, store, demo=True)
    settle(app, root)
    app.settings_dialog()
    pump(root)
    dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
    entries = []
    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Entry) and not isinstance(child, ttk.Combobox):
                entries.append(child)
            walk(child)
    walk(dialog)
    values = ["hellbender", "L40S, H100", "8", "48", "", ""]
    for entry, value in zip(entries, values):
        entry.delete(0, "end")
        entry.insert(0, value)
    def find(widget, text):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button) and child.cget("text") == text:
                return child
            found = find(child, text)
            if found:
                return found
    find(dialog, "保存").invoke()
    pump(root)
    saved = store.settings()
    assert saved["watch_gpus"] == "L40S,H100" and saved["gpu_cpus"] == 8 and saved["gpu_mem_gb"] == 48.0 and saved["cpu_cpus"] == 0
    assert not dialog.winfo_exists() and app.gpu_shape["cpus"] == 8 and app.gpu_shape["source"] == "按设置"
    app.close()
    assert root.errors == []
