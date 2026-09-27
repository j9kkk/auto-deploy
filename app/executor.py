"""Subprocess execution with streaming logs, timeouts and cancellation.

Every command the deploy pipeline runs goes through :func:`run_command`, so
logging, credential redaction, process-group cleanup and cancellation behave
identically for git, scripts and docker.

Processes are started in their own session (``start_new_session=True``) so that
a shell script which spawns children can be killed as a group; otherwise
``Ctrl-C``-equivalent cancellation would leave orphaned grandchildren holding
the deploy directory open.
"""

from __future__ import annotations

import os
import queue as _queue
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# Strings that must never reach a log file: tokens embedded in remote URLs.
_CREDENTIAL_PATTERNS = (
    re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@", re.IGNORECASE),
    re.compile(r"(//)([^/\s:@]+):([^/\s@]+)@"),
    re.compile(r"(Authorization:\s*)(\S+)", re.IGNORECASE),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})"),
    re.compile(r"\b(github_pat_[A-Za-z0-9_]{20,})"),
)


def redact(text: str) -> str:
    """Remove credentials from text destined for a log or an API response."""
    if not text:
        return text
    result = text
    result = _CREDENTIAL_PATTERNS[0].sub(r"\1***:***@", result)
    result = _CREDENTIAL_PATTERNS[1].sub(r"\1***:***@", result)
    result = _CREDENTIAL_PATTERNS[2].sub(r"\1***", result)
    result = _CREDENTIAL_PATTERNS[3].sub("***", result)
    result = _CREDENTIAL_PATTERNS[4].sub("***", result)
    return result


@dataclass
class CommandResult:
    """Outcome of one command execution."""

    command: str
    exit_code: int
    output: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


class CommandCancelled(Exception):
    """Raised internally when a run is cancelled while a command is executing."""


@dataclass
class ProcessHandle:
    """Bookkeeping for a process that may need to be killed."""

    process: subprocess.Popen
    cancelled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def mark_cancelled(self) -> None:
        with self._lock:
            self.cancelled = True

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self.cancelled

    def terminate(self, grace_seconds: int = 10) -> None:
        """SIGTERM the process group, escalating to SIGKILL after a grace period."""
        if self.process.poll() is not None:
            return
        _kill_process_group(self.process, signal.SIGTERM)
        deadline = time.monotonic() + max(1, grace_seconds)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return
            time.sleep(0.1)
        if self.process.poll() is None:
            _kill_process_group(self.process, signal.SIGKILL)


def _kill_process_group(process: subprocess.Popen, sig: int) -> None:
    """Signal the whole process group, falling back to the single process."""
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def run_command(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 600,
    log: Callable[[str], None] | None = None,
    label: str | None = None,
    handle_out: list[ProcessHandle] | None = None,
    register: Callable[[Any, ProcessHandle], None] | None = None,
    check_cancelled: Callable[[], bool] | None = None,
    kill_grace_seconds: int = 10,
) -> CommandResult:
    """Run a command, streaming combined stdout/stderr into ``log``.

    ``log`` is called once per output line (without the trailing newline) and
    once with the exit summary.  ``check_cancelled`` is polled while reading, so
    a cancellation request stops the command promptly even mid-stream.
    """
    if not args or not all(isinstance(a, str) for a in args):
        raise ValueError("args must be a non-empty sequence of strings")

    display = redact(" ".join(_quote_for_display(a) for a in args))
    header = f"$ {display}"
    if label:
        header = f"[{label}] {header}"
    if log:
        log(header)

    resolved_env = os.environ.copy()
    if env:
        resolved_env.update({str(k): str(v) for k, v in env.items()})

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(args),
            cwd=str(cwd) if cwd else None,
            env=resolved_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError:
        message = f"命令不存在: {args[0]}"
        if log:
            log(f"! {message}")
        return CommandResult(display, 127, "", 0, error=message)
    except PermissionError:
        message = f"没有执行权限: {args[0]}"
        if log:
            log(f"! {message}")
        return CommandResult(display, 126, "", 0, error=message)
    except OSError as exc:
        message = f"无法启动命令: {exc}"
        if log:
            log(f"! {message}")
        return CommandResult(display, 1, "", 0, error=message)

    handle = ProcessHandle(process)
    if handle_out is not None:
        handle_out.append(handle)
    if register is not None:
        register(process.pid, handle)

    collected: list[str] = []
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout if timeout and timeout > 0 else None

    # Read on a separate thread and hand lines to this thread through a queue.
    # Reading stdout directly here would block on a command that produces no
    # output (e.g. `sleep 30`), so the timeout and cancellation checks below
    # would never get a chance to run.
    line_queue: "_queue.Queue[str | None]" = _queue.Queue()
    stop_reading = threading.Event()

    def _pump(stream) -> None:
        try:
            for raw_line in stream:
                line_queue.put(redact(raw_line.rstrip("\n").rstrip("\r")))
                if stop_reading.is_set():
                    break
        except (ValueError, OSError):
            pass
        finally:
            line_queue.put(None)

    reader = threading.Thread(target=_pump, args=(process.stdout,), daemon=True)
    reader.start()

    def _drain_pending() -> None:
        """Move whatever the reader already queued into the log."""
        while True:
            try:
                item = line_queue.get_nowait()
            except _queue.Empty:
                return
            if item is None:
                continue
            collected.append(item)
            if log:
                log(item)

    try:
        while True:
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                break
            if handle.is_cancelled:
                cancelled = True
                break
            if check_cancelled is not None and check_cancelled():
                cancelled = True
                handle.mark_cancelled()
                break
            try:
                item = line_queue.get(timeout=0.25)
            except _queue.Empty:
                continue
            if item is None:
                break  # stream closed: the command has finished
            collected.append(item)
            if log:
                log(item)

        if cancelled or timed_out:
            handle.terminate(kill_grace_seconds)
        # Give the reader a moment to flush the tail, then collect the rest.
        _drain_pending()
        if cancelled or timed_out:
            reader.join(timeout=2.0)
            _drain_pending()
        else:
            reader.join(timeout=5.0)
            _drain_pending()
    finally:
        stop_reading.set()
        try:
            process.wait(timeout=max(5, kill_grace_seconds))
        except subprocess.TimeoutExpired:
            _kill_process_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        reader.join(timeout=2.0)
        if handle_out is not None and handle in handle_out:
            handle_out.remove(handle)

    duration_ms = int((time.monotonic() - started) * 1000)
    exit_code = process.returncode if process.returncode is not None else -1

    error = ""
    if cancelled:
        exit_code = exit_code if exit_code not in (0, None) else 143
        error = "任务已被取消"
        if log:
            log("! 任务已取消")
    elif timed_out:
        error = f"命令超时（{timeout} 秒）"
        if log:
            log(f"! {error}")
    elif exit_code != 0:
        error = f"命令退出码 {exit_code}"
        if log:
            log(f"! {error}")
    elif log:
        log(f"✓ 完成，耗时 {duration_ms / 1000:.1f}s")

    return CommandResult(
        command=display,
        exit_code=exit_code,
        output="\n".join(collected),
        duration_ms=duration_ms,
        timed_out=timed_out,
        cancelled=cancelled,
        error=error,
    )


def _quote_for_display(arg: str) -> str:
    if arg == "" or any(ch in arg for ch in " \t\n\"'\\$`*?[]{}()|&;<>#!"):
        return "'" + arg.replace("'", "'\\''") + "'"
    return arg


def which(executable: str) -> str | None:
    return shutil.which(executable)


def command_exists(executable: str) -> bool:
    return shutil.which(executable) is not None


def shell_command(script: str, shell: str = "/bin/bash") -> list[str]:
    """Wrap a script body for execution with ``shell -c``."""
    resolved = shell if os.path.exists(shell) else "/bin/sh"
    return [resolved, "-c", script]
