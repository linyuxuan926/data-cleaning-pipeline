"""Fault-injection acceptance test against the dedicated local Compose project.

Temporarily stops RabbitMQ / all Worker replicas. Do not run on a shared deployment.
Restores both services in finally. Uses real APIs, queue and object storage.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

from smoke_test import Client
from local_fault_scope import DEFAULT_PROJECT, add_scope_arguments, compose_command

ROOT = Path(__file__).resolve().parents[1]


def compose(*args, project=DEFAULT_PROJECT):
    process = subprocess.run(compose_command(project,*args), cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert process.returncode == 0, process.stdout + process.stderr
    return process.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_scope_arguments(parser)
    args = parser.parse_args()
    def selected_compose(*arguments): return compose(*arguments,project=args.project)
    assert json.loads(selected_compose("config", "--format", "json")).get("name") == args.project, "Unexpected Compose project"
    print(f"Local fault target: {args.base_url}; docker compose -p {args.project}",flush=True)
    suffix = uuid.uuid4().hex[:12]
    auth = Client(args.base_url).request("POST", "/api/auth/register", {
        "username": "fault_" + suffix, "password": uuid.uuid4().hex,
    })
    client = Client(args.base_url, auth["token"])
    file_id = client.upload("customers.csv", (ROOT / "samples/customers.csv").read_bytes())
    rule = client.request("POST", "/api/rule-sets", {
        "name": "Fault acceptance", "rules": json.loads((ROOT / "samples/rules.json").read_text()),
    })
    body = {"fileId": file_id, "ruleSetId": rule["id"], "ruleVersion": 1, "outputFormat": "csv"}
    try:
        print("Checking transactional outbox while RabbitMQ is stopped", flush=True)
        selected_compose("stop", "worker")
        selected_compose("stop", "rabbitmq")
        queued = client.request("POST", "/api/tasks", body)
        time.sleep(4)
        assert client.request("GET", f"/api/tasks/{queued['id']}")["status"] == "QUEUED"
        selected_compose("start", "rabbitmq")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            check = subprocess.run(compose_command(args.project,"exec", "-T", "rabbitmq", "rabbitmq-diagnostics", "-q", "ping"),
                                   cwd=ROOT, capture_output=True, timeout=15)
            if check.returncode == 0:
                break
            time.sleep(2)
        else:
            raise AssertionError("RabbitMQ failed to restart")
        selected_compose("start", "worker")
        assert client.wait(queued["id"], 180)["status"] == "SUCCEEDED"

        print("Checking queued cancellation and per-user concurrent submission limit", flush=True)
        selected_compose("stop", "worker")
        cancelled = client.request("POST", "/api/tasks", body)
        cancelled = client.request("POST", f"/api/tasks/{cancelled['id']}/cancel")
        assert cancelled["status"] == "CANCELED"
        active = [client.request("POST", "/api/tasks", body) for _ in range(3)]
        client.request("POST", "/api/tasks", body, expected=(429,))
        for task in active:
            client.request("POST", f"/api/tasks/{task['id']}/cancel")
        selected_compose("start", "worker")
        time.sleep(5)
        for task in [cancelled, *active]:
            assert client.request("GET", f"/api/tasks/{task['id']}")["status"] == "CANCELED"
            client.request("GET", f"/api/tasks/{task['id']}/artifacts/result", expected=(409,))
        print("PASS: broker outage recovery, durable submission, queued cancellation and concurrent task limits.")
    finally:
        selected_compose("start", "rabbitmq", "worker")


if __name__ == "__main__":
    main()
