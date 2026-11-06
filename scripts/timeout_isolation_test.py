"""SIGSTOP a real executor, prove its supervisor enforces deadline, then restore settings.

Only an explicitly isolated Compose project is accepted. Never runs on main data.
"""
import argparse,json,os,subprocess,time,uuid
from pathlib import Path
from acceptance_v3 import HeaderClient

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://localhost:8089')
    parser.add_argument('--project',default='data-cleaning-pipeline-v3-next')
    args=parser.parse_args()
    if args.project=='data-cleaning-pipeline':parser.error('Refusing to modify main deployment')
    base_env=os.environ.copy();base_env['WEB_PORT']='8089';base_env['DISK_BATCH_ROWS']='127'
    def compose(*parts,code=None,environment=None):
        result=subprocess.run(['docker','compose','-p',args.project,*parts],cwd=ROOT,input=code,text=True,encoding='utf-8',capture_output=True,env=environment or base_env,timeout=180)
        assert result.returncode==0,result.stderr[-2000:]
        return result.stdout
    def inside(code):return compose('exec','-T','worker','python','-c',code)
    temporary={**base_env,'EXECUTION_TIMEOUT_SECONDS':'12','CANCEL_GRACE_SECONDS':'1'}
    try:
        compose('up','-d','--no-deps','--wait','--wait-timeout','120','backend',environment=temporary)
        auth=HeaderClient(args.base_url).request('POST','/api/auth/register',{'username':'deadline_'+uuid.uuid4().hex[:10],'password':uuid.uuid4().hex})
        owner=HeaderClient(args.base_url,auth['token'])
        data=('id,label\n'+''.join(f'{i:08d},  Row {i}  \n' for i in range(100000))).encode()
        file=owner.upload('deadline.csv',data)
        rules=[{'id':f's{i}','enabled':True,'type':'case','fieldIds':['label'],'mode':'upper' if i%2 else 'lower'} for i in range(40)]
        rule=owner.request('POST','/api/rule-sets',{'name':'真实阻塞超时','schemaVersion':3,'engineVersion':'v3-disk','logicalFields':[{'id':'id','label':'ID'},{'id':'label','label':'文本'}],'rules':rules})
        task=owner.request('POST','/api/tasks',{'fileId':file,'ruleSetId':rule['id'],'ruleVersion':1,'schemaVersion':3,'fieldBindings':{'id':'f0','label':'f1'},'outputFormat':'csv'})
        deadline=time.monotonic()+30;child=None
        while time.monotonic()<deadline:
            state=owner.request('GET',f'/api/tasks/{task["id"]}')
            if state['status']=='RUNNING':
                code="""from pathlib import Path
import json,os,signal
found=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit():continue
 if int(p.name)==os.getpid():continue
 try:
  cmd=(p/'cmdline').read_bytes()
  if b'multiprocessing.spawn' in cmd and b'--multiprocessing-fork' in cmd:
   os.kill(int(p.name),signal.SIGSTOP);found.append(int(p.name))
 except (OSError,ValueError):pass
print(json.dumps(found))
"""
                pids=json.loads(inside(code))
                if pids:child=pids[0];break
            time.sleep(.1)
        assert child,'No real execution child observed'
        stopped_at=time.monotonic()
        final=owner.wait(task['id'],45)
        termination_seconds=round(time.monotonic()-stopped_at,3)
        assert final['status']=='FAILED' and final['errorCode']=='TASK_TIMEOUT',final
        assert final['stage'] and final['attempt']==1
        absent=json.loads(inside(f"from pathlib import Path;import json;print(json.dumps(not Path('/proc/{child}').exists()))"))
        assert absent,'Timed-out child still exists'
        # A late terminal message cannot resurrect the fenced attempt.
        late={"attempt":1,"attemptId":task['attemptId'],"eventId":str(uuid.uuid4()),"eventSeq":99999,"leaseToken":"obsolete-lease","status":"SUCCEEDED","progress":100,"message":"late after deadline"}
        code="import os,json,httpx; p="+repr(late)+";r=httpx.post(os.environ['JAVA_API_URL']+'/internal/tasks/"+task['id']+"/events',json=p,headers={'X-Internal-Token':os.environ['INTERNAL_API_TOKEN']});r.raise_for_status();print(r.status_code)"
        compose('exec','-T','python-api','python','-c',code)
        assert owner.request('GET',f'/api/tasks/{task["id"]}')['status']=='FAILED'
        tiny=owner.upload('after-timeout.csv',b'id,label\n001, hello \n')
        empty=owner.request('POST','/api/rule-sets',{'name':'超时后同Worker仍可用','schemaVersion':3,'engineVersion':'v3-disk','logicalFields':[{'id':'id','label':'ID'},{'id':'label','label':'文本'}],'rules':[]})
        check=owner.request('POST','/api/tasks',{'fileId':tiny,'ruleSetId':empty['id'],'ruleVersion':1,'schemaVersion':3,'fieldBindings':{'id':'f0','label':'f1'},'outputFormat':'csv'})
        assert owner.wait(check['id'],45)['status']=='SUCCEEDED'
        evidence={'taskId':task['id'],'attemptId':task['attemptId'],'stoppedChildPid':child,'status':final['status'],'errorCode':final['errorCode'],'stage':final['stage'],'secondsAfterSigstop':termination_seconds,'childReaped':True,'nextTaskSucceeded':True,'perAttemptDeadlineSeconds':12,'cancelGraceSeconds':1,'automaticAttemptCount':1}
        (ROOT/'.test-output/v3-next-timeout.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        print('PASS: real executor SIGSTOP with healthy supervisor -> deadline -> forced process termination -> TASK_TIMEOUT; late success ignored; next task succeeds on same Worker.')
        print(json.dumps(evidence))
    finally:
        compose('up','-d','--no-deps','--wait','--wait-timeout','120','backend',environment=base_env)

if __name__=='__main__':main()
