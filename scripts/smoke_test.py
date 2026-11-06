"""Exercise real HTTP -> MySQL/outbox -> RabbitMQ -> Worker -> MinIO pipeline.

Run after docker compose is healthy: python scripts/smoke_test.py
No mocked services and no extra Python dependencies. Creates isolated test users.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class Client:
    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, data=None, *, raw=False, expected=(200, 201, 204)):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if isinstance(data, bytes):
            headers["Content-Type"] = "application/octet-stream"
        elif data is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(data).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read()
        assert status in expected, f"{method} {path}: HTTP {status}: {body[:1000]!r}"
        if raw:
            return body
        return json.loads(body) if body else None

    def upload(self, name: str, data: bytes):
        size = 1024 * 1024
        info = self.request("POST", "/api/files/init", {"name": name, "size": len(data), "chunkSize": size})
        chunk_size = info["chunkSize"]
        chunks = [data[offset:offset + chunk_size] for offset in range(0, len(data), chunk_size)]
        assert info["totalChunks"] == len(chunks)
        for index, chunk in enumerate(chunks):
            self.request("PUT", f"/api/files/{info['id']}/chunks/{index}", chunk)
        self.request("POST", f"/api/files/{info['id']}/complete")
        # Completion must remain safe on browser/network retries.
        self.request("POST", f"/api/files/{info['id']}/complete")
        return info["id"]

    def wait(self, task_id: str, timeout: int):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            task = self.request("GET", f"/api/tasks/{task_id}")
            if task["status"] != last:
                print(f"  {task_id[:8]} {task['status']} ({task['progress']}%)", flush=True)
                last = task["status"]
            if task["status"] in {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"}:
                return task
            time.sleep(1)
        raise AssertionError(f"Task {task_id} did not finish within {timeout}s")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8088")
    parser.add_argument("--timeout", type=int, default=180, help="per-task deadline, seconds")
    args = parser.parse_args()
    anon = Client(args.base_url)
    suffix = uuid.uuid4().hex[:12]
    credentials = {"username": "test_" + suffix, "password": "Test_" + uuid.uuid4().hex}
    auth = anon.request("POST", "/api/auth/register", credentials)
    assert auth["token"] and auth["user"]["username"] == credentials["username"]
    owner = Client(args.base_url, auth["token"])
    assert owner.request("GET", "/api/auth/me")["username"] == credentials["username"]
    anon.request("GET", "/api/files", expected=(401,))
    anon.request("POST", "/api/auth/login", {**credentials, "password": "wrong-password"}, expected=(401,))
    other_auth = anon.request("POST", "/api/auth/register", {"username": "other_" + suffix, "password": uuid.uuid4().hex})
    other = Client(args.base_url, other_auth["token"])

    root = Path(__file__).resolve().parents[1]
    file_id = owner.upload("customers.csv", (root / "samples/customers.csv").read_bytes())
    preview = owner.request("GET", f"/api/files/{file_id}/preview?limit=3")
    assert len(preview["rows"]) == 3 and len(preview["columns"]) == 5
    other.request("GET", f"/api/files/{file_id}/preview", expected=(403, 404))
    owner.request("POST", "/api/rule-sets", {"name": "bad", "rules": [{"type": "execute_code"}]}, expected=(400, 422))
    rule = owner.request("POST", "/api/rule-sets", {"name": "Acceptance rules", "rules": json.loads((root / "samples/rules.json").read_text())})
    updated = owner.request("PUT", f"/api/rule-sets/{rule['id']}", {"name": "Acceptance rules v2", "rules": []})
    assert updated["version"] == rule["version"] + 1
    assert len(owner.request("GET", f"/api/rule-sets/{rule['id']}/versions")) == 2
    other.request("PUT", f"/api/rule-sets/{rule['id']}", {"name": "Stolen", "rules": []}, expected=(403, 404))

    for output_format in ("csv", "xlsx", "parquet"):
        print(f"Checking CSV input -> {output_format}, then {output_format} input -> CSV", flush=True)
        task = owner.request("POST", "/api/tasks", {"fileId": file_id, "ruleSetId": rule["id"], "ruleVersion": 1, "outputFormat": output_format})
        task = owner.wait(task["id"], args.timeout)
        assert task["status"] == "SUCCEEDED", task
        assert task["ruleVersion"] == 1 and task["progress"] == 100
        report = owner.request("GET", f"/api/tasks/{task['id']}/report")
        assert (report["inputRows"], report["outputRows"], report["dirtyRows"]) == (6, 3, 3), report
        result = owner.request("GET", f"/api/tasks/{task['id']}/artifacts/result", raw=True)
        assert result
        dirty = owner.request("GET", f"/api/tasks/{task['id']}/artifacts/dirty", raw=True)
        dirty_rows = list(csv.DictReader(io.StringIO(dirty.decode("utf-8-sig"))))
        assert len(dirty_rows) == 3 and all(row["_reason"] for row in dirty_rows)
        saved_report = owner.request("GET", f"/api/tasks/{task['id']}/artifacts/report", raw=True)
        assert json.loads(saved_report)["outputRows"] == 3
        result_preview = owner.request("GET", f"/api/tasks/{task['id']}/preview?kind=result&limit=100")
        dirty_preview = owner.request("GET", f"/api/tasks/{task['id']}/preview?kind=dirty&limit=100")
        assert result_preview["previewRowCount"] == 3 and dirty_preview["previewRowCount"] == 3
        assert report["inputRows"] == report["outputRows"] + report["finalRemovedRows"]
        assert report["outputRows"] == report["validOutputRows"] + report["flaggedOutputRows"]
        assert sum(report["finalRemovalByRule"].values()) == report["finalRemovedRows"]
        assert all(row["_file_id"] == file_id for row in dirty_preview["rows"])
        logs = owner.request("GET", f"/api/tasks/{task['id']}/logs?after=0")
        assert logs, "No persisted execution logs"
        other.request("GET", f"/api/tasks/{task['id']}", expected=(403, 404))
        other.request("GET", f"/api/tasks/{task['id']}/artifacts/result", expected=(403, 404))
        other.request("GET", f"/api/tasks/{task['id']}/report", expected=(403, 404))
        if output_format == "csv":
            cleaned = list(csv.DictReader(io.StringIO(result.decode("utf-8-sig"))))
            assert cleaned[0]["name"] == "Alice" and cleaned[0]["email"] == "alice@example.com"
            assert cleaned[1]["city"] == "Unknown"
        roundtrip_id = owner.upload("roundtrip." + output_format, result)
        roundtrip_preview = owner.request("GET", f"/api/files/{roundtrip_id}/preview?limit=20")
        assert len(roundtrip_preview["rows"]) == 3
        conversion = owner.request("POST", "/api/tasks", {"fileId": roundtrip_id, "ruleSetId": rule["id"], "ruleVersion": 2, "outputFormat": "csv"})
        conversion = owner.wait(conversion["id"], args.timeout)
        assert conversion["status"] == "SUCCEEDED", conversion

    print("Checking execution failure and explicit retry", flush=True)
    invalid_column = owner.request("POST", "/api/rule-sets", {"name": "Missing column", "rules": [{"type": "trim", "columns": ["missing_column"]}]})
    failing = owner.request("POST", "/api/tasks", {"fileId": file_id, "ruleSetId": invalid_column["id"], "ruleVersion": 1, "outputFormat": "csv"})
    failed = owner.wait(failing["id"], args.timeout)
    assert failed["status"] == "FAILED" and failed["error"], failed
    retried = owner.request("POST", f"/api/tasks/{failing['id']}/retry")
    assert retried["attempt"] == failed["attempt"] + 1
    assert owner.wait(failing["id"], args.timeout)["status"] == "FAILED"
    print("Checking multi-chunk upload and merge", flush=True)
    large_csv = ("id,payload\n" + "".join(f"{i},abcdefghijklmnopqrstuvwx\n" for i in range(50_000))).encode()
    assert len(large_csv) > 1024 * 1024
    large_file_id = owner.upload("multiple-chunks.csv", large_csv)
    multi = owner.request("POST", "/api/tasks", {"fileId": large_file_id, "ruleSetId": rule["id"], "ruleVersion": 2, "outputFormat": "csv"})
    assert owner.wait(multi["id"], args.timeout)["status"] == "SUCCEEDED"
    multi_report = owner.request("GET", f"/api/tasks/{multi['id']}/report")
    assert (multi_report["inputRows"], multi_report["outputRows"], multi_report["dirtyRows"]) == (50_000, 50_000, 0)
    # A second login must work with persisted user credentials.
    session = anon.request("POST", "/api/auth/login", credentials)
    logged_in = Client(args.base_url, session["token"])
    logged_in.request("POST", "/api/auth/logout")
    logged_in.request("GET", "/api/auth/me", expected=(401,))
    print("PASS: real asynchronous pipeline, three formats, immutable versions, reports, downloads, tenant isolation, failure/retry, multi-chunk merge and logout.")


if __name__ == "__main__":
    main()
