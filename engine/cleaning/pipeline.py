from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import math
import operator
import sys
from pathlib import Path
from threading import Event
import time
from typing import Any, Callable

import numpy as np
import pandas as pd
import regex

from .models import (
    CancelledError,
    InputError,
    TaskMessage,
    PHASES,
    validate_rules,
    validate_limits,
)
from . import budgets
from .budgets import check_frame, check_text_bytes, text_bytes
from .storage import ObjectStorage
from .tableio import is_missing, json_value, profile, read_table, write_table
from .types import cast_scalar, typed_equal

MAX_ISSUES = 100_000
MAX_DIRTY_CELLS = 5_000_000
DIRTY_META = [
    "_record_id",
    "_source_row",
    "_field",
    "_rule_index",
    "_error_code",
    "_reason",
]
V3_DIRTY_META = [
    "_file_id",
    "_sheet",
    "_rule_id",
    "_before_value",
    "_action",
    "_final_action",
]


def _typed_trace_value(value: Any) -> dict[str, Any]:
    """A rule-entry value with an explicit logical type and exact Decimal text."""
    if isinstance(value, dict):
        return {
            "type": "record",
            "value": {key: _typed_trace_value(item) for key, item in value.items()},
        }
    if is_missing(value):
        return {"type": "null", "value": None}
    if isinstance(value, (bool, np.bool_)):
        return {"type": "boolean", "value": bool(value)}
    if isinstance(value, (int, np.integer)):
        return {"type": "integer", "value": int(value)}
    if isinstance(value, Decimal):
        return {
            "type": "decimal",
            "value": str(value),
            "scale": max(0, -value.as_tuple().exponent) if value.is_finite() else None,
        }
    if isinstance(value, (float, np.floating)):
        return {
            "type": "number",
            "value": float(value) if math.isfinite(value) else str(value),
        }
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return {"type": "datetime", "value": value.isoformat()}
    return {"type": "string", "value": str(value)}


@dataclass
class CleanResult:
    data: pd.DataFrame
    dirty: pd.DataFrame
    report: dict[str, Any]


def _cancelled(cancel: Event) -> None:
    if cancel.is_set():
        raise CancelledError("Task cancelled")


def _different(before: Any, after: Any) -> bool:
    if is_missing(before) and is_missing(after):
        return False
    if is_missing(before) != is_missing(after):
        return True
    return bool(type(before) is not type(after) or before != after)


def _cast_scalar(value: Any, dtype: str) -> Any:
    return cast_scalar(value, dtype)


def _numeric(value):
    try:
        return cast_scalar(value, "number")
    except ValueError:
        return None


def _exact_numeric(value):
    """Predicates must preserve int64/Decimal precision; statistics use floats separately."""
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _comparison(value, rule):
    target = rule["value"]
    op = rule["operator"]
    if op in {"eq", "ne"}:
        equal = typed_equal(value, target)
        return equal if op == "eq" else not equal
    if op in {"contains", "starts_with", "ends_with"}:
        if not isinstance(value, str) or not isinstance(target, str):
            return False
        return (
            target in value
            if op == "contains"
            else (
                value.startswith(target)
                if op == "starts_with"
                else value.endswith(target)
            )
        )
    if type(target) in {int, float}:
        value = _exact_numeric(value)
        if value is None:
            return False
        target = Decimal(str(target))
    elif not isinstance(value, type(target)) or isinstance(value, bool):
        return False
    return {
        "gt": operator.gt,
        "gte": operator.ge,
        "lt": operator.lt,
        "lte": operator.le,
    }[op](value, target)


def clean_frame(
    original: pd.DataFrame,
    rules: list[dict[str, Any]],
    task_id: str = "local",
    attempt: int = 1,
    cancel: Event | None = None,
    progress: Callable[[int, str], None] | None = None,
    fields: list[dict] | None = None,
    engine_version: str = "v2-phased",
    attempt_id: str | None = None,
    file_id: str | None = None,
    sheet_name: str | None = None,
) -> CleanResult:
    rules = validate_rules(rules, engine_version)
    sequential = engine_version == "v3-sequential"
    cancel = cancel or Event()
    progress = progress or (lambda *_: None)
    _cancelled(cancel)
    start = time.monotonic()
    check_frame(original)
    if sequential and set(original.columns) & set(V3_DIRTY_META):
        raise InputError("Input headers conflict with reserved V3 dirty-row metadata columns")
    original = original.copy(deep=False)
    original.index = pd.RangeIndex(len(original))
    field_ids = [f"f{i}" for i in range(len(original.columns))]
    original_names = dict(zip(field_ids, original.columns))
    if fields is not None:
        if len(fields) != len(field_ids) or any(
            f.get("id") != fid or f.get("name") != original_names[fid]
            for f, fid in zip(fields, field_ids)
        ):
            raise InputError("Input schema does not match the immutable field snapshot")
    name_to_id = {name: fid for fid, name in original_names.items()}
    display_names = dict(original_names)
    bound = []
    for index, rule in enumerate(rules, 1):
        executable_rule = (
            {key: value for key, value in rule.items() if key not in {"id", "enabled"}}
            if engine_version == "v3-sequential"
            else rule
        )
        refs = executable_rule.get(
            "columns", [executable_rule["column"]] if "column" in executable_rule else []
        )
        columns = []
        for reference in refs:
            fid = (
                reference if reference in original_names else name_to_id.get(reference)
            )
            if fid is None:
                raise InputError(
                    f"Rule {index} ({executable_rule['type']}): missing columns: {reference}"
                )
            if fid in columns:
                raise InputError(f"Rule {index}: duplicate bound field reference")
            columns.append(fid)
        if engine_version == "v3-sequential":
            executable_rule = executable_rule | {
                "id": rule["id"],
                "enabled": rule["enabled"],
            }
        bound.append((index, executable_rule, columns))
    if engine_version == "v3-sequential":
        ordered = [entry for entry in bound if entry[1]["enabled"]]
        disabled = [entry for entry in bound if not entry[1]["enabled"]]
    else:
        ordered = sorted(bound, key=lambda entry: PHASES[entry[1]["type"]])
        disabled = []
    before_profile = profile(original, field_ids)
    frame = original.copy(deep=True)
    frame.columns = field_ids
    summaries = []
    issues = []
    issue_traces = {}
    issue_records = set()
    marked = {}
    changed_total = 0
    issue_payload = 0
    issue_memory = 0
    row_memory_cache = {}
    deferred_drop = set()
    previous_phase = None
    export_formats = {}
    dirty_meta = DIRTY_META + (V3_DIRTY_META if sequential else [])
    removed_by_row = {}
    final_removal_by_rule = {}
    duplicate_removed = set()
    outlier_handled = set()
    missing_filled_cells = 0
    type_conversion_failed_cells = 0

    def before_value_json(row, fid):
        if kind == "deduplicate":
            value = {key: frame.at[row, key] for key in columns}
        else:
            value = frame.at[row, fid]
        return json.dumps(
            _typed_trace_value(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    def projected_issue_memory(row, reason, before_value=None):
        if row not in row_memory_cache:
            row_memory_cache[row] = sum(sys.getsizeof(v) + 8 for v in original.loc[row])
        return (
            row_memory_cache[row]
            + 1024
            + sys.getsizeof(reason)
            + (512 + sys.getsizeof(before_value) if sequential else 0)
        )

    class Violations(list):
        """Bound the diagnostic list before appending and before dirty-frame allocation."""

        memory = 0

        def append(self, value):
            row, fid, reason = value
            count = len(issues) + len(self) + 1
            if count > MAX_ISSUES:
                raise InputError(
                    f"Issue count exceeds the {MAX_ISSUES}-issue safety budget"
                )
            if count * (len(original.columns) + len(dirty_meta)) > MAX_DIRTY_CELLS:
                raise InputError("Dirty output exceeds the 5000000-cell safety budget")
            projected = projected_issue_memory(
                row, reason, before_value_json(row, fid) if sequential else None
            )
            if issue_memory + self.memory + projected > budgets.MAX_FRAME_MEMORY_BYTES:
                raise InputError("Dirty output would exceed the 512 MiB memory budget")
            self.memory += projected
            super().append(value)

        def extend(self, values):
            for value in values:
                self.append(value)

    def add_issue(row, fid, index, kind, reason, policy):
        nonlocal issue_payload, issue_memory
        field = original_names.get(fid, fid)
        detail = {
            "ruleIndex": index,
            "field": field,
            "code": kind.upper(),
            "reason": reason,
        }
        before_value = before_value_json(row, fid) if sequential else None
        if sequential:
            detail.update(
                {
                    "ruleId": rule["id"],
                    "beforeValue": json.loads(before_value),
                    "actionAtRule": policy,
                }
            )
        if policy == "fail":
            raise InputError(
                f"Rule {index} ({kind}), field {field}, data row {row+1}: {reason}"
            )
        if len(issues) >= MAX_ISSUES:
            raise InputError(
                f"Issue count exceeds the {MAX_ISSUES}-issue safety budget"
            )
        # Dirty exports repeat original record payload for every violation.
        issue_payload += sum(text_bytes(v) for v in original.loc[row]) + sum(
            text_bytes(str(v)) for v in detail.values()
        )
        if sequential:
            issue_payload += (
                text_bytes(before_value)
                + text_bytes(file_id)
                + text_bytes(sheet_name)
                + text_bytes(rule["id"])
                + 100
            )
        check_text_bytes(issue_payload)
        issue_memory += projected_issue_memory(row, reason, before_value)
        issues.append((row, fid, index, kind.upper(), reason))
        if sequential:
            issue_traces[(row, fid, index)] = {
                "_file_id": file_id,
                "_sheet": sheet_name,
                "_rule_id": rule["id"],
                "_before_value": before_value,
                "_action": policy,
            }
        issue_records.add(row)
        if policy == "mark" or (sequential and policy == "null"):
            marked.setdefault(row, []).append(detail)

    for order, (index, rule, configured_columns) in enumerate(ordered, 1):
        _cancelled(cancel)
        kind = rule["type"]
        phase = PHASES[kind]
        if (
            engine_version == "v2-phased"
            and previous_phase == 50
            and phase != 50
            and deferred_drop
        ):
            frame = frame.drop(index=list(deferred_drop))
            deferred_drop.clear()
        if engine_version == "v2-phased":
            previous_phase = phase
        columns = list(configured_columns)
        if kind == "deduplicate" and not columns:
            columns = list(frame.columns)
        for fid in columns:
            if fid not in frame.columns:
                raise InputError(
                    f"Rule {index}: field {original_names[fid]} was removed by projection"
                )
        input_rows = len(frame)
        budget = check_frame(frame)
        policy = rule.get("onError", "drop")
        if kind == "fill_null":
            budget.preflight_fill(
                sum(int(frame[c].isna().sum()) for c in columns), rule["value"]
            )
        candidate = frame.copy(deep=True)
        violations = Violations()
        statistics = None
        if kind in {"trim", "case", "missing", "fill_null", "cast"}:
            for fid in columns:
                mapped_values = []
                for position, (row, value) in enumerate(frame[fid].items()):
                    if position % 1000 == 0:
                        _cancelled(cancel)
                    mapped = value
                    if kind == "trim" and isinstance(value, str):
                        mapped = value.strip()
                    elif kind == "case" and isinstance(value, str):
                        mapped = getattr(value, rule["mode"])()
                    elif kind == "missing" and (
                        is_missing(value)
                        or any(typed_equal(value, m) for m in rule["markers"])
                    ):
                        mapped = None
                    elif kind == "fill_null" and is_missing(value):
                        mapped = rule["value"]
                    elif kind == "cast":
                        try:
                            mapped = cast_scalar(
                                value,
                                rule["dtype"],
                                rule.get("dateFormat"),
                                rule.get("precision"),
                            )
                        except ValueError as exc:
                            violations.append((row, fid, str(exc)))
                            mapped = value
                    budget.replace_text(value, mapped)
                    mapped_values.append(mapped)
                candidate[fid] = pd.Series(
                    mapped_values, index=frame.index, dtype=object
                )
                if kind == "cast" and rule.get("outputFormat"):
                    export_formats[fid] = rule["outputFormat"]
        elif kind == "fill_stat":
            statistics = {}
            for fid in columns:
                valid = []
                invalid = 0
                for value in frame[fid]:
                    if is_missing(value):
                        continue
                    if rule["strategy"] == "mode":
                        valid.append(value)
                    else:
                        number = _numeric(value)
                        if number is None:
                            invalid += 1
                        else:
                            valid.append(number)
                fill = None
                leave = False
                if valid:
                    if rule["strategy"] == "mean":
                        fill = math.fsum(v / len(valid) for v in valid)
                    elif rule["strategy"] == "median":
                        sorted_values = sorted(valid)
                        middle = len(valid) // 2
                        fill = (
                            sorted_values[middle]
                            if len(valid) % 2
                            else math.fsum(
                                [
                                    sorted_values[middle - 1] / 2,
                                    sorted_values[middle] / 2,
                                ]
                            )
                        )
                    else:
                        counts = {}
                        values = {}
                        for value in valid:
                            key = json.dumps(
                                [type(value).__name__, json_value(value)],
                                ensure_ascii=False,
                            )
                            counts[key] = counts.get(key, 0) + 1
                            values.setdefault(key, value)
                        winner = max(counts, key=counts.get)
                        fill = values[winner]
                elif rule["emptyPolicy"] == "fail":
                    raise InputError(
                        f"Rule {index} (fill_stat), field {original_names[fid]}: no valid values for {rule['strategy']}"
                    )
                elif rule["emptyPolicy"] == "leave":
                    leave = True
                else:
                    fill = rule["value"]
                if (
                    isinstance(fill, (int, float, Decimal))
                    and not isinstance(fill, bool)
                    and not math.isfinite(fill)
                ):
                    raise InputError(
                        f"Rule {index} (fill_stat), field {original_names[fid]}: computed fill is nonfinite"
                    )
                statistics[fid] = {
                    "sampleCount": len(valid),
                    "invalidValues": invalid,
                    "fillValue": json_value(fill),
                    "emptyPolicy": rule["emptyPolicy"],
                }
                if not leave:
                    budget.preflight_fill(int(frame[fid].isna().sum()), fill)
                    values = [fill if is_missing(v) else v for v in frame[fid]]
                    for old, new in zip(frame[fid], values):
                        budget.replace_text(old, new)
                    candidate[fid] = pd.Series(values, index=frame.index, dtype=object)
        elif kind in {"drop_null", "required"}:
            for fid in columns:
                violations.extend(
                    (row, fid, "missing required value")
                    for row, value in frame[fid].items()
                    if is_missing(value)
                )
        elif kind == "deduplicate":
            duplicates = frame.duplicated(
                subset=columns, keep=rule.get("keep", "first")
            )
            label = ",".join(original_names[c] for c in columns)
            violations.extend(
                (row, label, "duplicate key") for row in frame.index[duplicates]
            )
        elif kind in {"range", "iqr"}:
            fid = columns[0]
            low = rule.get("min")
            high = rule.get("max")
            if kind == "iqr":
                values = [_numeric(v) for v in frame[fid] if not is_missing(v)]
                valid = [v for v in values if v is not None]
                statistics = {
                    "sampleCount": len(valid),
                    "invalidValues": len(values) - len(valid),
                    "factor": rule.get("factor", 1.5),
                }
                if not valid:
                    if rule["emptyPolicy"] == "fail":
                        raise InputError(
                            f"Rule {index} (iqr), field {original_names[fid]}: no valid numeric values"
                        )
                    statistics.update(
                        {"q1": None, "q3": None, "lower": None, "upper": None}
                    )
                else:
                    q1, q3 = pd.Series(valid).quantile([0.25, 0.75]).tolist()
                    width = q3 - q1
                    low = q1 - rule.get("factor", 1.5) * width
                    high = q3 + rule.get("factor", 1.5) * width
                    if not all(math.isfinite(v) for v in (q1, q3, low, high)):
                        raise InputError(
                            f"Rule {index} (iqr): thresholds exceed finite numeric range"
                        )
                    statistics.update({"q1": q1, "q3": q3, "lower": low, "upper": high})
            if kind != "iqr" or statistics["sampleCount"]:
                for row, value in frame[fid].items():
                    if is_missing(value):
                        continue
                    number = _exact_numeric(value)
                    lower = Decimal(str(low)) if low is not None else None
                    upper = Decimal(str(high)) if high is not None else None
                    invalid = (
                        number is None
                        or (
                            low is not None
                            and (
                                number < lower
                                if rule.get("inclusiveMin", True)
                                else number <= lower
                            )
                        )
                        or (
                            high is not None
                            and (
                                number > upper
                                if rule.get("inclusiveMax", True)
                                else number >= upper
                            )
                        )
                    )
                    if invalid:
                        violations.append(
                            (
                                row,
                                fid,
                                "nonnumeric value or value outside configured bounds",
                            )
                        )
        elif kind in {"enum", "length", "regex", "validate_type", "compare"}:
            compiled = regex.compile(rule["pattern"]) if kind == "regex" else None
            started = time.monotonic()
            for fid in columns:
                for position, (row, value) in enumerate(frame[fid].items()):
                    if position % 1000 == 0:
                        _cancelled(cancel)
                        if kind == "regex" and time.monotonic() - started > 10:
                            raise InputError(
                                f"Rule {index}: regex exceeded the 10-second validation budget"
                            )
                    if is_missing(value):
                        continue
                    reason = "validation did not match"
                    valid = False
                    if kind == "enum":
                        valid = any(typed_equal(value, v) for v in rule["values"])
                        reason = "value is not in the allowed enumeration"
                    elif kind == "length":
                        valid = (
                            isinstance(value, str)
                            and ("min" not in rule or len(value) >= rule["min"])
                            and ("max" not in rule or len(value) <= rule["max"])
                        )
                        reason = "string length outside bounds or nonstring value"
                    elif kind == "regex":
                        try:
                            valid = (
                                compiled.fullmatch(str(value), timeout=0.01) is not None
                            )
                        except TimeoutError as exc:
                            raise InputError(
                                f"Rule {index}: regex evaluation timed out"
                            ) from exc
                        reason = "regular expression did not match"
                    elif kind == "validate_type":
                        try:
                            cast_scalar(value, rule["dtype"], rule.get("dateFormat"))
                            valid = True
                        except ValueError as exc:
                            reason = str(exc)
                    elif kind == "compare":
                        valid = _comparison(value, rule)
                        reason = f"comparison {rule['operator']} failed or types are incompatible"
                    if not valid:
                        violations.append((row, fid, reason))
        elif kind == "select_fields":
            candidate = candidate[columns]
        elif kind == "drop_fields":
            if len(columns) >= len(frame.columns):
                raise InputError(f"Rule {index}: cannot drop every field")
            candidate = candidate.drop(columns=columns)
        elif kind == "rename":
            fid = columns[0]
            target = rule["target"]
            if any(
                other != fid and display_names[other] == target
                for other in candidate.columns
            ):
                raise InputError(f"Rule {index}: rename target already exists")
            display_names[fid] = target

        bad_rows = {row for row, _, _ in violations}
        for row, fid, reason in violations:
            add_issue(row, fid, index, kind, reason, policy)
        dropped = bad_rows if policy == "drop" else set()
        if policy == "null":
            for row, fid, _ in violations:
                candidate.at[row, fid] = None
        kept_index = candidate.index.difference(list(dropped), sort=False)
        rule_missing_filled = 0
        rule_type_failed = 0
        if sequential:
            if kind in {"fill_null", "fill_stat"}:
                rule_missing_filled = sum(
                    is_missing(before) and not is_missing(after)
                    for fid in columns
                    for before, after in zip(
                        frame.loc[kept_index, fid], candidate.loc[kept_index, fid]
                    )
                )
                missing_filled_cells += int(rule_missing_filled)
            if kind == "cast":
                rule_type_failed = len(violations)
                type_conversion_failed_cells += rule_type_failed
            if kind in {"range", "iqr"}:
                outlier_handled.update(bad_rows)
            for row in dropped:
                if row in removed_by_row:
                    raise InputError("Internal error: a source record was removed more than once")
                removed_by_row[row] = rule["id"]
            if dropped:
                final_removal_by_rule[rule["id"]] = len(dropped)
            if kind == "deduplicate":
                duplicate_removed.update(dropped)
        changed = 0
        if (
            kind in {"trim", "case", "missing", "fill_null", "fill_stat", "cast"}
            or policy == "null"
        ):
            for fid in candidate.columns:
                if fid in frame.columns:
                    changed += sum(
                        _different(a, b)
                        for a, b in zip(
                            frame.loc[kept_index, fid], candidate.loc[kept_index, fid]
                        )
                    )
        check_frame(candidate)
        if engine_version == "v2-phased" and phase == 50:
            deferred_drop.update(dropped)
            frame = candidate
        else:
            frame = candidate.loc[kept_index].copy()
        if kind == "cast":
            for fid in columns:
                if policy == "mark" and any(field == fid for _, field, _ in violations):
                    # Native nullable-array construction may accept values our
                    # converter rejected (e.g. 'NaN' -> NA or 'inf' -> infinity).
                    # A marked invalid cell must retain its original value.
                    continue
                try:
                    if rule["dtype"] == "integer":
                        frame[fid] = pd.array(frame[fid], dtype="Int64")
                    elif rule["dtype"] == "boolean":
                        frame[fid] = pd.array(frame[fid], dtype="boolean")
                    elif rule["dtype"] == "number" and "precision" not in rule:
                        frame[fid] = pd.array(frame[fid], dtype="Float64")
                    elif rule["dtype"] == "datetime":
                        frame[fid] = pd.to_datetime(frame[fid], utc=True)
                except (ValueError, TypeError):
                    # Mark retains original invalid values; no silent coercion.
                    pass
        check_frame(frame)
        changed = int(changed)
        changed_total = int(changed_total + changed)
        summary = {
            "index": index,
            "executionOrder": order,
            "type": kind,
            "inputRows": int(input_rows),
            "outputRows": int(input_rows - len(dropped)),
            "rejectedRows": int(len(dropped)),
            "changedCells": int(changed),
            "issueCount": int(len(violations)),
            "flaggedRows": int(len(bad_rows)) if policy in {"mark", "null"} else 0,
        }
        if engine_version == "v2-phased":
            summary["phase"] = phase
        else:
            summary["ruleId"] = rule["id"]
            summary["status"] = "COMPLETED"
            summary.update({
                "removedRows": int(len(dropped)),
                "markedRows": int(len(bad_rows)) if policy == "mark" else 0,
                "nulledCells": int(len(violations)) if policy == "null" else 0,
                "missingFilledCells": int(rule_missing_filled),
                "typeConversionFailedCells": int(rule_type_failed),
                "duplicateRemovedRows": int(len(dropped)) if kind == "deduplicate" else 0,
                "outlierHandledRows": int(len(bad_rows)) if kind in {"range", "iqr"} else 0,
            })
        if statistics is not None:
            summary["statistics"] = json_value(statistics)
        summaries.append(summary)
        progress(
            15 + int(order / max(1, len(ordered)) * 60),
            f"Completed rule {index} ({order}/{len(ordered)}): {kind}",
        )
    if deferred_drop:
        frame = frame.drop(index=list(deferred_drop))
    for index, rule, _ in disabled:
        summaries.append(
            {
                "index": index,
                "ruleId": rule["id"],
                "executionOrder": None,
                "type": rule["type"],
                "status": "DISABLED",
                "inputRows": 0,
                "outputRows": 0,
                "rejectedRows": 0,
                "changedCells": 0,
                "issueCount": 0,
                "flaggedRows": 0,
                "removedRows": 0,
                "markedRows": 0,
                "nulledCells": 0,
                "missingFilledCells": 0,
                "typeConversionFailedCells": 0,
                "duplicateRemovedRows": 0,
                "outlierHandledRows": 0,
            }
        )
    if engine_version == "v3-sequential":
        summaries.sort(key=lambda summary: summary["index"])
    _cancelled(cancel)
    surviving_ids = list(frame.columns)
    for fid, fmt in export_formats.items():
        if fid in frame.columns:
            frame[fid] = frame[fid].map(
                lambda value: (
                    value.strftime(fmt)
                    if isinstance(value, pd.Timestamp) and not is_missing(value)
                    else value
                )
            )
    frame = frame.rename(columns=display_names)
    after_profile = profile(frame, surviving_ids)
    if marked:
        frame["_quality_errors"] = [
            json.dumps(marked.get(row, []), ensure_ascii=False, separators=(",", ":"))
            for row in frame.index
        ]
        frame["_quality_valid"] = [row not in marked for row in frame.index]
    dirty_records = []
    for row, fid, index, code, reason in sorted(
        issues, key=lambda issue: (issue[0], issue[2], str(issue[1]))
    ):
        record = original.loc[row].to_dict()
        record.update(
            {
                "_record_id": str(row + 1),
                "_source_row": row + 1,
                "_field": original_names.get(fid, fid),
                "_rule_index": index,
                "_error_code": code,
                "_reason": reason,
            }
        )
        if sequential:
            trace = issue_traces[(row, fid, index)]
            if row in removed_by_row:
                final_action = (
                    "REMOVED_BY_THIS_RULE"
                    if removed_by_row[row] == trace["_rule_id"]
                    else "REMOVED_BY_LATER_RULE"
                )
            else:
                # Later rules can fill a null again; finalAction describes the
                # record's disposition, while _action preserves the rule event.
                final_action = "OUTPUT_FLAGGED"
            record.update(trace | {"_final_action": final_action})
        dirty_records.append(record)
    dirty = pd.DataFrame(dirty_records, columns=list(original.columns) + dirty_meta)
    check_frame(frame)
    check_frame(dirty)
    surviving = set(frame.index)
    v3_report = {}
    if sequential:
        if len(removed_by_row) != len(original) - len(frame):
            raise InputError("Internal error: final removal counts do not reconcile")
        v3_report = {
            "reportSchemaVersion": 3,
            "validOutputRows": int(len(surviving - issue_records)),
            "flaggedOutputRows": int(len(surviving & issue_records)),
            "finalRemovedRows": int(len(removed_by_row)),
            "duplicateRemovedRows": int(len(duplicate_removed)),
            "outlierHandledRows": int(len(outlier_handled)),
            "missingFilledCells": int(missing_filled_cells),
            "typeConversionFailedCells": int(type_conversion_failed_cells),
            "issueRows": int(len(issue_records)),
            "finalRemovalByRule": {
                key: int(value) for key, value in final_removal_by_rule.items()
            },
        }
    return CleanResult(
        frame,
        dirty,
        {
            "taskId": task_id,
            "attempt": attempt,
            "engineVersion": engine_version,
            **({"attemptId": attempt_id} if attempt_id else {}),
            "inputRows": int(len(original)),
            "outputRows": int(len(frame)),
            "removedRows": int(len(original) - len(frame)),
            "dirtyRows": int(len(issue_records)),
            "flaggedRows": int(len(issue_records & surviving)),
            "issueCount": int(len(issues)),
            "changedCells": int(changed_total),
            "durationSeconds": round(time.monotonic() - start, 3),
            "rules": summaries,
            "executionOrder": [
                rule["id"] if engine_version == "v3-sequential" else index
                for index, rule, _ in ordered
            ],
            "ruleSnapshot": rules,
            "columnsBefore": before_profile,
            "columnsAfter": after_profile,
            "warnings": [],
            **v3_report,
        },
    )


def execute_task(
    message: TaskMessage,
    storage: ObjectStorage,
    directory: Path,
    cancel: Event,
    progress: Callable[[int, str], None],
) -> tuple[dict[str, Any], dict[str, str]]:
    if message.engineVersion == "v3-disk":
        from .disk import execute_disk_task
        return execute_disk_task(message, storage, directory, cancel, progress)
    start = time.monotonic()
    _cancelled(cancel)
    validate_rules(message.rules, message.engineVersion)
    validate_limits(message.limits)
    progress(5, "Downloading original file")
    source = directory / f"input.{message.input.format}"
    storage.download_input(message.input.objectKey, source)
    _cancelled(cancel)
    progress(10, "Reading and profiling input")
    original, _, warnings = read_table(
        source,
        message.input.format,
        parse_options=message.input.parseOptions,
        limits=message.limits,
    )
    result = clean_frame(
        original,
        message.rules,
        str(message.taskId),
        message.attempt,
        cancel,
        progress,
        message.input.fields,
        message.engineVersion,
        str(message.attemptId) if message.attemptId else None,
        str(message.input.fileId) if message.input.fileId else None,
        (
            original.attrs["parseOptions"].get("sheetName")
            if message.input.format == "xlsx"
            else None
        ),
    )
    result.report["parseOptions"] = original.attrs["parseOptions"]
    result.report["limitsSnapshot"] = message.limits
    result.report["warnings"].extend(warnings)
    _cancelled(cancel)
    progress(80, "Writing cleaned data and rejected rows")
    output = directory / f"result.{message.outputFormat}"
    dirty = directory / "dirty.csv"
    escaped = write_table(result.data, output, message.outputFormat)
    dirty_escaped = write_table(result.dirty, dirty, "csv")
    if escaped or dirty_escaped:
        result.report["warnings"].append(
            f"Spreadsheet formula protection escaped {escaped} result cells and {dirty_escaped} dirty-data cells with a leading apostrophe"
        )
    _cancelled(cancel)
    task_id, attempt = str(message.taskId), message.attempt
    artifacts = {
        "result": f"results/{task_id}/{attempt}/result.{message.outputFormat}",
        "dirty": f"dirty/{task_id}/{attempt}/dirty.csv",
        "report": f"reports/{task_id}/{attempt}/report.json",
    }
    content_types = {
        "csv": "text/csv; charset=utf-8",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "parquet": "application/vnd.apache.parquet",
    }
    progress(90, "Uploading results")
    storage.upload(artifacts["result"], output, content_types[message.outputFormat])
    _cancelled(cancel)
    storage.upload(artifacts["dirty"], dirty, content_types["csv"])
    result.report["durationSeconds"] = round(time.monotonic() - start, 3)
    report_path = directory / "report.json"
    report_path.write_text(
        json.dumps(result.report, ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    storage.upload(artifacts["report"], report_path, "application/json")
    _cancelled(cancel)
    progress(98, "Artifacts uploaded; committing report")
    return result.report, artifacts
