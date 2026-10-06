import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from hellbender.collector import (Collector, MonitorError, SECTIONS, refresh_script, run_bounded, section_command,
                                  section_results, ssh_arguments, validate_host)
from hellbender.parsers import SEP

FIXTURES = Path(__file__).parent / "fixtures"
ALL = list(SECTIONS)
SINCE = "2026-09-22T08:00:00"

@pytest.mark.parametrize("host", ["-oProxyCommand=bad", "hellbender;bad", "host\ncommand", "$(bad)", ""])
def test_host_injection_rejected(host):
    with pytest.raises(ValueError):
        validate_host(host)

def test_script_contains_only_read_only_scheduler_commands():
    script = refresh_script(ALL, SINCE)
    commands = [part.split()[2] for part in script.split(";") if part.strip().startswith("timeout")]
    assert commands == ["squeue", "sinfo", "sshare", "scontrol", "sacct"]
    for forbidden in ("scancel", "sbatch", "srun", "salloc", "update", "sacctmgr", "tail ", "cat ", "find ", "seff", "ssh "):
        assert forbidden not in script
    assert 'squeue --user="$USER"' in script and 'sacct --user="$USER"' in script
    assert "sinfo --Node --noheader --Format=" in script and "sshare --noheader --parsable2 --Users" in script
    assert "scontrol show config | grep -E '^Priority'" in script
    assert not re.search(r"--all(\s|$)", script) and "--array" not in script
    assert script.count("timeout ") == len(ALL)

def test_plan_and_window_are_validated():
    for plan in ([], ["queue", "queue"], ["queue", "shell"], ["update"]):
        with pytest.raises(ValueError):
            refresh_script(plan)
    for bad in ("now; rm -rf ~", "2026-09-22", "", None):
        with pytest.raises(ValueError):
            section_command("acct", bad)
    with pytest.raises(ValueError):
        section_command("detail")

def test_ssh_arguments_keep_strict_existing_access():
    argv = ssh_arguments("hellbender", "true")
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "UpdateHostKeys=no", "ControlMaster=no",
                   "ControlPersist=no", "PermitLocalCommand=no", "RemoteCommand=none", "ClearAllForwardings=yes"):
        assert option in argv
    assert argv[-3:] == ["--", "hellbender", "true"]
    program = argv[0].lower()
    assert program == "ssh" or program.endswith(os.path.join("system32", "openssh", "ssh.exe"))

def framed(parts, rc=0):
    out, err = "", ""
    for name, body, code, stderr in parts:
        out += f"\x1eHB:{name}\n{body}" + (f"\x1eHB:rc:{code}\n" if code is not None else "")
        err += f"\x1eHB:{name}\n{stderr}"
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

def fixture(name, sep=None):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return text.replace("|", sep) if sep else text

def test_one_session_and_a_failing_section_does_not_empty_others():
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return framed([("queue", fixture("squeue.psv", SEP), 0, ""), ("nodes", fixture("sinfo.psv", SEP), 0, ""),
                       ("share", "", 1, "sshare: error: Access/permission denied"), ("config", "", 124, ""),
                       ("acct", "1|broken\n", 0, "")])
    result = Collector("hellbender", runner).refresh(ALL, SINCE)
    assert len(calls) == 1 and calls[0][1]["timeout"] == 95
    remote = calls[0][0][-1]
    assert remote.startswith("timeout 80s sh -c ") and "shell" not in calls[0][1]
    sections = result["sections"]
    assert sections["queue"]["ok"] and len(sections["queue"]["data"]) == 5
    assert sections["nodes"]["ok"] and len(sections["nodes"]["data"]) == 11
    assert not sections["share"]["ok"] and "permission denied" in sections["share"]["error"]
    assert not sections["config"]["ok"] and sections["config"]["error"] == "超时"
    assert not sections["acct"]["ok"] and "expected 15 fields" in sections["acct"]["error"]

def test_incomplete_or_missing_sections_are_not_used():
    response = framed([("queue", fixture("squeue.psv", SEP), None, "")])
    sections = section_results(["queue", "nodes"], response.stdout, response.stderr)
    assert not sections["queue"]["ok"] and "不完整" in sections["queue"]["error"]
    assert not sections["nodes"]["ok"]

def test_ssh_failures_raise_instead_of_returning_empty_data():
    def refused(*args, **kwargs):
        return SimpleNamespace(returncode=255, stdout="", stderr="Permission denied (publickey).")
    with pytest.raises(MonitorError, match="publickey") as caught:
        Collector("hellbender", refused).refresh(["queue"])
    assert caught.value.code == "ssh_failed"
    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 95)
    with pytest.raises(MonitorError) as caught:
        Collector("hellbender", slow).refresh(["queue"])
    assert caught.value.code == "ssh_timeout"
    def missing(*args, **kwargs):
        raise FileNotFoundError("ssh")
    with pytest.raises(MonitorError) as caught:
        Collector("hellbender", missing).refresh(["queue"])
    assert caught.value.code == "ssh_missing"

def test_receive_limit_is_enforced_while_reading():
    with pytest.raises(MonitorError, match="接收上限"):
        run_bounded([sys.executable, "-c", "import sys; sys.stdout.write('x'*2200000)"], timeout=10)

FAKES = {
    "squeue": "squeue.psv", "sinfo": "sinfo.psv", "sshare": "sshare.psv", "sacct": "sacct.psv", "scontrol": "config.txt"}

@pytest.mark.skipif(not shutil.which("sh") or not shutil.which("timeout"), reason="POSIX sh and timeout are needed")
def test_generated_script_runs_under_a_real_shell(tmp_path):
    """The exact remote script, executed locally against fake Slurm commands; nothing contacts a cluster."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for command, name in FAKES.items():
        data = fixture(name, SEP if name in ("squeue.psv", "sinfo.psv") else None)
        (tmp_path / name).write_text(data, encoding="utf-8", newline="\n")
        body = f'#!/bin/sh\nprintf "%s\\n" "{command} $*" >> "{log.as_posix()}"\n'
        if command == "sshare":
            body += 'echo "sshare: warning: fake stderr" >&2\nexit 3\n'
        else:
            body += f'cat "{(tmp_path / name).as_posix()}"\n'
        path = bin_dir / command
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}", USER="demo")
    shell = shutil.which("sh")
    def runner(args, timeout=95, **kwargs):
        remote = args[-1]
        assert remote.startswith("timeout 80s sh -c ")
        script = shlex.split(remote)[-1]
        done = subprocess.run([shell, "-c", f'PATH="{bin_dir.as_posix()}:$PATH"; ' + script], capture_output=True,
                              timeout=timeout, env=env)
        return SimpleNamespace(returncode=done.returncode, stdout=done.stdout.decode("utf-8", "replace"),
                               stderr=done.stderr.decode("utf-8", "replace"))
    result = Collector("hellbender", runner).refresh(ALL, SINCE)
    sections = result["sections"]
    assert [sections[name]["ok"] for name in ALL] == [True, True, False, True, True]
    assert "fake stderr" in sections["share"]["error"]
    assert sections["config"]["data"]["half_life"] == 7 * 86400
    calls = log.read_text(encoding="utf-8")
    assert "squeue --user=demo --noheader --format=" in calls
    assert "sinfo --Node --noheader --Format=NodeList:0" in calls
    assert f"--starttime={SINCE} --endtime=now" in calls and "scontrol show config" in calls
