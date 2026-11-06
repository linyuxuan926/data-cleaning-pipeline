"""Generate and statically verify the implemented HTTP contract; stdlib only.

Run from any directory: python scripts/generate_openapi.py [--check]
Documents implemented routes and both historical schema1 and disk schema3.
"""
from copy import deepcopy
import argparse
import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "openapi.json"
S = {}
PATHS = {}


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def string(**kwargs):
    return {"type": "string", **kwargs}


def integer(minimum=0, **kwargs):
    return {"type": "integer", "format": "int64", "minimum": minimum, **kwargs}


def enum(*values, **kwargs):
    return string(enum=list(values), **kwargs)


def array(items, **kwargs):
    return {"type": "array", "items": items, **kwargs}


def obj(properties, required=(), closed=True, **kwargs):
    result = {"type": "object", "properties": properties, "additionalProperties": not closed, **kwargs}
    if required:
        result["required"] = list(required)
    return result


def nullable(schema):
    # OAS3.0 nullable only extends an explicitly declared type, not a $ref sibling.
    return {"oneOf": [schema, {"type": "object", "nullable": True, "enum": [None]}]} if "$ref" in schema else {**schema, "nullable": True}


def param(name, location, schema, required=False, description=""):
    return {"name": name, "in": location, "schema": schema, "required": required, "description": description}


UUID = string(format="uuid")
TIME = string(format="date-time")
BOOL = {"type": "boolean"}
NUMBER = {"type": "number", "format": "double", "description": "Finite JSON number; NaN and Infinity are rejected."}
FORMAT = enum("csv", "xlsx", "parquet")
ENGINE = enum("v2-phased", "v3-sequential", "v3-disk")
FIELD = string(minLength=1, maxLength=128, description="Stable fN field ID or exact source field name. Stable IDs take precedence. Reserved metadata names are rejected.")
FIELDS = array(FIELD, minItems=1, maxItems=200, uniqueItems=True)
ACTION = enum("drop", "mark", "fail", default="drop")
NULL_ACTION = enum("drop", "mark", "null", "fail", default="drop")
DTYPE = enum("integer", "number", "string", "datetime", "boolean")
SCALAR = {"oneOf": [string(), {"type": "number"}, BOOL], "description": "Non-null finite scalar. Strings, numbers and booleans remain distinct."}
NULLABLE_SCALAR = {"oneOf": [string(nullable=True), {"type": "number"}, BOOL]}
S["Error"] = obj({"message": string(), "requestId": string(maxLength=128)}, ["message"], description="Java error envelope. requestId is included by controller advice; authentication/entry filters may return message only. X-Request-ID response header is always set by Java.")
S["PythonError"] = obj({"detail": {"oneOf": [string(), array(obj({"loc": array({"oneOf": [string(), integer()]}), "msg": string(), "type": string()}, ["loc", "msg", "type"], closed=False))]}}, ["detail"])
S["Message"] = obj({"message": string()}, ["message"])
S["User"] = obj({"id": UUID, "username": string(), "role": enum("USER", "ADMIN"), "enabled": BOOL}, ["id", "username", "role", "enabled"])
S["AdminUser"] = obj({**S["User"]["properties"], "createdAt": TIME}, ["id", "username", "role", "enabled", "createdAt"])
S["Credentials"] = obj({"username": string(minLength=3, maxLength=64, description="Unicode letters/numbers, underscore or hyphen only."), "password": string(format="password", minLength=10, description="At least 10 characters, at most 72 UTF-8 bytes.")}, ["username", "password"])
S["Session"] = obj({"token": string(minLength=43, maxLength=43), "user": ref("User")}, ["token", "user"], description="Opaque bearer session valid for 24 hours; logout or account disable revokes it.")
S["UserUpdate"] = obj({"role": enum("USER", "ADMIN"), "enabled": BOOL}, ["role", "enabled"])
caps = {"maxFileBytes": 104857600, "maxRows": 500000, "maxColumns": 200, "maxCells": 5000000, "maxExcelExpandedBytes": 536870912, "previewRows": 1000, "maxActivePerUser": 20, "maxActiveGlobal": 100, "submissionsPerMinute": 120,"maxRunningGlobal":8,"maxRunningPerUser":4}
S["Settings"] = obj({name: integer(1, maximum=cap) for name, cap in caps.items()}, [name for name in caps if name not in {"maxRunningGlobal","maxRunningPerUser"}], description="PUT requires nine original keys and rejects extras; omitted maxRunningGlobal/maxRunningPerUser retain current values. User limits cannot exceed global limits. New tasks freeze processing settings; retry checks current quotas. Claim enforces separate running slots; capacity conflicts delay queue execution.")
S["EngineLimits"] = obj({name: integer(1, maximum=cap) for name, cap in caps.items() if name in {"maxFileBytes", "maxRows", "maxColumns", "maxCells", "maxExcelExpandedBytes", "previewRows"}}, closed=False, description="Optional processing limits; known keys may lower but never raise hard caps. Extra platform settings are ignored by Python.")
S["ParseOptions"] = obj({"encoding": enum("auto", "utf-8-sig", "utf-8", "gb18030", "utf-16", "latin-1", default="auto"), "delimiter": enum("auto", ",", ";", "\t", "|", "\\t", default="auto"), "sheetName": string(minLength=1, maxLength=128), "missingValues": array(NULLABLE_SCALAR, maxItems=100, default=[""]), "fieldTypes": {"type": "object", "maxProperties": 200, "additionalProperties": DTYPE, "description": "Keys must match f[0-9]+; explicit type conversion rather than inference."}})
S["ColumnProfile"] = obj({"id": string(pattern="^f[0-9]+$"), "name": string(), "dtype": string(), "inferredType": DTYPE, "nullCount": integer()}, ["id", "name", "dtype", "inferredType", "nullCount"])
S["ArtifactColumn"] = obj({"name": string(), "dtype": enum("artifact"), "nullCount": integer()}, ["name", "dtype", "nullCount"])
S["PreviewRow"] = {"type": "object", "additionalProperties": NULLABLE_SCALAR, "description": "Source/output column names are keys. Datetimes/Decimal values serialize as strings; missing values as null."}
S["SheetSummary"] = obj({"name": string(), "rows": integer(), "columns": integer()}, ["name", "rows", "columns"])
S["Preview"] = obj({"columns": array(ref("ColumnProfile")), "rows": array(ref("PreviewRow")), "rowCount": integer(nullable=True), "previewRowCount": integer(), "parseOptions": ref("ParseOptions"), "sheets": array(ref("SheetSummary")), "warnings": array(string())}, ["columns", "rows", "rowCount", "previewRowCount", "parseOptions", "sheets", "warnings"], description="Bounded raw preview; rowCount is null until the reader knows the complete count. Inference/nullCount describe sampled rows. This is not full-task capacity validation. sheets contains XLSX sheet summaries and is empty for CSV/Parquet.")
S["ArtifactPreview"] = obj({"columns": array(ref("ArtifactColumn")), "rows": array(ref("PreviewRow")), "rowCount": integer(nullable=True), "previewRowCount": integer(maximum=100), "warnings": array(string())}, ["columns", "rows", "rowCount", "previewRowCount", "warnings"], description="Reads at most limit+1 records, returns at most limit; rowCount=null if more exist. Metadata headers and header-only empty artifacts are supported.")
S["FileInit"] = obj({"name": string(minLength=1, maxLength=255, description="Filename must end in .csv/.xlsx/.parquet; no path separators/control characters."), "size": integer(1, maximum=104857600), "chunkSize": integer(262144, maximum=8388608, default=4194304), "sha256": string(pattern="^[a-fA-F0-9]{64}$"), "parseOptions": ref("ParseOptions")}, ["name", "size"])
S["UploadInit"] = obj({"id": UUID, "uploadId": UUID, "chunkSize": integer(262144, maximum=8388608), "totalChunks": integer(1)}, ["id", "uploadId", "chunkSize", "totalChunks"])
S["File"] = obj({"id": UUID, "uploadId": UUID, "name": string(), "format": FORMAT, "size": integer(1), "status": enum("UPLOADING", "READY", "EXPIRED"), "sha256": string(nullable=True), "parseStatus": enum("UNPARSED", "PARSED", "FAILED"), "parseError": string(nullable=True), "parseOptions": ref("ParseOptions"), "fields": array(ref("ColumnProfile")), "uploader": obj({"id": UUID, "username": string(nullable=True)}, ["id", "username"]), "objectKey": string(nullable=True), "createdAt": TIME}, ["id", "uploadId", "name", "format", "size", "status", "sha256", "parseStatus", "parseError", "parseOptions", "fields", "uploader", "objectKey", "createdAt"])
S["UploadProgress"] = obj({"id": UUID, "uploadId": UUID, "status": enum("UPLOADING", "READY", "EXPIRED"), "name": string(), "size": integer(1), "chunkSize": integer(262144, maximum=8388608), "totalChunks": integer(1), "receivedChunks": array(integer(), uniqueItems=True)}, ["id", "uploadId", "status", "name", "size", "chunkSize", "totalChunks", "receivedChunks"], description="receivedChunks lists valid on-disk chunks only; completed uploads clean temporary chunks, so READY may return an empty list.")

rule_options = {
    "trim": ({"columns": FIELDS}, ["columns"], "Trim strings only; nonstrings unchanged."),
    "case": ({"columns": FIELDS, "mode": enum("lower", "upper")}, ["columns", "mode"], "Unicode lower/upper on strings only."),
    "cast": ({"columns": FIELDS, "dtype": DTYPE, "dateFormat": string(minLength=1, maxLength=128), "outputFormat": string(minLength=1, maxLength=128), "precision": integer(maximum=12), "onError": ACTION}, ["columns", "dtype"], "integer is exact signed int64. number is finite float unless precision (0..12) requests Decimal HALF_UP. precision requires number; dateFormat/outputFormat require datetime and portable strftime directives. No decimal/scale logical-field API exists."),
    "validate_type": ({"columns": FIELDS, "dtype": DTYPE, "dateFormat": string(minLength=1, maxLength=128), "onError": ACTION}, ["columns", "dtype"], "Validate convertibility without changing values; dateFormat only with datetime."),
    "missing": ({"columns": FIELDS, "markers": array(NULLABLE_SCALAR, minItems=1, maxItems=100)}, ["columns", "markers"], "Normalize matching typed markers to null; marker strings <=10000 characters."),
    "fill_null": ({"columns": FIELDS, "value": SCALAR}, ["columns", "value"], "Fill only missing values; string constant <=10000 characters."),
    "fill_stat": ({"columns": FIELDS, "strategy": enum("mean", "median", "mode"), "emptyPolicy": enum("fail", "leave", "constant"), "value": SCALAR}, ["columns", "strategy", "emptyPolicy"], "Global current-survivor mean/median/mode. Invalid numeric samples excluded/reported. constant requires non-null value <=10000 characters; value forbidden for fail/leave. Mode ties use earliest source record. Statistics currently use full-task in-memory DataFrame, not streaming."),
    "drop_null": ({"columns": FIELDS, "onError": ACTION}, ["columns"], "One issue per selected missing field; one removal per source record."),
    "required": ({"columns": FIELDS, "onError": ACTION}, ["columns"], "Missing values only; does not implicitly trim or normalize empty text."),
    "range": ({"column": FIELD, "min": NUMBER, "max": NUMBER, "inclusiveMin": {**BOOL, "default": True}, "inclusiveMax": {**BOOL, "default": True}, "onError": NULL_ACTION}, ["column"], "At least one finite numeric bound, min<=max. Missing skipped; current implementation also parses numeric strings for comparison. Exact Decimal comparison preserves int64 precision."),
    "iqr": ({"column": FIELD, "emptyPolicy": enum("fail", "leave"), "factor": {"type": "number", "minimum": 0, "maximum": 100, "default": 1.5}, "onError": NULL_ACTION}, ["column", "emptyPolicy"], "Full current-survivor sample with linear quartiles; zero IQR still enforces equal bounds. Numeric strings accepted; absent valid values follow emptyPolicy."),
    "enum": ({"column": FIELD, "values": array(NULLABLE_SCALAR, minItems=1, maxItems=1000), "onError": ACTION}, ["column", "values"], "Typed membership; null skipped. Boolean differs from number and numeric string differs from number."),
    "length": ({"column": FIELD, "min": integer(maximum=1000000), "max": integer(maximum=1000000), "onError": ACTION}, ["column"], "At least one inclusive bound, min<=max. Unicode code points; nonstring values violate; null skipped."),
    "regex": ({"column": FIELD, "pattern": string(minLength=1, maxLength=256), "onError": ACTION}, ["column", "pattern"], "Full match against str(value), null skipped. Lookaround/special groups/backreferences/repeated groups rejected. Per-value 10ms and per-rule 10s timeout fails task."),
    "compare": ({"column": FIELD, "operator": enum("eq", "ne", "gt", "gte", "lt", "lte", "contains", "starts_with", "ends_with"), "value": SCALAR, "onError": ACTION}, ["column", "operator", "value"], "Compare a field with a constant, not another field. Null skipped. Ordering numeric target parses source numeric strings; equality is typed."),
    "deduplicate": ({"columns": array(FIELD, maxItems=200, uniqueItems=True), "keep": enum("first", "last", default="first"), "onError": ACTION}, [], "Global current-survivor duplicate keys; omitted/empty columns means every current field. null keys compare equal. One issue per duplicate record."),
    "select_fields": ({"columns": FIELDS}, ["columns"], "Keep selected current fields in configured order. Later references to removed fields fail."),
    "drop_fields": ({"columns": FIELDS}, ["columns"], "Remove selected current fields; cannot remove every field."),
    "rename": ({"column": FIELD, "target": FIELD}, ["column", "target"], "Change display name without changing stable fN identity; duplicate targets rejected."),
}
rule_names = []
seq_names = []
for kind, (options, required, description) in rule_options.items():
    name = "Rule" + "".join(part.title() for part in kind.split("_"))
    rule_names.append(name)
    properties = {"type": enum(kind), **deepcopy(options)}
    schema = obj(properties, ["type", *required], description=description)
    if kind in {"range", "length"}:
        schema["anyOf"] = [{"required": ["min"]}, {"required": ["max"]}]
    if kind == "fill_stat":
        schema["oneOf"] = [
            {"properties": {"emptyPolicy": enum("constant")}, "required": ["value"]},
            {"properties": {"emptyPolicy": enum("fail", "leave")}, "not": {"required": ["value"]}},
        ]
    S[name] = schema
    seq = deepcopy(schema)
    seq["properties"].update({"id": string(minLength=1, maxLength=128, description="Unique within one rule array; omitted => rule-N."), "enabled": {**BOOL, "default": True}})
    if kind == "cast":
        seq["properties"]["onError"] = NULL_ACTION
    seq_name = "Sequential" + name
    seq_names.append(seq_name)
    S[seq_name] = seq
S["Rule"] = {"oneOf": [ref(name) for name in rule_names], "discriminator": {"propertyName": "type", "mapping": {kind: f"#/components/schemas/{name}" for kind, name in zip(rule_options, rule_names)}}}
S["SequentialRule"] = {"oneOf": [ref(name) for name in seq_names], "discriminator": {"propertyName": "type", "mapping": {kind: f"#/components/schemas/{name}" for kind, name in zip(rule_options, seq_names)}}, "description": "Only existing columns/column rule parameters. Optional id/enabled; disabled rules are validated and skipped. No logicalFields, fieldIds, decimal dtype, scale, replace or compare_fields support."}
S["Rules"] = array(ref("Rule"), maxItems=50, description="Empty array is valid. Serialized rules must be <=100000 UTF-8 bytes. Public rule-set persistence currently uses strict V2 validation even when a later task uses V3 sequential execution.")
S["SequentialRules"] = array(ref("SequentialRule"), maxItems=50)
S["RuleSetInput"] = obj({"name": string(minLength=1, maxLength=100), "rules": ref("Rules")}, ["name", "rules"])
S["RuleSetVersion"] = obj({"id": UUID, "name": string(), "version": integer(1), "rules": ref("Rules"), "createdAt": TIME}, ["id", "name", "version", "rules", "createdAt"])
S["TaskStatus"] = enum("QUEUED", "RUNNING", "CANCELING", "SUCCEEDED", "FAILED", "CANCELED", description="CANCELLED is normalized to CANCELED on public reads. Active quota counts QUEUED/RUNNING/CANCELING. Terminal attempts never regress.")
S["TaskInputSnapshot"] = obj({"fileId": UUID, "objectKey": string(pattern="^raw/", minLength=5, maxLength=512), "format": FORMAT, "name": string(minLength=1, maxLength=255), "parseOptions": ref("ParseOptions"), "fields": nullable(array(ref("ColumnProfile")))}, ["objectKey", "format", "name"], description="Immutable per-task source snapshot. fileId/fields may be absent on legacy snapshots; new Java snapshots include them.")
S["TaskCreate"] = obj({"fileId": UUID, "ruleSetId": UUID, "ruleVersion": integer(1), "outputFormat": FORMAT, "engineVersion": {**ENGINE, "default": "v3-sequential"}}, ["fileId", "ruleSetId", "ruleVersion", "outputFormat"])
task_props = {"id": UUID, "ownerId": UUID, "fileId": UUID, "fileName": string(), "ruleSetId": UUID, "ruleVersion": integer(1), "outputFormat": FORMAT, "engineVersion": ENGINE, "status": ref("TaskStatus"), "progress": integer(maximum=100), "attempt": integer(1), "attemptId": UUID, "currentAttemptId": UUID, "error": string(nullable=True), "createdAt": TIME, "updatedAt": TIME}
S["Task"] = obj(task_props, task_props, description="Task view; attemptId/currentAttemptId identify the current UUID attempt. Numeric attempt is retained for compatibility and artifact paths.")
S["TaskDetail"] = obj({**task_props, "rulesSnapshot": nullable(ref("Rules")), "inputSnapshot": nullable(ref("TaskInputSnapshot")), "limitsSnapshot": nullable(ref("Settings"))}, [*task_props, "rulesSnapshot", "inputSnapshot", "limitsSnapshot"])
S["AdminTask"] = obj({**task_props, "username": string(), "userId": UUID}, [*task_props, "username", "userId"])
attempt_props = {"attemptId": UUID, "taskId": UUID, "attempt": integer(1), "attemptNo": integer(1), "engineVersion": ENGINE, "status": ref("TaskStatus"), "progress": integer(maximum=100), "error": string(nullable=True), "leaseAt": nullable(TIME), "startedAt": nullable(TIME), "finishedAt": nullable(TIME), "createdAt": TIME, "updatedAt": TIME}
S["Attempt"] = obj(attempt_props, attempt_props, description="Historical attempt metadata remains readable after retry; attempt/attemptNo are exact aliases.")
S["Artifacts"] = obj({"result": string(pattern="^results/"), "dirty": string(pattern="^dirty/"), "report": string(pattern="^reports/")}, ["result", "dirty", "report"], description="Exact paths required: results/{taskId}/{attempt}/result.{outputFormat}; dirty/{taskId}/{attempt}/dirty.csv; reports/{taskId}/{attempt}/report.json.")
S["AttemptDetail"] = obj({**attempt_props, "report": nullable(ref("Report")), "artifacts": nullable(ref("Artifacts"))}, [*attempt_props, "report", "artifacts"])
S["TaskLog"] = obj({"id": integer(), "attemptId": nullable(UUID), "requestId": string(nullable=True), "level": enum("INFO", "WARN", "ERROR"), "message": string(), "createdAt": TIME}, ["id", "attemptId", "requestId", "level", "message", "createdAt"])
rule_metric_names = ["inputRows", "outputRows", "rejectedRows", "changedCells", "issueCount", "flaggedRows"]
seq_metric_names = ["removedRows", "markedRows", "nulledCells", "missingFilledCells", "typeConversionFailedCells", "duplicateRemovedRows", "outlierHandledRows"]
S["FillFieldStatistics"] = obj({"sampleCount": integer(), "invalidValues": integer(), "fillValue": NULLABLE_SCALAR, "emptyPolicy": enum("fail", "leave", "constant")}, ["sampleCount", "invalidValues", "fillValue", "emptyPolicy"])
S["FillStatistics"] = {"type": "object", "additionalProperties": ref("FillFieldStatistics"), "description": "Keys are stable fN field IDs; numeric invalid samples are excluded, mode includes nonmissing typed values."}
S["IqrStatistics"] = obj({"sampleCount": integer(), "invalidValues": integer(), "factor": NUMBER, **{name: {"type": "number", "nullable": True} for name in ["q1", "q3", "lower", "upper"]}}, ["sampleCount", "invalidValues", "factor", "q1", "q3", "lower", "upper"])
S["RuleReport"] = obj({"index": integer(1), "executionOrder": integer(1, nullable=True), "type": enum(*rule_options), "phase": integer(), "ruleId": string(), "status": enum("COMPLETED", "DISABLED"), **{name: integer() for name in rule_metric_names + seq_metric_names}, "statistics": {"oneOf": [ref("FillStatistics"), ref("IqrStatistics")]}}, ["index", "executionOrder", "type", *rule_metric_names], description="V2 has phase. V3 has ruleId/status and action metrics; disabled entries have zero metrics and executionOrder=null.")
report_metric_names = ["inputRows", "outputRows", "removedRows", "dirtyRows", "flaggedRows", "issueCount", "changedCells"]
new_metrics = {"validOutputRows": "Final output records with no quality issue.", "flaggedOutputRows": "Final output records with mark/null issues, including nulled values later filled.", "finalRemovedRows": "Unique source records removed exactly once.", "duplicateRemovedRows": "Subset of final removals attributed to deduplicate.", "outlierHandledRows": "Unique source records acted on by range/IQR; may overlap other event metrics.", "missingFilledCells": "Rule x source record x field events changing missing to nonmissing; repeated refill counts again.", "typeConversionFailedCells": "Cast failure events, including repeated cast rules on the same field.", "issueRows": "Unique source records with >=1 issue, including removed records."}
report_props = {"taskId": UUID, "attempt": integer(1), "attemptId": UUID, "engineVersion": ENGINE, **{name: integer() for name in report_metric_names}, "durationSeconds": {"type": "number", "minimum": 0}, "rules": array(ref("RuleReport")), "executionOrder": array({"oneOf": [integer(1), string()]}), "ruleSnapshot": {"anyOf": [ref("Rules"), ref("SequentialRules")]}, "columnsBefore": array(ref("ColumnProfile")), "columnsAfter": array(ref("ColumnProfile")), "warnings": array(string()), "parseOptions": ref("ParseOptions"), "limitsSnapshot": ref("EngineLimits"), "reportSchemaVersion": integer(3, maximum=3), **{name: integer(description=description) for name, description in new_metrics.items()}, "finalRemovalByRule": {"type": "object", "additionalProperties": integer(), "description": "Rule ID -> first actual unique removals; sum equals finalRemovedRows. Only rules that removed rows appear."}}
S["Report"] = obj(report_props, ["taskId", "attempt", "inputRows", "outputRows", "dirtyRows", "changedCells"], closed=False, description="Current V2/V3 and readable legacy reports. V3 fields are top-level, not nested under metrics. V3: inputRows=outputRows+finalRemovedRows; outputRows=validOutputRows+flaggedOutputRows. removedRows/flaggedRows/dirtyRows are exact aliases for finalRemovedRows/flaggedOutputRows/issueRows. Counts are integers; changedCells and fill/failure events are not unique final cells. Implementation uses bounded full DataFrame; streaming, logical bindings and compare_fields remain unimplemented.")
S["V3Report"] = {"allOf": [ref("Report"), {"required": ["attemptId", "engineVersion", "reportSchemaVersion", *new_metrics, "finalRemovalByRule"]}]}
S["V3DirtyTrace"] = obj({"_record_id": string(), "_source_row": integer(1), "_field": string(), "_rule_index": integer(1), "_error_code": string(), "_reason": string(), "_file_id": nullable(UUID), "_sheet": string(nullable=True), "_rule_id": string(), "_before_value": string(description="Typed JSON string of the value before that rule; dedup uses a typed key-field record. Original source columns are also retained separately."), "_action": enum("drop", "mark", "null"), "_final_action": enum("REMOVED_BY_THIS_RULE", "REMOVED_BY_LATER_RULE", "OUTPUT_FLAGGED")}, closed=False, description="CSV export schema, one row per issue plus immutable original source columns. V2 exports only the first six metadata columns. Source row is a 1-based logical data record, excluding header; embedded CSV newlines do not increment it.")
message_props = {"taskId": UUID, "attempt": integer(1, maximum=1000000), "attemptId": UUID, "requestId": string(minLength=1, maxLength=128, pattern="^[!-~]+$"), "input": ref("TaskInputSnapshot"), "outputFormat": FORMAT, "limits": ref("EngineLimits")}
message_required = ["taskId", "attempt", "input", "rules", "outputFormat"]
S["PhasedTaskMessage"] = obj({**message_props, "engineVersion": enum("v2-phased", default="v2-phased"), "rules": ref("Rules")}, message_required)
S["SequentialTaskMessage"] = obj({**message_props, "engineVersion": enum("v3-sequential"), "rules": ref("SequentialRules")}, [*message_required, "attemptId", "engineVersion"])
S["TaskMessage"] = {"oneOf": [ref("PhasedTaskMessage"), ref("SequentialTaskMessage")], "description": "RabbitMQ payload, not a public HTTP submit endpoint. V3 requires attemptId; missing engineVersion means frozen V2. New Java messages include requestId/attemptId UUIDs and numeric attempt. Maximum encoded payload128KiB. Per-schema rules are checked by execution validation, after envelope claim."}
S["Claim"] = obj({"attempt": integer(1), "attemptId": UUID}, description="V3 requires matching attemptId UUID; numeric attempt retained. V2 may omit UUID but requires numeric attempt.")
S["ClaimResponse"] = {"oneOf": [obj({"execute": {"type": "boolean", "enum": [False]}}, ["execute"]), obj({"execute": {"type": "boolean", "enum": [True]}, "leaseToken": UUID, "attempt": integer(1), "attemptId": UUID}, ["execute", "leaseToken", "attempt", "attemptId"])]}
S["ControlResponse"] = obj({"cancelled": BOOL, "status": ref("TaskStatus"), "attemptId": UUID}, ["cancelled", "status", "attemptId"])
S["WorkerEvent"] = obj({"attempt": integer(1), "attemptId": UUID, "leaseToken": UUID, "status": enum("RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"), "progress": integer(maximum=100), "message": string(description="Truncated by Java to 2000 characters."), "report": ref("Report"), "artifacts": ref("Artifacts")}, ["status", "progress"], description="V3 requires attemptId; V2 requires numeric attempt if UUID absent. Current leaseToken required for live attempt. SUCCEEDED requires report/artifacts; report taskId/attempt/attemptId/engineVersion must match. All three objects must exist and be nonempty; report size <=2000000 Java characters.")
S["InternalPreviewRequest"] = obj({"objectKey": string(pattern="^raw/", minLength=5, maxLength=512, description="No .. or backslash; only MinIO raw object keys, never URLs."), "format": FORMAT, "limit": integer(1, maximum=1000, default=20), "parseOptions": ref("ParseOptions"), "limits": ref("EngineLimits")}, ["objectKey", "format"])
S["ArtifactPreviewRequest"] = obj({"objectKey": string(pattern="^(results|dirty)/", minLength=5, maxLength=512, description="No .., backslash or colon. Download bounded to 256 MiB independently of stat size."), "format": FORMAT, "limit": integer(1, maximum=100, default=100)}, ["objectKey", "format"])
S["RuleValidationRequest"] = {"oneOf": [obj({"engineVersion": enum("v2-phased", default="v2-phased"), "rules": ref("Rules")}, ["rules"]), obj({"engineVersion": enum("v3-sequential"), "rules": ref("SequentialRules")}, ["engineVersion", "rules"])]}


def response(schema, description="Success", media="application/json"):
    return {"description": description, "content": {media: {"schema": schema}}}


def operation(path, method, summary, schema, *, body=None, code="200", description="", params=(), public=False, internal=False, python=False, admin=False, errors=()):
    security = [] if public else [{"internalToken": []}] if internal else [{"bearerAuth": []}]
    op = {"summary": summary, "description": description, "operationId": re.sub(r"[^A-Za-z0-9]+", "_", method + "_" + path).strip("_"), "tags": ["Python internal" if python else "Java internal" if internal else "Administration" if admin else path.split("/")[2].title()], "security": security, "responses": {code: response(schema)}, "parameters": []}
    if python:
        op["servers"] = [{"url": "http://python-api:8000", "description": "Compose internal network only; browsers call Java."}]
    for name in re.findall(r"\{([^}]+)\}", path):
        p_schema = integer() if name == "index" else integer(3,maximum=3) if name == "version" else enum("result", "dirty", "report") if name == "kind" else UUID
        op["parameters"].append(param(name, "path", p_schema, True))
    op["parameters"].extend(deepcopy(list(params)))
    op["parameters"].append(param("X-Request-ID", "header", string(maxLength=128, pattern="^[A-Za-z0-9._:-]+$"), description="Optional correlation ID; Java replaces invalid/missing values with UUID and returns X-Request-ID. Worker falls back to taskId for legacy messages."))
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    standard = {"400", "401", "404", "409", "413", "429", "500", "502"} if not python else {"401", "422", "429", "500", "502"}
    if public:
        standard = {"400", "401", "403", "409", "413", "429", "500"} if not python else {"500"}
    if admin:
        standard.add("403")
        op["description"] += " Requires ADMIN; ordinary users receive 403."
    for error in sorted(standard | set(errors)):
        op["responses"][error] = {"$ref": f"#/components/responses/{'Python' if python else 'Java'}{error}"}
    if not python:
        op["responses"][code]["headers"] = {"X-Request-ID": {"schema": string(), "description": "Correlation ID used for this Java request."}}
    PATHS.setdefault(path, {})[method] = op
    return op


operation("/api/auth/register", "post", "Register a USER and start a session", ref("Session"), body=ref("Credentials"), code="201", public=True, description="Registration may be disabled (403). Duplicate username:409. Login/register share an IP limit of 30 requests/minute.")
operation("/api/auth/login", "post", "Start a bearer session", ref("Session"), body=ref("Credentials"), public=True, description="Invalid credentials/disabled user:401. Session lasts 24 hours.")
operation("/api/auth/me", "get", "Read current user", ref("User"))
operation("/api/auth/logout", "post", "Revoke current bearer token", ref("Message"))
operation("/api/settings", "get", "Read current platform settings", ref("Settings"))
operation("/api/admin/users", "get", "List up to 500 users", array(ref("AdminUser"), maxItems=500), admin=True)
operation("/api/admin/users/{id}", "put", "Update role and enabled status", ref("User"), body=ref("UserUpdate"), admin=True, description="Cannot demote/disable the last enabled administrator (409). Disabling revokes sessions.")
operation("/api/admin/tasks", "get", "List up to 500 platform tasks", array(ref("AdminTask"), maxItems=500), admin=True)
operation("/api/admin/settings", "put", "Replace complete platform settings", ref("Settings"), body=ref("Settings"), admin=True)
operation("/api/files", "get", "List own latest 200 files", array(ref("File"), maxItems=200))
operation("/api/files/{id}", "get", "Read own file metadata", ref("File"))
operation("/api/files/init", "post", "Reserve a chunked upload", ref("UploadInit"), body=ref("FileInit"), code="201", description="Each call creates new fileId/uploadId UUIDs; no idempotency key. Incomplete-upload caps: user 20 files/512 MiB; platform 200 files/5 GiB. Expire after 24h.")
operation("/api/uploads/{uploadId}", "get", "Inspect resumable upload progress", ref("UploadProgress"))
for path in ["/api/uploads/{uploadId}/chunks/{index}", "/api/files/{id}/chunks/{index}"]:
    op = operation(path, "put", "Write one zero-based chunk", ref("Message"), description="Raw body, not multipart. Exact chunk length required (last may be shorter). Repeating same bytes overwrites the same chunk atomically; only UPLOADING accepted. Legacy /files path is supported.")
    op["requestBody"] = {"required": True, "content": {"application/octet-stream": {"schema": string(format="binary", maxLength=8388608)}}}
for path in ["/api/uploads/{uploadId}/complete", "/api/files/{id}/complete"]:
    operation(path, "post", "Merge chunks, verify digest, save raw object and parse", ref("File"), description="Idempotent after READY; repeat returns existing metadata without reparsing. Missing/corrupt chunks:409; digest mismatch:400 and stays UPLOADING. After successful raw save, parse error returns 200 READY/FAILED with parseError; reparse via /parse. Legacy /files path is supported.")
operation("/api/files/{id}/parse", "post", "Reparse READY file and persist schema/options", ref("File"), body=ref("ParseOptions"), description="Body optional (defaults {}). Must be READY. Parser failure is returned as File.parseStatus=FAILED/parseError with HTTP200; raw object remains. Does not alter existing task snapshots.")["requestBody"]["required"] = False
operation("/api/files/{id}/preview", "get", "Preview raw file using persisted parse options", ref("Preview"), params=[param("limit", "query", integer(1, maximum=1000), description="Default current settings.previewRows; Python additionally enforces current previewRows limit.")], description="File must be READY. Does not overwrite parse/task snapshots. Bounded preview does not prove the complete input fits execution budgets.")
operation("/api/rule-sets", "get", "List own latest rule-set versions", array(ref("RuleSetVersion"), maxItems=200))
operation("/api/rule-sets", "post", "Create immutable rule version 1", ref("RuleSetVersion"), body=ref("RuleSetInput"), code="201", description="Strict current public rule shape: no id/enabled metadata. Python validates V2 rule parameters. Unavailable Python:502; no fake success is persisted.")
operation("/api/rule-sets/{id}", "put", "Append an immutable rule-set version", ref("RuleSetVersion"), body=ref("RuleSetInput"), description="Every successful PUT increments the version even if content is unchanged; not content-idempotent. Historical versions stay unchanged.")
operation("/api/rule-sets/{id}/versions", "get", "List latest 200 immutable versions", array(ref("RuleSetVersion"), maxItems=200))
operation("/api/tasks", "get", "List own latest 200 tasks", array(ref("Task"), maxItems=200))
op = operation("/api/tasks", "post", "Create asynchronous cleaning task", ref("TaskDetail"), body=ref("TaskCreate"), code="201", params=[param("Idempotency-Key", "header", string(minLength=1, maxLength=200, pattern="^[!-~]+$"), description="Optional, scoped to current user for seven days. Same exact explicit request replays original saved response with 200; different request returns 409 IDEMPOTENCY_KEY_REUSED.")], description="READY/PARSED owned file and owned rule version required. Freezes rules/input/processing limits and engineVersion. New default is v3-sequential; v2-phased explicitly preserves phase sorting/deferred validation drops. Quota/frequency failures:429. Same key replays before quota checks and snapshot rereads, including concurrent identical requests; replay returns original create response (possibly QUEUED), not refreshed status. Omitted engineVersion and explicit v3-sequential are different explicit payloads for hashing. No key means independent creates. RabbitMQ transactional outbox decouples submission/execution; returns before worker completion.")
op["responses"]["200"] = response(ref("TaskDetail"), "Idempotent replay of stored original response")
operation("/api/tasks/{id}", "get", "Read task with immutable snapshots", ref("TaskDetail"), description="Owner or ADMIN. Poll active state every 2-3 seconds.")
operation("/api/tasks/{id}/attempts", "get", "List task attempts newest first", array(ref("Attempt")), description="Owner or ADMIN. Old attempt metadata/logs remain immutable after terminal state.")
operation("/api/tasks/{id}/attempts/{attemptId}", "get", "Read a historical UUID attempt", ref("AttemptDetail"), description="Owner or ADMIN; current and historical attempts use same authorization.")
operation("/api/tasks/{id}/cancel", "post", "Cancel queued task or request running cancellation", ref("TaskDetail"), description="Owner only, including ADMIN-owned resources. QUEUED -> CANCELED immediately; RUNNING -> CANCELING. Repeating CANCELING/CANCELED returns current state. Other terminal states:409. Only worker after execution stops confirms CANCELED. Late success/heartbeat cannot override CANCELING. Expired cancellation lease becomes FAILED/CANCEL_UNCONFIRMED, never false cancellation success.")
operation("/api/tasks/{id}/retry", "post", "Create a new attempt on failed/canceled task", ref("TaskDetail"), description="Owner only. FAILED/CANCELED required; otherwise409. Keeps task ID and frozen engine/rules/input/limits, increments numeric attempt and creates new attemptId UUID. Old attempt reports/logs/artifacts remain readable. Retry is not idempotent and still checks current submission/concurrency quotas. Running lease expiry (>120s) similarly creates a fresh attempt only when numeric attempt<3; queue timeout is1h.")
for suffix in ["", "/attempts/{attemptId}"]:
    base = "/api/tasks/{id}" + suffix
    operation(base + "/logs", "get", "Read incremental execution logs", array(ref("TaskLog"), maxItems=500), params=[param("after", "query", integer(default=0))], description="Owner or ADMIN. Ascending numeric IDs, up to500; resume from last received ID. Task-level route includes all attempts, attempt route filters UUID.")
    operation(base + "/report", "get", "Read successful execution report", ref("Report"), description="Owner or ADMIN. Only SUCCEEDED attempt; otherwise409. Task route reads current attempt, UUID route preserves historical report.")
    op = operation(base + "/artifacts/{kind}", "get", "Download authorized artifact", string(format="binary"), description="Owner or ADMIN; only SUCCEEDED attempt (409 otherwise). Kinds result/dirty/report; unknown kind404. Java streams object bytes with attachment filename and Cache-Control:no-store; no public MinIO URL.")
    op["responses"]["200"]["content"] = {"application/octet-stream": {"schema": string(format="binary")}}
    op["responses"]["200"]["headers"].update({"Content-Disposition": {"schema": string()}, "Cache-Control": {"schema": enum("no-store")}})
    operation(base + "/preview", "get", "Preview successful result or dirty data", ref("ArtifactPreview"), params=[param("kind", "query", enum("result", "dirty", default="result")), param("limit", "query", integer(1, maximum=100, default=100))], description="Owner or ADMIN, SUCCEEDED attempt only. Current task route or historical UUID route. Dirty format always CSV; result format is task outputFormat. Metadata headers and empty results supported; download bounded to256MiB.")
operation("/internal/tasks/{id}/claim", "post", "Acquire current attempt lease", ref("ClaimResponse"), body=ref("Claim"), internal=True, description="Trusted worker only. V3 UUID required; absent UUID400. Stale attempt/terminal/CANCELING returns execute:false (safe to ACK). Already RUNNING live lease409: retry/requeue, not ACK. Successful claim sets RUNNING/progress1 and returns leaseToken. One task per worker process; prefetch1.")
operation("/internal/tasks/{id}/control", "get", "Inspect lease and cancellation", ref("ControlResponse"), internal=True, params=[param("attempt", "query", integer(1), True), param("attemptId", "query", UUID, description="Required for V3."), param("leaseToken", "query", UUID, True)], description="cancelled=true for stale attempt/token or any state other than RUNNING. Worker cooperatively stops and reports CANCELED only after its executor finishes.")
operation("/internal/tasks/{id}/events", "post", "Commit worker heartbeat or terminal event", obj({"accepted": {"type": "boolean", "enum": [True]}}, ["accepted"]), body=ref("WorkerEvent"), internal=True, description="Stale/terminal callbacks acknowledged as accepted:true but ignored. Live wrong lease409. RUNNING progress monotonic and capped99; heartbeat renews lease120s. CANCELING ignores RUNNING/SUCCEEDED; CANCELED without cancellation409. SUCCEEDED validates report conservation, identity, paths, object presence. Worker ACKs RabbitMQ only after terminal HTTP accepted; transient callback failure retains delivery.")
operation("/health", "get", "Python process health", obj({"status": enum("UP")}, ["status"]), public=True, python=True, description="Internal process probe; does not imply dependencies or worker main-loop liveness.")
operation("/internal/preview", "post", "Preview raw MinIO input", ref("Preview"), body=ref("InternalPreviewRequest"), internal=True, python=True, description="Protected by X-Internal-Token, raw/ only, 100MiB download cap. Shares two preview slots per API process with artifact preview; busy429. Input/parse/limit errors422, storage502. Reserved metadata headers rejected in raw inputs.")
operation("/internal/artifact-preview", "post", "Preview trusted task artifact", ref("ArtifactPreview"), body=ref("ArtifactPreviewRequest"), internal=True, python=True, description="Protected internal object reference only; results/ or dirty/, 256MiB bounded download. Shares two preview slots per process; reserved output metadata headers accepted. Bad object/parse422, busy429, storage502.")
operation("/internal/validate-rules", "post", "Authoritatively validate current rule parameters", obj({"valid": {"type": "boolean", "enum": [True]}}, ["valid"]), body=ref("RuleValidationRequest"), internal=True, python=True, description="V2 default strict19 types and options. V3 uses same parameters with optional id/enabled (defaults rule-N/true), unique IDs and cast null action. No planned logical-field schema or compare_fields. Invalid422. Java public rule-save gateway currently omits engineVersion and therefore validates V2.")

def literal_assignment(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement,ast.Assign) and any(isinstance(target,ast.Name) and target.id == name for target in statement.targets):
            return ast.literal_eval(statement.value)
    raise AssertionError(f"Missing source assignment {path}:{name}")


def disk_contract():
    """Read the runtime schema factory without importing pandas or PyArrow."""
    options = literal_assignment("engine/cleaning/bindings.py", "OPTIONS")
    tree = ast.parse((ROOT / "engine/cleaning/ruleschemas.py").read_text(encoding="utf-8"))
    definition = next(statement for statement in tree.body if isinstance(statement,ast.FunctionDef) and statement.name == "rule_schemas")
    namespace = {"OPTIONS":options,"deepcopy":deepcopy}
    exec(compile(ast.Module(body=[definition],type_ignores=[]),"runtime-rule-schema-factory","exec"),namespace)
    return namespace["rule_schemas"](), options


def oas_schema(value):
    """Convert runtime Draft2020-12 primitives to equivalent OAS3.0 schemas."""
    if isinstance(value,list): return [oas_schema(item) for item in value]
    if not isinstance(value,dict): return value
    result = {key:oas_schema(item) for key,item in value.items() if key != "$schema"}
    if "const" in result: result["enum"] = [result.pop("const")]
    if result.get("type") == "null": return {"type":"object","nullable":True,"enum":[None]}
    if isinstance(result.get("type"),list):
        kinds = result.pop("type")
        result["oneOf"] = [{"type":kind} for kind in kinds]
    return result


DISK_CONTRACT, DISK_OPTIONS = disk_contract()
disk_names = {}
for kind,definition in DISK_CONTRACT["rules"].items():
    name = "DiskRule" + "".join(part.title() for part in kind.split("_"))
    disk_names[kind] = name
    S[name] = oas_schema(definition)
S["DiskRule"] = {"oneOf":[ref(name) for name in disk_names.values()],"discriminator":{"propertyName":"type","mapping":{kind:f"#/components/schemas/{name}" for kind,name in disk_names.items()}},"description":"Schema3:21 strict rule types. id/enabled required; logical field IDs, exact typed scalars, decimal precision<=38/scale<=12/HALF_UP. Configured order executes over current survivors. Additional semantic validation enforces parameter dependencies, type compatibility, safe regex, field lifecycle and decimal domain."}
S["DiskRules"] = array(ref("DiskRule"),maxItems=50,description="Serialized rule array <=100000 UTF-8 bytes; IDs unique. Disabled rules remain validated. Global operations use SQLite indexes over all current survivors, independent of batch boundaries.")
S["LogicalField"] = obj({"id":string(pattern="^[A-Za-z][A-Za-z0-9_]{0,63}$"),"label":string(minLength=1,maxLength=128)},["id","label"])
S["LogicalFields"] = array(ref("LogicalField"),minItems=1,maxItems=200,description="Unique logical IDs declared by the reusable rule set.")
S["FieldBindings"] = {"type":"object","maxProperties":200,"additionalProperties":string(pattern="^f[0-9]+$"),"description":"Explicit injective logical ID -> immutable input fN mapping. Every referenced logical ID must be declared and bound. No implicit name/position matching."}
S["OutputField"] = obj({"id":string(pattern="^f[0-9]+$"),"name":string(minLength=1,maxLength=128),"logicalType":enum("string","integer","decimal","datetime","boolean","unknown","mixed")},["id","name","logicalType"])
S["RuleSchemaResponse"] = obj({"schemaVersion":integer(3,maximum=3),"engineVersion":enum("v3-disk"),"ruleCount":integer(21,maximum=21),"rules":{"type":"object","additionalProperties":obj({},closed=False),"description":"Map of the21 rule type names to authoritative Draft2020-12 definitions."},"jsonSchema":obj({},closed=False,description="Complete Draft2020-12 array schema with oneOf and $defs; this value is a JSON Schema document, not an OpenAPI reference.")},["schemaVersion","engineVersion","ruleCount","rules","jsonSchema"])
S["LegacyRuleSetInput"] = obj({"name":string(minLength=1,maxLength=100),"schemaVersion":integer(1,maximum=1,default=1),"engineVersion":enum("v2-phased","v3-sequential",default="v3-sequential"),"logicalFields":array(ref("LogicalField")),"rules":ref("Rules")},["name","rules"],deprecated=True,description="Compatible historical schema1 save contract: strict19 columns/column rules; Java validates these through the historical Python gateway. Omitting metadata preserves existing clients.")
S["DiskRuleSetInput"] = obj({"name":string(minLength=1,maxLength=100),"schemaVersion":integer(3,maximum=3),"engineVersion":enum("v3-disk"),"logicalFields":ref("LogicalFields"),"rules":ref("DiskRules")},["name","schemaVersion","engineVersion","logicalFields","rules"])
S["RuleSetInput"] = {"oneOf":[ref("LegacyRuleSetInput"),ref("DiskRuleSetInput")]}
S["Rules"]["description"] = "Historical schema1 strict19 columns/column rule array; empty valid, <=100000 UTF-8 bytes. Preserved for existing saves and v2-phased execution."
S["Rules"]["deprecated"] = True
S["SequentialRule"]["description"] = "Historical schema1 execution shape:19 columns/column rules with optional id/enabled defaults. No logical fields or decimal dtype. Select v3-disk/schema3 for the new logical-field contract."
S["RuleCast"]["description"] = "Historical schema1 conversion: signed int64 integer, finite number, optional precision0..12 HALF_UP. precision is historical decimal places; schema3 uses distinct precision/scale fields. Datetime uses validated formats."
rule_union = {"anyOf":[ref("Rules"),ref("SequentialRules"),ref("DiskRules")]}
S["RuleSetVersion"]["properties"].update({"schemaVersion":integer(1,enum=[1,3]),"engineVersion":ENGINE,"logicalFields":array(ref("LogicalField")),"rules":rule_union})
S["RuleSetVersion"]["required"] += ["schemaVersion","engineVersion","logicalFields"]
S["TaskInputSnapshot"]["properties"]["parseVersion"] = integer(1)
S["SnapshotField"] = obj({"id":string(pattern="^f[0-9]+$"),"name":string(minLength=1,maxLength=128),"logicalType":enum("string","integer","decimal","datetime","boolean","unknown","mixed"),"dtype":string(),"inferredType":string(),"nullCount":integer()},["id","name"],closed=False,description="Immutable ordered fN/name parse field; optional preview/type metadata is preserved. Names must be unique and nonreserved.")
S["TaskInputSnapshot"]["properties"]["fields"] = nullable(array(ref("SnapshotField")))
S["DiskTaskInputSnapshot"] = deepcopy(S["TaskInputSnapshot"])
S["DiskTaskInputSnapshot"]["properties"]["fields"] = array(ref("SnapshotField"),minItems=1,maxItems=200)
S["DiskTaskInputSnapshot"]["required"] += ["fields"]
S["ColumnProfile"]["properties"]["inferredType"] = enum("integer","number","decimal","string","datetime","boolean","unknown","mixed")
S["File"]["properties"].update({"parseVersion":integer(),"objectStatus":nullable(enum("AVAILABLE","DELETING","EXPIRED"))})
S["File"]["required"] += ["parseVersion","objectStatus"]
disk_caps = {"batchRows":10000,"maxRecordBytes":16777216,"maxBatchBytes":67108864,"maxScratchBytes":4294967296,"maxRecordIssueBytes":4194304}
S["EngineLimits"]["properties"].update({key:integer(1,maximum=cap) for key,cap in disk_caps.items()})
S["EngineLimits"]["properties"].update({"executionTimeoutSeconds":integer(1,default=1800),"cancelGraceSeconds":integer(default=10),"maxRunningGlobal":integer(1,maximum=8),"maxRunningPerUser":integer(1,maximum=4)})
S["EngineLimits"]["description"] = "Immutable task limits: original processing caps, platform quotas, disk byte/row budgets, and parent-process total deadline/cancel grace. Defaults batch1000, record4MiB, batch32MiB, scratch1GiB, per-record issues1MiB. maxRecordBytes<=maxBatchBytes. Claim returns the frozen timeout/grace. Historical snapshots without keys retain their legacy behavior."
S["TaskCreate"]["properties"].update({"engineVersion":ENGINE,"schemaVersion":integer(1,enum=[1,3]),"fieldBindings":ref("FieldBindings"),"parseVersion":integer(1)})
S["TaskCreate"]["description"] = "Schema version must match selected immutable rule version. Schema3 requires explicit fieldBindings and v3-disk; schema1 defaults v3-sequential when engine omitted. parseVersion detects a stale binding after reparse (409 PARSE_VERSION_CHANGED). Unsupported combinations are rejected, never silently migrated."
execution_view = {"errorCode":string(nullable=True),"stage":string(nullable=True),"stageProcessedRows":integer(nullable=True),"totalRows":integer(nullable=True)}
for name in ("Task","TaskDetail","AdminTask"):
    S[name]["properties"].update(execution_view | {"schemaVersion":integer(1,enum=[1,3]),"username":string(nullable=True)})
    S[name]["required"] = [key for key in S[name].get("required",[]) if key != "userId"]
for name in ("Attempt","AttemptDetail"):
    S[name]["properties"].update(execution_view | {"lastEventSeq":integer()})
artifact_status = {"type":"object","additionalProperties":enum("AVAILABLE","DELETING","EXPIRED")}
S["TaskDetail"]["properties"].update({"rulesSnapshot":nullable(rule_union),"limitsSnapshot":nullable(ref("EngineLimits")),"schemaVersion":integer(1,enum=[1,3]),"logicalFieldsSnapshot":nullable(array(ref("LogicalField"))),"fieldBindingsSnapshot":nullable(ref("FieldBindings")),"outputFieldsSnapshot":nullable(array(ref("OutputField"))),"artifactStatus":artifact_status})
S["AttemptDetail"]["properties"]["artifactStatus"] = artifact_status
S["TaskRetry"] = obj({"expectedAttemptId":UUID},description="Optional optimistic guard; mismatch409 ATTEMPT_CHANGED. Retry keeps immutable task snapshots and creates a new attempt.")
S["InternalPreflightRequest"] = obj({"input":ref("DiskTaskInputSnapshot"),"schemaVersion":integer(3,maximum=3,default=3),"engineVersion":enum("v3-disk",default="v3-disk"),"logicalFields":ref("LogicalFields"),"fieldBindings":ref("FieldBindings"),"rules":ref("DiskRules")},["input","logicalFields","fieldBindings","rules"])
S["PublicPreflightRequest"] = obj({"fileId":UUID,"parseVersion":integer(1),"schemaVersion":integer(3,maximum=3),"engineVersion":enum("v3-disk"),"fieldBindings":ref("FieldBindings"),"ruleSetId":UUID,"ruleVersion":integer(1),"logicalFields":ref("LogicalFields"),"rules":ref("DiskRules")},["fileId","fieldBindings"],oneOf=[{"required":["ruleSetId","ruleVersion"]},{"required":["logicalFields","rules"],"not":{"required":["ruleSetId"]}}],description="Either a saved rule version or an explicit unsaved schema3 configuration. Java checks file ownership, successful parse and optional parseVersion before internal preflight.")
S["PreflightResponse"] = obj({"valid":{"type":"boolean","enum":[True]},"schemaVersion":integer(3,maximum=3),"engineVersion":enum("v3-disk"),"normalizedRules":ref("DiskRules"),"outputFields":array(ref("OutputField"),minItems=1,maxItems=200),"warnings":array(string())},["valid","schemaVersion","engineVersion","normalizedRules","outputFields","warnings"])
S["DiskRuleValidationRequest"] = obj({"schemaVersion":integer(3,maximum=3),"engineVersion":enum("v3-disk"),"logicalFields":ref("LogicalFields"),"rules":ref("DiskRules")},["schemaVersion","engineVersion","logicalFields","rules"])
for branch in S["RuleValidationRequest"]["oneOf"]:
    branch["properties"].update({"schemaVersion":integer(default=1),"logicalFields":array(ref("LogicalField"))})
S["RuleValidationRequest"]["oneOf"].append(ref("DiskRuleValidationRequest"))
S["RuleValidationResponse"] = obj({"valid":{"type":"boolean","enum":[True]},"normalizedRules":ref("DiskRules")},["valid"],description="Schema3 returns normalized logical-field rules, without requiring task bindings. Historical validation returns valid only.")
for name in ("PhasedTaskMessage","SequentialTaskMessage"):
    S[name]["properties"].update({"schemaVersion":integer(default=1),"logicalFields":array(ref("LogicalField")),"fieldBindings":ref("FieldBindings"),"outputFields":nullable(array(ref("OutputField")))})
S["DiskTaskMessage"] = obj({**message_props,"engineVersion":enum("v3-disk"),"schemaVersion":integer(3,maximum=3),"input":ref("DiskTaskInputSnapshot"),"logicalFields":ref("LogicalFields"),"fieldBindings":ref("FieldBindings"),"rules":ref("DiskRules"),"outputFields":nullable(array(ref("OutputField")))},[*message_required,"attemptId","engineVersion","schemaVersion","logicalFields","fieldBindings"])
S["TaskMessage"]["oneOf"].append(ref("DiskTaskMessage"))
S["TaskMessage"]["description"] = "RabbitMQ envelope: historical schema1 engines remain compatible; schema3 requires v3-disk, UUID attemptId and explicit logical bindings. Java freezes input/parseVersion/rules/bindings/limits in its transactional outbox. Worker claims the attempt before any heavy processing. Numeric attempt remains the artifact namespace. Encoded payload<=128KiB."
S["ClaimResponse"]["oneOf"][1]["properties"].update({"executionTimeoutSeconds":integer(1),"cancelGraceSeconds":integer(),"leaseSeconds":integer(1,default=120)})
S["ClaimResponse"]["oneOf"][1]["required"] += ["executionTimeoutSeconds","cancelGraceSeconds","leaseSeconds"]
S["WorkerEvent"]["properties"].update({"eventId":UUID,"eventSeq":integer(1),"errorCode":string(pattern="^[A-Z][A-Z0-9_]{0,79}$"),"stage":string(pattern="^[A-Za-z0-9_.:-]{1,80}$"),"stageProcessedRows":integer(),"totalRows":integer()})
S["WorkerEvent"]["description"] = "Current leaseToken and attempt UUID fence live attempts. v3-disk additionally requires eventId UUID and monotonic eventSeq>=1. Same event ID/seq+identical canonical payload is replay-safe; reuse with changed payload409. Stale/terminal/out-of-order events are recorded but cannot regress state. SUCCEEDED requires identity/conservation-checked report and exact nonempty artifacts. Deadline-expired successes become FAILED/TASK_TIMEOUT. Cancellation wins over late RUNNING/SUCCEEDED."
S["DiskWorkerEvent"] = {"allOf":[ref("WorkerEvent"),{"required":["attemptId","leaseToken","eventId","eventSeq","status","progress"]}]}
S["V3DirtyTrace"]["properties"].update({"_source_record_number":integer(1),"_removed_by_rule_id":string(nullable=True),"_attempt_id":UUID})
S["V3DirtyTrace"]["description"] += " Disk engine includes stable SHA256 recordId (file+parse snapshot+sheet+logical ordinal), source_record_number, removed_by_rule_id and attempt_id. Retries preserve source identity."
S["RuleReport"]["properties"].update({"type":enum(*DISK_OPTIONS),"durationMs":integer(),"stageRows":obj({"input":integer(),"output":integer()},["input","output"]),**{key:integer() for key in ["hitRows","hitCells","typeConversionAttemptedCells","typeConversionSucceededCells"]}})
S["RuleReport"]["required"] = [key for key in S["RuleReport"]["required"] if key != "executionOrder"]
S["RuleReport"]["properties"]["statistics"] = obj({},closed=False,description="Historical statistics use sampleCount/invalidValues. Disk fills use fN->{validValueCount,excludedInvalidValueCount,missingValueCount,fillValue:{type,value,...},sum?,scale?,rounding?,unroundedMean?}; disk IQR uses exact decimal-text q1/q3/lower/upper and counts; dedup uses distinctKeys/keepPolicy/nullsEqual.")
S["Report"]["properties"].update({"schemaVersion":integer(1,enum=[1,3]),"ruleSnapshot":rule_union,"logicalFields":array(ref("LogicalField")),"fieldBindings":ref("FieldBindings"),"scratchPeakBytes":integer()})
S["Report"]["description"] = "Historical reports remain readable. Schema3 disk execution preserves strict order with global exact statistics, top-level quality/conservation metrics, unique final deletion attribution, per-rule stage rows/duration and scratch peak. inputRows=outputRows+finalRemovedRows; outputRows=validOutputRows+flaggedOutputRows. Event counters can overlap; changedCells records actual transforms at each rule, including records removed later."
operation("/api/rules/preflight","post","Validate schema3 configuration against an owned parse snapshot",ref("PreflightResponse"),body=ref("PublicPreflightRequest"),description="Does not enqueue work or mutate saved rules. Explicit bindings only; conflicts and unsupported shapes fail before task creation.")
operation("/api/rule-schemas/{version}","get","Read authoritative schema3 rule definitions",ref("RuleSchemaResponse"),description="Only version3 exists; other versions404. Browser accesses Python definitions through authenticated Java.")
operation("/internal/preflight","post","Validate logical bindings and simulate field lifecycle",ref("PreflightResponse"),body=ref("InternalPreflightRequest"),internal=True,python=True)
operation("/internal/rule-schemas/3","get","Read21 authoritative schema3 JSON Schema definitions",ref("RuleSchemaResponse"),internal=True,python=True)
PATHS["/internal/validate-rules"]["post"]["responses"]["200"] = response(ref("RuleValidationResponse"))
PATHS["/internal/validate-rules"]["post"]["description"] = "Historical strict19 rules or sequential metadata remain supported. Schema3 validates21 logical-field rules, exact typed values and declarations; task bindings are checked separately by preflight. Invalid422."
PATHS["/api/rule-sets"]["post"]["description"] = "Schema3 requires engineVersion v3-disk, logicalFields and strict21 rules with id/enabled; Python validates before persistence. Historical schema1 strict19 saves remain compatible. Version defaults/rule shapes never silently migrate."
PATHS["/api/rule-sets/{id}"]["put"]["description"] += " A different schema requires a separate rule set (409); schema3 uses authoritative validation."
PATHS["/api/tasks"]["post"]["description"] = "Create from owned READY/PARSED file and saved rule version. Schema3 preflight freezes normalized rules, explicit bindings and output fields; schema1 omission defaults to v3-sequential. Freeze parseVersion and limits/deadline in one MySQL transaction with attempt, idempotency mapping and outbox. Optional key replays original response200 for7days; same key with changed explicit request409. No key creates independent tasks. Current submission/active quotas apply; separate running slots apply at Worker claim. Expired input410 INPUT_EXPIRED."
retry_op=PATHS["/api/tasks/{id}/retry"]["post"]
retry_op["requestBody"]={"required":False,"content":{"application/json":{"schema":ref("TaskRetry")}}}
retry_op["parameters"].append(param("Idempotency-Key","header",string(minLength=1,maxLength=200,pattern="^[!-~]+$"),description="Optional per-user retry operation key, retained7days; same request replays the saved result."))
retry_op["description"]="Owner only. FAILED/CANCELED required; frozen input/rules/bindings/limits retained. Optional expectedAttemptId prevents stale retries; optional Idempotency-Key makes network retries replay-safe for7days. New numeric attempt and UUID attemptId; historical artifacts remain readable unless expired. Input expiry410 prevents retry. Current submission quotas apply."
for path,item in PATHS.items():
    if "/artifacts/" in path or path.endswith("/preview") or path=="/api/tasks":
        for method,op in item.items():
            if path.startswith("/api/"): op["responses"]["410"]={"$ref":"#/components/responses/Java410"}
PATHS["/internal/tasks/{id}/claim"]["post"]["description"] += " Global/per-user running slot exhaustion409 RUNNING_CAPACITY; queue delivery is delayed. Response carries immutable executionTimeoutSeconds/cancelGraceSeconds and leaseSeconds."
PATHS["/internal/tasks/{id}/events"]["post"]["description"] = S["WorkerEvent"]["description"]
PATHS["/internal/tasks/{id}/control"]["get"]["description"] = "Lease/cancellation check; cancelled=true for stale identity/token or non-RUNNING state. Parent continues AMQP heartbeats, requests cooperative stop then terminates child after grace. CANCELED is reported only after executor exit and scratch cleanup."

S["RetentionPolicy"] = obj({"enabled":BOOL,"includeExisting":BOOL,**{key:integer(maximum=36500) for key in ["rawDays","resultDays","dirtyDays","reportDays","failedDays","logDays","orphanDays","graceHours"]},"protectedBefore":TIME},description="Defaults to dry-run. Pre-upgrade objects are protected unless includeExisting explicitly enabled; active references/download leases and grace periods remain protected.")
S["RetentionPolicyUpdate"] = obj({**{key:value for key,value in S["RetentionPolicy"]["properties"].items() if key!="protectedBefore"},"confirmation":enum("ENABLE_RETENTION","INCLUDE_EXISTING_DATA")},description="Partial update. Turning enabled on requires confirmation ENABLE_RETENTION; turning includeExisting on requires INCLUDE_EXISTING_DATA. Enable them in separate requests when both transitions are required.")
S["RetentionScope"] = obj({"fileIds":array(UUID,maxItems=100),"taskIds":array(UUID,maxItems=100),"includeOrphans":BOOL,"afterObjectKey":string(maxLength=512,description="Optional exclusive lexical starting key for an initial scan."),"continuation":string(maxLength=8192,description="Signed 24-hour continuation bound to admin, protocol version and unchanged original scope; copy nextContinuation into the same scope to build another dry-run plan.")},description="Omitted/empty lists select the available catalog; explicit lists are applied before catalog/log SQL limits. Discovery scans at most1000 total objects or5seconds between object-store calls; one network call may exceed that interval. Planning itself deletes nothing; use scan.nextContinuation until truncated=false.")
S["RetentionItem"] = obj({"id":UUID,"objectKey":string(nullable=True),"taskId":nullable(UUID),"kind":enum("raw","results","dirty","reports","logs"),"state":enum("PLANNED","PROTECTED","DELETING","FAILED","DELETED"),"reason":string(),"attempts":integer()},["id","objectKey","taskId","kind","state","reason","attempts"])
retention_summary={"id":UUID,"state":enum("DRY_RUN","RUNNING","PARTIAL_FAILURE","COMPLETED"),"createdBy":UUID,"truncated":BOOL,"createdAt":TIME,"updatedAt":TIME}
S["RetentionPlanSummary"]=obj(retention_summary,retention_summary)
S["RetentionScan"]=obj({"scannedObjects":integer(),"matchedObjects":integer(),"catalogInspected":integer(),"logTasksInspected":integer(),"nextContinuation":nullable(string(maxLength=8192)),"afterObjectKey":string(maxLength=512),"warnings":array(string())},["scannedObjects","matchedObjects","catalogInspected","logTasksInspected","nextContinuation","afterObjectKey","warnings"])
S["RetentionPlan"]=obj({"id":UUID,"state":retention_summary["state"],"policy":ref("RetentionPolicy"),"scope":ref("RetentionScope"),"truncated":BOOL,"scan":nullable(ref("RetentionScan")),"createdAt":TIME,"items":array(ref("RetentionItem"))},["id","state","policy","scope","truncated","createdAt","items"])
S["RetentionRun"]=obj({"id":integer(1),"planId":UUID,"itemId":UUID,"action":enum("INTENT","DELETED","FAILED","SKIPPED"),"message":string(),"requestId":string(nullable=True),"createdAt":TIME,"plan_id":UUID,"item_id":UUID,"request_id":string(nullable=True),"created_at":TIME},["id","planId","itemId","action","message","requestId","createdAt"],description="Camel-case fields are primary; underscore aliases remain for compatibility.")
retention_base="/api/admin/retention"
operation(retention_base+"/policy","get","Read retention policy and upgrade protection boundary",ref("RetentionPolicy"),admin=True)
operation(retention_base+"/policy","put","Update explicit retention policy",ref("RetentionPolicy"),body=ref("RetentionPolicyUpdate"),admin=True)
operation(retention_base+"/plans","post","Build audited retention dry-run plan",ref("RetentionPlan"),body=ref("RetentionScope"),admin=True,description="Discovers controlled MinIO object keys and records eligible/protected items. This operation never deletes objects/logs.")["requestBody"]["required"]=False
operation(retention_base+"/plans/{id}","get","Read retention plan and item outcomes",ref("RetentionPlan"),admin=True)
for action in ("execute","retry"):
    operation(retention_base+"/plans/{id}/"+action,"post","Execute or retry explicitly enabled retention plan",ref("RetentionPlan"),admin=True,description="Requires enabled policy or409. Revalidates references/download leases/policy under submission lock. Commits deletion intent before object-store deletion; failures remain tombstoned for retry. Audit runs record every action; successful deletion is confirmed before EXPIRED. Metadata and historical report summaries remain readable.")

def page_params(filters):
    available={"q":string(maxLength=255,description="Literal substring; SQL wildcard characters are escaped."),"status":string(description="Comma-separated states, at most8; logs use levels, retention plans use states and runs use actions."),"format":FORMAT,"ownerId":UUID,"role":enum("USER","ADMIN"),"enabled":enum("true","false"),"planId":UUID}
    values=[param("pageSize","query",integer(1,maximum=100,default=20)),param("cursor","query",string(maxLength=8192),description="Opaque HMAC cursor bound to user/resource/filter/sort and first-page asOf; mismatch400."),param("sort","query",enum("createdAt.desc","createdAt.asc",default="createdAt.desc")),param("createdFrom","query",TIME,description="Inclusive UTC timestamp."),param("createdTo","query",TIME,description="Exclusive UTC timestamp.")]
    return values+[param(key,"query",available[key]) for key in filters]

page_routes=[
    ("/api/files/page","File",["q","status","format"],False),
    ("/api/uploads/page","File",["q","status","format"],False),
    ("/api/rule-sets/page","RuleSetVersion",["q"],False),
    ("/api/rule-sets/{id}/versions/page","RuleSetVersion",["q"],False),
    ("/api/tasks/page","Task",["q","status"],False),
    ("/api/reports/page","Task",["q","status"],False),
    ("/api/tasks/{id}/attempts/page","Attempt",["status"],False),
    ("/api/tasks/{id}/logs/page","TaskLog",["status"],False),
    ("/api/tasks/{id}/attempts/{attemptId}/logs/page","TaskLog",["status"],False),
    ("/api/admin/users/page","AdminUser",["q","role","enabled"],True),
    ("/api/admin/tasks/page","Task",["q","status","ownerId"],True),
    (retention_base+"/plans/page","RetentionPlanSummary",["status","ownerId"],True),
    (retention_base+"/runs/page","RetentionRun",["status","planId"],True),
]
for path,item,filters,admin in page_routes:
    name="Page"+re.sub(r"[^A-Za-z0-9]","",path.title())
    S[name]=obj({"items":array(ref(item),maxItems=100),"nextCursor":string(nullable=True),"hasMore":BOOL,"asOf":TIME},["items","nextCursor","hasMore","asOf"])
    operation(path,"get","Read a cursor page of "+item,ref(name),params=page_params(filters),admin=admin,description="Server-side keyset pagination by (createdAt,id), default20/max100. Stable first-page asOf excludes later inserts; state filters reflect query-time state. Filter/sort/user/resource changes invalidate the cursor. Unknown/unsupported query keys400. Report pages contain successful task summaries.")
deprecated={"/api/files":"/api/files/page","/api/rule-sets":"/api/rule-sets/page","/api/rule-sets/{id}/versions":"/api/rule-sets/{id}/versions/page","/api/tasks":"/api/tasks/page","/api/tasks/{id}/attempts":"/api/tasks/{id}/attempts/page","/api/tasks/{id}/logs":"/api/tasks/{id}/logs/page","/api/tasks/{id}/attempts/{attemptId}/logs":"/api/tasks/{id}/attempts/{attemptId}/logs/page","/api/admin/users":"/api/admin/users/page","/api/admin/tasks":"/api/admin/tasks/page"}
for path,replacement in deprecated.items():
    PATHS[path]["get"]["deprecated"]=True
    PATHS[path]["get"]["description"] += f" Compatible capped legacy listing; use {replacement} for cursor pagination."
for path,method in (("/api/files/{id}/chunks/{index}","put"),("/api/files/{id}/complete","post")):
    PATHS[path][method]["deprecated"]=True

error_meanings = {"400": "Invalid request/options/rule/identity", "401": "Missing/invalid/expired authentication", "403": "Administrator required or registration disabled", "404": "Resource missing or not owned/readable", "409": "State/lease/version/idempotency conflict", "410":"Input/artifact expired or cleanup intent recorded; INPUT_EXPIRED/ARTIFACT_EXPIRED", "413": "Request body too large: Java8MiB chunk or3MiB other request", "422": "Python payload/parse/rule/processing-limit validation failed", "429": "Authentication/submission/upload/concurrency/preview quota exceeded", "500": "Unexpected service/storage failure", "502": "Internal data-processing or object-storage dependency unavailable"}
RESPONSES = {}
for prefix, schema_name in [("Java", "Error"), ("Python", "PythonError")]:
    for status, meaning in error_meanings.items():
        RESPONSES[prefix + status] = response(ref(schema_name), meaning)

DOC = {
    "openapi": "3.0.3",
    "info": {"title": "data-cleaning-pipeline implemented API", "version": "3.0.0-current", "description": "Implemented Java /api routes and protected Java/Python internal routes. Includes schema3 v3-disk logical bindings/21rules, exact decimals, cursor pages, explicit audited retention, event ledger and execution deadlines. Historical schema1 engines/19rules and capped listings remain compatible; legacy lists/upload aliases are marked deprecated. Java is the sole browser business origin; Python internal operations override servers to Compose python-api:8000. Ownership is enforced server-side; ADMIN may read all tasks while task writes/file/rule operations remain owner-scoped. GET /actuator/health is infrastructure-managed and excluded from application controllers. Generation: python scripts/generate_openapi.py; verification: python scripts/generate_openapi.py --check."},
    "servers": [{"url": "http://localhost:8080", "description": "Java local developer origin (configure port/proxy for deployment)"}],
    "security": [{"bearerAuth": []}],
    "paths": PATHS,
    "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer", "description": "Opaque24-hour session token, not JWT."}, "internalToken": {"type": "apiKey", "in": "header", "name": "X-Internal-Token", "description": "At least32-character shared secret; internal network only. Bearer tokens cannot substitute."}}, "schemas": S, "responses": RESPONSES},
    "x-rabbitmq-task-schema": ref("TaskMessage"),
    "x-worker-event-schema": ref("DiskWorkerEvent"),
    "x-rule-contracts": {"schema1":{"engines":["v2-phased","v3-sequential"],"ruleCount":19,"schema":ref("Rule")},"schema3":{"engines":["v3-disk"],"ruleCount":21,"schema":ref("DiskRule")}},
    "x-source-controllers": sorted(str(path.relative_to(ROOT)).replace("\\","/") for path in (ROOT/"backend/src/main/java").rglob("*Controller.java")) + ["engine/cleaning/api.py"],
}


def verify(document):
    assert document["openapi"] == "3.0.3"
    refs = 0

    def walk(node):
        nonlocal refs
        if isinstance(node, dict):
            if "$ref" in node:
                target = node["$ref"]
                assert target.startswith("#/"), target
                value = document
                for token in target[2:].split("/"):
                    value = value[token.replace("~1", "/").replace("~0", "~")]
                refs += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    operation_ids = set()
    actual_routes = set()
    for path, item in document["paths"].items():
        assert not path.startswith("/api/v3"), path
        expected = set(re.findall(r"\{([^}]+)\}", path))
        for method, operation_data in item.items():
            if method not in {"get", "post", "put", "delete", "patch", "head", "options"}:
                continue
            actual_routes.add((method, path))
            names = [p["name"] for p in operation_data.get("parameters", []) if p["in"] == "path"]
            assert set(names) == expected and len(names) == len(expected), (path, names, expected)
            assert all(p["required"] for p in operation_data.get("parameters", []) if p["in"] == "path")
            op_id = operation_data["operationId"]
            assert op_id not in operation_ids, op_id
            operation_ids.add(op_id)
            for requirement in operation_data["security"]:
                assert set(requirement) <= document["components"]["securitySchemes"].keys()
    expected_routes = set()
    for relative in document["x-source-controllers"]:
        source = (ROOT / relative).read_text(encoding="utf-8")
        if relative.endswith(".java"):
            class_header = source.split("public class",1)[0]
            prefixes = re.findall(r'@RequestMapping\("([^\"]+)"\)',class_header)
            prefix = prefixes[-1] if prefixes else ""
            for method, path in re.findall(r'@(Get|Post|Put|Delete|Patch)Mapping\("([^\"]+)"\)', source):
                expected_routes.add((method.lower(), prefix+path))
        else:
            for method, path in re.findall(r'@application\.(get|post|put|delete|patch)\("([^\"]+)"', source):
                expected_routes.add((method, path))
    assert actual_routes == expected_routes, {"missing": sorted(expected_routes - actual_routes), "extra": sorted(actual_routes - expected_routes)}
    tree = ast.parse((ROOT / "engine/cleaning/models.py").read_text(encoding="utf-8"))
    python_types = None
    python_options = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "RULE_FIELDS" for target in statement.targets):
            python_types = {ast.literal_eval(key) for key in statement.value.keys}
            for key, value in zip(statement.value.keys, statement.value.values):
                required_node, optional_node = value.elts
                required = {ast.literal_eval(item) for item in required_node.elts}
                optional = {ast.literal_eval(item) for item in optional_node.elts} if isinstance(optional_node, ast.Set) else set()
                python_options[ast.literal_eval(key)] = (required, optional)
    assert python_types == set(rule_options) and len(python_types) == 19
    assert len(document["components"]["schemas"]["Rule"]["oneOf"]) == 19
    for kind, name in zip(rule_options, rule_names):
        required, optional = python_options[kind]
        generated = document["components"]["schemas"][name]
        assert set(generated["required"]) == required, (kind, "required options differ")
        assert set(generated["properties"]) == required | optional, (kind, "accepted options differ")
    assert len(DISK_OPTIONS)==21 and set(DISK_OPTIONS)-python_types=={"replace","compare_fields"}
    assert len(document["components"]["schemas"]["DiskRule"]["oneOf"])==21
    for kind,name in disk_names.items():
        required,optional = DISK_OPTIONS[kind]
        generated = document["components"]["schemas"][name]
        assert set(generated["required"])==required|{"id","type","enabled"},(kind,"disk required options differ")
        assert set(generated["properties"])==required|optional|{"id","type","enabled"},(kind,"disk accepted options differ")
    # Source envelope and Java record changes must not silently drift from docs.
    classes={node.name:node for node in tree.body if isinstance(node,ast.ClassDef)}
    annotated=lambda name:{node.target.id for node in classes[name].body if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name)}
    assert set(document["components"]["schemas"]["DiskTaskMessage"]["properties"])==annotated("TaskMessage"),"TaskMessage source fields drifted"
    assert set(document["components"]["schemas"]["TaskInputSnapshot"]["properties"])==annotated("TaskInput"),"TaskInput source fields drifted"
    java=(ROOT/"backend/src/main/java/dev/cleaning/TaskService.java").read_text(encoding="utf-8")
    event_record=re.search(r'public record Event\((.*?)\)\s*\{',java,re.S).group(1)
    event_fields={field.strip().split()[-1] for field in event_record.split(",")}
    assert event_fields==set(document["components"]["schemas"]["WorkerEvent"]["properties"]),"WorkerEvent Java record fields drifted"
    assert all(PATHS[path]["get"].get("deprecated") for path in deprecated)
    deprecated_count=sum(bool(op.get("deprecated")) for item in document["paths"].values() for op in item.values())
    return {"operations": len(actual_routes), "paths": len(document["paths"]), "schemas": len(document["components"]["schemas"]), "resolved_refs": refs, "schema1_rule_types": len(python_types), "schema3_rule_types":len(DISK_OPTIONS), "deprecated_operations":deprecated_count,"source_controllers":len(document["x-source-controllers"]), "source_route_coverage": "exact", "task_message_fields":"exact", "worker_event_fields":"exact"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Verify committed JSON equals reproducible output and actual source routes.")
    args = parser.parse_args()
    rendered = json.dumps(DOC, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.check:
        committed = json.loads(TARGET.read_text(encoding="utf-8"))
        assert committed == DOC, "docs/openapi.json is stale; regenerate it"
        result = verify(committed)
    else:
        result = verify(DOC)
        TARGET.write_text(rendered, encoding="utf-8", newline="\n")
    print(json.dumps({"file": str(TARGET), **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
