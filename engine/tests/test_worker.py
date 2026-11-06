from types import SimpleNamespace
from threading import Event
import time
from uuid import uuid4

import httpx
import pytest

from cleaning.worker import Worker
from cleaning.worker import JavaClient
from cleaning.models import TaskMessage


class LocalExecution:
    """Adapter isolation for Worker protocol tests; real processes are tested separately."""
    def __init__(self, worker, task, directory):
        import threading
        self.cancel = Event()
        self.updates = []
        self.payload = None
        def run():
            from cleaning.worker import execute_task
            from cleaning.models import CancelledError
            try:
                report, artifacts = execute_task(task, worker.storage, directory, self.cancel, lambda n,m:self.updates.append((n,m)))
                self.payload={"status":"SUCCEEDED","report":report,"artifacts":artifacts}
            except CancelledError:
                self.payload={"status":"CANCELED"}
            except Exception as exc:
                self.payload={"status":"FAILED","errorCode":"RULE_ERROR","message":str(exc)}
        self.thread=threading.Thread(target=run)
        self.thread.start()
    def done(self):return not self.thread.is_alive()
    def sample(self):pass
    def read_progress(self):
        result=self.updates[:];self.updates.clear();return result
    def request_cancel(self):self.cancel.set()
    def terminate(self):self.cancel.set();self.thread.join(timeout=1)
    def result(self):self.thread.join(timeout=1);return self.payload
    def close(self):self.terminate()


@pytest.fixture(autouse=True)
def heartbeat_file(tmp_path, monkeypatch):
    target = tmp_path / "worker-heartbeat"
    monkeypatch.setattr("cleaning.worker.HEARTBEAT_PATH", target)
    return target


class Channel:
    def __init__(self, events):
        self.events = events

    def basic_ack(self, tag):
        self.events.append(("ack", tag))

    def basic_nack(self, tag, requeue):
        self.events.append(("nack", requeue))


class Connection:
    def process_data_events(self, time_limit=0):
        time.sleep(0.001)


class Java:
    def __init__(self, events, execute=True, terminal_error=False):
        self.events = events
        self.execute = execute
        self.terminal_error = terminal_error

    def claim(self, task):
        return {"execute": self.execute, "leaseToken": "lease"}

    def control(self, task, lease):
        return False

    def event(
        self, task, lease, status, progress, message, report=None, artifacts=None, **metadata
    ):
        if self.terminal_error and status != "RUNNING":
            response = httpx.Response(
                503, request=httpx.Request("POST", "http://java/internal")
            )
            response.raise_for_status()
        self.events.append(("event", status))


def make_worker(events):
    import tempfile
    worker = Worker.__new__(Worker)
    worker.settings=SimpleNamespace(scratch_root=tempfile.mkdtemp(prefix="worker-test-"),execution_timeout_seconds=60,cancel_grace_seconds=1)
    worker.stopping = Event()
    worker.connection = Connection()
    worker.storage = object()
    worker.java = Java(events)
    worker._delay_with_heartbeats = lambda seconds: None
    worker._start_execution=lambda task,directory:LocalExecution(worker,task,directory)
    return worker


def body():
    import json

    return json.dumps(
        {
            "taskId": str(uuid4()),
            "attempt": 1,
            "input": {
                "objectKey": "raw/test/data.csv",
                "format": "csv",
                "name": "data.csv",
            },
            "rules": [],
            "outputFormat": "csv",
        }
    ).encode()


def test_ack_only_after_java_accepts_terminal_callback(monkeypatch):
    events = []
    worker = make_worker(events)
    monkeypatch.setattr(
        "cleaning.worker.execute_task",
        lambda *args: ({"inputRows": 0}, {"result": "result"}),
    )
    worker._process(Channel(events), SimpleNamespace(delivery_tag=1), None, body())
    assert events[-2:] == [("event", "SUCCEEDED"), ("ack", 1)]


def test_missing_terminal_callback_keeps_message_for_redelivery(monkeypatch):
    events = []
    worker = make_worker(events)
    worker.java = Java(events, terminal_error=True)
    monkeypatch.setattr("cleaning.worker.execute_task", lambda *args: ({}, {}))
    worker._process(Channel(events), SimpleNamespace(delivery_tag=1), None, body())
    assert events[-1] == ("nack", True)
    assert not any(kind == "ack" for kind, value in events)


def test_stale_or_terminal_duplicate_ack_without_execution(monkeypatch):
    events = []
    worker = make_worker(events)
    worker.java = Java(events, execute=False)
    monkeypatch.setattr(
        "cleaning.worker.execute_task",
        lambda *args: pytest.fail("stale delivery must not execute"),
    )
    worker._process(Channel(events), SimpleNamespace(delivery_tag=1), None, body())
    assert events == [("ack", 1)]


def test_live_lease_conflict_requeues_and_poison_message_dead_letters():
    events = []
    worker = make_worker(events)

    def conflict(task):
        httpx.Response(
            409, request=httpx.Request("POST", "http://java/claim")
        ).raise_for_status()

    worker.java.claim = conflict
    channel = Channel(events)
    worker._process(channel, SimpleNamespace(delivery_tag=1), None, body())
    assert events[-1] == ("nack", True)
    worker._process(channel, SimpleNamespace(delivery_tag=2), None, b"not-json")
    assert events[-1] == ("nack", False)


def test_identifiable_task_with_python_invalid_regex_is_failed_and_acknowledged():
    import json

    events = []
    worker = make_worker(events)
    payload = json.loads(body())
    payload["rules"] = [{"type": "regex", "column": "x", "pattern": r"\e"}]
    worker._process(
        Channel(events),
        SimpleNamespace(delivery_tag=1),
        None,
        json.dumps(payload).encode(),
    )
    assert events[-2:] == [("event", "FAILED"), ("ack", 1)]
    assert not any(kind == "nack" for kind, value in events)


@pytest.mark.parametrize("request_id", [None, str(uuid4())])
def test_java_callbacks_include_uuid_attempt_and_request_identity_for_v3(request_id):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"execute": True, "cancelled": False}

    class Client:
        def __init__(self):
            self.calls = []

        def post(self, path, **kwargs):
            self.calls.append(("post", path, kwargs))
            return Response()

        def get(self, path, **kwargs):
            self.calls.append(("get", path, kwargs))
            return Response()

    attempt_id = uuid4()
    task = TaskMessage.model_validate(
        {
            "taskId": str(uuid4()),
            "attempt": 4,
            "attemptId": str(attempt_id),
            "engineVersion": "v3-sequential",
            "requestId": request_id,
            "input": {
                "objectKey": "raw/test/data.csv",
                "format": "csv",
                "name": "data.csv",
            },
            "rules": [],
            "outputFormat": "csv",
        }
    )
    java = JavaClient.__new__(JavaClient)
    java.client = Client()

    java.claim(task)
    java.control(task, "lease")
    java.event(task, "lease", "RUNNING", 1, "working")

    assert java.client.calls[0][2]["json"] == {
        "attempt": 4, "attemptId": str(attempt_id)
    }
    assert java.client.calls[1][2]["params"] == {
        "attempt": 4, "attemptId": str(attempt_id), "leaseToken": "lease"
    }
    assert java.client.calls[2][2]["json"]["attemptId"] == str(attempt_id)
    assert all(
        call[2]["headers"] == {"X-Request-ID": request_id or str(task.taskId)}
        for call in java.client.calls
    )


def test_active_task_heartbeat_is_written_by_amqp_main_loop_and_lifecycle_logs_are_safe(
    monkeypatch, heartbeat_file, caplog
):
    import json
    import logging
    from threading import get_ident

    main_thread = get_ident()
    main_loop_seen = Event()
    events = []
    worker = make_worker(events)
    pump_threads = []

    def process_data_events(time_limit):
        pump_threads.append(get_ident())
        main_loop_seen.set()

    worker.connection.process_data_events = process_data_events

    def execute(*_args):
        assert main_loop_seen.wait(1)
        return {}, {}

    monkeypatch.setattr("cleaning.worker.execute_task", execute)
    payload = json.loads(body())
    payload.update({
        "engineVersion": "v3-sequential",
        "attemptId": str(uuid4()),
        "requestId": str(uuid4()),
        "rules": [{"type": "fill_null", "columns": ["f0"], "value": "private-row-value"}],
    })
    caplog.set_level(logging.INFO, logger="cleaning.worker")

    worker._process(
        Channel(events), SimpleNamespace(delivery_tag=1), None, json.dumps(payload).encode()
    )

    assert heartbeat_file.is_file()
    assert pump_threads and all(thread == main_thread for thread in pump_threads)
    lifecycle = [record.getMessage() for record in caplog.records if "Worker task lifecycle" in record.getMessage()]
    assert len(lifecycle) == 3
    assert all(f"taskId={payload['taskId']}" in line for line in lifecycle)
    assert all(f"attemptId={payload['attemptId']}" in line for line in lifecycle)
    assert all(f"requestId={payload['requestId']}" in line for line in lifecycle)
    assert all("private-row-value" not in line for line in lifecycle)
    assert events[-2:] == [("event", "SUCCEEDED"), ("ack", 1)]


def test_failed_amqp_pump_cannot_refresh_heartbeat(heartbeat_file):
    worker = make_worker([])

    def broken_pump(time_limit):
        raise OSError("connection interrupted")

    worker.connection.process_data_events = broken_pump
    with pytest.raises(OSError):
        worker._pump_amqp(1)
    assert not heartbeat_file.exists()


@pytest.mark.parametrize("request_id", ["x" * 129, "unsafe\nheader", "中文", "has space"])
def test_task_message_rejects_unsafe_request_correlation(request_id):
    import json
    payload = json.loads(body()) | {"requestId": request_id}
    with pytest.raises(ValueError):
        TaskMessage.model_validate(payload)
