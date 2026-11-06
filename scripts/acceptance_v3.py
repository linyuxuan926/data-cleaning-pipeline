"""Exercise new V3 execution, idempotency, artifact previews and attempt history.

Run only against the local Compose deployment after it is healthy. Uses real
Java, MySQL, RabbitMQ, Worker, MinIO and Python services; no mocked APIs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from smoke_test import Client

ROOT = Path(__file__).resolve().parents[1]


class HeaderClient(Client):
    def request(self, method, path, data=None, *, raw=False, expected=(200, 201, 204), extra_headers=None):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        headers.update(extra_headers or {})
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8088")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    suffix = uuid.uuid4().hex[:12]
    anonymous = HeaderClient(args.base_url)
    auth = anonymous.request("POST", "/api/auth/register", {
        "username": "v3_" + suffix, "password": uuid.uuid4().hex,
    })
    owner = HeaderClient(args.base_url, auth["token"])
    other_auth = anonymous.request("POST", "/api/auth/register", {
        "username": "v3_other_" + suffix, "password": uuid.uuid4().hex,
    })
    other = HeaderClient(args.base_url, other_auth["token"])
    resume_bytes = ("id,payload\n" + "".join(f"{i},resume-01234567890123456789\n" for i in range(12000))).encode()
    resumable = owner.request("POST", "/api/files/init", {"name": "resume.csv", "size": len(resume_bytes), "chunkSize": 262144, "sha256": hashlib.sha256(resume_bytes).hexdigest()})
    owner.request("PUT", f"/api/uploads/{resumable['uploadId']}/chunks/0", resume_bytes[:262144])
    progress = owner.request("GET", f"/api/uploads/{resumable['uploadId']}")
    assert progress["receivedChunks"] == [0] and progress["totalChunks"] > 1, progress
    other.request("GET", f"/api/uploads/{resumable['uploadId']}", expected=(403, 404))
    owner.request("POST", f"/api/uploads/{resumable['uploadId']}/complete", expected=(409,))
    for index in range(1, progress["totalChunks"]):
        owner.request("PUT", f"/api/uploads/{resumable['uploadId']}/chunks/{index}", resume_bytes[index*262144:(index+1)*262144])
    completed = owner.request("POST", f"/api/uploads/{resumable['uploadId']}/complete")
    assert completed["sha256"] == hashlib.sha256(resume_bytes).hexdigest() and completed["parseStatus"] == "PARSED", completed
    assert owner.request("GET", f"/api/files/{completed['id']}")["id"] == completed["id"]
    print("PASS: incomplete merge rejected, actual persisted chunks queried, remaining chunks resumed and full SHA-256 verified.", flush=True)
    file_id = owner.upload("ordered-rules.csv", b"id,name\n1,Alice\n2,\n3,Bob\n")
    rule = owner.request("POST", "/api/rule-sets", {
        "name": "Order-sensitive rules " + suffix,
        "rules": [
            {"type": "required", "columns": ["name"], "onError": "mark"},
            {"type": "fill_null", "columns": ["name"], "value": "Unknown"},
        ],
    })
    request = {"fileId": file_id, "ruleSetId": rule["id"], "ruleVersion": 1, "outputFormat": "csv"}
    key = "v3-create-" + suffix
    first = owner.request("POST", "/api/tasks", request, extra_headers={"Idempotency-Key": key})
    replay = owner.request("POST", "/api/tasks", request, expected=(200,), extra_headers={"Idempotency-Key": key})
    assert replay["id"] == first["id"], "Same-key submission created a second task"
    owner.request("POST", "/api/tasks", {**request, "outputFormat": "xlsx"}, expected=(409,), extra_headers={"Idempotency-Key": key})

    v3 = owner.wait(first["id"], args.timeout)
    assert v3["status"] == "SUCCEEDED" and v3["engineVersion"] == "v3-sequential", v3
    v2 = owner.request("POST", "/api/tasks", {**request, "engineVersion": "v2-phased"})
    v2 = owner.wait(v2["id"], args.timeout)
    assert v2["status"] == "SUCCEEDED" and v2["engineVersion"] == "v2-phased", v2
    v3_report = owner.request("GET", f"/api/tasks/{v3['id']}/report")
    v2_report = owner.request("GET", f"/api/tasks/{v2['id']}/report")
    assert (v3_report["inputRows"], v3_report["outputRows"], v3_report["removedRows"]) == (3, 3, 0), v3_report
    assert v3_report["dirtyRows"] == 1 and v3_report["issueCount"] == 1, v3_report
    assert (v3_report["validOutputRows"], v3_report["flaggedOutputRows"], v3_report["missingFilledCells"]) == (2, 1, 1), v3_report
    assert sum(v3_report["finalRemovalByRule"].values()) == v3_report["finalRemovedRows"]
    assert v2_report["dirtyRows"] == 0, v2_report
    result = owner.request("GET", f"/api/tasks/{v3['id']}/preview?kind=result&limit=100")
    dirty = owner.request("GET", f"/api/tasks/{v3['id']}/preview?kind=dirty&limit=100")
    assert result["previewRowCount"] == 3 and result["rows"][1]["name"] == "Unknown", result
    assert result["rows"][1]["_quality_valid"] == "False", result
    assert dirty["previewRowCount"] == 1 and dirty["rows"][0]["_source_row"] == "2", dirty
    assert dirty["rows"][0]["_file_id"] == file_id and dirty["rows"][0]["_final_action"] == "OUTPUT_FLAGGED", dirty
    attempt_rows = owner.request("GET", f"/api/tasks/{v3['id']}/attempts")
    assert len(attempt_rows) == 1 and attempt_rows[0]["attemptId"] == v3["attemptId"], attempt_rows
    attempt_id = attempt_rows[0]["attemptId"]
    other.request("GET", f"/api/tasks/{v3['id']}/attempts", expected=(403, 404))
    other.request("GET", f"/api/tasks/{v3['id']}/attempts/{attempt_id}/artifacts/result", expected=(403, 404))
    historical_preview = owner.request("GET", f"/api/tasks/{v3['id']}/attempts/{attempt_id}/preview?kind=result")
    assert historical_preview["rows"] == result["rows"]

    broken_rules = owner.request("POST", "/api/rule-sets", {
        "name": "Retry history " + suffix, "rules": [{"type": "trim", "columns": ["missing"]}],
    })
    failed = owner.request("POST", "/api/tasks", {
        "fileId": file_id, "ruleSetId": broken_rules["id"], "ruleVersion": 1, "outputFormat": "csv",
    })
    assert owner.wait(failed["id"], args.timeout)["status"] == "FAILED"
    old_attempt_id = failed["attemptId"]
    retried = owner.request("POST", f"/api/tasks/{failed['id']}/retry")
    assert retried["attempt"] == 2 and retried["attemptId"] != old_attempt_id, retried
    assert owner.wait(failed["id"], args.timeout)["status"] == "FAILED"
    history = owner.request("GET", f"/api/tasks/{failed['id']}/attempts")
    assert len(history) == 2 and len({item["attemptId"] for item in history}) == 2, history
    assert {item["status"] for item in history} == {"FAILED"}, history
    print("PASS: configured order differs from frozen V2 phase order; task idempotency, protected current/historical previews, tenant isolation and immutable retry history.")


if __name__ == "__main__":
    main()
