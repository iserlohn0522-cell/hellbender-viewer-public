"""Allow-listed read-only Slurm queries over your existing SSH alias.

One Refresh opens one short SSH session that runs a fixed script of read-only
scheduler commands. Each command is framed and time-limited on its own, so a
failing section never empties the others. Nothing here accepts a user command.
"""
from __future__ import annotations
import os
import re
import shlex
import subprocess
import threading
import time
from types import SimpleNamespace
from .parsers import (SEP, QUEUE_FORMAT, ACCT_FORMAT, SINFO_FORMAT, SHARE_FORMAT, ParseError,
                      parse_queue, parse_history, parse_nodes, parse_share, parse_priority_config)

SECTIONS = ("queue", "nodes", "share", "config", "acct")
SECTION_TIMEOUT = {"queue": 15, "nodes": 15, "share": 15, "config": 10, "acct": 25}
REMOTE_TIMEOUT = 80
LOCAL_TIMEOUT = 95
PARSERS = {"queue": parse_queue, "nodes": parse_nodes, "share": parse_share,
           "config": parse_priority_config, "acct": parse_history}

class MonitorError(RuntimeError):
    def __init__(self, message, code="query_failed"):
        super().__init__(message)
        self.code = code

def validate_host(host: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", host or ""):
        raise ValueError("SSH 别名只能包含字母、数字和 . _ @ -")
    return host

def validate_since(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", value or ""):
        raise ValueError("Accounting window start must look like 2026-10-06T08:00:00.")
    return value

def section_command(name: str, since: str | None = None) -> str:
    if name == "queue":
        return 'squeue --user="$USER" --noheader --format=' + shlex.quote(SEP.join(QUEUE_FORMAT))
    if name == "nodes":
        # Size 0 prints each value in full; the unit separator suffix delimits fields.
        spec = ",".join(f"{field}:0" + (SEP if index < len(SINFO_FORMAT) - 1 else "")
                        for index, field in enumerate(SINFO_FORMAT))
        return "sinfo --Node --noheader --Format=" + shlex.quote(spec)
    if name == "share":
        return "sshare --noheader --parsable2 --Users --format=" + ",".join(SHARE_FORMAT)
    if name == "config":
        return "scontrol show config | grep -E '^Priority'"
    if name == "acct":
        return ('sacct --user="$USER" --allocations --noheader --parsable2 --starttime=' + validate_since(since)
                + " --endtime=now --format=" + shlex.quote(ACCT_FORMAT))
    raise ValueError("Unsupported monitoring query.")

def refresh_script(plan, since: str | None = None) -> str:
    if not plan or len(set(plan)) != len(plan) or any(name not in SECTIONS for name in plan):
        raise ValueError("Unsupported refresh plan.")
    lines = ["export LC_ALL=C SLURM_TIME_FORMAT=standard"]
    for name in plan:
        lines.append(f"printf '\\036HB:{name}\\n'; printf '\\036HB:{name}\\n' >&2; "
                     f"timeout {SECTION_TIMEOUT[name]}s {section_command(name, since)}; "
                     "printf '\\036HB:rc:%s\\n' \"$?\"")
    return "; ".join(lines)

def ssh_program() -> str:
    """Windows: the built-in OpenSSH, which uses the Windows ssh-agent; a Git or MSYS ssh earlier on
    PATH cannot see that agent and fails with 'Permission denied (publickey)'. Elsewhere: PATH ssh."""
    if os.name == "nt":
        system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
        if os.path.isfile(system):
            return system
    return "ssh"

def ssh_arguments(host: str, query: str) -> list[str]:
    return [ssh_program(), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1", "-o", "ClearAllForwardings=yes",
            "-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "PermitLocalCommand=no",
            "-o", "RemoteCommand=none", "--", validate_host(host), query]

def run_bounded(args, timeout=LOCAL_TIMEOUT, **options):
    """Bound memory while receiving, not only after the process has finished."""
    creation = {"creationflags": options["creationflags"]} if "creationflags" in options else {}
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **creation)
    buffers, exceeded = [bytearray(), bytearray()], threading.Event()
    def drain(stream, index, limit):
        while chunk := stream.read(4096):
            remaining = limit - len(buffers[index])
            buffers[index].extend(chunk[:max(0, remaining)])
            if len(chunk) > remaining:
                exceeded.set()
                process.kill()
                break
        stream.close()
    readers = [threading.Thread(target=drain, args=(process.stdout, 0, 2_000_000), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, 1, 16_384), daemon=True)]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=1)
    if exceeded.is_set():
        raise MonitorError("返回数据超过接收上限，已中止；保留上次数据。", "too_large")
    return SimpleNamespace(returncode=process.returncode, stdout=buffers[0].decode("utf-8", "replace"),
                           stderr=buffers[1].decode("utf-8", "replace"))

def split_sections(stdout: str, stderr: str) -> dict:
    sections, current = {}, None
    for chunk in stdout.split("\x1eHB:")[1:]:
        header, _, body = chunk.partition("\n")
        header = header.strip()
        if header.startswith("rc:"):
            if current:
                code = header[3:]
                sections[current]["rc"] = int(code) if re.fullmatch(r"-?\d+", code) else None
            current = None
        elif header:
            current = header
            sections[current] = {"body": body, "rc": None, "stderr": ""}
    for chunk in stderr.split("\x1eHB:")[1:]:
        header, _, body = chunk.partition("\n")
        if header.strip() in sections:
            sections[header.strip()]["stderr"] = body.strip()
    return sections

def section_results(plan, stdout: str, stderr: str) -> dict:
    parts, results = split_sections(stdout, stderr), {}
    for name in plan:
        part = parts.get(name)
        if part is None:
            results[name] = {"ok": False, "error": "没有收到这一段输出（会话中断或整体超时）。"}
        elif part["rc"] is None:
            results[name] = {"ok": False, "error": "输出不完整（超时或被中断），未使用。"}
        elif part["rc"] != 0:
            detail = part["stderr"][-300:] or ("超时" if part["rc"] == 124 else f"退出码 {part['rc']}")
            results[name] = {"ok": False, "error": detail}
        else:
            try:
                results[name] = {"ok": True, "data": PARSERS[name](part["body"]), "bytes": len(part["body"].encode())}
            except (ParseError, ValueError) as exc:
                results[name] = {"ok": False, "error": str(exc)}
    return results

class Collector:
    def __init__(self, host: str, runner=None):
        self.host = validate_host(host)
        self.runner = runner or run_bounded
        self.trace = []

    def refresh(self, plan, since: str | None = None) -> dict:
        query = f"timeout {REMOTE_TIMEOUT}s sh -c " + shlex.quote(refresh_script(plan, since))
        args = ssh_arguments(self.host, query)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        started = time.monotonic()
        try:
            response = self.runner(args, timeout=LOCAL_TIMEOUT, **options)
        except FileNotFoundError as exc:
            raise MonitorError("找不到本机 OpenSSH 客户端（ssh）。", "ssh_missing") from exc
        except subprocess.TimeoutExpired as exc:
            raise MonitorError(f"SSH 会话 {LOCAL_TIMEOUT} 秒内没有结束，已中止；保留上次数据。", "ssh_timeout") from exc
        trace = {"plan": list(plan), "seconds": round(time.monotonic() - started, 3),
                 "bytes": len(response.stdout.encode()), "returncode": response.returncode}
        self.trace.append(trace)
        if response.returncode == 255:
            raise MonitorError("SSH 连接失败：" + (response.stderr.strip()[-400:] or "没有错误信息"), "ssh_failed")
        return {"sections": section_results(plan, response.stdout, response.stderr), "since": since, "trace": trace}
