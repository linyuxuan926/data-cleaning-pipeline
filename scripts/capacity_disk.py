"""Measure two data sizes and two real disk workloads; no unmeasured capacity claim."""
from __future__ import annotations
import argparse,csv,io,json,math,statistics,subprocess,time,uuid
from collections import Counter
from decimal import Decimal,ROUND_HALF_UP,localcontext
from pathlib import Path
from acceptance_v3 import HeaderClient

ROOT=Path(__file__).resolve().parents[1]
MONITOR=r'''
import json,os,select,sys,time
from pathlib import Path
peak=peak_disk=peak_parent=peak_children=samples=0
while True:
    processes={}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name)==os.getpid():continue
        try:
            fields={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in (entry/'status').read_text().splitlines() if ':' in line}
            processes[int(entry.name)]=(int(fields['PPid']),int(fields.get('VmRSS','0 kB').split()[0])*1024)
        except (OSError,ValueError,KeyError):pass
    family={1}
    while True:
        grown=family|{pid for pid,(parent,rss) in processes.items() if parent in family}
        if grown==family:break
        family=grown
    parent=processes.get(1,(0,0))[1]
    children=sum(processes[pid][1] for pid in family if pid!=1 and pid in processes)
    total=parent+children
    if total>peak:peak,peak_parent,peak_children=total,parent,children
    root=Path(os.environ.get('SCRATCH_ROOT','/tmp/cleaning-attempts'))
    try:disk=sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink())
    except OSError:disk=0
    peak_disk=max(peak_disk,disk);samples+=1
    if select.select([sys.stdin],[],[],.2)[0]:
        sys.stdin.readline()
        break
print(json.dumps({'combinedPeakRssBytes':peak,'parentRssAtPeak':peak_parent,'childrenRssAtPeak':peak_children,'scratchPeakBytes':peak_disk,'sampleCount':samples,'samplingSeconds':.2}))
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://localhost:8088')
    parser.add_argument('--project',default='data-cleaning-pipeline')
    parser.add_argument('--sizes',default='10000,50000')
    args=parser.parse_args();sizes=[int(v) for v in args.sizes.split(',')]
    assert len(sizes)>=2 and all(1000<=v<=90000 for v in sizes)
    auth=HeaderClient(args.base_url).request('POST','/api/auth/register',{'username':'diskperf_'+uuid.uuid4().hex[:10],'password':uuid.uuid4().hex})
    owner=HeaderClient(args.base_url,auth['token']);evidence=[]
    logical=[{'id':key,'label':key} for key in ('id','amount','median','mode','label')]
    bindings={f['id']:f'f{i}' for i,f in enumerate(logical)}
    def step(kind,**kw):return {'id':kind+'-'+uuid.uuid4().hex[:6],'type':kind,'enabled':True,**kw}
    for workload in ('row','global'):
        for n in sizes:
            distinct=n//10;data=io.StringIO(newline='');writer=csv.writer(data,lineterminator='\n');writer.writerow(bindings)
            original=[]
            for i in range(n):
                number=None if i%11==0 else 10000 if i==n-1 else i%100
                original.append(number);writer.writerow([f'{i%distinct:08d}','' if number is None else number,'' if number is None else number,'' if i%13==0 else 'A' if i%2 else 'B','  Mixed Label  '])
            content=data.getvalue().encode();file=owner.upload(f'{workload}-{n}.csv',content)
            rules=[step('trim',fieldIds=['label']),step('replace',fieldIds=['label'],search='Label',replacement='Value'),step('cast',fieldIds=['amount','median'],dtype='decimal',precision=20,scale=2,rounding='HALF_UP',onError='fail')]
            if workload=='row':
                rules += [step('required',fieldIds=['amount'],onError='mark'),step('fill_null',fieldIds=['amount'],value={'type':'decimal','value':'0.00','scale':2})]
            else:
                rules += [step('fill_stat',fieldIds=['amount'],strategy='mean',emptyPolicy='fail',precision=20,scale=2,rounding='HALF_UP'),step('fill_stat',fieldIds=['median'],strategy='median',emptyPolicy='fail',precision=20,scale=2,rounding='HALF_UP'),step('fill_stat',fieldIds=['mode'],strategy='mode',emptyPolicy='fail'),step('iqr',fieldId='amount',factor='1.5',emptyPolicy='fail',onError='mark'),step('deduplicate',fieldIds=['id'],keep='last',nullsEqual=True,onError='drop')]
            rule=owner.request('POST','/api/rule-sets',{'name':f'实测{workload}-{n}','schemaVersion':3,'engineVersion':'v3-disk','logicalFields':logical,'rules':rules})
            monitor=subprocess.Popen(['docker','compose','-p',args.project,'exec','-T','worker','python','-u','-c',MONITOR],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
            started=time.monotonic()
            try:
                task=owner.request('POST','/api/tasks',{'fileId':file,'ruleSetId':rule['id'],'ruleVersion':1,'schemaVersion':3,'fieldBindings':bindings,'outputFormat':'csv'})
                task=owner.wait(task['id'],900)
                elapsed=time.monotonic()-started
            finally:
                metrics,errors=monitor.communicate('stop\n',timeout=15)
            assert monitor.returncode==0,errors
            measured=json.loads(metrics);assert measured['sampleCount']>1 and measured['childrenRssAtPeak']>0,measured
            assert task['status']=='SUCCEEDED',task
            report=owner.request('GET',f'/api/tasks/{task["id"]}/report')
            output=owner.request('GET',f'/api/tasks/{task["id"]}/artifacts/result',raw=True)
            dirty=owner.request('GET',f'/api/tasks/{task["id"]}/artifacts/dirty',raw=True)
            result=list(csv.DictReader(io.StringIO(output.decode('utf-8-sig'))))
            issues=list(csv.DictReader(io.StringIO(dirty.decode('utf-8-sig'))))
            expected_count=n if workload=='row' else distinct
            assert report['inputRows']==n and report['outputRows']==len(result)==expected_count
            assert n==report['outputRows']+report['finalRemovedRows']
            assert sum(report['finalRemovalByRule'].values())==report['finalRemovedRows']
            assert report['outputRows']==report['validOutputRows']+report['flaggedOutputRows']
            assert report['issueCount']==len(issues)
            with localcontext() as context:
                context.prec=80
                numbers=[Decimal(v) for v in original if v is not None]
                mean=(sum(numbers)/Decimal(len(numbers))).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
                median=Decimal(statistics.median(numbers)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
                mode_inputs=['A' if i%2 else 'B' for i in range(n) if i%13!=0]
                counts=Counter(mode_inputs);mode=max(mode_inputs,key=lambda value:counts[value])
            for index,row in zip(range(n) if workload=='row' else range(n-distinct,n),result):
                assert row['id']==f'{index%distinct:08d}' and row['label']=='Mixed Value'
                expected=Decimal(original[index]) if original[index] is not None else Decimal(0) if workload=='row' else mean
                assert Decimal(row['amount'])==expected,(index,row,expected)
                if workload=='global':
                    assert Decimal(row['median'])==(Decimal(original[index]) if original[index] is not None else median)
                    assert row['mode']==(mode if index%13==0 else 'A' if index%2 else 'B')
            if workload=='global':
                assert report['duplicateRemovedRows']==n-distinct
                assert Decimal(report['rules'][3]['statistics']['f1']['fillValue']['value'])==mean
                assert Decimal(report['rules'][4]['statistics']['f2']['fillValue']['value'])==median
                assert report['rules'][5]['statistics']['f3']['fillValue']['value']==mode
                ordered=sorted(Decimal(v) if v is not None else mean for v in original)
                def quantile(fraction):
                    position=Decimal(n-1)*fraction;lo=int(position);hi=min(lo+1,n-1)
                    return ordered[lo]+(ordered[hi]-ordered[lo])*(position-lo)
                assert Decimal(report['rules'][6]['statistics']['q1'])==quantile(Decimal('.25'))
                assert Decimal(report['rules'][6]['statistics']['q3'])==quantile(Decimal('.75'))
            measured.update({'workload':workload,'rows':n,'columns':5,'concurrentTasks':1,'inputBytes':len(content),'outputRows':len(result),'outputBytes':len(output),'dirtyIssueCount':len(issues),'dirtyBytes':len(dirty),'taskId':task['id'],'attemptId':task['attemptId'],'batchRows':report['limitsSnapshot']['batchRows'],'maxScratchBytes':report['limitsSnapshot']['maxScratchBytes'],'wallSecondsIncludingQueue':round(elapsed,3),'engineSeconds':report['durationSeconds'],'engineScratchPeakBytes':report.get('scratchPeakBytes'),'ruleStages':[{'id':s.get('id',s.get('ruleId')),'type':s['type'],'durationMs':s.get('durationMs'),'stageRows':s.get('stageRows')} for s in report['rules']],'allOutputRowsChecked':True})
            evidence.append(measured)
            (ROOT/'.test-output/v3-next-disk-capacity.json').write_text(json.dumps({'project':args.project,'baseUrl':args.base_url,'measurements':evidence},ensure_ascii=False,indent=2),encoding='utf-8')
            print('PASS measured '+json.dumps(measured,ensure_ascii=False),flush=True)
    machine=subprocess.run(['docker','info','--format','{{json .}}'],capture_output=True,text=True,encoding='utf-8',check=True)
    info=json.loads(machine.stdout);target=ROOT/'.test-output/v3-next-disk-capacity.json'
    target.write_text(json.dumps({'project':args.project,'baseUrl':args.base_url,'dockerLogicalCpus':info['NCPU'],'dockerMemoryBytes':info['MemTotal'],'measurements':evidence,'note':'RSS is the sum of supervisor and child RSS, shared pages may be counted more than once; 200ms samples may miss sub-sample peaks. This is measured workloads, not a capacity limit.'},ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
