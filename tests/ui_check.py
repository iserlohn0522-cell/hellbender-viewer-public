"""Real Tk widgets on demo data in a temporary database; saves window captures. Never connects.

Usage: python tests/ui_check.py OUTPUT_DIR [skin ...]
"""
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import ImageGrab
from hellbender import ui
from hellbender.storage import Store
from hellbender.tray import UnavailableTray

def pump(root, seconds=.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(.015)

def shot(root, path):
    root.attributes("-topmost", True)
    root.lift()
    pump(root, .5)
    if sys.platform == "win32":
        capture = ImageGrab.grab(window=root.winfo_id())
    else:
        x, y = root.winfo_rootx(), root.winfo_rooty()
        capture = ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height()), all_screens=True)
    capture.save(path)

def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    skins = sys.argv[2:] or ["Neon"]
    errors = []
    with tempfile.TemporaryDirectory(prefix="hb-ui-") as directory:
        store = Store(Path(directory) / "demo.sqlite3")
        for skin in skins:
            root = tk.Tk()
            root.report_callback_exception = lambda *args: errors.append(args)
            app = ui.App(root, store, demo=True, skin=skin, tray_factory=lambda *a, **k: UnavailableTray())
            root.geometry("1180x760+60+40")
            for _ in range(40):
                pump(root, .1)
                if not app.busy:
                    break
            app.tree.selection_set(app.tree.get_children()[0])
            pump(root)
            shot(root, out / f"{skin.lower()}-jobs.png")
            for name in ("resources", "share"):
                app.show_tab(name)
                pump(root)
                shot(root, out / f"{skin.lower()}-{name}.png")
            app.show_tab("jobs")
            app.view.set("finished")
            app.render_jobs()
            children = app.tree.get_children()
            if children:
                app.tree.selection_set(children[0])
            pump(root)
            shot(root, out / f"{skin.lower()}-finished.png")
            root.geometry("980x620+60+40")
            app.view.set("pending")
            app.render_jobs()
            app.tree.selection_set(app.tree.get_children()[0])
            pump(root)
            shot(root, out / f"{skin.lower()}-small-pending.png")
            app.close()
    print("callback errors:", len(errors))
    for error in errors:
        print(error)
    return 1 if errors else 0

if __name__ == "__main__":
    raise SystemExit(main())
