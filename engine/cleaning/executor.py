"""Spawn-isolated execution. Only the supervisor can report a terminal event to Java."""
from __future__ import annotations

import ctypes
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import time
from threading import Event as ThreadEvent, Lock, Thread
from typing import Callable
from uuid import uuid4

from .config import Settings
from .models import CancelledError, InputError, TaskMessage


def _parent_death_signal(parent: int) -> None:
    if os.name == "posix" and Path("/proc").exists():
        # The child must not continue writing artifacts after its supervisor dies.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise RuntimeError("Cannot configure execution parent-death signal")
        if os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGKILL)


def _child(task_json: str, settings: Settings, directory: str, cancel, updates, parent: int) -> None:
    _parent_death_signal(parent)
    from .pipeline import execute_task
    from .storage import ObjectStorage
    task = TaskMessage.model_validate_json(task_json)

    def progress(value: int, message: str) -> None:
        try:
            updates.put_nowait((value, message))
        except queue.Full:
            pass  # Heartbeats and final counters do not depend on progress sampling.

    try:
        report, artifacts = execute_task(task, ObjectStorage(settings), Path(directory), cancel, progress)
        payload = {"status": "SUCCEEDED", "report": report, "artifacts": artifacts}
    except CancelledError:
        payload = {"status": "CANCELED"}
    except Exception as exc:
        code = "RULE_ERROR" if isinstance(exc, InputError) else "EXECUTION_ERROR"
        if isinstance(exc, OSError) and exc.errno == 28:
            code = "SCRATCH_SPACE_EXHAUSTED"
        if isinstance(exc, InputError) and "scratch" in str(exc).lower():
            code = "SCRATCH_LIMIT_EXCEEDED"
        payload = {"status": "FAILED", "errorCode": code,
                   "message": str(exc)[:1000] if isinstance(exc, InputError) else "Execution failed; inspect worker service logs"}
    target = Path(directory) / "execution-terminal.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(target)


def process_rss(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def directory_bytes(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            continue  # SQLite may remove a journal between the directory and stat calls.
    return total


class ExecutionUnit:
    def __init__(self, task: TaskMessage, settings: Settings, directory: Path):
        self.directory = directory
        context = mp.get_context("spawn")
        self.cancel = context.Event()
        self.updates = context.Queue(maxsize=64)
        self.process = context.Process(target=_child, args=(task.model_dump_json(), settings, str(directory), self.cancel, self.updates, os.getpid()), daemon=False)
        self.process.start()
        self.peak_combined_rss = 0
        self.peak_scratch_bytes = 0
        self.scratch_limit = task.limits.get("maxScratchBytes", int(os.environ.get("MAX_SCRATCH_BYTES", "1073741824")))
        self.scratch_exceeded = False
        self._stop_lock = Lock()
        self._next_sample = 0.0

    def done(self) -> bool:
        return not self.process.is_alive()

    def read_progress(self):
        result = []
        while True:
            try:
                result.append(self.updates.get_nowait())
            except queue.Empty:
                return result

    def sample(self) -> None:
        now = time.monotonic()
        if now >= self._next_sample:
            self.peak_combined_rss = max(self.peak_combined_rss, process_rss(os.getpid()) + process_rss(self.process.pid))
            self.peak_scratch_bytes = max(self.peak_scratch_bytes, directory_bytes(self.directory))
            self.scratch_exceeded = self.peak_scratch_bytes > self.scratch_limit
            self._next_sample = now + 0.25

    def request_cancel(self) -> None:
        self.cancel.set()

    def terminate(self) -> None:
        with self._stop_lock:
            self.cancel.set()
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
            if self.process.is_alive():
                raise RuntimeError("EXECUTOR_STOP_UNCONFIRMED")

    def result(self) -> dict:
        self.process.join(timeout=1)
        target = self.directory / "execution-terminal.json"
        if not target.exists():
            return {"status": "FAILED", "errorCode": "EXECUTOR_CRASHED", "message": f"Execution process stopped unexpectedly (exit={self.process.exitcode})"}
        return json.loads(target.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.terminate()
        self.updates.close()
        self.updates.cancel_join_thread()
        self.process.close()


class ExecutionWatchdog:
    """A network stall in the AMQP thread cannot disable the local execution deadline."""
    def __init__(self, unit, timeout: float, grace: float):
        self.unit, self.grace = unit, grace
        self.deadline = time.monotonic() + timeout
        self.stop_reason = None
        self.stop_at = None
        self.failure = None
        self.closed = ThreadEvent()
        self.lock = Lock()
        self.thread = Thread(target=self._run, name="execution-watchdog", daemon=True)
        self.thread.start()

    def request_stop(self, reason: str) -> None:
        with self.lock:
            if self.stop_at is None:
                self.stop_at, self.stop_reason = time.monotonic(), reason
                self.unit.request_cancel()

    def _run(self) -> None:
        try:
            while not self.closed.wait(.05):
                if self.unit.done(): return
                if time.monotonic() >= self.deadline:
                    self.request_stop("TASK_TIMEOUT")
                with self.lock:
                    stop_at = self.stop_at
                if stop_at is not None and time.monotonic() - stop_at >= self.grace:
                    self.unit.terminate()
                    return
        except Exception as exc:
            self.failure = type(exc).__name__

    def close(self) -> None:
        self.closed.set()
        self.thread.join(timeout=4)
        if self.thread.is_alive():
            raise RuntimeError("EXECUTOR_WATCHDOG_STOP_UNCONFIRMED")


class ScratchDirectory:
    """Never accepts a user path. Reaping only examines marked, inactive attempt dirs."""
    def __init__(self, root: str, task: TaskMessage):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{task.taskId}-{task.attemptId or task.attempt}-{uuid4().hex}"
        self.path.mkdir(mode=0o700)
        (self.path / ".attempt.json").write_text(json.dumps({"host": socket.gethostname(), "createdAt": time.time(), "taskId": str(task.taskId), "attemptId": str(task.attemptId or task.attempt)}), encoding="utf-8")

    def cleanup(self) -> None:
        if self.path.parent == self.root and not self.path.is_symlink():
            shutil.rmtree(self.path)

    @staticmethod
    def reap(root: str, retention_seconds: int, active: Path | None = None) -> int:
        path = Path(root).resolve()
        path.mkdir(parents=True, exist_ok=True)
        removed = 0
        for item in path.iterdir():
            if active is not None and item.resolve() == active.resolve():
                continue
            marker = item / ".attempt.json"
            if not item.is_dir() or item.is_symlink() or not marker.is_file():
                continue
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
                if metadata.get("host") != socket.gethostname() or time.time() - metadata["createdAt"] < retention_seconds:
                    continue
                shutil.rmtree(item)
                removed += 1
            except (OSError, ValueError, KeyError):
                continue
        return removed
