import csv
from decimal import Decimal, ROUND_HALF_UP, localcontext
from io import StringIO
import json
from pathlib import Path
import random
from threading import Event
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from openpyxl import Workbook
from fastapi.testclient import TestClient

from cleaning.api import create_app
from cleaning.bindings import canonical_key, decimal_value, normalize_rules, preflight, typed_scalar
from cleaning.config import Settings
from cleaning.models import InputError, CancelledError, TaskMessage
from cleaning.pipeline import execute_task
from cleaning.disk import DiskEngine
from cleaning.storage import ObjectStorage


class Storage:
    def __init__(self, raw): self.raw, self.uploaded = raw, {}
    def download_input(self, key, target, max_bytes=104857600):
        if len(self.raw)>max_bytes: raise InputError("Input exceeds configured download byte limit")
        target.write_bytes(self.raw)
    def upload(self, key, path, content_type): self.uploaded[key] = path.read_bytes()


def rule(kind, **options):
    return {"id": kind, "type": kind, "enabled": True, **options}


def task(headers, rules, *, batch=2, fmt="csv", output="csv", limits=None):
    ids = [f"field{i}" for i in range(len(headers))]
    return TaskMessage.model_validate({
        "taskId": str(uuid4()), "attempt": 1, "attemptId": str(uuid4()),
        "engineVersion": "v3-disk", "schemaVersion": 3,
        "input": {"fileId": str(uuid4()), "parseVersion": 1, "objectKey": "raw/test/input", "format": fmt, "name": "input."+fmt,
                  "fields": [{"id": f"f{i}", "name": name} for i,name in enumerate(headers)]},
        "logicalFields": [{"id": fid,"label": name} for fid,name in zip(ids,headers)],
        "fieldBindings": {fid: f"f{i}" for i,fid in enumerate(ids)},
        "rules": rules, "outputFormat": output, "limits": {"batchRows": batch, **(limits or {})}})


def run(tmp_path, headers, rows, rules, **options):
    text = StringIO(newline="")
    writer = csv.writer(text); writer.writerow(headers); writer.writerows(rows)
    storage = Storage(text.getvalue().encode())
    message = task(headers,rules,**options)
    report, artifacts = execute_task(message,storage,tmp_path,Event(),lambda *_: None)
    result = list(csv.DictReader(StringIO(storage.uploaded[artifacts["result"]].decode("utf-8-sig")))) if message.outputFormat == "csv" else []
    dirty = list(csv.DictReader(StringIO(storage.uploaded[artifacts["dirty"]].decode("utf-8-sig"))))
    return result,dirty,report,storage,artifacts


@pytest.mark.parametrize("batch", [1,2,3,1000])
@pytest.mark.parametrize("output", ["csv","parquet","xlsx"])
def test_canonical_eight_rows(tmp_path,batch,output):
    example = Path(__file__).resolve().parents[2]/"docs/v3/examples"
    spec = json.loads((example/"ordered-rules.json").read_text(encoding="utf-8"))
    headers = next(csv.reader(StringIO((example/"input.csv").read_text(encoding="utf-8-sig"))))
    message = task(headers,spec["rules"],batch=batch,output=output)
    message.logicalFields = spec["logicalFields"]
    message.fieldBindings = {field["id"]:f"f{i}" for i,field in enumerate(spec["logicalFields"])}
    storage=Storage((example/"input.csv").read_bytes())
    report,artifacts=execute_task(message,storage,tmp_path,Event(),lambda *_:None)
    assert {k:report[k] for k in ["inputRows","outputRows","validOutputRows","flaggedOutputRows","finalRemovedRows","issueRows","issueCount","changedCells"]} == {
        "inputRows":8,"outputRows":4,"validOutputRows":3,"flaggedOutputRows":1,"finalRemovedRows":4,"issueRows":5,"issueCount":5,"changedCells":22}
    assert report["rules"][2]["statistics"]["f2"]["fillValue"]["value"] == "185.33"
    assert report["finalRemovalByRule"] == {"r02-cast-amount":1,"r04-range":1,"r06-dates-order":1,"r08-dedup":1}
    dirty=list(csv.DictReader(StringIO(storage.uploaded[artifacts["dirty"]].decode("utf-8-sig"))))
    assert [r["_source_record_number"] for r in dirty]==["1","3","4","6","7"]
    assert dirty[-1]["_final_action"] == "OUTPUT_FLAGGED"
    if output=="parquet":
        table=pq.read_table(tmp_path/"result.parquet")
        assert table.num_rows==4
        assert table.column("amount").to_pylist()==[Decimal("185.33"),Decimal("12.00"),Decimal("20.00"),Decimal("40.00")]
    assert json.loads(storage.uploaded[artifacts["report"]])==report


@pytest.mark.parametrize("batch", [1,4,17])
@pytest.mark.parametrize("strategy", ["mean","median","mode"])
def test_global_fill_matches_independent_whole_data_oracle(tmp_path,batch,strategy):
    rng=random.Random(41)
    samples=[rng.randrange(-999,1000) for _ in range(67)]
    rows=[[str(v)] for v in samples]+[[""]]*4
    if strategy=="mean":
        expected=(Decimal(sum(samples))/len(samples)).quantize(Decimal(".01"),rounding=ROUND_HALF_UP)
    elif strategy=="median": expected=Decimal(sorted(samples)[len(samples)//2]).quantize(Decimal(".01"))
    else: expected=Decimal(max(dict.fromkeys(samples),key=lambda v:samples.count(v))).quantize(Decimal(".01"))
    rules=[rule("cast",fieldIds=["field0"],dtype="decimal",scale=2),rule("fill_stat",fieldIds=["field0"],strategy=strategy,emptyPolicy="fail")]
    result,dirty,report,*_=run(tmp_path,["number"],rows,rules,batch=batch)
    assert [Decimal(row["number"]) for row in result[-4:]]==[expected]*4
    assert not dirty and report["missingFilledCells"]==4


@pytest.mark.parametrize("batch", [1,7,1000])
@pytest.mark.parametrize("keep", ["first","last"])
def test_global_dedup_matches_source_order_oracle(tmp_path,batch,keep):
    rng=random.Random(12)
    keys=[rng.choice(["a","b","c",None]) for _ in range(61)]
    expected={}
    for i,key in enumerate(keys):
        if keep=="last" or key not in expected: expected[key]=i
    rows=[[str(i),key] for i,key in enumerate(keys)]
    result,dirty,report,*_=run(tmp_path,["source","key"],rows,[rule("deduplicate",fieldIds=["field1"],keep=keep)],batch=batch)
    assert [int(row["source"]) for row in result]==sorted(expected.values())
    assert report["finalRemovedRows"]==len(keys)-len(expected)
    assert len(dirty)==len(keys)-len(expected)


@pytest.mark.parametrize("batch",[1,8])
def test_global_iqr_matches_linear_oracle(tmp_path,batch):
    values=[-100,0,1,2,3,4,5,6,7,8,9,10,150]
    ordered=sorted(values)
    def q(fraction):
        index=Decimal(len(ordered)-1)*fraction; left=int(index)
        return Decimal(ordered[left])+(Decimal(ordered[min(left+1,len(ordered)-1)])-ordered[left])*(index-left)
    low=q(Decimal(".25"))-Decimal("1.5")*(q(Decimal(".75"))-q(Decimal(".25")))
    high=q(Decimal(".75"))+Decimal("1.5")*(q(Decimal(".75"))-q(Decimal(".25")))
    result,dirty,report,*_=run(tmp_path,["n"],[[str(v)] for v in values],[rule("cast",fieldIds=["field0"],dtype="decimal"),rule("iqr",fieldId="field0",emptyPolicy="fail")],batch=batch)
    assert [Decimal(r["n"]) for r in result]==[Decimal(v) for v in values if low<=v<=high]
    assert report["outlierHandledRows"]==len(dirty)==2


def test_decimal_never_rounds_equality_keys_and_half_up():
    left=Decimal("12345678901234567890123456789012345678")
    right=left+Decimal(0)  # default context intentionally rounds this calculation.
    assert canonical_key(left)!=canonical_key(right)
    assert canonical_key(left)=="numeric:12345678901234567890123456789012345678"
    assert canonical_key(Decimal("-0.000"))==canonical_key(0)
    assert decimal_value(".01",2,2)==Decimal(".01")
    assert decimal_value("-1.235",38,2)==Decimal("-1.24")
    with pytest.raises(ValueError): decimal_value("999.99",4,2)
    assert typed_scalar({"type":"integer","value":"9223372036854775807"})==9223372036854775807
    with pytest.raises(InputError): typed_scalar({"type":"integer","value":"9223372036854775808"})


def test_new_rules_literal_replace_cross_field_null_and_trace(tmp_path):
    rules=[rule("replace",fieldIds=["field0"],search=".",replacement="$",count=1),
           rule("cast",fieldIds=["field1","field2"],dtype="decimal"),
           rule("compare_fields",leftFieldId="field1",rightFieldId="field2",operator="lte",valueType="decimal",nullPolicy="violation",onError="mark"),
           {**rule("required",fieldIds=["field0"]),"id":"last-drop"}]
    result,dirty,report,*_=run(tmp_path,["name","left","right"],[["a.b.c","1","2"],["z.y","9","2"],["","","3"]],rules)
    assert result[0]["name"]=="a$b.c" and result[1]["_quality_valid"]=="False"
    assert report["validOutputRows"]==1 and report["flaggedOutputRows"]==1
    row3=[r for r in dirty if r["_source_row"]=="3"]
    assert row3[0]["_final_action"]=="REMOVED_BY_LATER_RULE"
    assert row3[1]["_final_action"]=="REMOVED_BY_THIS_RULE"
    assert set(json.loads(row3[0]["_before_value"])["value"])=={"f1","f2"}


def test_explicit_bindings_projection_and_strict_schema(tmp_path):
    message=task(["a","b"],[rule("drop_fields",fieldIds=["field1"]),rule("trim",fieldIds=["field1"])])
    with pytest.raises(InputError,match="removed"): preflight(message.input.fields,message.logicalFields,message.fieldBindings,message.rules)
    with pytest.raises(InputError,match="one-to-one"): preflight(message.input.fields,message.logicalFields,{"field0":"f0","field1":"f0"},[])
    with pytest.raises(InputError,match="unbound"): preflight(message.input.fields,message.logicalFields,{},[rule("trim",fieldIds=["field0"])])
    with pytest.raises(InputError): normalize_rules([rule("trim",columns=["a"])])
    with pytest.raises(ValueError): TaskMessage.model_validate({**message.model_dump(),"schemaVersion":1})


@pytest.mark.parametrize("fmt",["xlsx","parquet"])
def test_streaming_formats_and_decimal_schema(tmp_path,fmt):
    source=tmp_path/("source."+fmt)
    if fmt=="parquet":
        data=pa.table({"n":pa.array([Decimal("12345678901234567890.123456789012"),None,Decimal("2.000000000001")],pa.decimal128(38,12))})
        pq.write_table(data,source,row_group_size=1)
        rules=[]
    else:
        book=Workbook(); book.active.title="one"; book.active.append(["n"]); book.active.append(["ignored"])
        sheet=book.create_sheet("two"); sheet.append(["n"]); sheet.append([1]); sheet.append([2]); book.save(source)
        rules=[rule("cast",fieldIds=["field0"],dtype="integer")]
    message=task(["n"],rules,fmt=fmt,output="parquet",batch=1)
    if fmt=="xlsx": message.input.parseOptions={"sheetName":"two"}
    storage=Storage(source.read_bytes())
    report,_=execute_task(message,storage,tmp_path/"attempt",Event(),lambda *_:None)
    actual=pq.read_table(tmp_path/"attempt/result.parquet")
    assert actual.num_rows==(3 if fmt=="parquet" else 2)
    if fmt=="parquet": assert actual.column("n").to_pylist()==data.column("n").to_pylist()
    else: assert "two" in report["warnings"][0]


def test_csv_source_is_logical_record_number_with_embedded_newline(tmp_path):
    _,dirty,report,*_=run(tmp_path,["text"],[["hello\nworld"],[""]],[rule("required",fieldIds=["field0"])],batch=1)
    assert report["inputRows"]==2 and dirty[0]["_source_record_number"]=="2"


def test_csv_large_cell_respects_configured_limit_instead_of_csv_default(tmp_path):
    text="a"*150000
    run(tmp_path,["text"],[[text]],[],batch=1,output="parquet")
    assert pq.read_table(tmp_path/"result.parquet").column("text").to_pylist()==[text]


def test_iqr_no_valid_values_leave_is_noop(tmp_path):
    result,dirty,report,*_=run(tmp_path,["value"],[["bad"],[""]],[rule("iqr",fieldId="field0",emptyPolicy="leave")])
    assert len(result)==2 and not dirty and report["outlierHandledRows"]==0


@pytest.mark.parametrize("limits,rows,rules,match",[
    ({"maxRecordBytes":60},[["x"*100]],[],"maxRecordBytes"),
    ({"maxScratchBytes":100},[["a"]],[],"maxScratchBytes"),
    ({"maxRecordIssueBytes":50},[[""]],[rule("required",fieldIds=["field0"],onError="mark")],"maxRecordIssueBytes"),
])
def test_resource_caps_fail_without_publishing(tmp_path,limits,rows,rules,match):
    with pytest.raises(InputError,match=match): run(tmp_path,["text"],rows,rules,limits=limits)


def test_cancel_before_download_and_no_full_frame_reader(tmp_path,monkeypatch):
    import cleaning.pipeline as module
    monkeypatch.setattr(module,"read_table",lambda *_args,**_kwargs:pytest.fail("full-frame read forbidden"))
    run(tmp_path/"success",["a"],[["x"]],[])
    event=Event(); event.set(); storage=Storage(b"a\nx\n")
    with pytest.raises(CancelledError): execute_task(task(["a"],[]),storage,tmp_path/"cancel",event,lambda *_:None)
    assert not storage.uploaded


def test_protected_schemas_preflight_and_save_without_bindings():
    settings=Settings("http://localhost:9000","access","secretsecret","cleaning","x"*40,"http://localhost:8080","localhost",5672,"user","password","cleaning.tasks")
    message=task(["a"],[rule("trim",fieldIds=["field0"])])
    with TestClient(create_app(settings)) as client:
        assert client.get("/internal/rule-schemas/3").status_code==401
        headers={"X-Internal-Token":"x"*40}
        schemas=client.get("/internal/rule-schemas/3",headers=headers).json()
        assert schemas["ruleCount"]==21 and len(schemas["jsonSchema"]["$defs"])==21
        saved=client.post("/internal/validate-rules",headers=headers,json={"schemaVersion":3,"engineVersion":"v3-disk","logicalFields":message.logicalFields,"rules":message.rules})
        assert saved.status_code==200
        checked=client.post("/internal/preflight",headers=headers,json={key:value for key,value in message.model_dump(mode="json").items() if key in {"input","schemaVersion","engineVersion","logicalFields","fieldBindings","rules"}})
        assert checked.status_code==200 and checked.json()["normalizedRules"][0]["fieldIds"]==["field0"]


@pytest.mark.parametrize("rules,rows,expected",[
    ([rule("trim",fieldIds=["field0"])],[[" x ","b"]],[{"a":"x","b":"b"}]),
    ([rule("case",fieldIds=["field0"],mode="upper")],[["Ab","b"]],[{"a":"AB","b":"b"}]),
    ([rule("missing",fieldIds=["field0"],markers=[{"type":"string","value":"NA"}])],[["NA","b"]],[{"a":"","b":"b"}]),
    ([rule("fill_null",fieldIds=["field0"],value={"type":"string","value":"filled"})],[["","b"]],[{"a":"filled","b":"b"}]),
    ([rule("drop_null",fieldIds=["field0"])],[["","b"],["x","b"]],[{"a":"x","b":"b"}]),
    ([rule("required",fieldIds=["field0"])],[["","b"],["x","b"]],[{"a":"x","b":"b"}]),
    ([rule("enum",fieldId="field0",values=[{"type":"string","value":"x"}])],[["y","b"],["x","b"]],[{"a":"x","b":"b"}]),
    ([rule("length",fieldId="field0",min=2,max=3)],[["x","b"],["abc","b"]],[{"a":"abc","b":"b"}]),
    ([rule("regex",fieldId="field0",pattern="[a-z]+")],[["ABC","b"],["abc","b"]],[{"a":"abc","b":"b"}]),
    ([rule("validate_type",fieldIds=["field0"],dtype="integer")],[["001","b"],["bad","b"]],[{"a":"001","b":"b"}]),
    ([rule("compare",fieldId="field0",operator="starts_with",value={"type":"string","value":"a"})],[["abc","b"],["bad","b"]],[{"a":"abc","b":"b"}]),
    ([rule("select_fields",fieldIds=["field1","field0"])],[["x","y"]],[{"b":"y","a":"x"}]),
    ([rule("drop_fields",fieldIds=["field0"])],[["x","y"]],[{"b":"y"}]),
    ([rule("rename",fieldId="field0",target="renamed")],[["x","y"]],[{"renamed":"x","b":"y"}]),
])
def test_all_row_and_projection_rules_are_real(tmp_path,rules,rows,expected):
    result,_,report,*_=run(tmp_path,["a","b"],rows,rules,batch=1)
    assert result==expected
    assert list(result[0])==list(expected[0])
    assert report["rules"][0]["stageRows"]["output"]==len(expected)


@pytest.mark.parametrize("action",["drop","mark","null","fail"])
def test_cast_error_policies_and_unique_flagged_metrics(tmp_path,action):
    rules=[rule("cast",fieldIds=["field0","field1"],dtype="integer",onError=action)]
    if action=="fail":
        with pytest.raises(InputError,match="source record 1"): run(tmp_path,["a","b"],[["bad","also bad"]],rules)
        return
    result,dirty,report,*_=run(tmp_path,["a","b"],[["bad","also bad"]],rules)
    assert len(dirty)==2 and report["issueRows"]==1
    assert report["rules"][0]["flaggedRows"]==(0 if action=="drop" else 1)
    assert report["flaggedOutputRows"]==(0 if action=="drop" else 1)
    if action=="null": assert result[0]["a"]==result[0]["b"]==""


def test_decimal_dedup_keeps_38_digits_and_large_integer_boundaries(tmp_path):
    numbers=["9007199254740992","9007199254740993","12345678901234567890123456789012345678","12345678901234567890123456789012345679","9007199254740992"]
    result,_,report,*_=run(tmp_path,["n"],[[n] for n in numbers],[rule("cast",fieldIds=["field0"],dtype="decimal",scale=0),rule("deduplicate",fieldIds=["field0"])],batch=2)
    assert [r["n"] for r in result]==numbers[:4]
    assert report["duplicateRemovedRows"]==1


def test_all_null_empty_policies_and_empty_parquet_output(tmp_path):
    rules=[rule("cast",fieldIds=["field0"],dtype="decimal"),rule("fill_stat",fieldIds=["field0"],strategy="median",emptyPolicy="constant",value={"type":"decimal","value":"1.25","scale":2})]
    result,_,report,*_=run(tmp_path/"constant",["n"],[[""]],rules)
    assert result[0]["n"]=="1.25" and report["missingFilledCells"]==1
    rules[1]["emptyPolicy"]="fail"; rules[1].pop("value")
    with pytest.raises(InputError,match="no valid values"): run(tmp_path/"fail",["n"],[[""]],rules)
    _,_,report,_,_=run(tmp_path/"empty",["n"],[[""]],[rule("cast",fieldIds=["field0"],dtype="decimal"),rule("required",fieldIds=["field0"])],output="parquet")
    assert report["outputRows"]==0
    output=pq.read_table(tmp_path/"empty/result.parquet")
    assert output.schema.field("n").type==pa.decimal128(38,2)


def test_mark_then_dedup_final_attribution_and_stable_record_identity(tmp_path):
    rules=[rule("enum",fieldId="field1",values=[{"type":"string","value":"OK"}],onError="mark"),rule("deduplicate",fieldIds=["field0"],keep="last")]
    message=task(["id","state"],rules)
    storage=Storage(b"id,state\nx,BAD\nx,OK\n")
    _,first=execute_task(message,storage,tmp_path/"one",Event(),lambda *_:None)
    dirty1=list(csv.DictReader(StringIO(storage.uploaded[first["dirty"]].decode("utf-8-sig"))))
    message.attempt=2; message.attemptId=uuid4()
    report,second=execute_task(message,storage,tmp_path/"two",Event(),lambda *_:None)
    dirty2=list(csv.DictReader(StringIO(storage.uploaded[second["dirty"]].decode("utf-8-sig"))))
    assert [r["_record_id"] for r in dirty1]==[r["_record_id"] for r in dirty2]
    assert dirty1[0]["_record_id"]==dirty1[1]["_record_id"] and len(dirty1[0]["_record_id"])==64
    assert dirty1[0]["_final_action"]=="REMOVED_BY_LATER_RULE" and report["flaggedOutputRows"]==0


def test_parquet_mixed_cast_mark_requires_explicit_final_cast(tmp_path):
    with pytest.raises(InputError,match="mixed-type"):
        run(tmp_path/"bad",["n"],[["1"],["bad"]],[rule("cast",fieldIds=["field0"],dtype="integer",onError="mark")],output="parquet")
    rules=[rule("cast",fieldIds=["field0"],dtype="integer",onError="mark"),{**rule("cast",fieldIds=["field0"],dtype="string"),"id":"string-final"}]
    run(tmp_path/"good",["n"],[["1"],["bad"]],rules,output="parquet")
    assert pq.read_table(tmp_path/"good/result.parquet").column("n").to_pylist()==["1","bad"]


@pytest.mark.parametrize("option,value",[("type",[]),("dtype",{}),("onError",[]),("precision",True),("scale",13),("rounding",{}),("fieldIds",[[]])])
def test_malformed_rules_are_user_errors(option,value):
    payload=rule("cast",fieldIds=["field0"],dtype="decimal")
    payload[option]=value
    with pytest.raises(InputError): normalize_rules([payload])


@pytest.mark.parametrize("keep",["first","last"])
@pytest.mark.parametrize("batch",[1,3,1000])
def test_real_historical_engine_differential_marker_null_mode_and_dedup(tmp_path,keep,batch):
    """Execute both production engines; fixtures alone cannot pass this test."""
    headers=["id","text","state"]
    rows=[["A"," NA ","BAD"],["B","x","OK"],["A","y","OK"],["C","y","OK"],["D","","BAD"],["E"," x ","OK"],["F","","OK"],["","","OK"]]
    rules=[rule("trim",fieldIds=["field1"]),
           rule("missing",fieldIds=["field1"],markers=[{"type":"string","value":"NA"}]),
           rule("fill_stat",fieldIds=["field1"],strategy="mode",emptyPolicy="fail"),
           rule("enum",fieldId="field2",values=[{"type":"string","value":"OK"}],onError="mark"),
           rule("required",fieldIds=["field0"]),
           rule("deduplicate",fieldIds=["field0"],keep=keep)]
    new_result,new_dirty,new_report,_,_=run(tmp_path/"disk",headers,rows,rules,batch=batch)
    old_rules=[]
    for current in rules:
        converted=dict(current)
        for multi,single in (("fieldIds","columns"),("fieldId","column")):
            if multi in converted:
                refs=converted.pop(multi)
                converted[single]=[f"f{int(fid[5:])}" for fid in refs] if isinstance(refs,list) else f"f{int(refs[5:])}"
        for key in ("markers","values"):
            if key in converted: converted[key]=[value["value"] for value in converted[key]]
        old_rules.append(converted)
    text=StringIO(newline=""); writer=csv.writer(text); writer.writerow(headers); writer.writerows(rows)
    storage=Storage(text.getvalue().encode())
    old=TaskMessage.model_validate({"taskId":str(uuid4()),"attempt":1,"attemptId":str(uuid4()),"engineVersion":"v3-sequential","schemaVersion":1,
        "input":{"fileId":str(uuid4()),"objectKey":"raw/test/input","format":"csv","name":"input.csv","fields":[{"id":f"f{i}","name":name} for i,name in enumerate(headers)]},"rules":old_rules,"outputFormat":"csv"})
    (tmp_path/"historical").mkdir()
    old_report,artifacts=execute_task(old,storage,tmp_path/"historical",Event(),lambda *_:None)
    old_result=list(csv.DictReader(StringIO(storage.uploaded[artifacts["result"]].decode("utf-8-sig"))))
    old_dirty=list(csv.DictReader(StringIO(storage.uploaded[artifacts["dirty"]].decode("utf-8-sig"))))
    comparable=headers+["_quality_valid"]
    assert [[row.get(key) for key in comparable] for row in new_result]==[[row.get(key) for key in comparable] for row in old_result]
    issue_keys=["_source_row","_field","_rule_index","_error_code","_reason","_rule_id","_before_value","_action","_final_action"]
    assert [[row[key] for key in issue_keys] for row in new_dirty]==[[row[key] for key in issue_keys] for row in old_dirty]
    old_removal={row["_source_row"]:row["_rule_id"] for row in old_dirty if row["_action"]=="drop"}
    assert [row["_removed_by_rule_id"] for row in new_dirty]==[old_removal.get(row["_source_row"],"") for row in old_dirty]
    metrics=["inputRows","outputRows","validOutputRows","flaggedOutputRows","finalRemovedRows","dirtyRows","issueRows","issueCount","changedCells","missingFilledCells","duplicateRemovedRows","finalRemovalByRule"]
    assert {key:new_report[key] for key in metrics}=={key:old_report[key] for key in metrics}
    for new_rule,old_rule in zip(new_report["rules"],old_report["rules"]):
        assert [new_rule[key] for key in ("inputRows","outputRows","removedRows","changedCells","issueCount")]==[old_rule[key] for key in ("inputRows","outputRows","removedRows","changedCells","issueCount")]


def test_repeated_datetime_cast_clears_prior_export_format(tmp_path):
    rules=[rule("cast",fieldIds=["field0"],dtype="datetime",outputFormat="%Y"),
           {**rule("cast",fieldIds=["field0"],dtype="datetime"),"id":"cast-again"}]
    run(tmp_path,["date"],[["2026-01-02"]],rules,output="parquet")
    table=pq.read_table(tmp_path/"result.parquet")
    assert table.schema.field("date").type==pa.timestamp("us",tz="UTC")
    assert table.column("date").to_pylist()[0].isoformat()=="2026-01-02T00:00:00+00:00"


def test_download_caps_remaining_attempt_scratch_before_creating_input(tmp_path):
    class CaptureStorage(Storage):
        def download_input(self,key,target,max_bytes=104857600):
            self.maximum=max_bytes
            return super().download_input(key,target,max_bytes)
    storage=CaptureStorage(b"a\n"+b"x\n"*50000)
    message=task(["a"],[],limits={"maxScratchBytes":80000})
    with pytest.raises(InputError,match="maxScratchBytes"):
        execute_task(message,storage,tmp_path,Event(),lambda *_:None)
    assert 0<storage.maximum<80000
    assert not (tmp_path/"input.csv").exists() and not storage.uploaded


def test_sqlite_journal_is_checked_before_commit_and_not_lost(tmp_path):
    engine=DiskEngine(task(["a"],[]),tmp_path,Event(),lambda *_:None)
    try:
        engine.db.execute("INSERT INTO records(seq,original,data) VALUES(1,?,?)",("x"*100000,"x"*100000))
        engine.db.commit()
        baseline=engine.check()
        engine.db.execute("UPDATE records SET data=? WHERE seq=1",("y"*100000,))
        journal=tmp_path/"scratch.sqlite-journal"
        assert journal.stat().st_size>0
        engine.config["maxScratchBytes"]=baseline+max(1,journal.stat().st_size//2)
        with pytest.raises(InputError,match="maxScratchBytes"): engine.commit()
        assert engine.db.in_transaction and journal.exists()
        engine.db.rollback()
        assert engine.db.execute("SELECT substr(data,1,1) FROM records").fetchone()[0]=="x"
    finally: engine.db.close()


def test_amplifying_mutation_flushes_byte_bounded_transactions(tmp_path,monkeypatch):
    calls=[]
    original=DiskEngine.commit
    def observed(engine):
        calls.append(engine.input_rows)
        return original(engine)
    monkeypatch.setattr(DiskEngine,"commit",observed)
    result,_,report,*_=run(tmp_path,["a","b"],[["x",""]]*40,[rule("fill_null",fieldIds=["field1"],value={"type":"string","value":"y"*500})],batch=100,limits={"maxRecordBytes":1024,"maxBatchBytes":2048})
    assert len(result)==40 and report["missingFilledCells"]==40
    assert len(calls)>=40


def test_real_input_download_limit_is_independent_of_stat_and_removes_partial(tmp_path):
    from types import SimpleNamespace
    class Response:
        closed=released=False
        def stream(self,_): yield b"123"; yield b"456"
        def close(self): self.closed=True
        def release_conn(self): self.released=True
    class Client:
        def __init__(self): self.response=Response(); self.called=False
        def stat_object(self,*_): return SimpleNamespace(size=3)
        def get_object(self,*_): self.called=True; return self.response
    storage=ObjectStorage.__new__(ObjectStorage); storage.bucket="test"; storage.client=Client()
    target=tmp_path/"raw.csv"
    with pytest.raises(InputError,match="download byte limit"): storage.download_input("raw/test/data",target,max_bytes=4)
    assert not target.exists() and storage.client.response.closed and storage.client.response.released
    with pytest.raises(InputError): storage.download_input("raw/test/data",target,max_bytes=104857601)


@pytest.mark.parametrize("batch",[1,2,1000])
@pytest.mark.parametrize("dtype",[pa.float32(),pa.float64()])
def test_native_parquet_floats_roundtrip_all_batches_without_default_scale(tmp_path,batch,dtype):
    table=pa.table({"number":pa.array([1.2345,None,0.1,-0.0000001,2.1,12345678.25],dtype)})
    source=tmp_path/"source.parquet"; pq.write_table(table,source,row_group_size=2)
    message=task(["number"],[],fmt="parquet",output="parquet",batch=batch)
    report,_=execute_task(message,Storage(source.read_bytes()),tmp_path/"attempt",Event(),lambda *_:None)
    result=pq.read_table(tmp_path/"attempt/result.parquet")
    assert result.schema.field("number").type==dtype
    assert result.column("number").to_pylist()==table.column("number").to_pylist()
    assert report["changedCells"]==0


def test_native_float_cast_decimal_stays_exact_decimal_and_fill_extends_scale(tmp_path):
    source=tmp_path/"source.parquet"
    pq.write_table(pa.table({"n":[1.2345,None,2.1]}),source,row_group_size=1)
    storage=Storage(source.read_bytes())
    rules=[rule("cast",fieldIds=["field0"],dtype="decimal",scale=4)]
    execute_task(task(["n"],rules,fmt="parquet",output="parquet"),storage,tmp_path/"cast",Event(),lambda *_:None)
    result=pq.read_table(tmp_path/"cast/result.parquet")
    assert result.schema.field("n").type==pa.decimal128(38,4)
    assert result.column("n").to_pylist()==[Decimal("1.2345"),None,Decimal("2.1000")]
    rules=[rule("fill_stat",fieldIds=["field0"],strategy="mean",emptyPolicy="fail",scale=2)]
    execute_task(task(["n"],rules,fmt="parquet",output="parquet",batch=1),storage,tmp_path/"fill",Event(),lambda *_:None)
    result=pq.read_table(tmp_path/"fill/result.parquet")
    assert result.schema.field("n").type==pa.decimal128(38,4)
    assert result.column("n").to_pylist()==[Decimal("1.2345"),Decimal("1.6700"),Decimal("2.1000")]


def test_cast_mark_mixed_profile_applies_only_to_failing_fields(tmp_path):
    rules=[rule("cast",fieldIds=["field0","field1"],dtype="integer",onError="mark"),{**rule("cast",fieldIds=["field0"],dtype="string"),"id":"resolve-first"}]
    _,_,report,_,_=run(tmp_path,["bad","good"],[["bad","2"],["3","4"]],rules,output="parquet",batch=1)
    table=pq.read_table(tmp_path/"result.parquet")
    assert table.schema.field("good").type==pa.int64()
    assert table.column("good").to_pylist()==[2,4]
    assert {item["name"]:item["dtype"] for item in report["columnsAfter"]}=={"bad":"string","good":"integer"}
    assert report["typeConversionFailedCells"]==1


def test_integer_fill_into_source_float_never_loses_int64_precision(tmp_path):
    source=tmp_path/"source.parquet"; pq.write_table(pa.table({"n":[1.25,None]}),source)
    rules=[rule("fill_null",fieldIds=["field0"],value={"type":"integer","value":"9007199254740993"})]
    execute_task(task(["n"],rules,fmt="parquet",output="parquet"),Storage(source.read_bytes()),tmp_path/"attempt",Event(),lambda *_:None)
    table=pq.read_table(tmp_path/"attempt/result.parquet")
    assert table.schema.field("n").type==pa.decimal128(38,2)
    assert table.column("n").to_pylist()==[Decimal("1.25"),Decimal("9007199254740993.00")]


@pytest.mark.parametrize("batch",[1,2,1000])
@pytest.mark.parametrize("removal",["drop","dedup"])
@pytest.mark.parametrize("dtype,good,expected_type,expected",[
    ("integer",["9007199254740993","9223372036854775807"],pa.int64(),[9007199254740993,9223372036854775807]),
    ("decimal",["12345678901234567890123456.123456789012","-0.000000000001"],pa.decimal128(38,12),[Decimal("12345678901234567890123456.123456789012"),Decimal("-0.000000000001")]),
    ("datetime",["2026-01-02T03:04:05Z","2026-01-02T11:04:05+08:00"],pa.timestamp("us",tz="UTC"),["2026-01-02T03:04:05+00:00","2026-01-02T03:04:05+00:00"]),
])
def test_removed_cast_mark_failures_do_not_poison_final_parquet_schema(tmp_path,batch,removal,dtype,good,expected_type,expected):
    cast=rule("cast",fieldIds=["field1"],dtype=dtype,onError="mark")
    if dtype=="decimal": cast.update(precision=38,scale=12)
    cleanup=rule("deduplicate",fieldIds=["field0"],keep="last") if removal=="dedup" else rule("enum",fieldId="field2",values=[{"type":"string","value":"keep"}])
    rows=[["A","invalid","remove"],["A",good[0],"keep"],["B",good[1],"keep"]]
    _,dirty,report,_,_=run(tmp_path,["id","value","selection"],rows,[cast,cleanup],output="parquet",batch=batch)
    table=pq.read_table(tmp_path/"result.parquet")
    assert table.schema.field("value").type==expected_type
    values=table.column("value").to_pylist()
    assert ([value.isoformat() for value in values] if dtype=="datetime" else values)==expected
    assert report["outputRows"]==report["validOutputRows"]==2 and report["flaggedOutputRows"]==0
    assert report["typeConversionFailedCells"]==1 and report["finalRemovedRows"]==1
    assert next(item["dtype"] for item in report["columnsAfter"] if item["id"]=="f1")==dtype
    assert dirty[0]["_final_action"]=="REMOVED_BY_LATER_RULE"


def test_final_survivor_scan_keeps_genuine_mixed_rejection(tmp_path):
    rules=[rule("cast",fieldIds=["field1"],dtype="integer",onError="mark"),rule("enum",fieldId="field2",values=[{"type":"string","value":"keep"}])]
    with pytest.raises(InputError,match="mixed-type"):
        run(tmp_path,["id","value","selection"],[["A","invalid","keep"],["B","9007199254740993","keep"],["C","other-invalid","remove"]],rules,output="parquet",batch=1)


def test_final_survivor_scan_empty_output_restores_declared_decimal_schema(tmp_path):
    rules=[rule("cast",fieldIds=["field0"],dtype="decimal",precision=20,scale=4,onError="mark"),rule("enum",fieldId="field1",values=[{"type":"string","value":"keep"}])]
    _,_,report,_,_=run(tmp_path,["value","selection"],[["bad","remove"]],rules,output="parquet")
    table=pq.read_table(tmp_path/"result.parquet")
    assert table.num_rows==0 and table.schema.field("value").type==pa.decimal128(20,4)
    assert report["outputRows"]==report["flaggedOutputRows"]==0
