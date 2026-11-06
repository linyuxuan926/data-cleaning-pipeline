# Expanded MVP contract (2026-11-06)

Extends the existing running application; preserve existing APIs and old rule JSON where possible. All new production functionality is real. No arbitrary code execution.

## Roles and settings (Java owner)

User has `{id,username,role:"ADMIN"|"USER",enabled}`. Register always USER. Disabled users cannot authenticate, existing sessions invalid immediately. Bootstrap an administrator only from `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` (no default password, never promote a preexisting user automatically). `GET /api/admin/users`; `PUT /api/admin/users/{id}` `{role,enabled}`; admin may list all tasks via `GET /api/admin/tasks` (include username). Prevent disabling/demoting final active ADMIN. `GET /api/settings` public-to-authenticated returns current limits; `PUT /api/admin/settings` accepts complete settings object and validates against hard safety caps. Persist in MySQL and attach immutable settings snapshot to each task.

Settings keys/defaults: `maxFileBytes:104857600,maxRows:500000,maxColumns:200,maxCells:5000000,maxExcelExpandedBytes:536870912,previewRows:100,maxActivePerUser:3,maxActiveGlobal:20,submissionsPerMinute:10`. Administrators may lower or increase within hard caps equal to engine safety budget (file100MiB/rows500k/cols200/cells5m/excel512MiB/preview1000/activeUser20/global100/rate120). Increasing safety caps requires an explicitly reviewed engine capacity change. Every browser file/parse/create workflow shows actual settings.

## Upload / parse separation (Java+Python)

File metadata adds `uploadId` (different UUID), `sha256`, `parseStatus:"UNPARSED"|"PARSED"|"FAILED"`, `parseError`, `parseOptions`, `fields`, uploader identity and objectKey. Existing `status=READY` means upload persisted, NOT parse success. `/files/init` accepts optional `sha256`, `parseOptions`; returns `{id,uploadId,chunkSize,totalChunks}`. New `PUT /api/uploads/{uploadId}/chunks/{index}` and `POST /api/uploads/{uploadId}/complete` use separate upload ID and ownership checks; old file-ID endpoints remain compatibility aliases. Calculate full SHA-256 server-side and compare supplied digest when present; actual parsers validate content, no suffix-only acceptance. Final completion attempts parse, persists failures without deleting the uploaded raw file. A malformed declared digest or a mismatch fails completion (never READY); omitting the optional expected digest still stores the server-calculated SHA-256.

`POST /api/files/{id}/parse` accepts parse-options JSON and reparses raw file. `GET /api/files/{id}/preview?limit=N` defaults to configured previewRows. Tasks only accept PARSED files and use their frozen parse options/field IDs regardless of later reparsing. Limits apply at parsing and full execution; bounded preview alone must not claim full-file row limits validated. Full execution remains final authoritative content validation.

Parse options: `{encoding:"auto"|"utf-8-sig"|"utf-8"|"gb18030"|"utf-16"|"latin-1",delimiter:"auto"|","|";"|"\\t"|"|",sheetName?:string,missingValues:[""],fieldTypes?:{f0:"string",...}}`. Defaults encoding/delimiter auto, missingValues=[""], first worksheet. Safe bounded sniff, expose detected resolved encoding/delimiter in preview `parseOptions`. Manual overrides supported. Empty strings are missing only if configured. XLSX validates ZIP plus workbook, returns worksheet names and dimensions, selected worksheet only. Parquet requires actual magic and scalar schema. Preview returns `{columns:[{id:"f0",name,dtype,inferredType,nullCount}],rows:[{originalName:value}],rowCount,previewRowCount,sheets?:[{name,rows,columns}],parseOptions,warnings}`. Field IDs derive from original position (`f0`, `f1`), remain stable within parse snapshot; IDs survive normalization, cast and final rename. Input headers may not be reserved dirty/quality metadata names.

Internal Python `/internal/preview`: `{objectKey,format,limit,parseOptions?,limits?}`; `/internal/validate-rules` `{rules}` -> `{valid:true}` or400/422, used by Java as authoritative Python validation before saving (Java also enforces size/envelope). No SQL writes in Python.

Queue adds `input.parseOptions`, `input.fields` (optional backward compat) and `limits`; task stores explicit `rules_snapshot`, `input_snapshot`, `limits_snapshot`, used for every attempt. Old tasks can fall back to immutable rule_versions and file metadata until migrated. Retry never uses modified file parse options. Task detail may return snapshots for review.

## Deterministic rule semantics (Python owner; Java forwards validated JSON)

Default stable phase order: normalization(10) -> cast(20) -> missing(30) -> outliers(40) -> validation including validate_type(50) -> deduplicate(60) -> select/drop/rename(70). Within phase preserve configured array order; preview/editor show actual phase. Report rule `index` is original 1-based array index, `executionOrder` is actual order. All column references accept stable IDs (`f0`) preferentially, legacy original names accepted and bound once before execution. Rename is final projection only; it never changes internal identifiers. Original input order defines first/last dedup; always global to task.

All validation-capable rules accept `onError:"drop"|"mark"|"fail"` (default drop); range/iqr also `onError:"null"` replaces offending field with null while recording issue. Mark preserves record and appends `_quality_errors` JSON and `_quality_valid=false` to result. Fail records diagnostic context in FAILED message (no successful downloads). Within validation phase evaluate all rules against records entering that phase and collect every violation before removing rows; dropped rows never enter later phases/statistics. Other phases remove rejected rows at rule completion. Multiple mark violations per record are always retained. Metadata columns reserved: `_source_row,_rule_index,_reason,_field,_error_code,_record_id,_quality_errors,_quality_valid`.

Keep legacy trim/case/fill_null/drop_null/regex/range/rename/cast/deduplicate. Add/options:

- `trim`,`case` as before.
- `cast`: `columns,dtype:"integer"|"number"|"string"|"datetime"|"boolean",dateFormat?:string,precision?:0..12,outputFormat?:string,onError?`. datetime uses explicit dateFormat when supplied, UTC internally; outputFormat applies to export representation. Number uses deterministic decimal rounding; integer exact int64. Booleans accept true/false,1/0,yes/no case-insensitive, reject others. No silent coercion failures.
- `missing`: `columns,markers:[scalar,...]` normalizes configured markers (including empty string) to null; explicit JSON null always missing. Parse-level markers apply before rules, then missing rule can normalize e.g. trim-produced empties.
- `fill_null`: `columns,value` finite scalar; `fill_stat`: `columns,strategy:"mean"|"median"|"mode",emptyPolicy:"fail"|"leave"|"constant",value?`. Statistics use current surviving nonmissing valid values; mean/median numeric invalid values excluded and reported in per-rule statistics; no valid values follow emptyPolicy. Mode tie -> first value in original input order, deterministic. No silent all-null fallback.
- `deduplicate`: `columns?:[ids]` (absent/empty -> full row), `keep:"first"|"last"` default first, onError optional; keys exclude quality metadata; global current survivors. Dirty issue for each dropped/marked duplicate.
- `range`: `column,min?,max?,inclusiveMin?:boolean=true,inclusiveMax?:boolean=true,onError?`.
- `iqr`: `column,factor?:number=1.5,onError?,emptyPolicy:"fail"|"leave"`; calculate Q1/Q3 current valid numeric values, threshold Q1-factor*IQR..Q3+factor*IQR inclusive. Report thresholds/sample count; zero IQR bounds still applied.
- `required`: `columns,onError?` nonmissing validation.
- `enum`: `column,values:[finite scalar,...],onError?`, missing skipped unless explicitly enum includes null? Use required for nonnull; enum skips missing.
- `length`: `column,min?:integer,max?:integer,onError?` string Unicode character count, nonstring violation, missing skipped.
- `regex`: as before, fullmatch, per-cell timeout10ms/per-rule10s, missing skipped.
- `validate_type`: `columns,dtype` same allowed dtype, missing skipped, verifies convertibility without mutating; phase50 (cast only phase20).
- `compare`: `column,operator:"eq"|"ne"|"gt"|"gte"|"lt"|"lte"|"contains"|"starts_with"|"ends_with",value:finiteScalar,onError?` restricted declarative predicates, no code/eval; comparison incompatible types explicit violation, missing skipped. Numeric targets use numeric comparison for ordered comparisons; eq/ne use typed equality.
- `select_fields`: `columns` nonempty preserve requested order; `drop_fields`: `columns` cannot remove every column; `rename` same target validation.

## Dirty data / reports

Dirty CSV is issue-oriented: one row per violation, original data plus `_record_id` (stable source row string), `_source_row`(1-based), `_field`(original field name or joined relevant names), `_rule_index`, `_error_code`, `_reason`. Same record can have many issue rows. Deletion count is unique removed rows; marked row stays in result. Report old fields kept, adds `removedRows,flaggedRows,issueCount,executionOrder,ruleSnapshot`; `dirtyRows` means unique records with >=1 issue. Conservation: `inputRows=outputRows+removedRows`; `dirtyRows` may exceed removedRows. Per-rule old counters plus `issueCount,flaggedRows,statistics?`. All report counters nonnegative. Java validates new conservation when removedRows present, legacy old rule otherwise. Failed tasks have error log identifying rule/field/reason.

## Verification and compatibility

Keep existing true end-to-end tests working where semantics allow. Old sample explicit dedup before validation now executes after validation but still 6->3 result. Add tests for roles/disabled sessions/last admin, uploads checksum+independent IDs, parsing failures/manual CSV/XLSX sheets, stable IDs/immutable parse and rule snapshots, statistical empty strategies/ties/global keep-last, all failure policies/multiple issues/conservation, safe custom predicates, resource budgets and no eval.
