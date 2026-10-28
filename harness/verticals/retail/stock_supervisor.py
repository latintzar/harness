"""One-shot Linux x86_64 supervisor for the pinned stock commissioning worker.

The inherited seccomp filter enforces group membership. It is not a general
sandbox: networking/files remain the trusted worker's host authority. A prior,
independently verified provider-stop receipt is required; this code never arms
or verifies a provider watchdog. SIGKILL/loss of this supervisor still requires
that independent provider guard. CPU tests do not prove model compatibility.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import math
import os
import platform
import selectors
import signal
import stat
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from stock_commissioning import PLUGIN_SHA, SOURCE_COMMIT, file_sha, save

MAX_LOG_BYTES = 8 * 1024**2


class UnsupportedHost(RuntimeError):
    pass


def _prctl(option, arg=0):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(option, arg, 0, 0, 0):
        raise UnsupportedHost("Required Linux process controls are unavailable")


def install_group_filter():
    """Called only inside the isolated child, before the worker can fork."""
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise UnsupportedHost("Commissioning containment requires Linux x86_64")

    class Filter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint32),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]

    deny = 0x00050000 | errno.EPERM
    unsupported = 0x00050000 | errno.ENOSYS
    # Reject other ABIs, including x32, so syscall numbering cannot bypass this filter.
    ops = [
        (0x20, 0, 0, 4),
        (0x15, 1, 0, 0xC000003E),
        (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),
        (0x35, 0, 1, 0x40000000),
        (0x06, 0, 0, unsupported),
    ]
    # setpgid, setsid, unshare, setns. Descendants inherit this across fork/exec.
    for number in (109, 112, 272, 308):
        ops.extend([(0x15, 0, 1, number), (0x06, 0, 0, deny)])
    # clone3 has indirect flags: fail ENOSYS so ordinary libc clone fallback works.
    ops.extend([(0x15, 0, 1, 435), (0x06, 0, 0, unsupported)])
    # clone may create threads/processes, but may not create any new namespace.
    ops.extend(
        [
            (0x15, 0, 3, 56),
            (0x20, 0, 0, 16),
            (0x45, 0, 1, 0x7E020000),
            (0x06, 0, 0, deny),
            (0x06, 0, 0, 0x7FFF0000),
        ]
    )
    filters = (Filter * len(ops))(*(Filter(*op) for op in ops))
    program = Program(len(ops), filters)
    _prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0):  # PR_SET_SECCOMP / FILTER
        raise UnsupportedHost("Kernel refused inherited commissioning containment")


def _private_json(path, expected_sha):
    path = Path(path).absolute()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > 65536
            or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o077
        ):
            raise ValueError("Use a private bounded host-verified provider-stop receipt")
        body = os.read(fd, 65537)
    finally:
        os.close(fd)
    if len(body) > 65536 or hashlib.sha256(body).hexdigest() != expected_sha:
        raise ValueError("Provider-stop receipt differs from its commissioned hash")
    return path, json.loads(body)


def validate_provider_reference(path, expected_sha, *, deadline_epoch, manifest_sha256, now=None):
    """Validate a prior host attestation's binding, not the remote provider state."""
    now = time.time() if now is None else now
    path, value = _private_json(path, expected_sha)
    if (
        not isinstance(value, dict)
        or value.get("schema") != "rocoto-provider-stop-attestation-v1"
        or value.get("state") != "independently_verified_armed"
        or value.get("deadline_epoch") != deadline_epoch
        or value.get("manifest_sha256") != manifest_sha256
    ):
        raise ValueError("Provider-stop attestation must bind this manifest and fixed deadline")
    armed = value.get("verified_at_epoch")
    if (
        type(armed) not in (int, float)
        or not math.isfinite(armed)
        or not 0 <= now - armed <= 3600
        or type(deadline_epoch) not in (int, float)
        or not math.isfinite(deadline_epoch)
        or not 0 < deadline_epoch - now <= 3300
    ):
        raise ValueError("Use a fresh prior stop attestation and a future bounded deadline")
    for key in ("provider_resource_id", "independent_guard_reference"):
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 512:
            raise ValueError("Stop attestation requires its resource and independent guard reference")
    return {
        "reference": str(path),
        "sha256": expected_sha,
        "provider_resource_id": value["provider_resource_id"],
        "independent_guard_reference": value["independent_guard_reference"],
    }


def _group_signal(pid, signum):
    with suppress(ProcessLookupError):
        os.killpg(pid, signum)


def _group_present(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _gated_worker(gate, ready, parent_pid, command):
    # Popen created this session before Python started; exec preserves this direct child PID.
    if os.getpid() != os.getpgrp() or os.getsid(0) != os.getpid():
        raise UnsupportedHost("Worker requires its own session and process group")
    _prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG; parent loss before exec fails closed for the leader.
    if os.getppid() != parent_pid:
        raise UnsupportedHost("Commissioning supervisor exited before the gate")
    install_group_filter()
    os.write(ready, b"R")
    os.close(ready)
    granted = os.read(gate, 1)
    os.close(gate)
    if granted != b"G" or os.getppid() != parent_pid:
        raise UnsupportedHost("Commissioning gate was not granted")
    os.execv(command[0], command)


def supervise(
    command,
    *,
    manifest,
    output,
    deadline_epoch,
    provider_receipt,
    provider_sha256,
    max_log_bytes=MAX_LOG_BYTES,
):
    """Host-only launch primitive; CLI below pins command to the stock runner."""
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise UnsupportedHost("Commissioning containment requires Linux x86_64")
    if type(max_log_bytes) is not int or not 1024 <= max_log_bytes <= MAX_LOG_BYTES:
        raise ValueError("Set a bounded private log cap")
    if not Path(manifest).is_file() or Path(manifest).stat().st_size > 1024**2:
        raise ValueError("Use a bounded host-selected commissioning manifest")
    manifest_sha = file_sha(manifest)
    provider = validate_provider_reference(
        provider_receipt, provider_sha256, deadline_epoch=deadline_epoch, manifest_sha256=manifest_sha
    )
    root = Path(output).resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    receipt_path = root / "supervisor.json"
    command = [
        str(receipt_path)
        if v == "{supervisor_receipt}"
        else str(deadline_epoch)
        if v == "{deadline_epoch}"
        else str(v)
        for v in command
    ]
    if (
        not command
        or not Path(command[0]).is_absolute()
        or len(command) > 128
        or sum(len(arg) for arg in command) > 16384
    ):
        raise ValueError("Use the explicit bounded host-selected executable")
    previous = ctypes.c_int()
    _prctl(37, ctypes.addressof(previous))  # PR_GET_CHILD_SUBREAPER
    _prctl(36, 1)
    handlers, interrupted, child, status = {}, [None], None, "launch_rejected"
    gate_r, gate_w = os.pipe()
    ready_r, ready_w = os.pipe()
    total, kept, ready, cleanup = 0, 0, False, False
    started = time.time()
    monotonic_deadline = time.monotonic() + deadline_epoch - started
    returncode, error = None, None
    selector = selectors.DefaultSelector()
    logfile = os.open(root / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, lambda signum, frame: interrupted.__setitem__(0, signum))
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--gated-worker",
                str(gate_r),
                str(ready_w),
                str(os.getpid()),
                "--",
                *command,
            ],
            start_new_session=True,
            pass_fds=(gate_r, ready_w),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        os.close(gate_r)
        gate_r = -1
        os.close(ready_w)
        ready_w = -1
        os.set_blocking(child.stdout.fileno(), False)
        os.set_blocking(ready_r, False)
        selector.register(child.stdout, selectors.EVENT_READ, "log")
        selector.register(ready_r, selectors.EVENT_READ, "ready")
        while True:
            if interrupted[0] is not None:
                status = "interrupted"
                _group_signal(child.pid, interrupted[0])
                break
            if time.time() >= deadline_epoch or time.monotonic() >= monotonic_deadline:
                status = "timeout"
                break
            dead = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if dead is not None:
                status = (
                    "completed"
                    if ready and dead.si_code == os.CLD_EXITED and dead.si_status == 0
                    else "worker_failed"
                )
                break
            if not ready and time.time() - started > 5:
                raise UnsupportedHost("Worker containment handshake exceeded its bound")
            for key, _ in selector.select(0.025):
                if key.data == "ready":
                    value = os.read(ready_r, 1)
                    selector.unregister(ready_r)
                    if value != b"R":
                        raise UnsupportedHost("Worker failed to install inherited containment")
                    record = {
                        "schema": "rocoto-isolated-worker-supervisor-v1",
                        "worker_pid": child.pid,
                        "worker_pgid": child.pid,
                        "supervisor_pid": os.getpid(),
                        "deadline_epoch": deadline_epoch,
                        "manifest_sha256": manifest_sha,
                        "cleanup": "kill_process_group_after_exit_or_deadline",
                        "descendants_remain_in_group": True,
                        "containment": "linux-x86_64-inherited-seccomp-group-v1",
                        "provider_shutdown_receipt_sha256": provider_sha256,
                        "provider_shutdown_receipt_reference": provider["reference"],
                        "prior_host_provider_verification": provider,
                        "source_commit": SOURCE_COMMIT,
                        "plugin_sha256": PLUGIN_SHA,
                        "supervisor_sha256": file_sha(__file__),
                        "model_compatibility_tested": False,
                    }
                    save(receipt_path, record)
                    os.write(gate_w, b"G")  # Only after the immutable receipt and directory are fsynced.
                    os.close(gate_w)
                    gate_w = -1
                    ready = True
                else:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    total += len(chunk)
                    portion = chunk[: max(0, max_log_bytes - kept)]
                    os.write(logfile, portion)
                    kept += len(portion)
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)[:400]
    finally:
        if child is not None:
            # Hold the leader as a zombie until this kill, preventing PID/group reuse.
            try:
                _group_signal(child.pid, signal.SIGKILL)
            except OSError as exc:
                error = "Group cleanup signal failed: " + type(exc).__name__
            try:
                returncode = child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                error = "Leader remained present after the cleanup wait"
            until = time.monotonic() + 3
            while time.monotonic() < until:
                try:
                    while True:
                        reaped, wait_status = os.waitpid(-child.pid, os.WNOHANG)
                        if not reaped:
                            break
                        if reaped == child.pid:
                            returncode = os.waitstatus_to_exitcode(wait_status)
                            child.returncode = returncode
                except ChildProcessError:
                    pass
                if not _group_present(child.pid):
                    cleanup = True
                    break
                time.sleep(0.01)
            if not cleanup:
                status = "cleanup_failed"
            # Drain only already-available final output; byte cap remains enforced.
            while True:
                try:
                    chunk = os.read(child.stdout.fileno(), 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                total += len(chunk)
                portion = chunk[: max(0, max_log_bytes - kept)]
                os.write(logfile, portion)
                kept += len(portion)
            child.stdout.close()
        for fd in (gate_r, gate_w, ready_r, ready_w):
            if fd >= 0:
                os.close(fd)
        selector.close()
        os.fsync(logfile)
        os.close(logfile)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        _prctl(36, previous.value)
        result = {
            "schema": "rocoto-stock-supervisor-result-v1",
            "status": status,
            "worker_pid": child.pid if child else None,
            "worker_returncode": returncode,
            "deadline_epoch": deadline_epoch,
            "started_at_epoch": started,
            "finished_at_epoch": time.time(),
            "gate_opened": ready,
            "group_cleanup_verified": cleanup,
            "signal_received": interrupted[0],
            "log_bytes_received": total,
            "log_bytes_retained": kept,
            "log_truncated": total > kept,
            "error": error,
            "provider_shutdown_receipt_sha256": provider_sha256,
            "provider_arm_performed": False,
            "model_compatibility_tested": False,
        }
        save(root / "result.json", result)
    return result


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--gated-worker":
        _gated_worker(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[6:])
        return
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--base-config", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--supervisor-output", type=Path, required=True)
    p.add_argument("--provider-stop-receipt", type=Path, required=True)
    p.add_argument("--provider-stop-sha256", required=True)
    p.add_argument("--deadline-epoch", type=float, required=True)
    p.add_argument("--plugin-dir", type=Path, required=True)
    p.add_argument("--segment-length-sec", type=int, choices=range(1, 11), required=True)
    p.add_argument("--recipe", choices=["reasoning-unseeded-stock"], required=True)
    p.add_argument("--execute", action="store_true", required=True)
    args = p.parse_args()
    if not 120 < args.deadline_epoch - time.time() <= 3300:
        p.error("Use the authorized fixed deadline with preservation headroom")
    command = [
        sys.executable,
        str(Path(__file__).with_name("run_stock_commissioning.py")),
        "--manifest",
        str(args.manifest.resolve()),
        "--base-config",
        str(args.base_config.resolve()),
        "--output",
        str(args.output.resolve()),
        "--plugin-dir",
        str(args.plugin_dir.resolve()),
        "--segment-length-sec",
        str(args.segment_length_sec),
        "--recipe",
        args.recipe,
        "--deadline-epoch",
        "{deadline_epoch}",
        "--supervisor-receipt",
        "{supervisor_receipt}",
        "--execute",
    ]
    result = supervise(
        command,
        manifest=args.manifest,
        output=args.supervisor_output,
        deadline_epoch=args.deadline_epoch,
        provider_receipt=args.provider_stop_receipt,
        provider_sha256=args.provider_stop_sha256,
    )
    raise SystemExit(0 if result["status"] == "completed" and result["group_cleanup_verified"] else 1)


if __name__ == "__main__":
    main()
