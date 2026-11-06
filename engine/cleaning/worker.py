"""AMQP supervisor with one independently terminable execution process per attempt."""

import json
import logging
import re
from pathlib import Path
import signal
from threading import Event
import time
from typing import Any
from uuid import uuid4

import httpx
import pika
from pydantic import ValidationError

from .config import Settings
from .models import CancelledError, InputError, TaskMessage
from .storage import ObjectStorage
from .executor import ExecutionUnit, ScratchDirectory, ExecutionWatchdog

logger = logging.getLogger("cleaning.worker")
HEARTBEAT_PATH = Path("/tmp/cleaning-worker-heartbeat")


def execute_task(*args, **kwargs):
    # Keep the callable adapter without importing pandas/PyArrow into the supervisor.
    from .pipeline import execute_task as run
    return run(*args, **kwargs)


def _request_id(task: TaskMessage) -> str:
    return task.requestId or str(task.taskId)


def _stage_code(text: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", text):
        return text
    rule = re.search(r"Completed rule (\d+)", text)
    if rule:
        return "RULE_" + rule.group(1)
    for prefix, code in (("Downloading", "DOWNLOAD"), ("Reading", "READ_INPUT"),
                         ("Writing", "WRITE_OUTPUT"), ("Uploading", "UPLOAD"),
                         ("Artifacts", "COMMIT"), ("Cleaning completed", "COMPLETE"),
                         ("Task claimed", "CLAIMED")):
        if text.startswith(prefix): return code
    return "EXECUTING"


def _log_lifecycle(task: TaskMessage, event: str) -> None:
    logger.info(
        "Worker task lifecycle event=%s taskId=%s attemptId=%s requestId=%s",
        event,
        task.taskId,
        task.attemptId or "-",
        _request_id(task),
    )


class JavaClient:
    def __init__(self, settings: Settings):
        self.sequences: dict[str, int] = {}
        self.client = httpx.Client(
            base_url=settings.java_api_url,
            headers={"X-Internal-Token": settings.internal_token},
            timeout=httpx.Timeout(10, connect=5),
        )

    def close(self) -> None:
        self.client.close()

    def claim(self, task: TaskMessage) -> dict[str, Any]:
        response = self.client.post(
            f"/internal/tasks/{task.taskId}/claim",
            headers={"X-Request-ID": _request_id(task)},
            json={
                "attempt": task.attempt,
                **({"attemptId": str(task.attemptId)} if task.attemptId else {}),
            },
        )
        response.raise_for_status()
        return response.json()

    def control(self, task: TaskMessage, lease_token: str) -> bool:
        response = self.client.get(
            f"/internal/tasks/{task.taskId}/control",
            headers={"X-Request-ID": _request_id(task)},
            params={
                "attempt": task.attempt,
                "leaseToken": lease_token,
                **({"attemptId": str(task.attemptId)} if task.attemptId else {}),
            },
        )
        response.raise_for_status()
        return bool(response.json()["cancelled"])

    def event(
        self,
        task: TaskMessage,
        lease_token: str,
        status: str,
        progress: int,
        message: str,
        report: dict | None = None,
        artifacts: dict | None = None,
        *, error_code: str | None = None, stage: str | None = None,
    ) -> None:
        if not hasattr(self, "sequences"):
            self.sequences = {}
        identity = str(task.attemptId or f"{task.taskId}:{task.attempt}")
        sequence = self.sequences.get(identity, 0) + 1
        self.sequences[identity] = sequence
        payload: dict[str, Any] = {
            "attempt": task.attempt,
            "leaseToken": lease_token,
            "status": status,
            "progress": progress,
            "message": message[:2000],
            "stage": _stage_code(stage or message),
        }
        if error_code:
            payload["errorCode"] = error_code
        if task.attemptId:
            payload["attemptId"] = str(task.attemptId)
            payload["eventId"] = str(uuid4())
            payload["eventSeq"] = sequence
        if report is not None:
            payload["report"] = report
        if artifacts is not None:
            payload["artifacts"] = artifacts
        # An ambiguous HTTP response is retried with exactly the same event ID/seq/body.
        for retry in range(3):
            try:
                response = self.client.post(
                    f"/internal/tasks/{task.taskId}/events", json=payload,
                    headers={"X-Request-ID": _request_id(task)},
                )
                response.raise_for_status()
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 or retry == 2:
                    raise
            except httpx.HTTPError:
                if retry == 2:
                    raise
            time.sleep(0.2 * (2 ** retry))


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = ObjectStorage(settings)
        self.java = JavaClient(settings)
        self.stopping = Event()
        self.connection: pika.BlockingConnection | None = None

    def stop(self, *_args) -> None:
        self.stopping.set()

    def _touch_liveness(self) -> None:
        # Called only by the AMQP main loop. A blocked main loop cannot refresh it.
        HEARTBEAT_PATH.touch()
        if time.monotonic() >= getattr(self, "_next_scratch_reap", 0):
            ScratchDirectory.reap(self.settings.scratch_root, getattr(self.settings, "scratch_retention_seconds", 3600), getattr(self, "_active_directory", None))
            self._next_scratch_reap = time.monotonic() + 60

    def _pump_amqp(self, time_limit: float) -> None:
        self.connection.process_data_events(time_limit=time_limit)
        self._touch_liveness()

    def _delay_with_heartbeats(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stopping.is_set():
            if self.connection and self.connection.is_open:
                self._pump_amqp(min(0.5, max(0, deadline - time.monotonic())))
            else:
                self.stopping.wait(min(0.5, max(0, deadline - time.monotonic())))

    def _start_execution(self, task: TaskMessage, directory: Path) -> ExecutionUnit:
        return ExecutionUnit(task, self.settings, directory)

    def _discard_artifacts(self, task: TaskMessage) -> None:
        # Exact keys only, never a prefix deletion. A failed delete is left for audited cleanup.
        keys = [f"results/{task.taskId}/{task.attempt}/result.{task.outputFormat}",
                f"dirty/{task.taskId}/{task.attempt}/dirty.csv",
                f"reports/{task.taskId}/{task.attempt}/report.json"]
        for key in keys:
            try:
                self.storage.remove(key)
            except Exception:
                logger.warning("Partial artifact cleanup deferred taskId=%s attemptId=%s", task.taskId, task.attemptId)

    def _process(self, channel, method, properties, body: bytes) -> None:
        if self.stopping.is_set():
            channel.basic_nack(method.delivery_tag, requeue=True)
            return
        if len(body) > 128 * 1024:
            logger.error("Rejecting oversized task message")
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        try:
            task = TaskMessage.model_validate_json(body)
        except (ValidationError, ValueError) as exc:
            # Poison messages go to Java-declared cleaning.dead, never infinite retries.
            logger.error("Rejecting invalid task message: %s", type(exc).__name__)
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        _log_lifecycle(task, "received")
        try:
            claim = self.java.claim(task)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.error("Rejecting task that does not exist: %s", task.taskId)
                channel.basic_nack(method.delivery_tag, requeue=False)
            else:
                # A live lease is a transient conflict: allow it to finish before redelivery.
                self._delay_with_heartbeats(10)
                channel.basic_nack(method.delivery_tag, requeue=True)
            return
        except httpx.HTTPError:
            logger.exception("Java unavailable while claiming %s", task.taskId)
            self._delay_with_heartbeats(5)
            channel.basic_nack(method.delivery_tag, requeue=True)
            return
        if not claim.get("execute"):
            _log_lifecycle(task, "skipped")
            channel.basic_ack(method.delivery_tag)
            return
        lease_token = claim.get("leaseToken")
        if not isinstance(lease_token, str) or not lease_token:
            logger.error("Claim did not return a lease token")
            channel.basic_nack(method.delivery_tag, requeue=True)
            return
        _log_lifecycle(task, "claimed")

        progress, latest_message = 1, "Task claimed by worker"
        communication_failed = False
        scratch = ScratchDirectory(self.settings.scratch_root, task)
        self._active_directory = scratch.path
        unit = None
        watchdog = None
        try:
            unit = self._start_execution(task, scratch.path)
            started = time.monotonic()
            timeout = float(claim.get("executionTimeoutSeconds", task.limits.get("executionTimeoutSeconds", self.settings.execution_timeout_seconds)))
            grace = float(claim.get("cancelGraceSeconds", task.limits.get("cancelGraceSeconds", self.settings.cancel_grace_seconds)))
            watchdog = ExecutionWatchdog(unit, timeout, grace)
            stop_reason, stop_requested = None, None
            next_control, next_heartbeat = 0.0, 0.0
            while not unit.done():
                self._pump_amqp(0.2)
                unit.sample()
                changed = False
                for value, message in unit.read_progress():
                    progress, latest_message, changed = max(progress, value), message, True
                now = time.monotonic()
                if getattr(unit, "scratch_exceeded", False) and stop_reason is None:
                    stop_reason, stop_requested = "SCRATCH_LIMIT_EXCEEDED", now
                if self.stopping.is_set() and stop_reason is None:
                    stop_reason, stop_requested = "WORKER_STOPPING", now
                if now - started >= timeout and stop_reason is None:
                    stop_reason, stop_requested = "TASK_TIMEOUT", now
                if not communication_failed:
                    try:
                        if now >= next_control:
                            # A committed user cancellation wins over timeout reporting.
                            if self.java.control(task, lease_token):
                                stop_reason = "USER_CANCELED"
                                stop_requested = stop_requested or now
                            next_control = now + 2
                        if changed or now >= next_heartbeat:
                            self.java.event(task, lease_token, "RUNNING", progress, latest_message)
                            next_heartbeat = now + 10
                    except httpx.HTTPError:
                        communication_failed = True
                        stop_reason, stop_requested = "CONTROL_UNAVAILABLE", stop_requested or now
                if stop_reason:
                    watchdog.request_stop(stop_reason)
                    if now - stop_requested >= grace:
                        unit.terminate()
                        break
            if communication_failed or self.stopping.is_set():
                self._discard_artifacts(task)
                _log_lifecycle(task, "requeued")
                channel.basic_nack(method.delivery_tag, requeue=True)
                return
            payload = unit.result()
            if self.java.control(task, lease_token) or stop_reason == "USER_CANCELED":
                self._discard_artifacts(task)
                self.java.event(task, lease_token, "CANCELED", progress, "Task cancelled; execution process stopped", error_code="USER_CANCELED", stage=latest_message)
                _log_lifecycle(task, "canceled")
            elif stop_reason in {"TASK_TIMEOUT", "SCRATCH_LIMIT_EXCEEDED"} or time.monotonic() - started >= timeout:
                self._discard_artifacts(task)
                code="SCRATCH_LIMIT_EXCEEDED" if stop_reason=="SCRATCH_LIMIT_EXCEEDED" else "TASK_TIMEOUT"
                detail="Attempt scratch budget exceeded" if code=="SCRATCH_LIMIT_EXCEEDED" else "Execution time limit exceeded"
                self.java.event(task, lease_token, "FAILED", progress, detail+"; execution process stopped", error_code=code, stage=latest_message)
                _log_lifecycle(task, "timed-out")
            elif payload["status"] == "SUCCEEDED":
                self.java.event(task, lease_token, "SUCCEEDED", 100, "Cleaning completed", payload["report"], payload["artifacts"])
                _log_lifecycle(task, "succeeded")
            else:
                self._discard_artifacts(task)
                self.java.event(task, lease_token, "FAILED", progress, payload.get("message", "Execution stopped"), error_code=payload.get("errorCode", "EXECUTION_ERROR"), stage=latest_message)
                _log_lifecycle(task, "failed")
            channel.basic_ack(method.delivery_tag)
        except httpx.HTTPStatusError as exc:
            # Terminal conflict or stale lease means another attempt owns the task.
            if exc.response.status_code in {404, 409, 410}:
                _log_lifecycle(task, "stale-callback")
                channel.basic_ack(method.delivery_tag)
            else:
                self._delay_with_heartbeats(5)
                channel.basic_nack(method.delivery_tag, requeue=True)
        except httpx.HTTPError:
            logger.exception(
                "Terminal callback not accepted for %s; delivery retained", task.taskId
            )
            self._delay_with_heartbeats(5)
            _log_lifecycle(task, "requeued")
            channel.basic_nack(method.delivery_tag, requeue=True)
        finally:
            if watchdog is not None:
                watchdog.close()
            if unit is not None:
                unit.close()
            scratch.cleanup()
            self._active_directory = None
            if hasattr(self.java, "sequences"):
                self.java.sequences.pop(str(task.attemptId or f"{task.taskId}:{task.attempt}"), None)

    def run(self) -> None:
        ScratchDirectory.reap(self.settings.scratch_root, self.settings.scratch_retention_seconds)
        HEARTBEAT_PATH.unlink(missing_ok=True)
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        while not self.stopping.is_set():
            try:
                self.connection = pika.BlockingConnection(
                    pika.ConnectionParameters(
                        host=self.settings.rabbitmq_host,
                        port=self.settings.rabbitmq_port,
                        credentials=pika.PlainCredentials(
                            self.settings.rabbitmq_user, self.settings.rabbitmq_password
                        ),
                        heartbeat=60,
                        blocked_connection_timeout=60,
                        connection_attempts=3,
                        retry_delay=2,
                        socket_timeout=10,
                        stack_timeout=30,
                    )
                )
                channel = self.connection.channel()
                # Topology must match the backend's durable declarations.
                channel.exchange_declare(
                    exchange="cleaning", exchange_type="direct", durable=True
                )
                channel.exchange_declare(
                    exchange="cleaning.dlx", exchange_type="direct", durable=True
                )
                channel.queue_declare(queue="cleaning.dead", durable=True)
                channel.queue_bind(
                    queue="cleaning.dead", exchange="cleaning.dlx", routing_key="dead"
                )
                channel.queue_declare(
                    queue=self.settings.worker_queue,
                    durable=True,
                    arguments={
                        "x-dead-letter-exchange": "cleaning.dlx",
                        "x-dead-letter-routing-key": "dead",
                    },
                )
                channel.queue_bind(
                    queue=self.settings.worker_queue,
                    exchange="cleaning",
                    routing_key="task",
                )
                channel.basic_qos(prefetch_count=1)
                channel.basic_consume(
                    queue=self.settings.worker_queue, on_message_callback=self._process
                )
                logger.info("Worker ready; queue=%s", self.settings.worker_queue)
                while not self.stopping.is_set() and self.connection.is_open:
                    self._pump_amqp(1)
            except (pika.exceptions.AMQPError, OSError):
                logger.exception("AMQP connection interrupted; reconnecting")
                self.stopping.wait(5)
            finally:
                if self.connection and self.connection.is_open:
                    self.connection.close()
                self.connection = None
                HEARTBEAT_PATH.unlink(missing_ok=True)
        self.java.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    Worker(Settings.from_env()).run()


if __name__ == "__main__":
    main()
