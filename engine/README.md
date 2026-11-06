# Python cleaning engine

Python 3.11.10 is the deployment runtime. FastAPI and Worker share an image but run as separate processes. All production adapters use real MinIO, RabbitMQ and Java HTTP APIs. Java owns platform MySQL; the disk engine uses private per-attempt SQLite scratch only. Runtime and test dependencies are pinned.

```sh
python -m venv .venv
# Linux/macOS: .venv/bin/python; Windows: .venv/Scripts/python.exe
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest
```

API: `uvicorn cleaning.api:app --host 0.0.0.0 --port 8000`.
Worker: `python -m cleaning.worker`.
Use environment variables from the root deployment guide. INTERNAL_API_TOKEN requires at least 32 characters. Java owns bucket/queue provisioning. `/health` is public; all `/internal/*` routes require the internal token. These include input/artifact previews, rule validation, task preflight and `GET /internal/rule-schemas/3`. Rule validation is authoritative before Java saves an immutable version.

## Schema 3 disk engine

New schema-3 rules use `engineVersion: "v3-disk"`; historical schema-1 tasks keep their explicit `v3-sequential` or `v2-phased` engines. No rule-shape migration occurs implicitly. A disk task requires UUID attemptId, the immutable input fN/name parse snapshot, logicalFields and explicit one-to-one fieldBindings. Rules refer to logical `fieldId` / `fieldIds`; preflight rejects missing bindings, references after projection, conflicting names and known incompatible types. Java freezes normalized rules and output fields before publishing a task. `POST /internal/validate-rules` accepts schema3 saves without task bindings; `POST /internal/preflight` validates bindings against a specific input snapshot. The protected schemas route returns 21 JSON Schema definitions and a combined Draft 2020-12 schema. Semantic validation additionally enforces field lifecycle, decimal domains, safe regex syntax and parameter dependencies.

`cleaning.disk` executes each enabled rule in configured order over current survivors. CSV uses logical records (quoted newlines count once), XLSX uses read-only rows for one selected sheet, and Parquet uses Arrow batches with scalar rows materialized incrementally. Current/original records and issue events live in an indexed SQLite database; no complete source/result/dirty DataFrame or growing list is built. First/last duplicate winners use original source order. Global numeric samples use exact scaled sort keys; indexed quantiles use linear interpolation; means use exact sums/counts; modes use typed keys with ties resolved by first source occurrence. Indexes are maintained during ingestion to avoid a full in-memory sort. Result and dirty artifacts are written incrementally.

Schema-3 decimal values use precision 1..38, scale 0..12 (default2), and HALF_UP. Decimal constants carry exact text, e.g. `{ "type":"decimal", "value":"1.23", "scale":2 }`. Integer constants accept JSON integers or exact signed int64 strings, allowing browser clients to preserve values above 2^53. Numeric constants and statistics never pass through float for equality, sorting, aggregation or range checks. Mean/median require an explicit numeric cast for known textual columns. `replace` performs bounded literal replacement; `compare_fields` compares two current typed fields with explicit null policy. Boolean comparisons allow eq/ne only.

Disk limits are independently frozen in `limits`: batchRows default1000/cap10000; maxRecordBytes default4MiB/cap16MiB; maxBatchBytes default32MiB/cap64MiB; maxScratchBytes default1GiB/cap4GiB; maxRecordIssueBytes default1MiB/cap4MiB. maxRecordBytes cannot exceed maxBatchBytes. `DISK_BATCH_ROWS` and `MAX_SCRATCH_BYTES` supply defaults only when absent from the task snapshot. Scratch accounting includes downloaded input, SQLite/journals, Excel temporary files and output artifacts within the attempt directory. Byte-aware batches bound serialized source/current/original payloads. Existing file/row/cell/logical-text/issue/output caps still apply. Parent Worker enforces total execution deadline and cancellation by stopping the child process before reporting completion and cleaning attempt scratch.

Reports reconcile inputRows = outputRows + finalRemovedRows and outputRows = validOutputRows + flaggedOutputRows. Each rule reports actual input/output stageRows, durationMs, issues, changed cells and its own actions; finalRemovalByRule assigns one removing rule per source. Dirty events preserve typed rule-entry before values, rule action and final disposition, including a mark followed by a later removal. Stable record IDs hash file identity, parse version/snapshot, resolved parsing, sheet and logical source ordinal, and remain equal across retry attempts. All artifacts retain the established task/numeric-attempt object paths. A mixed-type Parquet output fails with a request for a final explicit cast or another output format; decimal precision/scale conflicts fail instead of silently rounding. Fully empty outputs retain their declared schema.

## Parsing and immutable fields

CSV detects UTF-8/BOM, BOM-marked UTF-16 or GB18030 from a bounded sample; manual UTF-8/BOM, GB18030, UTF-16 and Latin-1 are available. Delimiters support auto, comma, semicolon, tab and pipe. Ambiguous encodings require manual overrides. Resolved parse options are returned and frozen in task snapshots. CSV retains text/leading zeros. Default missingValues is `[""]`; `NA` is text unless configured. With an empty marker array, empty fields remain strings. Explicit null is always missing. Blank records count as one row; field-count mismatches fail.

XLSX validates ZIP and workbook contents, reports sheet names/dimensions, and reads sheetName or the first sheet. Formula expressions remain literal strings. Parquet verifies both PAR1 magic markers and accepts only scalar, non-binary fields. Actual binary/file-format mismatches fail parsing.

Original positions define stable f0, f1, ... IDs. Preview returns original headers, dtype, inferredType and sample null counts. Inference is advisory; fieldTypes explicitly converts selected IDs and fails with field/source-row context for invalid data. Types: integer, number, string, datetime, boolean. Immutable input.fields must match original names/IDs in full execution. Stable IDs take precedence over a conflicting legacy original name. All eight issue/quality metadata headers are reserved.

Preview reads at most limit+1 rows. A null rowCount means total rows were not counted. Known Parquet row/cell metadata is validated immediately. Bounded CSV/XLSX preview does not imply full-file capacity or type validation; execution validates the entire selected input. Preview inference/null counts describe its displayed sample.

## Rules and quality policies

Historical schema-1 `v3-sequential` tasks execute in configured array order, applying each rule's removals immediately. Explicit or migrated `v2-phased` tasks retain the following historical phase order:

| Phase | Rules |
|---|---|
| 10 normalization | trim, case |
| 20 conversion | cast |
| 30 missing | missing, fill_null, fill_stat, drop_null |
| 40 outliers | range, iqr |
| 50 validation | required, enum, length, regex, validate_type, compare |
| 60 duplicates | deduplicate |
| 70 projection | select_fields, drop_fields, rename |

All references bind once to stable IDs or legacy original names. Rename changes only final output labels; selection preserves requested ordering and cannot drop every field. Report records original index, actual order, phase and ruleSnapshot.

Validation-capable rules accept onError drop/mark/fail, default drop. Range/IQR also accept null, clearing the offending field and recording its issue; V3 cast also supports null. Mark retains invalid cast values, appending reasons to result `_quality_errors` JSON and `_quality_valid`. Fail stops with rule/field/source-row context. V2 phase-50 validations collect before deferred removal. V3 removes rows immediately after each rule and collects all failing fields of that rule; mark/null retain the row for later diagnostics. Removed records never enter later statistics or deduplication.

Integer casts require exact signed int64. Number casts reject nonfinite values; precision 0..12 uses Decimal ROUND_HALF_UP (rounding, not a display-width template). Boolean accepts case-insensitive true/false, yes/no and 1/0. Datetime accepts dateFormat, uses UTC internally, and optionally exports using a validated portable strftime outputFormat. outputFormat applies only to datetime; numeric formatting uses precision. Numeric Unix timestamps are rejected. validate_type checks convertibility without mutation.

missing normalizes explicit markers, including trim-produced empties. fill_null accepts non-null finite scalars. fill_stat computes mean/median/mode from current surviving valid data, reports excluded invalid numeric samples, and applies explicit fail/leave/constant when no valid data exists. Mode ties keep first value in original input order. Mean/median avoid intermediate overflow. IQR reports Q1/Q3, valid sample count, factor and thresholds; zero spread still enforces its bounds. Nonfinite statistics fail explicitly.

Deduplication is global over current survivors, supports first/last in original input order and all business fields when columns are absent/empty. Quality metadata never affects keys. Compare supports only declared typed equality, order and string predicates; no eval, scripts, SQL or Python execution. Numeric comparison targets convert numeric operands; incompatible ordered/string values are violations. Range supports inclusive/exclusive bounds. Enum uses typed equality. Missing values skip ordinary validators; required enforces nonnull. Regex uses fullmatch, restricted syntax, 10 ms per cell and 10 seconds per rule.

Dirty CSV has one row per violation: original values plus _record_id, _source_row, _field, _rule_index, _error_code, _reason. dirtyRows counts unique issue records; issueCount counts diagnostics; removedRows counts unique removed records; flaggedRows counts output records with issues (mark/null). Conservation: inputRows = outputRows + removedRows. changedCells counts transformations of each rule's surviving cells; later removals can already have contributed to earlier transformation counts.

## Resource and export budgets

Task limits freeze administrator settings within hard caps: file100MiB, rows500000, columns200, cells5000000, expandedXLSX512MiB, preview1000. Java enforces coexisting business limit keys. Additional engine budgets:

- Logical string payload <=256MiB, counting every repeated cell even for shared/dictionary strings.
- Historical DataFrame engines enforce pandas deep memory <=512MiB before/after each rule. Fill amplification is checked before candidate allocation; text transformations check incrementally. The disk engine uses the byte-aware batches and scratch caps described above.
- Dirty output <=100000 issues and 5000000 cells. Collection checks predicted original-cell/metadata memory before appending and before dirty DataFrame construction.
- Every artifact <=256MiB. CSV checks encoded bytes before writes; completed XLSX/Parquet outputs are checked and removed on overflow.

CSV uses UTF-8 BOM. CSV/XLSX formula-leading headers/cells receive a protective apostrophe with a report warning, including dirty exports. Native negative numbers stay numeric. Parquet stores typed values without escaping. Mixed-type columns require an explicit cast or CSV/XLSX output, never silent stringification. XLSX strings longer than32767 fail instead of truncating. Dates export as ISO text by default in CSV/XLSX; native Parquet timestamps stay typed unless outputFormat is set.

## Queue and failure behavior

A Worker executes one delivery with prefetch1. A child process performs data work while the parent services AMQP heartbeats, polls cancellation and renews Java leases. The parent enforces the total execution deadline, requests a cooperative stop and terminates the child after the configured grace period. It reports CANCELED only after the child stops. This also bounds cancellation while a native library is busy. Attempt scratch is removed after the child exits.

Claims return leaseToken; V3 control/results carry token+attempt+UUID attemptId. Stale/terminal duplicates are acknowledged without execution; live-lease conflicts delay/requeue. Terminal deliveries are acknowledged after Java accepts callbacks. Expired leases recover as a new attempt with isolated paths only before the execution deadline and within the retry limit; an expired deadline fails with TASK_TIMEOUT. Cancellation reports CANCELED only after the executor stops. The supervisor removes its three exact partial artifact keys on failure/cancellation; failed removals are discoverable by the Java retention plan (dry-run by default). Invalid envelopes go to cleaning.dead; identifiable invalid-rule tasks are claimed/reported FAILED. Callback headers and lifecycle logs carry requestId. Main-loop heartbeat file supports the Compose Worker liveness check.

Tests substitute infrastructure adapters only for deterministic failure paths. Root Docker acceptance scripts exercise actual services and downloads.

`tests/test_disk_v3.py` executes all21 rules, the canonical eight-row oracle at batch1/2/3/1000 with CSV/XLSX/Parquet results, independent full-data statistical/dedup oracles, exact 38-digit values, logical multiline source positions, empty outputs and resource failures. It also runs both real historical and disk engines over the same marker/null/mode/dedup cases, comparing business output order, issues/reasons/before-values/actions/final attribution, report totals and per-rule row flow at several batch sizes.
