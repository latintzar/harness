"""Real CPU processes only: containment, gate ordering, cleanup and receipt binding."""

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest
from stock_commissioning import file_sha
from stock_supervisor import UnsupportedHost, supervise, validate_provider_reference

WORKER = r'''
import ctypes, errno, json, os, signal, sys, time
from pathlib import Path
from stock_commissioning import load, validate_supervisor
mode, receipt, deadline, manifest, proof = sys.argv[1:]
record = load(receipt)
validate_supervisor(record, worker_pid=os.getpid(), worker_pgid=os.getpgrp(),
                   worker_sid=os.getsid(0), parent_pid=os.getppid(), parent_pgid=os.getpgid(os.getppid()),
                   deadline_epoch=float(deadline),
                   manifest_sha256=__import__('hashlib').sha256(Path(manifest).read_bytes()).hexdigest())
child = os.fork()
if child == 0:
    if mode == 'exec':
        code = """import json,os,time,sys
from pathlib import Path
try: os.setsid(); answer='escaped'
except OSError as exc: answer=exc.errno
Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'escape':{'setsid':answer}}))
while True: time.sleep(.1)
"""
        os.execv(sys.executable,[sys.executable,'-c',code,proof+'.child'])
    failures = {}
    if mode == 'escape':
        for name, call in [('setsid',os.setsid),('setpgid',lambda:os.setpgid(0,0))]:
            try: call(); failures[name] = 'escaped'
            except OSError as exc: failures[name] = exc.errno
        libc=ctypes.CDLL(None,use_errno=True)
        for name, number, arg in [('unshare',272,0x10000000),('clone_namespace',56,0x20000000)]:
            value=libc.syscall(number,arg,0,0,0,0)
            failures[name]=ctypes.get_errno() if value == -1 else 'escaped'
        value=libc.syscall(435,0,0)
        failures['clone3']=ctypes.get_errno() if value == -1 else 'escaped'
    Path(proof+'.child').write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'escape':failures}))
    while True: time.sleep(.1)
Path(proof).write_text(json.dumps({'pid':os.getpid(),'child':child,'gate_receipt_exists':True}))
while not Path(proof+'.child').exists(): time.sleep(.005)
if mode == 'log':
    os.write(1,b'x'*200000)
if mode == 'interrupt':
    os.kill(os.getppid(),signal.SIGTERM)
if mode in ('timeout','interrupt'):
    while True: time.sleep(.1)
os._exit(0)
'''


def files(tmp_path, lifetime=4):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"fixture":"CPU only"}')
    deadline = time.time() + lifetime
    receipt = tmp_path / "provider-stop.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "rocoto-provider-stop-attestation-v1",
                "state": "independently_verified_armed",
                "deadline_epoch": deadline,
                "manifest_sha256": file_sha(manifest),
                "verified_at_epoch": time.time(),
                "provider_resource_id": "cpu-fixture-only",
                "independent_guard_reference": "test-fixture/not-a-provider-arm",
            }
        )
    )
    receipt.chmod(0o600)
    worker = tmp_path / "cpu_worker.py"
    worker.write_text(WORKER)
    return manifest, deadline, receipt, worker


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize(
    "mode,status",
    [
        ("normal", "completed"),
        ("escape", "completed"),
        ("exec", "completed"),
        ("timeout", "timeout"),
        ("interrupt", "interrupted"),
        ("log", "completed"),
    ],
)
def test_real_child_grandchild_cleanup_and_inherited_escape_filter(tmp_path, monkeypatch, mode, status):
    manifest, deadline, receipt, worker = files(tmp_path, 0.7 if mode == "timeout" else 4)
    # The fixture imports only the receipt validator, never the model runner.
    import layout
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(map(str, layout.code_directories())))
    proof = tmp_path / "proof.json"
    result = supervise(
        [
            sys.executable,
            str(worker),
            mode,
            "{supervisor_receipt}",
            "{deadline_epoch}",
            str(manifest),
            str(proof),
        ],
        manifest=manifest,
        output=tmp_path / "out",
        deadline_epoch=deadline,
        provider_receipt=receipt,
        provider_sha256=file_sha(receipt),
        max_log_bytes=1024,
    )
    assert result["status"] == status, result
    assert result["group_cleanup_verified"] is True
    assert result["gate_opened"] is True
    pids = json.loads(proof.read_text())
    child = json.loads(Path(str(proof) + ".child").read_text())
    assert child["pgid"] == pids["pid"] and child["pid"] == pids["child"]
    assert not alive(pids["pid"]) and not alive(child["pid"])
    assert result["provider_arm_performed"] is False
    for path in (tmp_path / "out").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "out").stat().st_mode) == 0o700
    if mode == "escape":
        assert child["escape"] == {
            "setsid": 1,
            "setpgid": 1,
            "unshare": 1,
            "clone_namespace": 1,
            "clone3": 38,
        }
    if mode == "exec":
        assert child["escape"] == {"setsid": 1}
    if mode == "log":
        assert result["log_bytes_received"] == 200000 and result["log_truncated"]
        assert (tmp_path / "out/worker.log").stat().st_size == 1024
    if mode == "interrupt":
        assert result["signal_received"] == signal.SIGTERM


@pytest.mark.parametrize("change", ["hash", "deadline", "manifest", "stale", "future", "unarmed", "public"])
def test_stale_and_mismatched_provider_binding_refuses_before_child(tmp_path, change):
    manifest, deadline, receipt, _ = files(tmp_path)
    data = json.loads(receipt.read_text())
    if change == "deadline":
        data["deadline_epoch"] += 1
    if change == "manifest":
        data["manifest_sha256"] = "a" * 64
    if change == "stale":
        data["verified_at_epoch"] -= 3601
    if change == "future":
        data["verified_at_epoch"] += 60
    if change == "unarmed":
        data["state"] = "pending"
    receipt.write_text(json.dumps(data))
    if change == "public":
        receipt.chmod(0o644)
    with pytest.raises(ValueError):
        supervise(
            [sys.executable, "-c", "raise Exception('must never execute')"],
            manifest=manifest,
            output=tmp_path / "out",
            deadline_epoch=deadline,
            provider_receipt=receipt,
            provider_sha256="f" * 64 if change == "hash" else file_sha(receipt),
        )
    assert not (tmp_path / "out").exists()


def test_unsupported_host_refuses_before_gate_or_child(tmp_path, monkeypatch):
    import stock_supervisor

    monkeypatch.setattr(stock_supervisor.platform, "machine", lambda: "aarch64")
    manifest, deadline, receipt, _ = files(tmp_path)
    with pytest.raises(UnsupportedHost):
        supervise(
            [sys.executable, "-c", "raise Exception('must never execute')"],
            manifest=manifest,
            output=tmp_path / "out",
            deadline_epoch=deadline,
            provider_receipt=receipt,
            provider_sha256=file_sha(receipt),
        )
    assert not (tmp_path / "out").exists()


def test_deadline_cannot_be_reused_after_expiry(tmp_path):
    manifest, deadline, receipt, _ = files(tmp_path)
    with pytest.raises(ValueError, match="future bounded"):
        validate_provider_reference(
            receipt,
            file_sha(receipt),
            deadline_epoch=deadline,
            manifest_sha256=file_sha(manifest),
            now=deadline + 1,
        )
