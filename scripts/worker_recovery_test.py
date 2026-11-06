"""Kill a real RUNNING Worker and verify lease recovery in the isolated local Compose project.

Run only against your own dedicated data-cleaning-pipeline deployment. This test
sends SIGKILL to every Worker replica and restarts them in finally. Never run it
concurrently with another acceptance test or against a shared deployment.

Uses real HTTP, Docker CLI, RabbitMQ, MySQL and MinIO. Does not edit database
state, shorten the 120-second lease or call protected internal execution APIs.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import subprocess
import time
import uuid

from smoke_test import Client
from local_fault_scope import DEFAULT_PROJECT, add_scope_arguments, compose_command

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"}


def compose(*arguments: str, timeout: int = 45, project: str = DEFAULT_PROJECT) -> str:
    process = subprocess.run(
        compose_command(project,*arguments), cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if process.returncode:
        raise AssertionError(f"docker compose {' '.join(arguments)} failed:\n{process.stdout}{process.stderr}")
    return process.stdout


def bounded_int(low: int, high: int):
    def parse(value: str) -> int:
        parsed = int(value)
        if not low <= parsed <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return parsed
    return parse


def wait_for_running(client: Client, task_id: str, timeout: int = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.request("GET", f"/api/tasks/{task_id}")
        if task["status"] == "RUNNING":
            return task
        if task["status"] in TERMINAL:
            raise AssertionError(
                f"Task reached {task['status']} before RUNNING could be observed; "
                "no Worker fault was injected. Increase --rows/--rule-count and rerun."
            )
        time.sleep(0.1)
    raise AssertionError("Task never entered RUNNING within 60 seconds; no Worker fault was injected")


def wait_for_recovery(client: Client, task_id: str, previous_attempt: int, deadline: float) -> dict:
    previous_state = None
    while time.monotonic() < deadline:
        task = client.request("GET", f"/api/tasks/{task_id}")
        state = (task["attempt"], task["status"], task["progress"])
        if state != previous_state:
            print(f"  attempt={state[0]} status={state[1]} progress={state[2]}%", flush=True)
            previous_state = state
        assert task["attempt"] in {previous_attempt, previous_attempt + 1}, task
        if task["status"] in {"FAILED", "CANCELED", "CANCELLED"}:
            raise AssertionError(f"Recovery failed: {task}")
        if task["status"] == "SUCCEEDED":
            assert task["attempt"] == previous_attempt + 1, (
                "The killed attempt completed without lease recovery; the fault was not validated", task
            )
            return task
        if task["attempt"] > previous_attempt and task["status"] == "QUEUED":
            client.request("GET", f"/api/tasks/{task_id}/report", expected=(409,))
            client.request("GET", f"/api/tasks/{task_id}/artifacts/report", expected=(409,))
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise AssertionError("Worker lease recovery did not finish within the configured <=240-second deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_scope_arguments(parser)
    parser.add_argument("--rows", type=bounded_int(50_000, 250_000), default=200_000,
                        help="CSV data rows; default 200000 keeps the task running long enough to kill")
    parser.add_argument("--rule-count", type=bounded_int(10, 50), default=32,
                        help="alternating trim/case rules; default 32")
    parser.add_argument("--timeout", type=bounded_int(180, 240), default=240,
                        help="maximum recovery wait from SIGKILL, seconds; default/max 240")
    args = parser.parse_args()
    def selected_compose(*arguments,**options): return compose(*arguments,project=args.project,**options)
    configuration = json.loads(selected_compose("config", "--format", "json"))
    assert configuration.get("name") == args.project, "Unexpected Compose project; refusing to kill workers"
    assert selected_compose("ps", "--status", "running", "-q", "worker").strip(), "Start the Worker before running this test"
    print(f"Local fault target: {args.base_url}; docker compose -p {args.project}",flush=True)
    print("Dedicated local fault test: all Worker replicas will receive SIGKILL and be restarted.", flush=True)

    suffix = uuid.uuid4().hex[:12]
    auth = Client(args.base_url).request("POST", "/api/auth/register", {
        "username": "lease_" + suffix, "password": uuid.uuid4().hex,
    })
    client = Client(args.base_url, auth["token"])
    payload = "  MixedCase-Recovery-Payload-0123456789 " * 3
    source = io.BytesIO()
    source.write(b"id,payload\n")
    for index in range(args.rows):
        source.write(f"{index},{payload}\n".encode("utf-8"))
    file_id = client.upload("worker-recovery.csv", source.getvalue())
    source.close()
    rules = []
    final_payload = payload
    for index in range(args.rule_count):
        if index % 2 == 0:
            rules.append({"type": "trim", "columns": ["payload"]})
            final_payload = final_payload.strip()
        else:
            mode = "upper" if (index // 2) % 2 == 0 else "lower"
            rules.append({"type": "case", "columns": ["payload"], "mode": mode})
            final_payload = getattr(final_payload, mode)()
    rule_set = client.request("POST", "/api/rule-sets", {"name": "Worker lease recovery", "rules": rules})
    task = client.request("POST", "/api/tasks", {
        "fileId": file_id, "ruleSetId": rule_set["id"], "ruleVersion": rule_set["version"], "outputFormat": "csv",
    })
    task_id = task["id"]
    workers_may_be_stopped = False
    try:
        running = wait_for_running(client, task_id)
        previous_attempt = running["attempt"]
        assert previous_attempt == 1, running
        print(f"Observed real RUNNING task {task_id}, attempt {previous_attempt}; injecting SIGKILL.", flush=True)
        workers_may_be_stopped = True
        killed_at = time.monotonic()
        selected_compose("kill", "-s", "SIGKILL", "worker")
        interrupted = client.request("GET", f"/api/tasks/{task_id}")
        assert interrupted["status"] == "RUNNING" and interrupted["attempt"] == previous_attempt, (
            "The task finished or changed before SIGKILL took effect; no recovery PASS can be claimed", interrupted
        )
        # An interrupted/partial attempt must never expose public result artifacts.
        client.request("GET", f"/api/tasks/{task_id}/report", expected=(409,))
        for kind in ("result", "dirty", "report"):
            client.request("GET", f"/api/tasks/{task_id}/artifacts/{kind}", expected=(409,))
        selected_compose("start", "worker")
        print("Worker restarted; waiting for the unchanged 120-second lease and recovery scheduler.", flush=True)
        recovered = wait_for_recovery(client, task_id, previous_attempt, killed_at + args.timeout)
        assert recovered["progress"] == 100 and recovered["ruleVersion"] == rule_set["version"], recovered
        report = client.request("GET", f"/api/tasks/{task_id}/report")
        downloaded_report = json.loads(client.request("GET", f"/api/tasks/{task_id}/artifacts/report", raw=True))
        assert report == downloaded_report, "Database report and authenticated report artifact disagree"
        assert report["taskId"] == task_id and report["attempt"] == previous_attempt + 1, report
        assert (report["inputRows"], report["outputRows"], report["dirtyRows"]) == (args.rows, args.rows, 0), report
        assert len(report["rules"]) == args.rule_count, report
        # Public download routes select the current successful attempt. Supplying
        # an obsolete attempt does not select or disclose the interrupted report.
        obsolete_selector = json.loads(client.request(
            "GET", f"/api/tasks/{task_id}/artifacts/report?attempt={previous_attempt}", raw=True,
        ))
        assert obsolete_selector == report and obsolete_selector["attempt"] != previous_attempt
        result = client.request("GET", f"/api/tasks/{task_id}/artifacts/result", raw=True)
        rows = csv.DictReader(io.StringIO(result.decode("utf-8-sig")))
        count = 0
        for count, row in enumerate(rows, 1):
            assert row == {"id": str(count - 1), "payload": final_payload}, (count, row)
        assert count == args.rows, count
        dirty = client.request("GET", f"/api/tasks/{task_id}/artifacts/dirty", raw=True)
        assert not list(csv.DictReader(io.StringIO(dirty.decode("utf-8-sig"))))
        logs = client.request("GET", f"/api/tasks/{task_id}/logs?after=0")
        assert any("心跳超时" in log["message"] for log in logs), "No persisted lease-expiry recovery log"
        elapsed = time.monotonic() - killed_at
        print(f"PASS: SIGKILL during RUNNING -> expired lease -> attempt {report['attempt']} SUCCEEDED; "
              f"latest report/downloads validated ({args.rows} rows, {elapsed:.1f}s after kill).", flush=True)
    finally:
        if workers_may_be_stopped:
            selected_compose("start", "worker")


if __name__ == "__main__":
    main()
