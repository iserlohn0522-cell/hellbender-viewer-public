"""Build a standard-library zipapp; no package installs or online build step."""
import hashlib
import json
import platform
import zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent
DEST = ROOT / "dist"
DEST.mkdir(exist_ok=True)
from hellbender import __version__
artifact = DEST / f"HellbenderViewer-{__version__}.pyz"
entry = "import multiprocessing\nfrom hellbender.__main__ import main\nif __name__ == '__main__':\n    multiprocessing.freeze_support()\n    raise SystemExit(main())\n"
with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("__main__.py",entry)
    for source in sorted((ROOT / "hellbender").glob("*.py")):
        archive.write(source,source.relative_to(ROOT).as_posix())
manifest = {"artifact":artifact.name,"bytes":artifact.stat().st_size,
            "sha256":hashlib.sha256(artifact.read_bytes()).hexdigest(),"python":platform.python_version(),
            "runtime":"Python 3.10+ with Tk; existing OpenSSH client for live monitoring",
            "source_sha256":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / "hellbender").glob("*.py"))}}
(DEST / "build-manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
print(json.dumps({k:v for k,v in manifest.items() if k!='source_sha256'},indent=2))
