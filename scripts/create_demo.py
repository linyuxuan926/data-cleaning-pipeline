"""Create a local-only review account and real three-format sample tasks."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
import uuid
from smoke_test import Client

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8088")
    args = parser.parse_args()
    if urlparse(args.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Sample-account provisioning is limited to localhost")
    target = ROOT / ".test-output/local-demo.json"
    anonymous = Client(args.base_url)
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        try:
            anonymous.request("POST", "/api/auth/login", {"username": existing["username"], "password": existing["password"]})
            print(f"Existing local review account: {existing['username']}; credentials and tasks: {target}")
            return
        except AssertionError:
            pass
    credentials = {"username": "local_demo_"+uuid.uuid4().hex[:8], "password": "Local_"+uuid.uuid4().hex}
    auth = anonymous.request("POST", "/api/auth/register", credentials)
    owner = Client(args.base_url,auth["token"])
    rule = owner.request("POST", "/api/rule-sets", {"name": "客户清洗验收规则", "rules": json.loads((ROOT/"samples/rules.json").read_text(encoding="utf-8"))})
    manifest = {**credentials, "baseUrl": args.base_url, "ruleSetId": rule["id"], "files": [], "tasks": []}
    for fmt in ("csv","xlsx","parquet"):
        file_id = owner.upload("customers."+fmt,(ROOT/f"samples/customers.{fmt}").read_bytes())
        task = owner.request("POST", "/api/tasks", {"fileId": file_id, "ruleSetId": rule["id"], "ruleVersion": 1, "outputFormat": "csv"})
        task = owner.wait(task["id"],180)
        assert task["status"] == "SUCCEEDED", task
        report=owner.request("GET",f"/api/tasks/{task['id']}/report")
        assert (report["inputRows"],report["outputRows"],report["finalRemovedRows"]) == (6,3,3), report
        manifest["files"].append({"format":fmt,"id":file_id})
        manifest["tasks"].append({"format":fmt,"id":task["id"],"attemptId":task["attemptId"]})
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Local review account: {credentials['username']}; credentials and real sample tasks: {target}")


if __name__ == "__main__":
    main()
