"""Exercise real schema3 event replay/order/hash fencing on a local Compose stack.

Stops only the selected project's Worker, creates and manually claims a real
queued task, then calls Java's protected event API from python-api. No cleaning
executor is started for this controlled attempt. Restarts Worker in finally.
Run serially with other fault tests; this script never reads/prints credentials.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import uuid

from acceptance_v3_reliability import engine_call
from local_fault_scope import add_scope_arguments, compose_command
from smoke_test import Client

ROOT=Path(__file__).resolve().parents[1]


def compose(project: str,*arguments: str):
    result=subprocess.run(compose_command(project,*arguments),cwd=ROOT,
                          capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=120)
    assert result.returncode==0,"Compose command failed: "+result.stderr[-1500:]
    return result.stdout


def internal_request(project: str,method: str,path: str,body: dict,expected=200):
    result=engine_call("""import json,os,sys,httpx
p=json.load(sys.stdin)
r=httpx.request(p['method'],os.environ['JAVA_API_URL']+p['path'],json=p['body'],headers={'X-Internal-Token':os.environ['INTERNAL_API_TOKEN']},timeout=10)
assert r.status_code==p['expected'],f"Internal API returned {r.status_code}: {r.text[:1000]}"
print(json.dumps({'status':r.status_code,'body':r.json()}))
""",{"method":method,"path":path,"body":body,"expected":expected},project=project)
    assert result["status"]==expected
    return result["body"]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    add_scope_arguments(parser)
    args=parser.parse_args()
    assert json.loads(compose(args.project,"config","--format","json")).get("name")==args.project,"Unexpected Compose project"
    print(f"Local event ledger target: {args.base_url}; docker compose -p {args.project}",flush=True)
    auth=Client(args.base_url).request("POST","/api/auth/register",{
        "username":"ledger_"+uuid.uuid4().hex[:12],"password":uuid.uuid4().hex})
    owner=Client(args.base_url,auth["token"])
    file_id=owner.upload("event-ledger.csv",b"id,name\n001, Alice \n002,Bob\n")
    rules=owner.request("POST","/api/rule-sets",{
        "name":"Real schema3 event ledger acceptance","schemaVersion":3,"engineVersion":"v3-disk",
        "logicalFields":[{"id":"name","label":"Name"}],
        "rules":[{"id":"trim-name","type":"trim","enabled":True,"fieldIds":["name"]}]})
    task=lease=None
    summary=None
    checks=[]
    try:
        compose(args.project,"stop","worker")
        task=owner.request("POST","/api/tasks",{
            "fileId":file_id,"ruleSetId":rules["id"],"ruleVersion":rules["version"],
            "schemaVersion":3,"engineVersion":"v3-disk","fieldBindings":{"name":"f1"},"outputFormat":"csv"})
        assert task["status"]=="QUEUED" and task["engineVersion"]=="v3-disk",task
        path=f"/internal/tasks/{task['id']}/events"
        claimed=internal_request(args.project,"POST",f"/internal/tasks/{task['id']}/claim",{
            "attempt":task["attempt"],"attemptId":task["attemptId"]})
        assert claimed["execute"] is True
        lease=claimed["leaseToken"]
        identity={"attempt":task["attempt"],"attemptId":task["attemptId"],"leaseToken":lease}
        seq2={**identity,"eventId":str(uuid.uuid4()),"eventSeq":2,"status":"RUNNING","progress":20,
              "message":"event-ledger-seq2","stage":"LEDGER_TEST","stageProcessedRows":1,"totalRows":2}

        def event(payload,expected=200):
            response=internal_request(args.project,"POST",path,payload,expected)
            if expected==200: assert response=={"accepted":True},response
            return response

        def current_attempt():
            return owner.request("GET",f"/api/tasks/{task['id']}/attempts/{task['attemptId']}")

        event(seq2)
        first=current_attempt()
        assert first["status"]=="RUNNING" and first["progress"]==20 and first["lastEventSeq"]==2,first
        event(dict(seq2)); event(dict(reversed(list(seq2.items()))))
        assert current_attempt()["lastEventSeq"]==2
        messages=owner.request("GET",f"/api/tasks/{task['id']}/attempts/{task['attemptId']}/logs?after=0")
        assert sum(log["message"]=="event-ledger-seq2" for log in messages)==1,messages
        checks.append("exact event replay, including JSON key reordering, accepted once")

        seq1={**seq2,"eventId":str(uuid.uuid4()),"eventSeq":1,"progress":99,"message":"out-of-order must be ignored"}
        event(seq1)
        after=current_attempt()
        assert after["lastEventSeq"]==2 and after["progress"]==20,after
        checks.append("lower eventSeq acknowledged without progress or sequence regression")

        changed={**seq2,"progress":30,"message":"same event ID with changed canonical payload"}
        conflict=event(changed,409)
        assert "EVENT_PAYLOAD_CONFLICT" in conflict["message"],conflict
        other_id={**seq2,"eventId":str(uuid.uuid4())}
        conflict=event(other_id,409)
        assert "EVENT_PAYLOAD_CONFLICT" in conflict["message"],conflict
        assert current_attempt()["progress"]==20
        checks.append("changed payload or reused attempt sequence with another eventId returns409")

        cancel=owner.request("POST",f"/api/tasks/{task['id']}/cancel")
        assert cancel["status"]=="CANCELING",cancel
        event({**seq2,"eventId":str(uuid.uuid4()),"eventSeq":3,"progress":90,"message":"late running after cancellation"})
        event({**seq2,"eventId":str(uuid.uuid4()),"eventSeq":4,"status":"SUCCEEDED","progress":100,"message":"late success after cancellation"})
        assert owner.request("GET",f"/api/tasks/{task['id']}")["status"]=="CANCELING"
        event({**seq2,"eventId":str(uuid.uuid4()),"eventSeq":5,"status":"CANCELED","progress":20,
               "message":"Controlled claim had no executor; ledger acceptance completed cancellation","errorCode":"USER_CANCELED"})
        final=current_attempt()
        assert final["status"]=="CANCELED" and final["lastEventSeq"]==5,final
        event({**seq2,"eventId":str(uuid.uuid4()),"eventSeq":6,"status":"SUCCEEDED","progress":100,"message":"terminal regression must be ignored"})
        assert owner.request("GET",f"/api/tasks/{task['id']}")["status"]=="CANCELED"
        owner.request("GET",f"/api/tasks/{task['id']}/report",expected=(409,))
        owner.request("GET",f"/api/tasks/{task['id']}/artifacts/result",expected=(409,))
        assert len(owner.request("GET",f"/api/tasks/{task['id']}/attempts"))==1
        checks.append("cancellation and terminal state fence late RUNNING/SUCCEEDED; no artifacts exposed")
        summary={"status":"PASS","baseUrl":args.base_url,"project":args.project,
                 "taskId":task["id"],"attemptId":task["attemptId"],"checks":checks}
    finally:
        try:
            if task is not None:
                state=owner.request("GET",f"/api/tasks/{task['id']}")
                if state["status"] in {"QUEUED","RUNNING"}:
                    state=owner.request("POST",f"/api/tasks/{task['id']}/cancel")
                if state["status"]=="CANCELING" and lease:
                    internal_request(args.project,"POST",f"/internal/tasks/{task['id']}/events",{
                        "attempt":task["attempt"],"attemptId":task["attemptId"],"leaseToken":lease,
                        "eventId":str(uuid.uuid4()),"eventSeq":100,"status":"CANCELED","progress":20,
                        "message":"Controlled event acceptance cleanup; no executor was started","errorCode":"USER_CANCELED","stage":"LEDGER_TEST"})
        finally:
            compose(args.project,"start","worker")
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=="__main__": main()
