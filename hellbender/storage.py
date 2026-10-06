"""Transactional local persistence; demo and live stores are deliberately separate."""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from .parsers import TERMINAL

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def data_directory() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "HellbenderViewer"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/HellbenderViewer"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "hellbender-viewer"

DEFAULTS = {"host": "hellbender", "skin": "Neon", "interval": 120, "auto": False, "topmost": False,
            "geometry": "1180x760", "minimize_to_tray": True, "watch_gpus": "L40S",
            "gpu_cpus": 0, "gpu_mem_gb": 0, "cpu_cpus": 0, "cpu_mem_gb": 0}
# A refresh also covers what these older per-category reads did; their pending cooldowns still apply.
RELATED = {"refresh": ("queue", "nodes", "history", "cluster_queue", "details")}
HISTORY_LIMIT = {"gpu_history": 400, "fairshare_history": 500}
SECTION_RETRY = {"share": 3600, "config": 6 * 3600, "acct": 1800, "nodes": 900, "queue": 300}

class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError(f"Database schema {version} is newer than this viewer supports. No migration was attempted.")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (profile TEXT NOT NULL, jobid TEXT NOT NULL,
              submitted TEXT NOT NULL, active INTEGER NOT NULL, first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(profile,jobid,submitted));
            CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, profile TEXT NOT NULL,
              jobid TEXT NOT NULL, submitted TEXT NOT NULL, observed TEXT NOT NULL, ratio REAL,
              label TEXT, source TEXT);
            CREATE TABLE IF NOT EXISTS snapshots (profile TEXT NOT NULL, kind TEXT NOT NULL,
              observed TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(profile,kind));
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, profile TEXT NOT NULL,
              observed TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
            PRAGMA user_version=1;
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def settings(self):
        with closing(self.connect()) as db:
            return DEFAULTS | {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM settings")}

    def save_settings(self, values: dict):
        with closing(self.connect()) as db, db:
            db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)", [(k, json.dumps(v)) for k, v in values.items()])

    def jobs(self, profile: str) -> list[dict]:
        with closing(self.connect()) as db:
            return [json.loads(r["payload"]) | {"active": bool(r["active"]), "observed": r["last_seen"], "first_seen": r["first_seen"]}
                    for r in db.execute("SELECT * FROM jobs WHERE profile=? ORDER BY active DESC,last_seen DESC,jobid DESC", (profile,))]

    @staticmethod
    def _load(db, profile, kind):
        row = db.execute("SELECT * FROM snapshots WHERE profile=? AND kind=?", (profile, kind)).fetchone()
        return {"observed": row["observed"], "data": json.loads(row["payload"])} if row else None

    @staticmethod
    def _save(db, profile, kind, payload, stamp):
        db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)", (profile, kind, stamp, json.dumps(payload)))

    def snapshot(self, profile: str, kind: str):
        with closing(self.connect()) as db:
            return self._load(db, profile, kind)

    def save_snapshot(self, profile: str, kind: str, payload, observed=None):
        with closing(self.connect()) as db, db:
            self._save(db, profile, kind, payload, observed or now())

    def history(self, profile: str, kind: str) -> list:
        snapshot = self.snapshot(profile, kind)
        return snapshot["data"] if snapshot else []

    def _append(self, db, profile, kind, point, stamp):
        saved = self._load(db, profile, kind)
        points = (saved["data"] if saved else []) + [point]
        self._save(db, profile, kind, points[-HISTORY_LIMIT.get(kind, 400):], stamp)

    @staticmethod
    def _until(budget, kind):
        entry = budget.get(kind, {})
        return max(budget.get("last", 0) + 30, budget.get("lease", 0), entry.get("next", 0),
                   *(budget.get(k, {}).get("next", 0) for k in RELATED.get(kind, ())))

    def peek_read(self, profile, kind, clock=None) -> int:
        """Seconds until a manual read is allowed; reserves nothing."""
        stamp = time.time() if clock is None else clock
        saved = self.snapshot(profile, "_read_budget")
        until = self._until(saved["data"] if saved else {}, kind)
        return int(until - stamp) + 1 if stamp < until else 0

    def reserve_read(self, profile, kind, floor, clock=None):
        """Persistent manual-read debounce, one local lease, and the shared action gap."""
        stamp = time.time() if clock is None else clock
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM snapshots WHERE profile=? AND kind='_read_budget'", (profile,)).fetchone()
            budget = json.loads(row[0]) if row else {}
            entry = budget.get(kind, {})
            until = self._until(budget, kind)
            if stamp < until:
                return int(until - stamp) + 1
            budget.update(last=stamp, lease=stamp + 95)
            budget[kind] = entry | {"next": stamp + floor, "floor": floor}
            self._save(db, profile, "_read_budget", budget, now())
        return 0

    def finish_read(self, profile, kind, failed=False, clock=None):
        stamp = time.time() if clock is None else clock
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM snapshots WHERE profile=? AND kind='_read_budget'", (profile,)).fetchone()
            budget = json.loads(row[0]) if row else {}
            entry = budget.get(kind, {})
            failures = entry.get("failures", 0) + 1 if failed else 0
            entry["failures"] = failures
            if failed:
                entry["next"] = stamp + min(3600, entry.get("floor", 300) * 2 ** min(failures, 4))
            budget.update(lease=0)
            budget[kind] = entry
            self._save(db, profile, "_read_budget", budget, now())

    def _merge(self, db, profile, jobs, kind, stamp) -> int:
        """Queue: returns how many tracked jobs left the queue. Accounting: how many final results were added."""
        changed = 0
        incoming = {(j["jobid"], j.get("submitted") or "unknown") for j in jobs}
        if kind == "queue":
            # A vanished job is not proof of success or completion.
            for row in db.execute("SELECT * FROM jobs WHERE profile=? AND active=1", (profile,)).fetchall():
                payload = json.loads(row["payload"])
                payload["queue_absent"] = True
                if "[" in row["jobid"]:
                    payload["superseded"] = True  # grouped array summary; tasks are tracked individually
                changed += "[" not in row["jobid"] and (row["jobid"], row["submitted"]) not in incoming
                db.execute("UPDATE jobs SET active=0,payload=? WHERE profile=? AND jobid=? AND submitted=?",
                           (json.dumps(payload), profile, row["jobid"], row["submitted"]))
        for record in jobs:
            job = dict(record)
            jid, submitted = job["jobid"], job.get("submitted") or "unknown"
            # Accounting only fills in finished jobs; the queue is the authority for active ones.
            if kind == "history" and job.get("state") not in TERMINAL:
                continue
            old = db.execute("SELECT * FROM jobs WHERE profile=? AND jobid=? AND submitted=?", (profile, jid, submitted)).fetchone()
            if kind == "history" and old and old["active"]:
                continue
            payload = json.loads(old["payload"]) if old else {}
            if kind == "history" and (not old or payload.get("queue_absent") or payload.get("state") not in TERMINAL):
                changed += 1
            payload.update(job)
            payload["submitted"] = submitted
            payload.update(queue_absent=False, controller_missing=False)
            if kind == "queue":
                payload.update(state_confirmed=False, superseded=False)
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)",
                       (profile, jid, submitted, int(kind == "queue"), old["first_seen"] if old else stamp, stamp, json.dumps(payload)))
        self._save(db, profile, kind, {"count": len(jobs)}, stamp)
        return changed

    def merge_jobs(self, profile: str, jobs: list[dict], kind: str, observed=None) -> int:
        with closing(self.connect()) as db, db:
            return self._merge(db, profile, jobs, kind, observed or now())

    def apply_refresh(self, profile: str, result: dict, extras: dict | None = None, observed=None) -> dict:
        """Persist one refresh atomically. Failed sections keep their previous data and back off."""
        stamp, epoch, extras = observed or now(), time.time(), extras or {}
        sections = result.get("sections", {})
        summary = {"ok": [], "failed": {}, "vanished": 0, "resolved": 0}
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            saved = self._load(db, profile, "_sync")
            sync = saved["data"] if saved else {}
            for name, section in sections.items():
                entry = sync.get(name, {})
                if section.get("ok"):
                    summary["ok"].append(name)
                    entry.update(ok=stamp, ok_epoch=epoch, failures=0, retry=0)
                else:
                    failures = entry.get("failures", 0) + 1
                    entry.update(failures=failures, failed=stamp, error=str(section.get("error", ""))[:300],
                                 retry=epoch + min(86400, SECTION_RETRY.get(name, 900) * 2 ** min(failures - 1, 5)))
                    summary["failed"][name] = section.get("error", "")
                sync[name] = entry
            ok = lambda name: sections.get(name, {}).get("ok")
            if ok("queue"):
                summary["vanished"] = self._merge(db, profile, sections["queue"]["data"], "queue", stamp)
            if ok("acct"):
                summary["resolved"] = self._merge(db, profile, sections["acct"]["data"], "history", stamp)
                sync["acct"]["since"] = result.get("since")
            if ok("nodes"):
                self._save(db, profile, "nodes", {"nodes": sections["nodes"]["data"]}, stamp)
                if extras.get("gpu_point") is not None:
                    self._append(db, profile, "gpu_history", {"t": stamp, "rows": extras["gpu_point"]}, stamp)
            if ok("share"):
                self._save(db, profile, "fairshare", {"rows": sections["share"]["data"]}, stamp)
                if extras.get("share_point"):
                    self._append(db, profile, "fairshare_history", extras["share_point"], stamp)
            if ok("config"):
                self._save(db, profile, "priority_config", sections["config"]["data"], stamp)
            self._save(db, profile, "_sync", sync, stamp)
            trace = result.get("trace", {})
            message = (f"refresh {','.join(trace.get('plan', sections))}: ok={','.join(summary['ok']) or '-'}"
                       + (f"; failed={','.join(summary['failed'])}" if summary["failed"] else "")
                       + f"; {trace.get('bytes', 0)} B in {trace.get('seconds', 0)} s")
            db.execute("INSERT INTO events(profile,observed,kind,message) VALUES (?,?,?,?)",
                       (profile, stamp, "warning" if summary["failed"] else "refresh", message[:1000]))
        return summary

    def update_job(self, profile: str, job: dict, updates: dict, observation=False):
        stamp = now()
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT payload FROM jobs WHERE profile=? AND jobid=? AND submitted=?",
                             (profile, job["jobid"], job["submitted"])).fetchone()
            if not row:
                raise ValueError("Job no longer exists in the local store.")
            payload = json.loads(row["payload"]) | updates
            if updates.get("state_confirmed") and updates.get("state") in TERMINAL:
                payload["queue_absent"] = False
                db.execute("UPDATE jobs SET active=0 WHERE profile=? AND jobid=? AND submitted=?",
                           (profile, job["jobid"], job["submitted"]))
            db.execute("UPDATE jobs SET payload=? WHERE profile=? AND jobid=? AND submitted=?",
                       (json.dumps(payload), profile, job["jobid"], job["submitted"]))
            if observation:
                db.execute("INSERT INTO observations(profile,jobid,submitted,observed,ratio,label,source) VALUES (?,?,?,?,?,?,?)",
                           (profile, job["jobid"], job["submitted"], stamp, updates.get("progress_ratio"), updates.get("progress_label"), updates.get("progress_source")))

    def observations(self, profile: str, job: dict):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM observations WHERE profile=? AND jobid=? AND submitted=? ORDER BY id DESC LIMIT 200",
                                               (profile, job["jobid"], job["submitted"]))][::-1]

    def event(self, profile: str, kind: str, message: str):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO events(profile,observed,kind,message) VALUES (?,?,?,?)", (profile, now(), kind, message[:1000]))

    def events(self, profile: str):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM events WHERE profile=? ORDER BY id DESC LIMIT 100", (profile,))]

    def export(self, destination: Path, profile: str):
        # Exclusive creation avoids overwriting an existing user export.
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            observations = [dict(r) for r in db.execute("SELECT * FROM observations WHERE profile=? ORDER BY id", (profile,))]
            events = [dict(r) for r in db.execute("SELECT * FROM events WHERE profile=? ORDER BY id", (profile,))]
            jobs = [dict(r) | {"payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM jobs WHERE profile=?", (profile,))]
            snapshots = [dict(r) | {"payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM snapshots WHERE profile=?", (profile,))]
            payload = {"schema_version": 1, "exported": now(), "profile": profile, "jobs": jobs,
                       "observations": observations, "snapshots": snapshots, "events": events,
                       "settings": {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}}
        with Path(destination).open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
