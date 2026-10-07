"""Linux child subreaper for process commands and ACP stdio transports."""
from __future__ import annotations

import ctypes
import errno
import json
import os
import selectors
import signal
import subprocess
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
_shutdown = False


def _stop(_signum, _frame):
    global _shutdown
    _shutdown = True


def _children():
    try:
        with open(f"/proc/{os.getpid()}/task/{os.getpid()}/children", encoding="ascii") as f:
            return [int(value) for value in f.read().split()]
    except OSError as exc:
        raise RuntimeError("Cannot inspect subreaper children; quiescence is unproven") from exc


def _kill_children():
    for pid in _children():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _relay_stdio(target):
    """Bounded bidirectional byte relay for ACP's unframed stdio transport.

    Each direction has at most 1 MiB buffered. Reads are disabled at the limit,
    so a slow peer applies kernel-pipe backpressure instead of unbounded memory
    growth. A root-output EOF ends the protocol stream even if the host still
    has its stdin open; descendants are reaped only after this function returns.
    """
    assert target.stdin and target.stdout
    selector = selectors.DefaultSelector()
    parent_in, target_in = sys.stdin.fileno(), target.stdin.fileno()
    target_out, parent_out = target.stdout.fileno(), sys.stdout.fileno()
    for fd in (parent_in, target_in, target_out, parent_out):
        os.set_blocking(fd, False)
    pending_to_target = bytearray()
    pending_to_parent = bytearray()
    max_buffer = 1024 * 1024
    parent_eof = output_eof = False
    target_in_open = parent_out_open = True
    interests = {}

    def update(fd, events, label):
        old = interests.get(fd)
        if events == 0:
            if old is not None:
                selector.unregister(fd)
                del interests[fd]
        elif old is None:
            selector.register(fd, events, label)
            interests[fd] = (events, label)
        elif old[0] != events:
            selector.modify(fd, events, label)
            interests[fd] = (events, label)

    def close_target_input():
        nonlocal target_in_open
        if target_in_open:
            update(target_in, 0, "target_in")
            target.stdin.close()
            target_in_open = False

    try:
        while not (output_eof and not pending_to_parent):
            if _shutdown:
                break
            update(parent_in, selectors.EVENT_READ if not parent_eof and target_in_open and len(pending_to_target) < max_buffer else 0, "parent_in")
            update(target_out, selectors.EVENT_READ if not output_eof and len(pending_to_parent) < max_buffer else 0, "target_out")
            update(target_in, selectors.EVENT_WRITE if target_in_open and pending_to_target else 0, "target_in")
            update(parent_out, selectors.EVENT_WRITE if parent_out_open and pending_to_parent else 0, "parent_out")
            for key, _mask in selector.select(.05):
                fd, mode = key.fd, key.data
                try:
                    if mode == "parent_in":
                        data = os.read(fd, min(65536, max_buffer - len(pending_to_target)))
                        if data:
                            pending_to_target.extend(data)
                        else:
                            parent_eof = True
                    elif mode == "target_out":
                        data = os.read(fd, min(65536, max_buffer - len(pending_to_parent)))
                        if data:
                            pending_to_parent.extend(data)
                        else:
                            output_eof = True
                            if pending_to_target:
                                raise RuntimeError("ACP agent closed output before queued input was delivered")
                            close_target_input()
                    elif mode == "target_in" and pending_to_target:
                        n = os.write(fd, pending_to_target[:65536])
                        del pending_to_target[:n]
                    elif mode == "parent_out" and pending_to_parent:
                        n = os.write(fd, pending_to_parent[:65536])
                        del pending_to_parent[:n]
                except BlockingIOError:
                    continue
                except (BrokenPipeError, ConnectionResetError) as exc:
                    raise RuntimeError(f"ACP stdio peer closed during {mode}") from exc
                if parent_eof and not pending_to_target:
                    close_target_input()
        # EOF must reach the host before waiting for detached descendants to
        # exit/reap. Close only after all buffered output has reached the pipe.
        try:
            os.close(parent_out)
        except OSError:
            pass
    finally:
        selector.close()



def _reap_all(root_pid):
    root_status = None
    while True:
        if _shutdown:
            # Direct/adopted children remain waitable until we reap them, so
            # their PIDs cannot be reused between enumeration and termination.
            # A reaped root's process-group ID has no such lifetime guarantee.
            _kill_children()
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return root_status, True
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            return root_status, False
        if pid == 0:
            time.sleep(.01)
            continue
        if pid == root_pid:
            root_status = status


def _write_windows_receipt(receipt_path, nonce, exit_code, cancelled):
    record = {"nonce": nonce, "exit_code": exit_code, "quiescent": True,
              "cancelled": cancelled}
    with open(receipt_path, "x", encoding="ascii") as stream:
        json.dump(record, stream, separators=(",", ":"))
        stream.flush(); os.fsync(stream.fileno())


def _job_runner():
    # Source scripts have no package; frozen dispatch imports this module from PYZ.
    if __package__:
        from process_runtime.windows_job import run_in_job
    else:
        from windows_job import run_in_job
    return run_in_job


def _windows_process_main():
    if len(sys.argv) != 5:
        return 125
    _mode, receipt_path, cancel_path, nonce = sys.argv[1:]
    if len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
        return 125
    try:
        payload = json.loads(sys.stdin.buffer.readline(512 * 1024))
        if not isinstance(payload, dict) or set(payload) != {"argv", "cwd", "env", "stdio"} or payload["stdio"] != "process":
            return 125
        from pathlib import Path
        exit_code, cancelled = _job_runner()(payload, Path(cancel_path))
        _write_windows_receipt(receipt_path, nonce, exit_code, cancelled)
        return 0
    except BaseException:
        return 125


def _windows_acp_main():
    if len(sys.argv) != 6:
        return 125
    _mode, raw_handle, receipt_path, cancel_path, nonce = sys.argv[1:]
    if len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce): return 125
    fd = None
    try:
        import msvcrt
        fd = msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as stream:
            fd = None
            payload = json.loads(stream.readline(512 * 1024))
        if (not isinstance(payload, dict) or set(payload) != {"argv", "cwd", "env", "stdio"}
                or payload["stdio"] != "acp"):
            return 125
        from pathlib import Path
        exit_code, cancelled = _job_runner()(payload, Path(cancel_path))
        _write_windows_receipt(receipt_path, nonce, exit_code, cancelled)
        return 0
    except BaseException:
        return 125
    finally:
        if fd is not None:
            os.close(fd)


def main():
    if os.name == "nt" and len(sys.argv) > 1:
        if sys.argv[1] == "--windows-process": return _windows_process_main()
        if sys.argv[1] == "--windows-acp": return _windows_acp_main()
    target_pid = None
    receipt_fd = None
    try:
        if not sys.platform.startswith("linux") or len(sys.argv) != 4:
            return 125
        config_fd, receipt_fd, nonce = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
        if len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
            return 125
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            return 125
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        with os.fdopen(config_fd, "rb") as stream:
            payload = json.loads(stream.readline(512 * 1024))
        if (not isinstance(payload, dict) or set(payload) != {"argv", "cwd", "env", "stdio"}
                or payload["stdio"] not in {"process", "acp"}):
            return 125
        acp_mode = payload["stdio"] == "acp"
        target = subprocess.Popen(payload["argv"], cwd=payload["cwd"], env=payload["env"],
                                  stdin=subprocess.PIPE if acp_mode else subprocess.DEVNULL,
                                  stdout=subprocess.PIPE if acp_mode else None,
                                  stderr=None, close_fds=True, start_new_session=True)
        target_pid = target.pid
        if acp_mode:
            _relay_stdio(target)
        root_status, complete = _reap_all(target.pid)
        if not complete or root_status is None:
            return 125
        code = (os.WEXITSTATUS(root_status) if os.WIFEXITED(root_status)
                else -os.WTERMSIG(root_status) if os.WIFSIGNALED(root_status) else None)
        if code is None:
            return 125
        record = json.dumps({"nonce": nonce, "exit_code": code, "quiescent": True},
                            separators=(",", ":")).encode("ascii")
        os.write(receipt_fd, record)
        return 0
    except BaseException:
        if target_pid is not None:
            global _shutdown
            _shutdown = True
            _reap_all(target_pid)
        return 125
    finally:
        for fd in (receipt_fd,):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass


if __name__ == "__main__":
    raise SystemExit(main())
