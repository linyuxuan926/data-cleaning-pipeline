import json
from pathlib import Path
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from cleaning.api import create_app
from cleaning.config import Settings
from cleaning.models import TaskMessage
from cleaning.pipeline import execute_task
from cleaning.storage import ObjectStorage


@pytest.fixture
def settings():
    return Settings(
        "http://localhost:9000",
        "access",
        "secretsecret",
        "cleaning",
        "x" * 40,
        "http://localhost:8080",
        "localhost",
        5672,
        "user",
        "password",
        "cleaning.tasks",
    )


class MemoryStorage:
    """Test-only adapter. Production always uses MinIO's ObjectStorage."""

    def __init__(self):
        self.objects = {"raw/user/file.csv": b"id,name,age\n1, Alice ,20\n2,Bob,bad\n"}

    def download_input(self, key: str, target: Path):
        target.write_bytes(self.objects[key])

    def upload(self, key: str, path: Path, content_type: str):
        self.objects[key] = path.read_bytes()

    def download_artifact(self, key: str, target: Path):
        target.write_bytes(self.objects[key])


def test_execute_writes_report_dirty_and_attempt_scoped_artifacts(tmp_path):
    storage = MemoryStorage()
    message = TaskMessage.model_validate(
        {
            "taskId": str(uuid4()),
            "attempt": 2,
            "input": {
                "objectKey": "raw/user/file.csv",
                "format": "csv",
                "name": "file.csv",
            },
            "rules": [
                {"type": "trim", "columns": ["name"]},
                {"type": "cast", "columns": ["age"], "dtype": "integer"},
            ],
            "outputFormat": "parquet",
        }
    )
    events = []
    report, artifacts = execute_task(
        message, storage, tmp_path, Event(), lambda p, m: events.append((p, m))
    )
    assert (
        report["inputRows"] == 2
        and report["outputRows"] == 1
        and report["dirtyRows"] == 1
    )
    assert json.loads(storage.objects[artifacts["report"]]) == report
    assert all(f"/{message.taskId}/2/" in key for key in artifacts.values())
    assert "Bob" in storage.objects[artifacts["dirty"]].decode("utf-8-sig")
    assert events[-1][0] == 98


def test_task_message_accepts_engine_version_and_optional_attempt_uuid(tmp_path):
    storage = MemoryStorage()
    attempt_id = uuid4()
    message = TaskMessage.model_validate(
        {
            "taskId": str(uuid4()),
            "attempt": 2,
            "attemptId": str(attempt_id),
            "engineVersion": "v3-sequential",
            "input": {
                "objectKey": "raw/user/file.csv",
                "format": "csv",
                "name": "file.csv",
            },
            "rules": [
                {
                    "id": "trim-name",
                    "enabled": True,
                    "type": "trim",
                    "columns": ["name"],
                }
            ],
            "outputFormat": "csv",
        }
    )

    report, _ = execute_task(message, storage, tmp_path, Event(), lambda *_: None)

    assert message.attemptId == attempt_id
    assert report["engineVersion"] == "v3-sequential"
    assert report["attemptId"] == str(attempt_id)
    assert report["executionOrder"] == ["trim-name"]


def test_internal_validation_accepts_v3_rule_metadata_and_cast_null(settings):
    payload = {
        "engineVersion": "v3-sequential",
        "rules": [{
            "id": "cast-score", "enabled": True, "type": "cast",
            "columns": ["f0"], "dtype": "integer", "onError": "null",
        }],
    }
    with TestClient(create_app(settings)) as client:
        headers = {"X-Internal-Token": settings.internal_token}
        assert client.post("/internal/validate-rules", json=payload, headers=headers).json() == {"valid": True}
        assert client.post(
            "/internal/validate-rules",
            json={**payload, "engineVersion": "v2-phased"},
            headers=headers,
        ).status_code == 422


def test_artifact_preview_is_protected_and_includes_metadata_columns(settings):
    storage = MemoryStorage()
    storage.objects["results/task/1/result.csv"] = (
        b"id,_quality_valid,_quality_errors\n1,false,\\\"bad\\\"\n"
    )
    storage.objects["dirty/task/1/dirty.csv"] = b"id,_source_row,_reason\n"
    with TestClient(create_app(settings)) as client:
        client.app.state.storage = storage
        payload = {
            "objectKey": "results/task/1/result.csv",
            "format": "csv",
            "limit": 10,
        }
        assert client.post("/internal/artifact-preview", json=payload).status_code == 401
        headers = {"X-Internal-Token": settings.internal_token}
        result = client.post(
            "/internal/artifact-preview", json=payload, headers=headers
        )
        assert result.status_code == 200
        assert [column["name"] for column in result.json()["columns"]] == [
            "id", "_quality_valid", "_quality_errors"
        ]
        assert result.json()["previewRowCount"] == 1
        empty = client.post(
            "/internal/artifact-preview",
            json={
                "objectKey": "dirty/task/1/dirty.csv",
                "format": "csv",
                "limit": 10,
            },
            headers=headers,
        )
        assert empty.status_code == 200
        assert empty.json()["rowCount"] == 0
        assert empty.json()["previewRowCount"] == 0
        assert client.post(
            "/internal/artifact-preview",
            json={**payload, "limit": 101},
            headers=headers,
        ).status_code == 422
        assert client.post(
            "/internal/artifact-preview",
            json={**payload, "objectKey": "raw/user/file.csv"},
            headers=headers,
        ).status_code == 422
        slots = client.app.state.preview_slots
        slots.acquire()
        slots.acquire()
        try:
            assert client.post(
                "/internal/artifact-preview", json=payload, headers=headers
            ).status_code == 429
        finally:
            slots.release()
            slots.release()


def test_v3_task_message_requires_attempt_uuid():
    with pytest.raises(ValueError, match="require attemptId"):
        TaskMessage.model_validate(
            {
                "taskId": str(uuid4()),
                "attempt": 1,
                "engineVersion": "v3-sequential",
                "input": {
                    "objectKey": "raw/user/file.csv",
                    "format": "csv",
                    "name": "file.csv",
                },
                "rules": [],
                "outputFormat": "csv",
            }
        )


def test_artifact_download_enforces_prefix_size_and_stream_bounds(tmp_path, monkeypatch):
    import cleaning.storage as storage_module
    from types import SimpleNamespace
    from cleaning.models import InputError

    class Response:
        def __init__(self, blocks):
            self.blocks = blocks

        def stream(self, _size):
            yield from self.blocks

        def close(self):
            pass

        def release_conn(self):
            pass

    class Client:
        def __init__(self, size, blocks):
            self.size = size
            self.blocks = blocks
            self.get_called = False

        def stat_object(self, bucket, key):
            return SimpleNamespace(size=self.size)

        def get_object(self, bucket, key):
            self.get_called = True
            return Response(self.blocks)

    monkeypatch.setattr(storage_module, "MAX_ARTIFACT_BYTES", 4)
    storage = ObjectStorage.__new__(ObjectStorage)
    storage.bucket = "cleaning"
    storage.client = Client(4, [b"1234"])
    target = tmp_path / "allowed.csv"
    storage.download_artifact("results/task/1/result.csv", target)
    assert target.read_bytes() == b"1234"

    storage.client = Client(5, [b"12345"])
    with pytest.raises(InputError, match="256 MiB"):
        storage.download_artifact("results/task/1/result.csv", target)
    assert not storage.client.get_called
    with pytest.raises(InputError, match="artifact reference"):
        storage.download_artifact("raw/user/file.csv", target)

    storage.client = Client(1, [b"1234", b"5"])
    with pytest.raises(InputError, match="256 MiB"):
        storage.download_artifact("dirty/task/1/dirty.csv", target)
    assert not target.exists()


def test_internal_preview_token_and_validation(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.storage = MemoryStorage()
        assert client.get("/health").json() == {"status": "UP"}
        payload = {"objectKey": "raw/user/file.csv", "format": "csv", "limit": 1}
        assert client.post("/internal/preview", json=payload).status_code == 401
        headers = {"X-Internal-Token": settings.internal_token}
        response = client.post("/internal/preview", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()["rows"][0]["id"] == "1"
        assert (
            client.post(
                "/internal/preview",
                json={**payload, "objectKey": "results/other.csv"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/internal/preview", json={**payload, "limit": 10000}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/internal/preview", json={**payload, "unknown": 1}, headers=headers
            ).status_code
            == 422
        )


def test_preview_rejects_when_slots_exhausted(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.storage = MemoryStorage()
        sem = client.app.state.preview_slots
        sem.acquire()
        sem.acquire()
        try:
            response = client.post(
                "/internal/preview",
                headers={"X-Internal-Token": settings.internal_token},
                json={"objectKey": "raw/user/file.csv", "format": "csv"},
            )
            assert response.status_code == 429
        finally:
            sem.release()
            sem.release()


def test_python_is_authoritative_for_rule_validation(settings):
    with TestClient(create_app(settings)) as client:
        headers = {"X-Internal-Token": settings.internal_token}
        payload = {"rules": [{"type": "cast", "columns": ["f0"], "dtype": "boolean"}]}
        assert client.post("/internal/validate-rules", json=payload).status_code == 401
        assert client.post(
            "/internal/validate-rules", headers=headers, json=payload
        ).json() == {"valid": True}
        response = client.post(
            "/internal/validate-rules",
            headers=headers,
            json={"rules": [{"type": "regex", "column": "f0", "pattern": r"\e"}]},
        )
        assert (
            response.status_code == 422 and "invalid regex" in response.json()["detail"]
        )
        response = client.post(
            "/internal/validate-rules",
            headers=headers,
            json={
                "rules": [
                    {
                        "type": "compare",
                        "column": "f0",
                        "operator": "eval",
                        "value": "malicious",
                    }
                ]
            },
        )
        assert response.status_code == 422


@pytest.mark.parametrize("strategy", ["mean", "median", "mode"])
def test_statistical_fill_cast_report_serializes_complete_real_artifacts(
    tmp_path, strategy
):
    storage = MemoryStorage()
    storage.objects["raw/user/stats.csv"] = b"id,score\n1,10\n2,30\n3,\n4,invalid\n"
    message = TaskMessage.model_validate(
        {
            "taskId": str(uuid4()),
            "attempt": 1,
            "input": {
                "objectKey": "raw/user/stats.csv",
                "format": "csv",
                "name": "stats.csv",
            },
            "rules": [
                {
                    "type": "fill_stat",
                    "columns": ["f1"],
                    "strategy": strategy,
                    "emptyPolicy": "fail",
                },
                {"type": "cast", "columns": ["f1"], "dtype": "number"},
            ],
            "outputFormat": "csv",
        }
    )
    report, artifacts = execute_task(
        message, storage, tmp_path, Event(), lambda *_: None
    )
    serialized = json.dumps(report, allow_nan=False)
    assert json.loads(serialized) == json.loads(storage.objects[artifacts["report"]])
    assert report["executionOrder"] == [2, 1]
    assert (
        report["inputRows"] == 4
        and report["outputRows"] == 3
        and report["removedRows"] == 1
    )
    count_fields = {
        "inputRows",
        "outputRows",
        "removedRows",
        "dirtyRows",
        "flaggedRows",
        "issueCount",
        "changedCells",
    }
    assert all(type(report[field]) is int for field in count_fields)
    for rule in report["rules"]:
        assert all(
            type(rule[field]) is int
            for field in (
                "inputRows",
                "outputRows",
                "rejectedRows",
                "changedCells",
                "issueCount",
                "flaggedRows",
            )
        )
    statistics = report["rules"][1]["statistics"]["f1"]
    assert (
        type(statistics["sampleCount"]) is int
        and type(statistics["invalidValues"]) is int
    )
    assert all(storage.objects[key] for key in artifacts.values())
