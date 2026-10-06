"""Desktop entry point for Hellbender Viewer (pythonw-friendly; no console window)."""
import multiprocessing
from hellbender.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
