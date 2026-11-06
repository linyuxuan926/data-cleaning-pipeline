"""Real three-format schema3/disk pipeline, binding preflight and frozen retry semantics."""
from __future__ import annotations
import argparse,csv,io,json,uuid
from pathlib import Path
from acceptance_v3 import HeaderClient

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://localhost:8088')
    args=parser.parse_args()
    anonymous=HeaderClient(args.base_url)
    auth=anonymous.request('POST','/api/auth/register',{'username':'disk_'+uuid.uuid4().hex[:12],'password':uuid.uuid4().hex})
    owner=HeaderClient(args.base_url,auth['token'])
    config=json.loads((ROOT/'docs/v3/examples/ordered-rules.json').read_text(encoding='utf-8'))
    payload={'name':'三格式磁盘规则验收','schemaVersion':3,'engineVersion':'v3-disk','logicalFields':config['logicalFields'],'rules':config['rules']}
    rule=owner.request('POST','/api/rule-sets',payload)
    bindings=json.loads((ROOT/'docs/v3/examples/fixture.json').read_text(encoding='utf-8'))['fieldBindings']
    evidence=[]
    for fmt in ('csv','xlsx','parquet'):
        file=owner.upload('v3-disk-orders.'+fmt,(ROOT/f'samples/v3-disk-orders.{fmt}').read_bytes())
        detail=owner.request('GET',f'/api/files/{file}')
        preflight={'fileId':file,'ruleSetId':rule['id'],'ruleVersion':1,'schemaVersion':3,'fieldBindings':bindings,'parseVersion':detail['parseVersion']}
        checked=owner.request('POST','/api/rules/preflight',preflight)
        assert checked['valid'],checked
        missing={**bindings};missing.pop('amount')
        owner.request('POST','/api/tasks',{**preflight,'fieldBindings':missing,'outputFormat':'csv'},expected=(400,422))
        original=owner.request('POST','/api/tasks',{**preflight,'outputFormat':'csv'})
        task=owner.wait(original['id'],240)
        assert task['status']=='SUCCEEDED' and task['engineVersion']=='v3-disk',task
        report=owner.request('GET',f'/api/tasks/{task["id"]}/report')
        assert (report['inputRows'],report['outputRows'],report['validOutputRows'],report['flaggedOutputRows'],report['finalRemovedRows'])==(8,4,3,1,4),report
        assert report['missingFilledCells']==1 and report['typeConversionFailedCells']==1
        assert report['duplicateRemovedRows']==1 and report['outlierHandledRows']==1 and report['issueCount']==5
        assert report['finalRemovalByRule']=={'r02-cast-amount':1,'r04-range':1,'r06-dates-order':1,'r08-dedup':1}
        rows=list(csv.DictReader(io.StringIO(owner.request('GET',f'/api/tasks/{task["id"]}/artifacts/result',raw=True).decode('utf-8-sig'))))
        assert [r['order_id'] for r in rows]==['002','001','006','007'],rows
        assert [r['amount'] for r in rows]==['185.33','12.00','20.00','40.00'],rows
        dirty=list(csv.DictReader(io.StringIO(owner.request('GET',f'/api/tasks/{task["id"]}/artifacts/dirty',raw=True).decode('utf-8-sig'))))
        assert len(dirty)==5 and {int(r['_source_row']) for r in dirty}=={1,3,4,6,7},dirty
        assert all(r['_file_id']==file and r['_attempt_id']==task['attemptId'] and r['_record_id'] for r in dirty)
        if fmt=='xlsx':assert all(r['_sheet']=='订单' for r in dirty)
        saved=json.loads(owner.request('GET',f'/api/tasks/{task["id"]}/artifacts/report',raw=True))
        assert saved==report
        assert owner.request('GET',f'/api/tasks/{task["id"]}/preview?kind=result&limit=100')
        assert owner.request('GET',f'/api/tasks/{task["id"]}/preview?kind=dirty&limit=100')
        evidence.append({'inputFormat':fmt,'fileId':file,'taskId':task['id'],'attemptId':task['attemptId'],'report':report})
        print(f'PASS: real {fmt} disk input -> CSV, 8=4+4, exact mean/Decimal, dates/field comparison, global keep-last, source trace.',flush=True)
    # Update the rule set; already submitted task snapshots retain original declarations.
    changed=owner.request('PUT',f'/api/rule-sets/{rule["id"]}',{**payload,'rules':[],'name':'新版本不修改历史'})
    assert changed['version']==2
    for record in evidence:
        task=owner.request('GET',f'/api/tasks/{record["taskId"]}')
        assert task['ruleVersion']==1 and task['rulesSnapshot']
        assert owner.request('GET',f'/api/tasks/{record["taskId"]}/report')==record['report']
    # Marked artifacts contain protected quality columns; preview via their authorized artifact route.
    for fmt in ('xlsx','parquet'):
        task=owner.request('POST','/api/tasks',{'fileId':evidence[0]['fileId'],'ruleSetId':rule['id'],'ruleVersion':1,'schemaVersion':3,'fieldBindings':bindings,'outputFormat':fmt})
        task=owner.wait(task['id'],240);assert task['status']=='SUCCEEDED',task
        data=owner.request('GET',f'/api/tasks/{task["id"]}/artifacts/result',raw=True)
        assert data.startswith(b'PK' if fmt=='xlsx' else b'PAR1')
        preview=owner.request('GET',f'/api/tasks/{task["id"]}/preview?kind=result&limit=100')
        assert preview['previewRowCount']==4,preview
        assert [r['order_id'] for r in preview['rows']]==['002','001','006','007'],preview
        print(f'PASS: incremental {fmt} output downloaded and parsed through authenticated artifact preview.',flush=True)
    # Retry operation uses a new UUID exactly once; same key+request replays that attempt.
    invalid_file=owner.upload('runtime-invalid.csv',b'order_id,name,amount,start_at,end_at,state\n001,A,bad,2026-01-01,2026-01-02,OK\n')
    fail_rules=[{**config['rules'][1],'onError':'fail'}]
    failing=owner.request('POST','/api/rule-sets',{**payload,'name':'运行期非法数据','rules':fail_rules})
    bad=owner.request('POST','/api/tasks',{'fileId':invalid_file,'ruleSetId':failing['id'],'ruleVersion':1,'schemaVersion':3,'fieldBindings':bindings,'outputFormat':'csv'})
    bad=owner.wait(bad['id'],120);assert bad['status']=='FAILED'
    retry_body={'expectedAttemptId':bad['attemptId']};key='retry-'+uuid.uuid4().hex
    retried=owner.request('POST',f'/api/tasks/{bad["id"]}/retry',retry_body,extra_headers={'Idempotency-Key':key})
    replay=owner.request('POST',f'/api/tasks/{bad["id"]}/retry',retry_body,extra_headers={'Idempotency-Key':key})
    assert retried['attemptId']==replay['attemptId'] and retried['attemptId']!=bad['attemptId']
    owner.wait(bad['id'],120)
    owner.request('POST',f'/api/tasks/{bad["id"]}/retry',{'expectedAttemptId':retried['attemptId']},extra_headers={'Idempotency-Key':key},expected=(409,))
    attempts=owner.request('GET',f'/api/tasks/{bad["id"]}/attempts/page?pageSize=100')
    assert len(attempts['items'])==2 and all(a['engineVersion']=='v3-disk' for a in attempts['items'])
    target=ROOT/'.test-output/v3-next-disk-acceptance.json';target.write_text(json.dumps({'cases':evidence,'retryTask':bad['id']},ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: snapshots survive rule edit; retry key creates one new attempt with frozen disk semantics; runtime data validation and history confirmed.')

if __name__=='__main__':main()
