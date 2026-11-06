import json
import os
from pathlib import Path
import socket
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cleaning.executor import ExecutionUnit, ScratchDirectory
from cleaning.models import TaskMessage


def forever(task, settings, directory, cancel, updates, parent):
    # Deliberately ignores cancellation like a hung native parser.
    (Path(directory)/"started").write_text(str(os.getpid()))
    while True:
        time.sleep(.1)


def message():
    return TaskMessage.model_validate({"taskId":str(uuid4()),"attempt":1,"attemptId":str(uuid4()),"engineVersion":"v3-sequential","input":{"objectKey":"raw/test/input.csv","format":"csv","name":"input.csv"},"rules":[],"outputFormat":"csv"})


def test_really_blocked_child_is_killed_without_stopping_parent(tmp_path,monkeypatch):
    monkeypatch.setattr("cleaning.executor._child",forever)
    unit=ExecutionUnit(message(),None,tmp_path)
    try:
        deadline=time.monotonic()+10
        while not (tmp_path/"started").exists() and time.monotonic()<deadline:
            time.sleep(.02)
        assert (tmp_path/"started").exists()
        unit.request_cancel()
        assert not unit.done()  # Cooperative cancellation cannot stop the simulated native hang.
        started=time.monotonic()
        unit.terminate()
        assert unit.done() and time.monotonic()-started<4
        assert unit.result()["errorCode"]=="EXECUTOR_CRASHED"
        assert os.getpid()!=unit.process.pid
    finally:
        unit.close()


def test_watchdog_enforces_deadline_while_supervisor_is_blocked_in_network_io(tmp_path,monkeypatch):
    from cleaning.executor import ExecutionWatchdog
    monkeypatch.setattr("cleaning.executor._child",forever)
    unit=ExecutionUnit(message(),None,tmp_path)
    guard=None
    try:
        deadline=time.monotonic()+10
        while not (tmp_path/"started").exists() and time.monotonic()<deadline:time.sleep(.02)
        assert (tmp_path/"started").exists()
        guard=ExecutionWatchdog(unit,.1,.1)
        # Models a synchronous control HTTP call that has not returned yet.
        time.sleep(1)
        assert unit.done() and guard.stop_reason=="TASK_TIMEOUT" and guard.failure is None
    finally:
        if guard:guard.close()
        unit.close()


def test_reaper_only_removes_marked_expired_local_attempts(tmp_path):
    old=ScratchDirectory(str(tmp_path),message())
    fresh=ScratchDirectory(str(tmp_path),message())
    unknown=tmp_path/"user-content";unknown.mkdir();(unknown/"keep").write_text("safe")
    marker=old.path/".attempt.json"
    data=json.loads(marker.read_text());data["createdAt"]=time.time()-7200;marker.write_text(json.dumps(data))
    assert ScratchDirectory.reap(str(tmp_path),3600)==1
    assert not old.path.exists() and fresh.path.exists() and (unknown/"keep").exists()
    fresh.cleanup()


def test_worker_timeout_stops_real_hung_process_and_acknowledges_terminal(tmp_path,monkeypatch):
    from cleaning.worker import Worker
    from test_worker import Channel,make_worker,body
    monkeypatch.setattr("cleaning.executor._child",forever)
    monkeypatch.setattr("cleaning.worker.HEARTBEAT_PATH",tmp_path.parent/"test-heartbeat")
    events=[];worker=make_worker(events)
    worker.settings.execution_timeout_seconds=.15
    worker.settings.cancel_grace_seconds=.1
    worker.settings.scratch_root=str(tmp_path)
    worker._start_execution=lambda task,directory:ExecutionUnit(task,None,directory)
    codes=[]
    old=worker.java.event
    def record(*args,**kwargs):
        codes.append(kwargs.get("error_code"));return old(*args,**kwargs)
    worker.java.event=record
    started=time.monotonic()
    worker._process(Channel(events),SimpleNamespace(delivery_tag=1),None,body())
    assert time.monotonic()-started<6
    assert "TASK_TIMEOUT" in codes
    assert events[-2:]==[("event","FAILED"),("ack",1)]
    assert list(tmp_path.iterdir())==[]


def test_ambiguous_callback_reuses_event_identity(monkeypatch):
    import httpx
    from cleaning.worker import JavaClient
    calls=[]
    class Client:
        def post(self,path,**kwargs):
            calls.append(kwargs["json"])
            if len(calls)==1:raise httpx.ReadTimeout("response lost")
            return httpx.Response(200,request=httpx.Request("POST","http://java/events"))
    java=JavaClient.__new__(JavaClient);java.client=Client()
    monkeypatch.setattr("cleaning.worker.time.sleep",lambda n:None)
    task=message();java.event(task,"lease","RUNNING",10,"read")
    java.event(task,"lease","FAILED",10,"timeout",error_code="TASK_TIMEOUT",stage="read")
    assert calls[0]==calls[1]
    assert calls[0]["eventSeq"]==1 and calls[2]["eventSeq"]==2
    assert calls[0]["eventId"]!=calls[2]["eventId"]


@pytest.mark.parametrize("description,expected",[("Downloading original file","DOWNLOAD"),("Reading input in bounded batches","READ_INPUT"),("Completed rule 12: display-name with spaces","RULE_12"),("Artifacts uploaded; committing report","COMMIT")])
def test_progress_descriptions_are_encoded_as_valid_stage_identifiers(description,expected):
    from cleaning.worker import _stage_code
    import re
    assert _stage_code(description)==expected
    assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}",_stage_code(description))
