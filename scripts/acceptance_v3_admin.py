"""Real isolated MySQL/API pagination and scoped retention acceptance.

The 507+ metadata rows are explicitly marked PAGINATION_FIXTURE_NOT_EXECUTED.
They verify history navigation; they are not claimed to be executed cleaning
jobs. Retention uses separately uploaded real objects and one actual Worker run.
Never run this against the main project/port. --allow-faults briefly stops only
the selected isolated project's MinIO, and restores it in finally.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from smoke_test import Client

ROOT = Path(__file__).resolve().parents[1]


class Api(Client):
    def request(self, method, path, data=None, *, raw=False, expected=(200, 201, 204), extra_headers=None):
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        headers.update(extra_headers or {})
        if isinstance(data, bytes):
            headers["Content-Type"] = "application/octet-stream"
        elif data is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(data).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read()
        assert status in expected, f"{method} {path}: HTTP {status}: {body[:1200]!r}"
        return body if raw else json.loads(body) if body else None


def env_file() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def compose(project: str, *args: str, timeout=180) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", "compose", "-p", project, *args], cwd=ROOT,
                            text=True, encoding="utf-8", capture_output=True, timeout=timeout)
    assert result.returncode == 0, f"Isolated Compose command failed: {result.stderr[-1200:]}"
    return result


def sql(project: str, statement: str):
    command = ["docker", "compose", "-p", project, "exec", "-T", "mysql", "sh", "-c",
               'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql --default-character-set=utf8mb4 -u cleaning -D cleaning -N -B']
    result = subprocess.run(command, cwd=ROOT, input=statement, text=True, encoding="utf-8",
                            capture_output=True, timeout=120)
    assert result.returncode == 0, "Fixture SQL failed: " + result.stderr[-1200:]
    return result.stdout


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def insert(table, columns, rows):
    statements = []
    for offset in range(0, len(rows), 80):
        values = ["(" + ",".join(literal(value) if value != "@paginationFixtureAt" else value for value in row) + ")"
                  for row in rows[offset:offset + 80]]
        statements.append(f"INSERT INTO {table}({columns}) VALUES " + ",".join(values) + ";")
    return "\n".join(statements)


def fixture_metadata(project, owner, marker, count):
    files = [str(uuid.uuid4()) for _ in range(count)]
    rules = [str(uuid.uuid4()) for _ in range(count)]
    tasks = [str(uuid.uuid4()) for _ in range(count)]
    attempts = [str(uuid.uuid4()) for _ in range(count)]
    history = [attempts[0]] + [str(uuid.uuid4()) for _ in range(count - 1)]
    fixture_error = "PAGINATION_FIXTURE_NOT_EXECUTED"
    statements = ["START TRANSACTION; SET @paginationFixtureAt=UTC_TIMESTAMP(6)-INTERVAL 30 SECOND;"]
    statements.append(insert("files", "id,user_id,name,format,size,chunk_size,total_chunks,status,created_at,upload_id,parse_status,parse_error", [
        (id_, owner, f"{marker}_file_{i:04}.csv", "csv", 1, 262144, 1, "READY", "@paginationFixtureAt", str(uuid.uuid4()), "FAILED", fixture_error)
        for i, id_ in enumerate(files)]))
    statements.append(insert("rule_sets", "id,user_id,name,current_version,created_at", [
        (id_, owner, f"{marker}_rule_{i:04}", count if i == 0 else 1, "@paginationFixtureAt") for i, id_ in enumerate(rules)]))
    versions = [(id_, 1, f"{marker}_rule_{i:04}", "[]", "@paginationFixtureAt") for i, id_ in enumerate(rules)]
    versions.extend((rules[0], version, f"{marker}_version_{version:04}", "[]", "@paginationFixtureAt") for version in range(2, count + 1))
    statements.append(insert("rule_versions", "rule_set_id,version,name,rules_json,created_at", versions))
    statements.append(insert("tasks", "id,user_id,file_id,rule_set_id,rule_version,output_format,status,progress,attempt,error,created_at,updated_at", [
        (id_, owner, files[i], rules[i], 1, "csv", "FAILED", 0, count if i == 0 else 1, fixture_error, "@paginationFixtureAt", "@paginationFixtureAt")
        for i, id_ in enumerate(tasks)]))
    attempt_rows = [(attempts[i], task, 1, "FAILED", 0, fixture_error, "@paginationFixtureAt", "@paginationFixtureAt", "@paginationFixtureAt") for i, task in enumerate(tasks)]
    attempt_rows.extend((history[i - 1], tasks[0], i, "FAILED", 0, fixture_error, "@paginationFixtureAt", "@paginationFixtureAt", "@paginationFixtureAt") for i in range(2, count + 1))
    statements.append(insert("task_attempts", "attempt_id,task_id,attempt_no,status,progress,error,created_at,updated_at,finished_at", attempt_rows))
    statements.append(insert("task_logs", "task_id,attempt_id,request_id,level,message,created_at", [
        (tasks[0], history[-1], marker, "INFO", f"{fixture_error}: history record {i}", "@paginationFixtureAt") for i in range(count + 100)]))
    for i in range(count):
        statements.append("INSERT INTO users(id,username,password_hash,role,enabled,created_at) SELECT " +
                          literal(str(uuid.uuid4())) + "," + literal(f"{marker}_u{i:04}") + ",password_hash,'USER',FALSE,@paginationFixtureAt FROM users WHERE id=" + literal(owner) + ";")
    statements.append("COMMIT;")
    sql(project, "\n".join(statements))
    return {"files": files, "rules": rules, "tasks": tasks, "history": history,
            "historyTask": tasks[0], "historyRule": rules[0], "logCount": count + 100}


def page_path(endpoint, query):
    return endpoint + "?" + urllib.parse.urlencode(query)


def walk(client, endpoint, query=None, *, key="id", first_hook=None):
    query = {"pageSize": "73", "sort": "createdAt.asc", **(query or {})}
    items, cursors, as_of = [], [], None
    while True:
        page = client.request("GET", page_path(endpoint, query))
        assert set(("items", "nextCursor", "hasMore", "asOf")).issubset(page), page
        assert as_of is None or page["asOf"] == as_of, "Insertion cutoff moved between pages"
        as_of = page["asOf"]
        assert len(page["items"]) <= int(query["pageSize"])
        if not items and first_hook:
            first_hook(page)
        items.extend(page["items"])
        if not page["hasMore"]:
            assert page["nextCursor"] is None
            break
        cursor = page["nextCursor"]
        assert isinstance(cursor, str) and cursor not in cursors, "Cursor did not advance"
        cursors.append(cursor)
        query["cursor"] = cursor
    keys = [row[key] for row in items]
    assert len(keys) == len(set(keys)), f"Duplicate key in {endpoint}"
    return items, cursors


def pagination(owner, other, admin, project, marker, fixture, owner_id):
    first_cursor = None
    inserted = str(uuid.uuid4())

    def after_first(page):
        nonlocal first_cursor
        first_cursor = page["nextCursor"]
        # Deliberately after the first asOf. This row is a metadata-only fixture.
        sql(project, "INSERT INTO files(id,user_id,name,format,size,chunk_size,total_chunks,status,created_at,upload_id,parse_status,parse_error) VALUES(" +
            ",".join(literal(v) for v in (inserted, owner_id, marker + "_after_asof.csv", "csv", 1, 262144, 1, "READY")) +
            ",UTC_TIMESTAMP(6)+INTERVAL 2 SECOND," + ",".join(literal(v) for v in (str(uuid.uuid4()), "FAILED", "PAGINATION_FIXTURE_NOT_EXECUTED")) + ");")

    files, _ = walk(owner, "/api/files/page", {"q": marker}, first_hook=after_first)
    assert [row["id"] for row in files] == sorted(fixture["files"])
    assert inserted not in {row["id"] for row in files}
    query = {"q": marker, "sort": "createdAt.asc", "pageSize": "73", "cursor": first_cursor}
    other.request("GET", page_path("/api/files/page", query), expected=(400, 403, 404))
    owner.request("GET", page_path("/api/files/page", {**query, "q": marker + "_different"}), expected=(400,))
    owner.request("GET", page_path("/api/files/page", {**query, "cursor": first_cursor[:-1] + ("A" if first_cursor[-1] != "A" else "B")}), expected=(400,))
    rules, _ = walk(owner, "/api/rule-sets/page", {"q": marker})
    assert [row["id"] for row in rules] == sorted(fixture["rules"])
    versions, _ = walk(owner, f"/api/rule-sets/{fixture['historyRule']}/versions/page", key="version")
    assert [row["version"] for row in versions] == list(range(1, len(fixture["files"]) + 1))
    tasks, task_cursors = walk(owner, "/api/tasks/page", {"q": marker, "status": "FAILED"})
    assert [row["id"] for row in tasks] == sorted(fixture["tasks"])
    history_task = fixture["historyTask"]
    history, _ = walk(owner, f"/api/tasks/{history_task}/attempts/page", key="attemptId")
    assert [row["attemptId"] for row in history] == sorted(fixture["history"])
    logs, _ = walk(owner, f"/api/tasks/{history_task}/logs/page")
    attempt_logs, _ = walk(owner, f"/api/tasks/{history_task}/attempts/{fixture['history'][-1]}/logs/page")
    assert len(logs) == len(attempt_logs) == fixture["logCount"]
    assert [row["id"] for row in logs] == sorted(row["id"] for row in logs)
    assert all("PAGINATION_FIXTURE_NOT_EXECUTED" in row["message"] for row in logs)
    other.request("GET", f"/api/tasks/{history_task}/attempts/page", expected=(403, 404))
    assert other.request("GET", page_path("/api/tasks/page", {"q": marker}))["items"] == []
    admin_tasks, _ = walk(admin, "/api/admin/tasks/page", {"q": marker, "ownerId": owner_id})
    assert {row["id"] for row in admin_tasks} == set(fixture["tasks"])
    users, _ = walk(admin, "/api/admin/users/page", {"q": marker, "enabled": "false"})
    assert len(users) == len(fixture["files"])
    owner.request("GET", "/api/admin/users/page", expected=(403,))
    # Status is deliberately live: the insertion cutoff does not freeze mutable state.
    unread = tasks[73]["id"]
    sql(project, "UPDATE tasks SET status='CANCELED' WHERE id=" + literal(unread) + ";")
    live = owner.request("GET", page_path("/api/tasks/page", {"q": marker, "status": "FAILED", "sort": "createdAt.asc", "pageSize": "73", "cursor": task_cursors[0]}))
    assert unread not in {row["id"] for row in live["items"]}
    print(f"PASS: real MySQL metadata fixtures, {len(files)} files/rules/tasks/users/versions/attempts and {len(logs)} logs; signed cursors, equal timestamps, asOf, live status and ownership.", flush=True)


def wait_minio(project):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = subprocess.run(["docker", "compose", "-p", project, "exec", "-T", "minio", "curl", "-fsS", "http://localhost:9000/minio/health/ready"], cwd=ROOT, capture_output=True, timeout=8)
        if result.returncode == 0:
            # MinIO liveness can precede a usable backend connection after a restart.
            # Validate object storage through the Java health indicator before retry.
            backend = subprocess.run(["docker", "compose", "-p", project, "exec", "-T", "backend", "curl", "-fsS", "http://localhost:8080/actuator/health"], cwd=ROOT, capture_output=True, timeout=15)
            if backend.returncode == 0 and json.loads(backend.stdout).get("status") == "UP":
                return
        time.sleep(2)
    raise AssertionError("Isolated MinIO did not recover")


def retention(owner, admin, project, allow_faults, timeout, owner_id):
    original = admin.request("GET", "/api/admin/retention/policy")
    assert original["enabled"] is False, "Start with retention disabled in the isolated project"
    owner.request("GET", "/api/admin/retention/policy", expected=(403,))
    saved = {key: value for key, value in original.items() if key != "protectedBefore"}
    saved["enabled"] = False
    active_task = None
    minio_stopped = False
    try:
        file_id = owner.upload("real-retention-input.csv", b"id,name\n1,Alice\n2,Bob\n")
        rule = owner.request("POST", "/api/rule-sets", {"name": "Real retention run", "rules": []})
        task = owner.request("POST", "/api/tasks", {"fileId": file_id, "ruleSetId": rule["id"], "ruleVersion": 1, "outputFormat": "csv", "engineVersion": "v3-sequential"})
        task = owner.wait(task["id"], timeout)
        assert task["status"] == "SUCCEEDED", task
        assert owner.request("GET", f"/api/tasks/{task['id']}/report")["inputRows"] == 2
        scope = {"fileIds": [file_id], "taskIds": [task["id"]], "includeOrphans": False}
        dry = admin.request("POST", "/api/admin/retention/plans", scope)
        assert dry["state"] == "DRY_RUN" and dry["items"]
        admin.request("POST", f"/api/admin/retention/plans/{dry['id']}/execute", expected=(409,))
        assert owner.request("GET", f"/api/tasks/{task['id']}/artifacts/result", raw=True)
        admin.request("PUT", "/api/admin/retention/policy", {"enabled": True, "confirmation": "ENABLE_RETENTION", "rawDays": 0, "resultDays": 0, "dirtyDays": 0, "reportDays": 0, "failedDays": 0, "logDays": 0, "graceHours": 0})
        plan = admin.request("POST", "/api/admin/retention/plans", scope)
        assert all(item["objectKey"] is None or (file_id in item["objectKey"] or task["id"] in item["objectKey"]) for item in plan["items"])
        assert any(item["state"] == "PLANNED" for item in plan["items"]), plan
        done = admin.request("POST", f"/api/admin/retention/plans/{plan['id']}/execute")
        assert done["state"] == "COMPLETED" and any(item["state"] == "DELETED" for item in done["items"]), done
        owner.request("GET", f"/api/tasks/{task['id']}/artifacts/result", expected=(410,))
        owner.request("GET", f"/api/files/{file_id}/preview", expected=(410,))
        assert owner.request("GET", f"/api/tasks/{task['id']}")["artifactStatus"]["result"] == "EXPIRED"
        assert owner.request("GET", f"/api/tasks/{task['id']}/report")["inputRows"] == 2, "Summary metadata must survive artifact expiry"
        admin.request("POST", f"/api/admin/retention/plans/{plan['id']}/execute")

        protected_file = owner.upload("active-reference-input.csv", b"id,name\n1,Active\n")
        active_task, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
        sql(project, "START TRANSACTION; INSERT INTO tasks(id,user_id,file_id,rule_set_id,rule_version,output_format,status,progress,attempt,error,created_at,updated_at) VALUES(" +
            ",".join(literal(v) for v in (active_task, owner_id, protected_file, rule["id"], 1, "csv", "QUEUED", 0, 1, "ACTIVE_REFERENCE_FIXTURE_NOT_SUBMITTED")) + ",UTC_TIMESTAMP(6),UTC_TIMESTAMP(6));" +
            "INSERT INTO task_attempts(attempt_id,task_id,attempt_no,status,progress,created_at,updated_at) VALUES(" + ",".join(literal(v) for v in (attempt_id, active_task, 1, "QUEUED", 0)) + ",UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)); COMMIT;")
        protected = admin.request("POST", "/api/admin/retention/plans", {"fileIds": [protected_file], "includeOrphans": False})
        assert any(item["reason"] == "ACTIVE_INPUT_REFERENCE" for item in protected["items"]), protected
        admin.request("POST", f"/api/admin/retention/plans/{protected['id']}/execute")
        assert owner.request("GET", f"/api/files/{protected_file}/preview")["previewRowCount"] == 1
        print("PASS: default dry-run, explicit scoped deletion, repeated execution, HTTP410/expiry badges, retained summary and active raw reference protection.", flush=True)

        if allow_faults:
            fault_file = owner.upload("partial-delete-own-input.csv", b"id\n1\n")
            fault_plan = admin.request("POST", "/api/admin/retention/plans", {"fileIds": [fault_file], "includeOrphans": False})
            assert any(item["state"] == "PLANNED" for item in fault_plan["items"]), fault_plan
            print(f"FAULT START: stopping only {project}/minio; main project is untouched.", flush=True)
            compose(project, "stop", "minio");minio_stopped = True
            partial = admin.request("POST", f"/api/admin/retention/plans/{fault_plan['id']}/execute")
            assert partial["state"] == "PARTIAL_FAILURE" and any(item["state"] == "FAILED" for item in partial["items"]), partial
            compose(project, "start", "minio");minio_stopped = False;wait_minio(project)
            recovered = admin.request("POST", f"/api/admin/retention/plans/{fault_plan['id']}/retry")
            assert recovered["state"] == "COMPLETED" and any(item["state"] == "DELETED" for item in recovered["items"]), recovered
            owner.request("GET", f"/api/files/{fault_file}/preview", expected=(410,))
            print("PASS: actual isolated MinIO outage leaves audited PARTIAL_FAILURE/DELETING tombstone; service recovery and /retry finish deletion.", flush=True)
        else:
            print("SKIP: MinIO outage requires --allow-faults and orchestration by the isolated-stack owner.", flush=True)
        audit, _ = walk(admin, "/api/admin/retention/runs/page", {"planId": plan["id"], "pageSize": "2"})
        assert audit and all(row["planId"] == plan["id"] for row in audit)
        assert walk(admin, "/api/admin/retention/plans/page", {"pageSize": "2"})[0]
    finally:
        try:
            if minio_stopped:
                compose(project, "start", "minio");wait_minio(project)
        finally:
            try:
                if active_task:
                    owner.request("POST", f"/api/tasks/{active_task}/cancel")
            finally:
                admin.request("PUT", "/api/admin/retention/policy", saved)
                assert admin.request("GET", "/api/admin/retention/policy")["enabled"] is False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8089")
    parser.add_argument("--project", default="data-cleaning-pipeline-v3-next")
    parser.add_argument("--fixture-count", type=int, default=507)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--allow-faults", action="store_true")
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.base_url)
    assert parsed.hostname in {"localhost", "127.0.0.1"} and parsed.port != 8088, "Use the isolated local port; main 8088 is forbidden"
    assert args.project in {"data-cleaning-pipeline-v3-next", "data-cleaning-pipeline-v3-fresh"} or args.project.startswith("data-cleaning-pipeline-v3-admin-"), "Only an explicitly isolated acceptance project is permitted"
    assert 501 <= args.fixture_count <= 1500
    values = env_file()
    admin_auth = Api(args.base_url).request("POST", "/api/auth/login", {"username": values["BOOTSTRAP_ADMIN_USERNAME"], "password": values["BOOTSTRAP_ADMIN_PASSWORD"]})
    admin = Api(args.base_url, admin_auth["token"])
    assert admin_auth["user"]["role"] == "ADMIN"
    suffix = uuid.uuid4().hex[:10]
    auth = Api(args.base_url).request("POST", "/api/auth/register", {"username": "v3admin_owner_" + suffix, "password": uuid.uuid4().hex})
    other_auth = Api(args.base_url).request("POST", "/api/auth/register", {"username": "v3admin_other_" + suffix, "password": uuid.uuid4().hex})
    owner, other = Api(args.base_url, auth["token"]), Api(args.base_url, other_auth["token"])
    marker = "pg_fixture_" + suffix
    fixture = fixture_metadata(args.project, auth["user"]["id"], marker, args.fixture_count)
    pagination(owner, other, admin, args.project, marker, fixture, auth["user"]["id"])
    retention(owner, admin, args.project, args.allow_faults, args.timeout, auth["user"]["id"])
    print("PASS: isolated admin acceptance complete. Metadata fixture owner=" + auth["user"]["id"] + "; no passwords/tokens were logged.")


if __name__ == "__main__":
    main()
