import argparse
import multiprocessing
import sys
from pathlib import Path
from .storage import Store, data_directory
from . import __version__
from .tray import restore_existing

def main():
    parser = argparse.ArgumentParser(description="Hellbender Viewer - read-only desktop monitoring")
    parser.add_argument("--demo", action="store_true", help="Synthetic offline data in a separate database")
    parser.add_argument("--data-dir", type=Path, help="Override local database directory")
    parser.add_argument("--skin", choices=["Neon", "Midnight", "Daylight"])
    parser.add_argument("--version", action="version", version=f"Hellbender Viewer {__version__}")
    args = parser.parse_args()
    directory = args.data_dir or data_directory()
    database = directory / ("demo.sqlite3" if args.demo else "monitor.sqlite3")
    if restore_existing(database):
        return 0
    import tkinter as tk
    from tkinter import messagebox
    from .ui import App
    root = tk.Tk()
    try:
        store = Store(database)
        App(root, store, demo=args.demo, skin=args.skin)
    except Exception as exc:
        messagebox.showerror("Hellbender Viewer could not start", str(exc), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
