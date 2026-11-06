"""Real local queue duplicate, cooperative cancellation and stale callback checks.

Runs against the dedicated local Compose stack. Internal calls execute inside
python-api with its service credential; no credential is printed or returned.
"""
from __future__ import annotations

import json
import argparse
import subprocess
import time
import uuid
from pathlib import Path

from smoke_test import Client
from worker_recovery_test import wait_for_running
from local_fault_scope import DEFAULT_PROJECT, add_scope_arguments, compose_command

ROOT = Path(__file__).resolve().parents[1]


def engine_call(code: str, payload: dict, *, project: str = DEFAULT_PROJECT):
    result = subprocess.run(
        compose_command(project,"exec", "-T", "python-api", "python", "-c", code),
        cwd=ROOT, input=json.dumps(payload), text=True, encoding="utf-8",
        capture_output=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    return json.loads(result.stdout) if result.stdout.strip() else None


def callback(task, status, *, project: str = DEFAULT_PROJECT):
    return engine_call("""import json,os,sys,httpx
p=json.load(sys.stdin)
response=httpx.post(os.environ['JAVA_API_URL']+'/internal/tasks/'+p.pop('taskId')+'/events',json=p,headers={'X-Internal-Token':os.environ['INTERNAL_API_TOKEN']},timeout=10)
response.raise_for_status()
print(response.text)
""", {"taskId": task["id"], "attempt": task["attempt"], "attemptId": task["attemptId"],
       "leaseToken": "obsolete-test-lease", "status": status, "progress": 100, "message": "Late callback regression"},project=project)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    add_scope_arguments(parser)
    args=parser.parse_args()
    configuration=subprocess.run(compose_command(args.project,"config","--format","json"),cwd=ROOT,capture_output=True,text=True,encoding="utf-8",timeout=45)
    assert configuration.returncode==0,configuration.stderr[-1500:]
    assert json.loads(configuration.stdout).get("name")==args.project,"Unexpected Compose project"
    print(f"Local reliability target: {args.base_url}; docker compose -p {args.project}",flush=True)
    auth = Client(args.base_url).request("POST", "/api/auth/register", {
        "username": "v3_fault_" + uuid.uuid4().hex[:12], "password": uuid.uuid4().hex,
    })
    owner = Client(args.base_url, auth["token"])
    small_id = owner.upload("duplicate-message.csv", b"id,name\n001, Alice \n002,Bob\n")
    simple_rule = owner.request("POST", "/api/rule-sets", {
        "name": "Duplicate message check", "rules": [{"type": "trim", "columns": ["name"]}],
    })
    task = owner.request("POST", "/api/tasks", {
        "fileId": small_id, "ruleSetId": simple_rule["id"], "ruleVersion": 1, "outputFormat": "csv",
    })
    task = owner.wait(task["id"], 180)
    assert task["status"] == "SUCCEEDED", task
    task = owner.request("GET", f"/api/tasks/{task['id']}")
    original_report = owner.request("GET", f"/api/tasks/{task['id']}/report")
    payload = {"taskId": task["id"], "attempt": task["attempt"], "attemptId": task["attemptId"],
               "engineVersion": task["engineVersion"], "input": task["inputSnapshot"],
               "rules": task["rulesSnapshot"], "limits": task["limitsSnapshot"], "outputFormat": task["outputFormat"]}
    engine_call("""import json,os,sys,pika
p=json.load(sys.stdin)
c=pika.BlockingConnection(pika.ConnectionParameters(host=os.environ['RABBITMQ_HOST'],credentials=pika.PlainCredentials(os.environ['RABBITMQ_USER'],os.environ['RABBITMQ_PASSWORD'])))
ch=c.channel()
for _ in range(2): ch.basic_publish(exchange='cleaning',routing_key='task',body=json.dumps(p).encode(),properties=pika.BasicProperties(delivery_mode=2,content_type='application/json'))
c.close()
""", payload,project=args.project)
    callback(task, "SUCCEEDED",project=args.project)
    callback(task, "SUCCEEDED",project=args.project)
    time.sleep(4)
    assert owner.request("GET", f"/api/tasks/{task['id']}/report") == original_report
    assert len(owner.request("GET", f"/api/tasks/{task['id']}/attempts")) == 1
    print("PASS: duplicate real RabbitMQ deliveries and duplicate terminal callbacks do not create another valid attempt/result.", flush=True)

    source = ("id,payload\n" + "".join(f"{i},  MixedCase-Data-Payload-0123456789  \n" for i in range(100_000))).encode()
    file_id = owner.upload("running-cancel.csv", source)
    rules = [{"type": "case", "columns": ["payload"], "mode": "upper" if i % 2 else "lower"} for i in range(40)]
    rule = owner.request("POST", "/api/rule-sets", {"name": "Cooperative cancel", "rules": rules})
    active = owner.request("POST", "/api/tasks", {"fileId": file_id, "ruleSetId": rule["id"], "ruleVersion": 1, "outputFormat": "csv"})
    wait_for_running(owner, active["id"])
    canceled = owner.request("POST", f"/api/tasks/{active['id']}/cancel")
    assert canceled["status"] == "CANCELING", canceled
    stopped = owner.wait(active["id"], 120)
    assert stopped["status"] == "CANCELED", stopped
    callback(active, "SUCCEEDED",project=args.project)
    assert owner.request("GET", f"/api/tasks/{active['id']}")["status"] == "CANCELED"
    owner.request("GET", f"/api/tasks/{active['id']}/artifacts/result", expected=(409,))
    # Start a new attempt, then deliver an obsolete callback for the first UUID.
    retried = owner.request("POST", f"/api/tasks/{active['id']}/retry")
    assert retried["attemptId"] != active["attemptId"] and retried["attempt"] == 2
    callback(active, "FAILED",project=args.project)
    assert owner.request("GET", f"/api/tasks/{active['id']}")["attemptId"] == retried["attemptId"]
    owner.request("POST", f"/api/tasks/{active['id']}/cancel")
    final = owner.wait(active["id"], 120)
    assert final["status"] == "CANCELED", final
    history = owner.request("GET", f"/api/tasks/{active['id']}/attempts")
    assert len(history) == 2 and all(item["status"] == "CANCELED" for item in history), history
    print("PASS: real RUNNING cancellation confirms stopped executor; late success and old-attempt callbacks cannot replace cancellation or a new retry.", flush=True)


if __name__ == "__main__":
    main()
