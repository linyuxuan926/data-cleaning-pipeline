# Legacy MVP integration contract

> Current expanded behavior is defined by [V2-CONTRACT.md](V2-CONTRACT.md), with user-facing semantics in [RULES.md](RULES.md). The V2 contract overrides the legacy-only limitations below (roles, parsing, field references, phase order and dirty-data conservation). Existing endpoint shapes remain compatible where possible. This file preserves the original message/lease protocol for maintainers.

All browser requests use `/api` on the Java origin. JSON uses camelCase. Bearer tokens protect every business endpoint. `GET /actuator/health` is public. Errors: `{ "message": "..." }` with suitable HTTP status. IDs are strings (UUID). Lists are JSON arrays; the MVP bounds list sizes.

## Browser API

- `POST /api/auth/register` `{username,password}` and `POST /api/auth/login` → `{token,user:{id,username}}`; `GET /api/auth/me` → `{id,username}`; `POST /api/auth/logout`.
- `GET /api/files` → `[{id,name,format,size,status,createdAt}]`.
- `POST /api/files/init` `{name,size,chunkSize}` → `{id,chunkSize,totalChunks}`. Supported suffixes: `.csv`, `.xlsx`, `.parquet`. `PUT /api/files/{id}/chunks/{index}` accepts raw bytes, 0-based index. `POST /api/files/{id}/complete` → file metadata. Repeated completion is idempotent. `GET /api/files/{id}/preview?limit=20` → preview below.
- `GET /api/rule-sets` → `[{id,name,version,rules,createdAt}]`; `POST /api/rule-sets` `{name,rules}` → same; `PUT /api/rule-sets/{id}` `{name,rules}` creates immutable next version; `GET /api/rule-sets/{id}/versions` → version array.
- `GET /api/tasks` → task array. `POST /api/tasks` `{fileId,ruleSetId,ruleVersion,outputFormat}` → task. `GET /api/tasks/{id}` → task. `POST /api/tasks/{id}/cancel`, `POST /api/tasks/{id}/retry` → task. Retry reuses the ID and increments `attempt`.
- Task: `{id,fileId,fileName,ruleSetId,ruleVersion,status,progress,attempt,outputFormat,error,createdAt,updatedAt}`. Statuses: `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`.
- `GET /api/tasks/{id}/logs?after=0` → `[{id,level,message,createdAt}]`, ascending numeric IDs.
- `GET /api/tasks/{id}/report` → complete report JSON; 409 until success. `GET /api/tasks/{id}/artifacts/{kind}` streams authenticated download; kinds `result`, `dirty`, `report`.

## Python / Java integration

Internal HTTP requests carry `X-Internal-Token`, compared securely against `INTERNAL_API_TOKEN`. Python API: `GET /health`, `POST /internal/preview` `{objectKey,format,limit}` → `{columns:[{name,dtype,nullCount}],rows:[{column:value}],rowCount,previewRowCount}`. rowCount may be null when a bounded preview does not count the entire file. Preview rows are JSON-safe; missing/nonfinite cells become null.

Java declares durable RabbitMQ queue `cleaning.tasks`, durable direct exchange `cleaning`, routing key `task`, with a dead-letter exchange `cleaning.dlx` and queue `cleaning.dead`. A transactional database outbox plus publisher confirms provides reliable submission. Messages use persistent delivery, JSON:

```json
{"taskId":"uuid","attempt":1,"input":{"objectKey":"raw/...","format":"csv","name":"file.csv"},"rules":[{"type":"trim","columns":["name"]}],"outputFormat":"csv"}
```

Worker uses prefetch 1, manually acknowledges after Java accepts a terminal callback. Redelivery and stale attempts are checked by calling `POST /internal/tasks/{id}/claim` with `{attempt}` → `{execute:true|false,leaseToken:"uuid"}`. Claim returns false for stale or terminal attempts. HTTP 409 means another worker owns a live lease and the delivery must be requeued after a bounded delay, never acknowledged. Lease expires after 120s and heartbeat refreshes it. Java increments attempt when recovering an expired lease before requeueing. Duplicate delivery of an already terminal attempt is acknowledged.

`GET /internal/tasks/{id}/control?attempt=N&leaseToken=uuid` → `{cancelled:boolean}`; stale or terminal attempts return cancelled=true. `POST /internal/tasks/{id}/events` accepts `{attempt,leaseToken,status,progress,message,report?,artifacts?}`, where status is `RUNNING`, `SUCCEEDED`, `FAILED`, or `CANCELLED`. Progress is 0..100. Worker sends periodic RUNNING heartbeat while its executor thread performs blocking IO. Java ignores stale callbacks, checks leaseToken, and prevents terminal-state regression. artifacts is `{result:"results/{taskId}/{attempt}/result.ext",dirty:"dirty/{taskId}/{attempt}/dirty.csv",report:"reports/{taskId}/{attempt}/report.json"}`. Full report is persisted as JSON and report summary available from it. Task cancellation is cooperative and an immediately cancelled QUEUED task must never execute.

## Rules and report

Rules are an ordered JSON array, validated by both services; unknown types/options rejected. Columns always use exact string names; missing configured columns fail the task with an actionable error. An empty array is valid (format conversion / baseline profiling).

- `{type:"trim",columns:["name"]}` trims strings only.
- `{type:"case",columns:["name"],mode:"lower"|"upper"}` changes strings only.
- `{type:"fill_null",columns:["city"],value:"unknown"}` fills missing values.
- `{type:"drop_null",columns:["id"]}` rejects rows with any selected missing value.
- `{type:"deduplicate",columns:["id"]}` keeps first occurrence.
- `{type:"cast",columns:["age"],dtype:"integer"|"number"|"string"|"datetime"}` rejects rows with non-null values that cannot be converted; integer rejects fractional and out-of-range values.
- `{type:"range",column:"age",min:0,max:150}` rejects non-null nonnumeric/out-of-range cells (inclusive bounds).
- `{type:"regex",column:"email",pattern:"^[^@]+@[^@]+$"}` rejects non-null strings that do not match; pattern length <=256 and bounded safe regex validation.
- `{type:"rename",column:"old",target:"new"}` disallows duplicate destination.

Rejected rows are removed from subsequent rules and saved with original input values plus `_source_row` (1-based data row), `_rule_index` (1-based), `_reason`. Reserved metadata column collisions cause an explicit input validation error. CSV output uses UTF-8 BOM for Excel compatibility. Result formats: csv, xlsx, parquet. Reports: `{taskId,attempt,inputRows,outputRows,dirtyRows,changedCells,durationSeconds,rules:[{index,type,inputRows,outputRows,rejectedRows,changedCells}],columnsBefore:[{name,dtype,nullCount}],columnsAfter:[...],warnings:[]}`. Per-rule changedCells includes transformation cells and excludes rejected cells. Report is both stored as a MinIO JSON object and persisted through the Java callback.

## Limits

Single-node MVP: max 100 MiB per input file, <=500000 rows / 200 columns and <=5000000 total cells for execution; <=256 MiB logical UTF-8 string-cell bytes (repeated values counted per cell), <=512 MiB estimated deep memory per DataFrame and <=256 MiB per output file. Bounded 8 MiB chunks; <=50 rules and <=100000 UTF-8 bytes of serialized rules; max 3 active tasks per user, max 10 submissions/minute, global active task limit 20. One Worker process executes one task at a time; scale workers up to configured service capacity. Queue wait limit and expired heartbeat recovery are handled by Java scheduler. No promise of streaming multi-GB inputs.
