"""Expanded MVP acceptance: real authenticated APIs and asynchronous execution.

Requires the isolated local Compose deployment. Temporarily changes system
limits (restored in finally) and stops Workers for a snapshot-isolation check.
Administrator credentials are read locally, never printed. No mocked services.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
from urllib.parse import urlparse
import uuid

from smoke_test import Client

ROOT = Path(__file__).resolve().parents[1]


def compose(*args):
    result = subprocess.run(["docker", "compose", *args], cwd=ROOT, text=True,
                            encoding="utf-8", errors="replace", capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def read_env(path: Path):
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if "=" in line and not line.lstrip().startswith("#"))


def new_user(anon: Client):
    credentials = {"username": "v2_" + uuid.uuid4().hex[:12], "password": uuid.uuid4().hex}
    auth = anon.request("POST", "/api/auth/register", credentials)
    assert auth["user"]["role"] == "USER", auth["user"]
    return Client(anon.base, auth["token"]), auth["user"], credentials


def upload(client: Client, name: str, data: bytes, options=None):
    checksum = hashlib.sha256(data).hexdigest()
    init = client.request("POST", "/api/files/init", {
        "name": name, "size": len(data), "chunkSize": 262144, "sha256": checksum,
        **({"parseOptions": options} if options is not None else {}),
    })
    assert init["uploadId"] != init["id"]
    for index, offset in enumerate(range(0, len(data), init["chunkSize"])):
        part = data[offset:offset + init["chunkSize"]]
        client.request("PUT", f"/api/uploads/{init['uploadId']}/chunks/{index}", part)
        # Retransmitting the same part must remain idempotent.
        if index == 0:
            client.request("PUT", f"/api/uploads/{init['uploadId']}/chunks/{index}", part)
    complete = client.request("POST", f"/api/uploads/{init['uploadId']}/complete")
    assert complete["sha256"] == checksum
    assert complete["status"] == "READY"
    return complete


def csv_rows(data: bytes):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def run_task(client: Client, file_id: str, rules: list, *, expect="SUCCEEDED"):
    rule = client.request("POST", "/api/rule-sets", {"name": "Acceptance " + uuid.uuid4().hex[:8], "rules": rules})
    task = client.request("POST", "/api/tasks", {
        "fileId": file_id, "ruleSetId": rule["id"], "ruleVersion": 1, "outputFormat": "csv", "engineVersion": "v2-phased",
    })
    task = client.wait(task["id"], 180)
    assert task["status"] == expect, task
    if expect != "SUCCEEDED":
        assert task["error"]
        return task, None, None, None
    report = client.request("GET", f"/api/tasks/{task['id']}/report")
    result = csv_rows(client.request("GET", f"/api/tasks/{task['id']}/artifacts/result", raw=True))
    dirty = csv_rows(client.request("GET", f"/api/tasks/{task['id']}/artifacts/dirty", raw=True))
    assert report["inputRows"] == report["outputRows"] + report["removedRows"], report
    assert report["issueCount"] == len(dirty)
    assert report["dirtyRows"] == len({r["_source_row"] for r in dirty})
    return task, report, result, dirty


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8088")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    if urlparse(args.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Use an isolated local Compose deployment")
    secrets = read_env(args.env_file)
    anon = Client(args.base_url)
    auth = anon.request("POST", "/api/auth/login", {
        "username": secrets["BOOTSTRAP_ADMIN_USERNAME"], "password": secrets["BOOTSTRAP_ADMIN_PASSWORD"],
    })
    assert auth["user"]["role"] == "ADMIN"
    admin = Client(args.base_url, auth["token"])
    owner, user, credentials = new_user(anon)
    other, _, _ = new_user(anon)
    owner.request("GET", "/api/admin/users", expected=(403,))
    owner.request("GET", "/api/admin/tasks", expected=(403,))
    limits = admin.request("GET", "/api/settings")
    changed_limits = {**limits, "submissionsPerMinute": 120, "previewRows": 100}
    try:
        admin.request("PUT", "/api/admin/settings", changed_limits)
        owner.request("PUT", "/api/admin/settings", changed_limits, expected=(403,))
        assert owner.request("GET", "/api/settings")["previewRows"] == 100
        admin.request("PUT", "/api/admin/settings", {**changed_limits, "maxRows": 500001}, expected=(400, 422))
        print("Checking role management, immediate session disable and last-admin protection", flush=True)
        assert any(u["id"] == user["id"] for u in admin.request("GET", "/api/admin/users"))
        admin.request("PUT", f"/api/admin/users/{user['id']}", {"role": "USER", "enabled": False})
        owner.request("GET", "/api/auth/me", expected=(401,))
        anon.request("POST", "/api/auth/login", credentials, expected=(401, 403))
        admin.request("PUT", f"/api/admin/users/{user['id']}", {"role": "USER", "enabled": True})
        owner.token = anon.request("POST", "/api/auth/login", credentials)["token"]
        admin.request("PUT", f"/api/admin/users/{auth['user']['id']}", {"role": "USER", "enabled": True}, expected=(409,))

        print("Checking separate upload IDs, checksum, parsing failure and CSV manual options", flush=True)
        bad = owner.request("POST", "/api/files/init", {
            "name": "checksum.csv", "size": 8, "chunkSize": 262144, "sha256": "0" * 64,
        })
        owner.request("PUT", f"/api/uploads/{bad['uploadId']}/chunks/0", b"id\n0001\n")
        other.request("PUT", f"/api/uploads/{bad['uploadId']}/chunks/0", b"id\n0001\n", expected=(403, 404))
        owner.request("POST", f"/api/uploads/{bad['uploadId']}/complete", expected=(400, 409))
        spoof = upload(owner, "not-excel.xlsx", b"id,name\n1,Alice\n")
        assert spoof["parseStatus"] == "FAILED" and spoof["parseError"]
        empty_rule = owner.request("POST", "/api/rule-sets", {"name": "Empty", "rules": []})
        owner.request("POST", "/api/tasks", {"fileId": spoof["id"], "ruleSetId": empty_rule["id"], "ruleVersion": 1, "outputFormat": "csv"}, expected=(400, 409))
        gb = "邮编;姓名;金额\n00101;明日香;10.239\n00102;绫波;20\n".encode("gb18030")
        manual = upload(owner, "gb.csv", gb, {"encoding": "gb18030", "delimiter": ";", "missingValues": [""]})
        assert manual["parseStatus"] == "PARSED", manual
        preview = owner.request("GET", f"/api/files/{manual['id']}/preview")
        assert [c["id"] for c in preview["columns"]] == ["f0", "f1", "f2"]
        assert preview["rows"][0]["邮编"] == "00101", preview
        owner.request("POST", f"/api/files/{manual['id']}/parse", {
            "encoding": "gb18030", "delimiter": ";", "missingValues": [""], "fieldTypes": {"f0": "string"},
        })
        _, _, result, _ = run_task(owner, manual["id"], [
            {"type": "rename", "column": "f1", "target": "customer"},
            {"type": "cast", "columns": ["f2"], "dtype": "number", "precision": 2},
            {"type": "select_fields", "columns": ["f0", "f1", "f2"]},
        ])
        assert result[0]["邮编"] == "00101" and result[0]["customer"] == "明日香"
        assert float(result[0]["金额"]) == 10.24

        workbook = upload(owner, "multi-sheet.xlsx", (ROOT / "samples/multi-sheet.xlsx").read_bytes())
        assert workbook["parseStatus"] == "PARSED", workbook
        sheets_preview = owner.request("GET", f"/api/files/{workbook['id']}/preview")
        assert {s["name"] for s in sheets_preview["sheets"]} == {"说明", "客户"}
        owner.request("POST", f"/api/files/{workbook['id']}/parse", {"sheetName": "客户", "missingValues": [""], "fieldTypes": {"f0": "string"}})
        _, sheet_report, sheet_result, _ = run_task(owner, workbook["id"], [])
        assert sheet_report["inputRows"] == 2 and sheet_result[0]["邮编"] == "00101" and sheet_result[0]["姓名"] == "明日香"

        print("Checking mark/drop/fail policies and multiple reasons per original record", flush=True)
        quality = upload(owner, "quality.csv", b"id,name,email,state\n1, Alice ,bad,X\n2,Bob,bob@example.com,A\n")
        rules = [
            {"type": "trim", "columns": ["f1"]},
            {"type": "regex", "column": "f2", "pattern": "[^@]+@[^@]+", "onError": "mark"},
            {"type": "enum", "column": "f3", "values": ["A", "B"], "onError": "mark"},
            {"type": "length", "column": "f1", "max": 3, "onError": "mark"},
        ]
        _, report, result, dirty = run_task(owner, quality["id"], rules)
        assert (report["inputRows"], report["outputRows"], report["removedRows"], report["dirtyRows"], report["issueCount"]) == (2, 2, 0, 1, 3), report
        assert result[0]["name"] == "Alice" and result[0]["_quality_valid"].lower() == "false"
        assert len(json.loads(result[0]["_quality_errors"])) == 3
        assert all(d["name"] == " Alice " for d in dirty), dirty
        assert all(d["_field"] and d["_error_code"] and d["_record_id"] for d in dirty)
        _, report, result, dirty = run_task(owner, quality["id"], [
            {**r, "onError": "drop"} if r["type"] != "trim" else r for r in rules
        ])
        assert (report["outputRows"], report["removedRows"], report["issueCount"]) == (1, 1, 3), report
        run_task(owner, quality["id"], [{"type": "enum", "column": "f3", "values": ["A"], "onError": "fail"}], expect="FAILED")
        owner.request("POST", "/api/rule-sets", {"name": "No code", "rules": [{"type": "compare", "column": "f0", "operator": "eval", "value": "__import__('os')"}]}, expected=(400, 422))

        print("Checking statistical fill, casting, global keep-last and outliers", flush=True)
        stats = upload(owner, "stats.csv", b"id,score\n1,10\n2,30\n3,\n4,invalid\n")
        _, report, result, _ = run_task(owner, stats["id"], [
            {"type": "fill_stat", "columns": ["f1"], "strategy": "mean", "emptyPolicy": "fail"},
            {"type": "cast", "columns": ["f1"], "dtype": "number"},
        ])
        assert report["removedRows"] == 1 and len(result) == 3 and float(result[2]["score"]) == 20
        types = upload(owner, "types.csv", b"id,flag,date,number\n001,yes,2026/11/06,1.235\n002,no,2026/11/07,2.345\n")
        _, _, result, _ = run_task(owner, types["id"], [
            {"type": "cast", "columns": ["f1"], "dtype": "boolean"},
            {"type": "cast", "columns": ["f2"], "dtype": "datetime", "dateFormat": "%Y/%m/%d", "outputFormat": "%d-%m-%Y"},
            {"type": "cast", "columns": ["f3"], "dtype": "number", "precision": 2},
            {"type": "validate_type", "columns": ["f1"], "dtype": "boolean"},
            {"type": "compare", "column": "f3", "operator": "gt", "value": 1},
        ])
        assert result[0]["id"] == "001" and result[0]["flag"].lower() == "true" and result[0]["date"] == "06-11-2026", result
        duplicates = upload(owner, "dedup.csv", b"id,value\n1,first\n2,middle\n1,last\n")
        _, report, result, _ = run_task(owner, duplicates["id"], [{"type": "deduplicate", "columns": ["f0"], "keep": "last"}])
        assert [r["value"] for r in result] == ["middle", "last"] and report["removedRows"] == 1
        iqr = upload(owner, "outliers.csv", b"id,value\n1,1\n2,2\n3,2\n4,3\n5,100\n")
        _, report, result, _ = run_task(owner, iqr["id"], [{"type": "iqr", "column": "f1", "factor": 1.5, "emptyPolicy": "fail", "onError": "null"}])
        assert report["removedRows"] == 0 and report["dirtyRows"] == 1 and result[-1]["value"] == "", (report, result)

        print("Checking invalid numeric/date tokens remain explicit conversion failures", flush=True)
        tokens = upload(owner, "numeric-tokens.csv", b"id,value\n1,NaN\n2,inf\n3,10\n")
        _, report, result, dirty = run_task(owner, tokens["id"], [
            {"type": "cast", "columns": ["f1"], "dtype": "number", "onError": "mark"},
            {"type": "validate_type", "columns": ["f1"], "dtype": "number", "onError": "mark"},
        ])
        assert [row["value"] for row in result[:2]] == ["NaN", "inf"], result
        assert (report["outputRows"], report["removedRows"], report["dirtyRows"], report["issueCount"]) == (3, 0, 2, 4), report
        assert len(dirty) == 4 and all(row["_error_code"] for row in dirty)
        dates = upload(owner, "date-tokens.csv", b"id,when\n1,NaT\n2,\n3,2026-11-06\n", {"missingValues": []})
        _, report, result, _ = run_task(owner, dates["id"], [
            {"type": "cast", "columns": ["f1"], "dtype": "datetime", "onError": "mark"},
            {"type": "validate_type", "columns": ["f1"], "dtype": "datetime", "onError": "mark"},
        ])
        assert [row["when"] for row in result[:2]] == ["NaT", ""], result
        assert (report["outputRows"], report["dirtyRows"], report["issueCount"]) == (3, 2, 4), report

        print("Checking queued task freezes rules, parsing and limits snapshots", flush=True)
        compose("stop", "worker")
        frozen = owner.request("POST", "/api/tasks", {"fileId": manual["id"], "ruleSetId": empty_rule["id"], "ruleVersion": 1, "outputFormat": "csv"})
        owner.request("PUT", f"/api/rule-sets/{empty_rule['id']}", {"name": "Now incompatible", "rules": [{"type": "drop_fields", "columns": ["f0"]}]})
        owner.request("POST", f"/api/files/{manual['id']}/parse", {"encoding": "gb18030", "delimiter": ",", "missingValues": [""]})
        admin.request("PUT", "/api/admin/settings", {**changed_limits, "maxRows": 1})
        compose("start", "worker")
        frozen = owner.wait(frozen["id"], 180)
        assert frozen["status"] == "SUCCEEDED" and frozen["ruleVersion"] == 1, frozen
        old_result = csv_rows(owner.request("GET", f"/api/tasks/{frozen['id']}/artifacts/result", raw=True))
        assert len(old_result) == 2 and old_result[0]["邮编"] == "00101", old_result
        assert any(t["id"] == frozen["id"] for t in admin.request("GET", "/api/admin/tasks"))
        assert admin.request("GET", f"/api/tasks/{frozen['id']}")["ownerId"] == user["id"]
        assert admin.request("GET", f"/api/tasks/{frozen['id']}/report")["inputRows"] == 2
        admin.request("GET", f"/api/tasks/{frozen['id']}/artifacts/result", raw=True)
        admin.request("POST", f"/api/tasks/{frozen['id']}/retry", expected=(403, 404))
        other.request("GET", f"/api/tasks/{frozen['id']}/report", expected=(403, 404))
        print("PASS: expanded real MVP roles, settings, upload integrity, parsing, stable IDs, policies, statistics, types, dedup/outliers and immutable snapshots.")
    finally:
        admin.request("PUT", "/api/admin/settings", limits)
        compose("start", "worker")


if __name__ == "__main__":
    main()
