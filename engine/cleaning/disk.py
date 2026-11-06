"""Sequential, bounded-memory V3 execution backed by per-attempt SQLite scratch.

Each rule is a full survivor pass. Global rules build disk indexes before their
mutation pass; chunk boundaries never define statistics or duplicate identity.
"""
from datetime import datetime
from decimal import Decimal, localcontext
import json
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import time
import tempfile

import pandas as pd
import regex

from . import budgets
from .bindings import cast_v3, decimal_value, typed_scalar, typed, canonical_key, numeric, preflight, OPS
from .models import InputError, CancelledError, validate_limits
from .streamio import TableStream, StreamWriter
from .tableio import is_missing

DEFAULTS = {"batchRows": 1000, "maxRecordBytes": 4*1024*1024, "maxBatchBytes": 32*1024*1024, "maxScratchBytes": 1024*1024*1024, "maxRecordIssueBytes": 1024*1024}
CAPS = {"batchRows": 10000, "maxRecordBytes": 16*1024*1024, "maxBatchBytes": 64*1024*1024, "maxScratchBytes": 4*1024*1024*1024, "maxRecordIssueBytes": 4*1024*1024}
DIRTY = ["_record_id", "_source_row", "_field", "_rule_index", "_error_code", "_reason", "_file_id", "_sheet", "_rule_id", "_before_value", "_action", "_final_action", "_source_record_number", "_removed_by_rule_id", "_attempt_id"]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def encode(record):
    values = {}
    for key,value in record.items():
        if isinstance(value,float) and not is_missing(value):
            if not math.isfinite(value): raise InputError("Nonfinite source numeric value is unsupported")
            # Preserve native source float identity; explicit Decimal casts remain Decimal.
            values[key] = {"type":"float","value":repr(value)}
        else: values[key] = typed(value)
    return dumps(values)


def decode(payload):
    result = {}
    for key, item in json.loads(payload).items():
        dtype, value = item["type"], item["value"]
        result[key] = None if dtype == "null" else Decimal(value) if dtype == "decimal" else float(value) if dtype == "float" else pd.Timestamp(value) if dtype == "datetime" else value
    return result


def different(left, right):
    if is_missing(left) and is_missing(right): return False
    return type(left) is not type(right) or left != right


class DiskEngine:
    def __init__(self, message, directory, cancel, progress):
        self.message, self.directory, self.cancel, self.progress = message, Path(directory), cancel, progress
        self.config = dict(DEFAULTS)
        for key, name in (("batchRows", "DISK_BATCH_ROWS"), ("maxScratchBytes", "MAX_SCRATCH_BYTES")):
            if key not in message.limits and os.environ.get(name):
                try: self.config[key] = int(os.environ[name])
                except ValueError as exc: raise InputError(f"{name} must be an integer") from exc
                if not 1 <= self.config[key] <= CAPS[key]: raise InputError(f"{name} exceeds supported limits")
        for key, cap in CAPS.items():
            if key in message.limits:
                value = message.limits[key]
                if type(value) is not int or not 1 <= value <= cap: raise InputError(f"{key} must be integer 1..{cap}")
                self.config[key] = value
        if self.config["maxRecordBytes"] > self.config["maxBatchBytes"]: raise InputError("maxRecordBytes cannot exceed maxBatchBytes")
        self.batch_rows = self.config["batchRows"]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "scratch.sqlite")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA temp_store=FILE")
        scratch_sql = str(self.directory.resolve()).replace("'", "''")
        self.db.execute(f"PRAGMA temp_store_directory='{scratch_sql}'")
        self.db.execute("PRAGMA cache_size=-4096")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE records(seq INTEGER PRIMARY KEY, original TEXT NOT NULL, data TEXT NOT NULL, alive INTEGER NOT NULL DEFAULT 1, flagged INTEGER NOT NULL DEFAULT 0, removed_rule TEXT, removed_code TEXT, issue_bytes INTEGER NOT NULL DEFAULT 0);
          CREATE INDEX survivors ON records(alive,seq);
          CREATE TABLE issues(id INTEGER PRIMARY KEY,seq INTEGER NOT NULL,rule_index INTEGER NOT NULL,rule_id TEXT NOT NULL,field TEXT NOT NULL,before_value TEXT NOT NULL,action TEXT NOT NULL,code TEXT NOT NULL,reason TEXT NOT NULL);
          CREATE INDEX issue_order ON issues(seq,rule_index,id);
          CREATE INDEX issue_code_seq ON issues(code,seq);
          CREATE INDEX removed_rules ON records(alive,removed_rule);
        """)
        self.stats = []
        self.issue_count = self.issue_payload = self.changed_total = self.filled_total = self.type_failed = 0
        self.headers = self.names = self.types = self.original_names = None
        self.field_map = message.fieldBindings
        self.rule_snapshot = []
        self.formats = {}
        self.warnings = []
        self.input_rows = 0
        self.scratch_peak = 0
        self.null_before = {}
        self.sheet = None

    def check(self, anticipated_bytes=0):
        if self.cancel.is_set(): raise CancelledError("Task canceled")
        size = sum(path.stat().st_size for path in self.directory.rglob("*") if path.is_file())
        self.scratch_peak = max(self.scratch_peak, size)
        if size+anticipated_bytes > self.config["maxScratchBytes"]: raise InputError("Attempt scratch exceeds maxScratchBytes")
        return size

    def commit(self):
        # The DELETE journal can disappear during commit. Inspect while it exists.
        self.check()
        self.db.commit()
        self.check()

    def batches(self, alive=True):
        after = 0
        while True:
            self.check()
            cursor = self.db.execute("SELECT seq,data,original,flagged FROM records WHERE seq>?" + (" AND alive=1" if alive else "") + " ORDER BY seq LIMIT ?", (after, self.batch_rows))
            rows, size = [], 0
            for row in cursor:
                row_size = len(row[1].encode("utf-8")) + len(row[2].encode("utf-8")) + 128
                if row_size > self.config["maxBatchBytes"]: raise InputError("Stored record exceeds maxBatchBytes")
                if rows and size + row_size > self.config["maxBatchBytes"]: break
                rows.append(row); size += row_size
            cursor.close()
            if not rows: return
            after = rows[-1][0]
            yield rows

    def ingest(self, path):
        limits = self.message.limits | self.config
        with TableStream(path, self.message.input.format, self.message.input.parseOptions, limits, self.batch_rows) as source:
            self.headers = [f"f{i}" for i in range(len(source.headers))]
            self.names = dict(zip(self.headers, source.headers))
            self.original_names = dict(self.names)
            self.types = {fid: dict(source.initial_specs.get(fid, {"dtype": source.initial_types.get(fid, "unknown")})) for fid in self.headers}
            self.sheet = source.options.get("sheetName") if self.message.input.format == "xlsx" else None
            expected = self.message.input.fields
            if expected is None or len(expected) != len(self.headers) or any(field.get("id") != fid or field.get("name") != name for field, fid, name in zip(expected, self.headers, source.headers)):
                raise InputError("Input schema does not match immutable parse snapshot")
            check_fields = [dict(field, logicalType=source.initial_types.get(field["id"], "unknown")) for field in expected]
            plan = preflight(check_fields, self.message.logicalFields, self.field_map, self.message.rules, self.message.schemaVersion)
            self.rule_snapshot = plan["normalizedRules"]
            self.null_before = {fid: 0 for fid in self.headers}
            for batch in source.batches():
                self.check()
                inserts = []
                inserted_bytes = 0
                for values in batch:
                    self.input_rows += 1
                    record = dict(zip(self.headers, values))
                    for fid, value in record.items():
                        if source.initial_types.get(fid) == "unknown" and not is_missing(value):
                            dtype = typed(value)["type"]
                            previous = self.types[fid]["dtype"]
                            self.types[fid]["dtype"] = dtype if previous == "unknown" else previous if previous == dtype else "decimal" if {previous, dtype} <= {"integer", "decimal"} else "mixed"
                            if isinstance(value,float) and (previous == "unknown" or previous == "decimal" and self.types[fid].get("nativeFloat")):
                                self.types[fid]["nativeFloat"] = 64
                            elif isinstance(value,int) and not isinstance(value,bool): self.types[fid].pop("nativeFloat",None)
                    for fid, value in record.items():
                        if is_missing(value): self.null_before[fid] += 1
                    payload = encode(record)
                    encoded_size = len(payload.encode("utf-8"))
                    if encoded_size > self.config["maxRecordBytes"]: raise InputError("Encoded source record exceeds maxRecordBytes")
                    stored_size = encoded_size*2+128
                    if stored_size > self.config["maxBatchBytes"]: raise InputError("Stored record exceeds maxBatchBytes")
                    if inserts and inserted_bytes+stored_size > self.config["maxBatchBytes"]:
                        self.check(inserted_bytes+16384)
                        self.db.executemany("INSERT INTO records(seq,original,data) VALUES(?,?,?)", inserts)
                        self.commit()
                        inserts, inserted_bytes = [],0
                    inserts.append((self.input_rows, payload, payload))
                    inserted_bytes += stored_size
                self.check(inserted_bytes+16384)
                self.db.executemany("INSERT INTO records(seq,original,data) VALUES(?,?,?)", inserts)
                self.commit()
                self.check()
            self.parse_options = source.options
            self.warnings.extend(source.warnings)

    def refs(self, rule):
        if "fieldIds" in rule: return [self.field_map[fid] for fid in rule["fieldIds"]]
        if "fieldId" in rule: return [self.field_map[rule["fieldId"]]]
        if rule["type"] == "compare_fields": return [self.field_map[rule["leftFieldId"]], self.field_map[rule["rightFieldId"]]]
        return []

    def numeric_samples(self, fid, allow_text):
        self.db.execute("DROP TABLE IF EXISTS samples")
        self.db.execute("CREATE TABLE samples(sort_key TEXT NOT NULL,seq INTEGER NOT NULL,value TEXT NOT NULL)")
        self.db.execute("CREATE INDEX sample_order ON samples(sort_key,seq)")
        count = invalid = missing_count = 0
        total = Decimal(0)
        with localcontext() as context:
            context.prec = 100
            for batch in self.batches():
                self.check(len(batch)*256+16384)
                inserts = []
                for seq, payload, _, _ in batch:
                    value = decode(payload)[fid]
                    if is_missing(value): missing_count += 1; continue
                    try: number = decimal_value(value) if allow_text else numeric(value)
                    except ValueError: number = None
                    if number is None: invalid += 1; continue
                    scaled = int(number * Decimal(10)**12)
                    sort_key = f"{scaled + 10**51:052d}"
                    inserts.append((sort_key, seq, str(number)))
                    total += number; count += 1
                self.db.executemany("INSERT INTO samples VALUES(?,?,?)", inserts)
                self.commit()
        self.check()
        return count, invalid, missing_count, total

    def quantile(self, count, fraction):
        with localcontext() as context:
            context.prec = 100
            position = Decimal(count-1) * fraction
            lower = int(position); delta = position - lower
            values = self.db.execute("SELECT value FROM samples ORDER BY sort_key,seq LIMIT 2 OFFSET ?", (lower,)).fetchall()
            left = Decimal(values[0][0]); right = Decimal(values[-1][0])
            return left + (right-left)*delta

    def prepare_global(self, rule, fields):
        kind = rule["type"]
        result, statistics = {}, {}
        if kind == "deduplicate":
            self.db.execute("DROP TABLE IF EXISTS keys")
            self.db.execute("CREATE TABLE keys(key TEXT PRIMARY KEY,winner INTEGER NOT NULL,count INTEGER NOT NULL)")
            for batch in self.batches():
                self.check(sum(len(payload.encode("utf-8")) for _,payload,_,_ in batch)+16384)
                for seq, payload, _, _ in batch:
                    record = decode(payload)
                    key = self.dedup_key(record, fields, rule, seq)
                    self.db.execute("INSERT INTO keys VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET winner=" + ("min(keys.winner,excluded.winner)" if rule["keep"] == "first" else "max(keys.winner,excluded.winner)") + ",count=keys.count+1", (key, seq))
                self.commit()
            statistics = {"distinctKeys": self.db.execute("SELECT COUNT(*) FROM keys").fetchone()[0], "keepPolicy": rule["keep"], "nullsEqual": rule["nullsEqual"]}
        elif kind == "iqr":
            count, invalid, misses, _ = self.numeric_samples(fields[0], False)
            if not count and rule["emptyPolicy"] == "fail": raise InputError(f"Rule {rule['id']}: no valid numeric IQR samples")
            low = high = q1 = q3 = None
            if count:
                with localcontext() as context:
                    context.prec = 100
                    q1 = self.quantile(count, Decimal(".25")); q3 = self.quantile(count, Decimal(".75"))
                    factor = decimal_value(rule["factor"])
                    low, high = q1-factor*(q3-q1), q3+factor*(q3-q1)
            result = {"low": low, "high": high}
            statistics = {"validValueCount": count, "excludedInvalidValueCount": invalid, "missingValueCount": misses, "q1": str(q1) if q1 is not None else None, "q3": str(q3) if q3 is not None else None, "lower": str(low) if low is not None else None, "upper": str(high) if high is not None else None, "factor": str(rule["factor"])}
        elif kind == "fill_stat":
            for fid in fields:
                if rule["strategy"] == "mode":
                    self.db.execute("DROP TABLE IF EXISTS modes")
                    self.db.execute("CREATE TABLE modes(key TEXT PRIMARY KEY,value TEXT,count INTEGER,first_seq INTEGER)")
                    self.db.execute("CREATE INDEX mode_winner ON modes(count DESC,first_seq)")
                    count = invalid = misses = 0
                    for batch in self.batches():
                        self.check(sum(len(payload.encode("utf-8"))*2 for _,payload,_,_ in batch)+16384)
                        for seq, payload, _, _ in batch:
                            value = decode(payload)[fid]
                            if is_missing(value): misses += 1; continue
                            count += 1
                            self.db.execute("INSERT INTO modes VALUES(?,?,1,?) ON CONFLICT(key) DO UPDATE SET count=modes.count+1", (canonical_key(value), encode({"v": value}), seq))
                        self.commit()
                    winner = self.db.execute("SELECT value FROM modes ORDER BY count DESC,first_seq LIMIT 1").fetchone()
                    fill = decode(winner[0])["v"] if winner else None
                    total = None
                else:
                    count, invalid, misses, total = self.numeric_samples(fid, True)
                    with localcontext() as context:
                        context.prec = 100
                        raw = total/Decimal(count) if count and rule["strategy"] == "mean" else self.quantile(count, Decimal(".5")) if count else None
                        fill = decimal_value(raw, rule.get("precision", 38), rule.get("scale", 2)) if raw is not None else None
                if not count:
                    if rule["emptyPolicy"] == "fail": raise InputError(f"Rule {rule['id']}, field {fid}: no valid values for fill")
                    fill = typed_scalar(rule["value"], False) if rule["emptyPolicy"] == "constant" else None
                result[fid] = fill
                statistics[fid] = {"validValueCount": count, "excludedInvalidValueCount": invalid, "missingValueCount": misses, "fillValue": typed(fill), "emptyPolicy": rule["emptyPolicy"]}
                if total is not None:
                    statistics[fid].update({"sum": str(total), "scale": rule.get("scale", 2), "rounding": "HALF_UP", "unroundedMean": {"numerator": str(total), "denominator": count} if rule["strategy"] == "mean" else None})
        return result, statistics

    def dedup_key(self, record, fields, rule, seq):
        values = [record[fid] for fid in fields]
        if not rule["nullsEqual"] and any(is_missing(value) for value in values): return "source:" + str(seq)
        return dumps([canonical_key(value) for value in values])

    def predicate(self, value, target, op):
        if op in {"eq", "ne"}:
            equal = canonical_key(value) == canonical_key(target)
            return equal if op == "eq" else not equal
        if op in {"contains", "starts_with", "ends_with"}:
            return isinstance(value, str) and isinstance(target, str) and (target in value if op == "contains" else value.startswith(target) if op == "starts_with" else value.endswith(target))
        left, right = numeric(value), numeric(target)
        if left is not None and right is not None: return OPS[op](left, right)
        if type(value) is type(target) and isinstance(value, (str, datetime, pd.Timestamp)):
            return OPS[op](value, target)
        return False

    def apply_row(self, record, rule, fields, global_values, seq):
        """Return new record, quality issues, and transform cell events."""
        kind = rule["type"]
        new, issues, hits = dict(record), [], set()
        def issue(fid, value, reason): issues.append((fid, value, reason))
        if kind == "iqr" and global_values.get("low") is None and global_values.get("high") is None:
            return new, issues, hits
        if kind == "deduplicate":
            key = self.dedup_key(record, fields, rule, seq)
            winner = self.db.execute("SELECT winner FROM keys WHERE key=?", (key,)).fetchone()[0]
            if winner != seq: issue(",".join(fields), {fid: record[fid] for fid in fields}, "duplicate key")
        elif kind == "compare_fields":
            left, right = (record[fid] for fid in fields)
            if is_missing(left) or is_missing(right):
                valid = rule["nullPolicy"] == "skip"
            else:
                expected = rule["valueType"]
                valid_types = (type(left) is int and type(right) is int) if expected == "integer" else (numeric(left) is not None and numeric(right) is not None) if expected == "decimal" else isinstance(left, str) and isinstance(right, str) if expected == "string" else isinstance(left, (datetime, pd.Timestamp)) and isinstance(right, (datetime, pd.Timestamp)) if expected == "datetime" else type(left) is bool and type(right) is bool
                valid = valid_types and self.predicate(left, right, rule["operator"])
            if not valid: issue(",".join(fields), {fid: record[fid] for fid in fields}, "cross-field comparison failed or null/type policy violated")
        elif kind not in {"select_fields", "drop_fields", "rename"}:
            pattern = regex.compile(rule["pattern"]) if kind == "regex" else None
            for fid in fields:
                value = record[fid]
                if kind in {"trim", "case", "replace"}:
                    if isinstance(value, str):
                        if kind == "trim": new[fid] = value.strip()
                        elif kind == "case": new[fid] = getattr(value, rule["mode"])()
                        else:
                            occurrences = value.count(rule["search"])
                            if occurrences > 100000: raise InputError("replace exceeds per-cell occurrence budget")
                            count = rule["count"] or occurrences
                            estimate = len(value.encode("utf-8")) + min(count, occurrences)*max(0, len(rule["replacement"].encode("utf-8"))-len(rule["search"].encode("utf-8")))
                            if estimate > self.config["maxRecordBytes"]: raise InputError("replace exceeds maxRecordBytes")
                            new[fid] = value.replace(rule["search"], rule["replacement"], count)
                elif kind == "missing":
                    if any(canonical_key(value) == canonical_key(typed_scalar(marker)) for marker in rule["markers"]): new[fid] = None
                elif kind == "fill_null":
                    if is_missing(value): new[fid] = typed_scalar(rule["value"], False)
                elif kind == "fill_stat":
                    if is_missing(value) and global_values[fid] is not None: new[fid] = global_values[fid]
                elif kind in {"cast", "validate_type"}:
                    if not is_missing(value):
                        try:
                            converted = cast_v3(value, rule["dtype"], rule)
                            hits.add(fid)
                            if kind == "cast": new[fid] = converted
                        except ValueError as exc: issue(fid, value, str(exc))
                elif kind in {"required", "drop_null"}:
                    if is_missing(value): issue(fid, value, "missing required value")
                elif not is_missing(value):
                    valid = True
                    if kind in {"range", "iqr"}:
                        number = numeric(value)
                        low = decimal_value(rule["min"]) if "min" in rule else global_values.get("low")
                        high = decimal_value(rule["max"]) if "max" in rule else global_values.get("high")
                        valid = number is not None and (low is None or (number >= low if rule.get("inclusiveMin", True) else number > low)) and (high is None or (number <= high if rule.get("inclusiveMax", True) else number < high))
                    elif kind == "enum": valid = any(canonical_key(value) == canonical_key(typed_scalar(v)) for v in rule["values"])
                    elif kind == "length": valid = isinstance(value, str) and ("min" not in rule or len(value) >= rule["min"]) and ("max" not in rule or len(value) <= rule["max"])
                    elif kind == "regex":
                        try: valid = isinstance(value, str) and pattern.fullmatch(value, timeout=.01) is not None
                        except TimeoutError as exc: raise InputError("Regex exceeded per-cell timeout") from exc
                    elif kind == "compare": valid = self.predicate(value, typed_scalar(rule["value"], False), rule["operator"])
                    if not valid:
                        reason = "nonnumeric value or value outside configured bounds" if kind in {"range","iqr"} else "value is not in the allowed enumeration" if kind == "enum" else "string length outside bounds or nonstring value" if kind == "length" else "regular expression did not match" if kind == "regex" else f"comparison {rule['operator']} failed or types are incompatible" if kind == "compare" else kind+" validation failed"
                        issue(fid, value, reason)
                if different(value, new[fid]): hits.add(fid)
        return new, issues, hits

    def run_rule(self, index, rule):
        started = time.monotonic()
        count = self.db.execute("SELECT COUNT(*) FROM records WHERE alive=1").fetchone()[0]
        metrics = {name: 0 for name in ["hitRows", "hitCells", "issueCount", "changedCells", "removedRows", "markedRows", "flaggedRows", "nulledCells", "missingFilledCells", "typeConversionAttemptedCells", "typeConversionSucceededCells", "typeConversionFailedCells", "duplicateRemovedRows", "outlierHandledRows"]}
        summary = {"index": index, "ruleId": rule["id"], "type": rule["type"], "status": "COMPLETED" if rule["enabled"] else "DISABLED", "inputRows": count if rule["enabled"] else 0, "outputRows": count if rule["enabled"] else 0, **metrics}
        if not rule["enabled"]:
            summary["durationMs"] = 0; summary["stageRows"] = {"input": 0,"output": 0}; self.stats.append(summary); return
        kind = rule["type"]
        fields = self.refs(rule)
        if kind == "deduplicate" and not fields: fields = list(self.headers)
        if kind == "rename":
            self.names[fields[0]] = rule["target"]
        elif kind == "select_fields": self.headers = fields
        elif kind == "drop_fields": self.headers = [fid for fid in self.headers if fid not in fields]
        global_values, statistics = self.prepare_global(rule, fields) if kind in {"fill_stat", "deduplicate", "iqr"} else ({}, {})
        current_text = 0
        failed_fields,filled_fields = set(),set()
        for batch in self.batches():
            self.check()
            transaction_bytes = 0
            for seq, payload, original, flagged in batch:
                record = decode(payload)
                new, issues, hits = self.apply_row(record, rule, fields, global_values, seq)
                action = rule.get("onError", "drop")
                if issues and action == "fail": raise InputError(f"Rule {index} ({rule['id']}), field {issues[0][0]}, source record {seq}: {issues[0][2]}")
                if kind == "regex" and time.monotonic()-started > 10: raise InputError(f"Rule {rule['id']}: regex exceeds 10-second rule budget")
                dropped = bool(issues and action == "drop")
                summary["issueCount"] += len(issues)
                summary["hitRows"] += int(bool(hits or issues))
                summary["hitCells"] += len(hits | {fid for fid, _, _ in issues})
                summary["removedRows"] += int(dropped)
                summary["markedRows"] += int(bool(issues and action == "mark"))
                summary["flaggedRows"] += int(bool(issues and action in {"mark", "null"}))
                summary["nulledCells"] += len(issues) if action == "null" else 0
                summary["outlierHandledRows"] += int(bool(issues and kind in {"range", "iqr"}))
                summary["duplicateRemovedRows"] += int(dropped and kind == "deduplicate")
                if kind == "cast":
                    failed_fields.update(fid for fid,_,_ in issues)
                    attempted = sum(not is_missing(record[fid]) for fid in fields)
                    summary["typeConversionAttemptedCells"] += attempted
                    summary["typeConversionSucceededCells"] += attempted-len(issues)
                    summary["typeConversionFailedCells"] += len(issues)
                issue_size = 0
                for fid, value, reason in issues:
                    before = dumps({"type": "record", "value": {key: typed(v) for key, v in value.items()}} if isinstance(value, dict) else typed(value))
                    issue_size += len(before.encode("utf-8")) + len(reason.encode("utf-8")) + 512
                    self.issue_payload += len(original.encode("utf-8")) + len(before.encode("utf-8")) + 1024
                    budgets.check_text_bytes(self.issue_payload)
                    self.issue_count += 1
                    if self.issue_count > 100000 or self.issue_count*(len(self.original_names)+len(DIRTY)) > 5000000: raise InputError("Dirty output exceeds issue/cell budget")
                    self.db.execute("INSERT INTO issues(seq,rule_index,rule_id,field,before_value,action,code,reason) VALUES(?,?,?,?,?,?,?,?)", (seq,index,rule["id"],fid,before,action,kind.upper(),reason))
                    if action == "null": new[fid] = None
                if issue_size:
                    self.db.execute("UPDATE records SET issue_bytes=issue_bytes+? WHERE seq=?", (issue_size,seq))
                    if self.db.execute("SELECT issue_bytes FROM records WHERE seq=?", (seq,)).fetchone()[0] > self.config["maxRecordIssueBytes"]: raise InputError("Record issues exceed maxRecordIssueBytes")
                if dropped:
                    mutation_size = issue_size+len(payload.encode("utf-8"))+1024
                    if transaction_bytes and transaction_bytes+mutation_size > self.config["maxBatchBytes"]:
                        self.commit(); transaction_bytes=0
                    if mutation_size >= 65536: self.check(mutation_size)
                    transaction_bytes += mutation_size
                    self.db.execute("UPDATE records SET alive=0,removed_rule=?,removed_code=? WHERE seq=?", (rule["id"],kind.upper(),seq))
                else:
                    if kind in {"fill_null", "fill_stat"}:
                        filled = {fid for fid in fields if is_missing(record[fid]) and not is_missing(new[fid])}
                        filled_fields.update(filled)
                        summary["missingFilledCells"] += len(filled)
                    if kind not in {"rename", "select_fields", "drop_fields"}: summary["changedCells"] += sum(different(record[fid], new[fid]) for fid in self.headers)
                    projected = {fid: new[fid] for fid in self.headers}
                    encoded = encode(projected)
                    encoded_size = len(encoded.encode("utf-8"))
                    if encoded_size > self.config["maxRecordBytes"]: raise InputError("Transformed record exceeds maxRecordBytes")
                    stored_size = encoded_size+len(original.encode("utf-8"))+128
                    if stored_size > self.config["maxBatchBytes"]: raise InputError("Transformed stored record exceeds maxBatchBytes")
                    mutation_size = encoded_size+len(payload.encode("utf-8"))+issue_size+1024
                    if transaction_bytes and transaction_bytes+mutation_size > self.config["maxBatchBytes"]:
                        self.commit(); transaction_bytes=0
                    if mutation_size >= 65536:
                        self.check(mutation_size)
                    transaction_bytes += mutation_size
                    current_text += sum(budgets.text_bytes(v) for v in projected.values())
                    budgets.check_text_bytes(current_text)
                    self.db.execute("UPDATE records SET data=?,flagged=? WHERE seq=?", (encoded, int(flagged or bool(issues)),seq))
            self.commit()
        summary["outputRows"] -= summary["removedRows"]
        summary["stageRows"] = {"input": count, "output": summary["outputRows"]}
        summary["rejectedRows"] = summary["removedRows"]
        if kind == "cast":
            for fid in fields:
                self.formats.pop(fid,None)
                self.types[fid] = {key: rule[key] for key in ("dtype", "precision", "scale") if key in rule}
                if rule.get("onError") == "mark" and fid in failed_fields:
                    self.types[fid] = {"dtype": "mixed", "castSpec": dict(self.types[fid])}
                if "outputFormat" in rule: self.formats[fid] = rule["outputFormat"]
        if kind == "fill_stat" and rule["strategy"] != "mode":
            for fid in filled_fields:
                if self.types[fid]["dtype"] in {"integer", "decimal", "unknown"}:
                    self.types[fid] = {"dtype": "decimal", "precision": 38, "scale": max(self.types[fid].get("scale",0),rule["scale"])}
        if kind == "fill_stat" and rule["strategy"] == "mode":
            for fid in filled_fields:
                if self.types[fid]["dtype"] == "unknown" and global_values[fid] is not None:
                    item=typed(global_values[fid]); self.types[fid]={"dtype":item["type"],"precision":38,"scale":item.get("scale",0)}
        if kind == "fill_null":
            for fid in filled_fields:
                if self.types[fid]["dtype"] == "unknown": self.types[fid]={"dtype":rule["value"]["type"]}
        if kind == "fill_null" and rule["value"]["type"] in {"integer","decimal"}:
            for fid in filled_fields:
                if self.types[fid]["dtype"] == "decimal" or rule["value"]["type"] == "decimal":
                    self.types[fid] = {"dtype":"decimal","precision":38,"scale":max(self.types[fid].get("scale",0),rule["value"].get("scale",0))}
        if statistics: summary["statistics"] = statistics
        summary["durationMs"] = int((time.monotonic()-started)*1000)
        self.changed_total += summary["changedCells"]; self.filled_total += summary["missingFilledCells"]; self.type_failed += summary["typeConversionFailedCells"]
        self.stats.append(summary)
        self.progress(15 + int(index/max(1,len(self.rule_snapshot))*60), f"Completed rule {index}: {rule['id']}")

    def profile(self, original=False):
        fields = list(self.original_names) if original else self.headers
        nulls = dict(self.null_before) if original else {fid: 0 for fid in fields}
        if not original:
            for batch in self.batches():
                for _, payload, _, _ in batch:
                    record = decode(payload)
                    for fid in fields: nulls[fid] += int(is_missing(record[fid]))
        return [{"id": fid, "name": (self.original_names if original else self.names)[fid], "dtype": "source" if original else self.types[fid]["dtype"], "inferredType": "unknown" if original else self.types[fid]["dtype"], "nullCount": nulls[fid]} for fid in fields]

    def resolve_surviving_mixed_types(self):
        """Observe current survivors with only bounded per-field type metadata.

        A past marked cast failure may have been removed by a later rule. Source
        history must not force a mixed output schema after those records leave.
        """
        fields=[fid for fid in self.headers if self.types[fid]["dtype"] == "mixed"]
        if not fields: return
        observed={fid:set() for fid in fields}
        for batch in self.batches():
            for _,payload,_,_ in batch:
                for fid,item in json.loads(payload).items():
                    if fid in observed and item["type"] != "null": observed[fid].add(item["type"])
        for fid,kinds in observed.items():
            declared=self.types[fid].get("castSpec",{})
            if not kinds:
                self.types[fid]=dict(declared) if declared else {"dtype":"unknown"}
            elif kinds <= {"integer","decimal","float"}:
                if kinds == {"integer"}: self.types[fid]={"dtype":"integer"}
                elif kinds == {"float"}: self.types[fid]={"dtype":"decimal","nativeFloat":64}
                elif kinds == {"decimal"} and declared.get("dtype")=="decimal": self.types[fid]=dict(declared)
                else:
                    self.types[fid]={"dtype":"decimal","precision":38,"scale":declared.get("scale",0)}
            elif len(kinds)==1:
                dtype=next(iter(kinds))
                self.types[fid]=dict(declared) if declared.get("dtype")==dtype else {"dtype":dtype}

    def export(self):
        output = self.directory / ("result." + self.message.outputFormat)
        dirty = self.directory / "dirty.csv"
        if self.message.outputFormat == "parquet": self.resolve_surviving_mixed_types()
        flagged_count = self.db.execute("SELECT COUNT(*) FROM records WHERE alive=1 AND flagged=1").fetchone()[0]
        headers = [self.names[fid] for fid in self.headers] + (["_quality_errors", "_quality_valid"] if flagged_count else [])
        specs = {self.names[fid]: self.types[fid] for fid in self.headers}
        for fid in self.formats: specs[self.names[fid]] = {"dtype": "string"}
        if self.message.outputFormat == "parquet" and any(spec.get("dtype") == "mixed" for spec in specs.values()):
            raise InputError("Parquet mixed-type output requires a final explicit cast or CSV/XLSX output")
        decimal_fields = [fid for fid in self.headers if specs[self.names[fid]].get("dtype") == "decimal" and not specs[self.names[fid]].get("nativeFloat")]
        if self.message.outputFormat == "parquet" and decimal_fields:
            shapes={fid:[0,specs[self.names[fid]].get("scale",0)] for fid in decimal_fields}
            for batch in self.batches():
                for _,payload,_,_ in batch:
                    record=decode(payload)
                    for fid in decimal_fields:
                        value=record[fid]
                        if is_missing(value): continue
                        try: number=decimal_value(value)
                        except ValueError as exc:
                            raise InputError(f"Parquet field {self.names[fid]} exceeds exact decimal domain; add an explicit decimal cast with scale<=12 or cast to string") from exc
                        shapes[fid][0]=max(shapes[fid][0],max(0,number.adjusted()+1))
                        shapes[fid][1]=max(shapes[fid][1],max(0,-number.as_tuple().exponent))
            for fid,(integral,scale) in shapes.items():
                required=max(1,integral+scale)
                if required>38: raise InputError(f"Parquet field {self.names[fid]} requires precision>38; add an explicit decimal or string cast")
                previous=specs[self.names[fid]]
                resolved={"dtype":"decimal","precision":max(required,previous.get("precision",38)),"scale":scale}
                self.types[fid]=resolved
                specs[self.names[fid]]=resolved
        specs.update({"_quality_errors": {"dtype": "string"}, "_quality_valid": {"dtype": "boolean"}})
        with StreamWriter(output, self.message.outputFormat, headers, specs) as writer:
            for batch in self.batches():
                rows = []
                for seq, payload, _, flagged in batch:
                    record = decode(payload)
                    values = []
                    for fid in self.headers:
                        value = record[fid]
                        if fid in self.formats and isinstance(value, pd.Timestamp):
                            fmt = self.formats[fid]
                            value = value.isoformat() if fmt == "iso8601" else value.strftime(fmt)
                        values.append(value)
                    if flagged_count:
                        issue_rows = self.db.execute("SELECT rule_id,field,code,reason,action FROM issues WHERE seq=? ORDER BY rule_index,id", (seq,)).fetchall()
                        values.extend([dumps([{"ruleId": rid, "fieldId": fid, "code": code, "reason": reason, "action": action} for rid,fid,code,reason,action in issue_rows]), not bool(flagged)])
                    rows.append(values)
                writer.write_rows(rows); self.check()
            escaped = writer.escaped
        with StreamWriter(dirty, "csv", list(self.original_names.values()) + DIRTY) as writer:
            cursor = self.db.execute("SELECT i.seq,i.rule_index,i.rule_id,i.field,i.before_value,i.action,i.code,i.reason,r.original,r.removed_rule FROM issues i JOIN records r ON r.seq=i.seq ORDER BY i.seq,i.rule_index,i.id")
            while True:
                self.check()
                item = cursor.fetchone()
                if item is None: break
                seq,index,rid,fid,before,action,code,reason,original,removed = item
                record = decode(original)
                final = "REMOVED_BY_THIS_RULE" if removed == rid else "REMOVED_BY_LATER_RULE" if removed else "OUTPUT_FLAGGED"
                record_id = self.record_id(seq)
                values = [record[key] for key in self.original_names] + [record_id,seq,",".join(self.original_names.get(key,key) for key in fid.split(",")),index,code,reason,str(self.message.input.fileId) if self.message.input.fileId else None,self.sheet,rid,before,action,final,seq,removed,str(self.message.attemptId)]
                writer.write_rows([values]); self.check()
            escaped += writer.escaped
        if escaped: self.warnings.append(f"Spreadsheet formula protection escaped {escaped} cells")
        return output, dirty

    def record_id(self, seq):
        identity = {"fileId": str(self.message.input.fileId) if self.message.input.fileId else self.message.input.objectKey,
                    "parseVersion": self.message.input.parseVersion, "fields": self.message.input.fields,
                    "parseOptions": self.parse_options, "sheet": self.sheet, "sourceRecordNumber": seq}
        return hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()

    def report(self, started):
        output = self.db.execute("SELECT COUNT(*) FROM records WHERE alive=1").fetchone()[0]
        flagged = self.db.execute("SELECT COUNT(*) FROM records WHERE alive=1 AND flagged=1").fetchone()[0]
        removal = dict(self.db.execute("SELECT removed_rule,COUNT(*) FROM records WHERE alive=0 GROUP BY removed_rule"))
        issue_rows = self.db.execute("SELECT COUNT(*) FROM records r WHERE EXISTS(SELECT 1 FROM issues i WHERE i.seq=r.seq)").fetchone()[0]
        outliers = self.db.execute("SELECT COUNT(*) FROM records r WHERE EXISTS(SELECT 1 FROM issues i WHERE i.seq=r.seq AND i.code IN ('RANGE','IQR'))").fetchone()[0]
        duplicates = self.db.execute("SELECT COUNT(*) FROM records WHERE removed_code='DEDUPLICATE'").fetchone()[0]
        if self.input_rows != output+sum(removal.values()): raise InputError("Final source disposition does not reconcile")
        return {"reportSchemaVersion": 3, "schemaVersion": 3, "taskId": str(self.message.taskId), "attempt": self.message.attempt, "attemptId": str(self.message.attemptId), "engineVersion": "v3-disk", "inputRows": self.input_rows, "outputRows": output, "validOutputRows": output-flagged, "flaggedOutputRows": flagged, "finalRemovedRows": self.input_rows-output, "removedRows": self.input_rows-output, "dirtyRows": issue_rows, "issueRows": issue_rows, "flaggedRows": flagged, "issueCount": self.issue_count, "changedCells": self.changed_total, "duplicateRemovedRows": duplicates, "outlierHandledRows": outliers, "missingFilledCells": self.filled_total, "typeConversionFailedCells": self.type_failed, "finalRemovalByRule": removal, "rules": self.stats, "ruleSnapshot": self.rule_snapshot, "logicalFields": self.message.logicalFields, "fieldBindings": self.field_map, "executionOrder": [rule["id"] for rule in self.rule_snapshot if rule["enabled"]], "columnsBefore": self.profile(True), "columnsAfter": self.profile(), "parseOptions": self.parse_options, "limitsSnapshot": self.message.limits | self.config, "warnings": self.warnings, "durationSeconds": round(time.monotonic()-started,3)}


def execute_disk_task(message, storage, directory, cancel, progress):
    started = time.monotonic()
    # Validate declarations before downloading; actual header snapshot checked at ingest.
    preflight(message.input.fields, message.logicalFields, message.fieldBindings, message.rules, message.schemaVersion)
    engine = DiskEngine(message, directory, cancel, progress)
    previous_tempdir = tempfile.tempdir
    tempfile.tempdir = str(Path(directory).resolve())
    try:
        engine.check(); progress(5, "Downloading original file")
        source = Path(directory) / ("input." + message.input.format)
        remaining = engine.config["maxScratchBytes"]-engine.check()
        input_cap = validate_limits(message.limits)["maxFileBytes"]
        download_cap = min(input_cap, remaining)
        if download_cap < 1: raise InputError("Attempt scratch exceeds maxScratchBytes before download")
        try:
            storage.download_input(message.input.objectKey, source, max_bytes=download_cap)
        except InputError as exc:
            if download_cap < input_cap and "download byte limit" in str(exc):
                raise InputError("Input download would exceed maxScratchBytes") from exc
            raise
        engine.check(); progress(10, "Reading input in bounded batches")
        engine.ingest(source)
        for index, rule in enumerate(engine.rule_snapshot,1): engine.run_rule(index,rule)
        engine.check(); progress(80, "Writing results and issue trace incrementally")
        output, dirty = engine.export()
        report = engine.report(started)
        report["scratchPeakBytes"] = engine.scratch_peak
        task_id, attempt = str(message.taskId), message.attempt
        artifacts = {"result": f"results/{task_id}/{attempt}/result.{message.outputFormat}", "dirty": f"dirty/{task_id}/{attempt}/dirty.csv", "report": f"reports/{task_id}/{attempt}/report.json"}
        report_path = Path(directory)/"report.json"
        report_path.write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding="utf-8")
        engine.check(); progress(90, "Uploading complete artifacts")
        content = {"csv":"text/csv; charset=utf-8","xlsx":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","parquet":"application/vnd.apache.parquet"}
        for kind, path in (("result",output),("dirty",dirty),("report",report_path)):
            engine.check(); storage.upload(artifacts[kind],path,"application/json" if kind == "report" else content["csv"] if kind == "dirty" else content[message.outputFormat])
        engine.check(); progress(98,"Artifacts uploaded; committing report")
        return report, artifacts
    finally:
        engine.db.close()
        tempfile.tempdir = previous_tempdir
