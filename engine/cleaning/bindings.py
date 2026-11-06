"""Strict version-three rule contract and immutable logical-field preflight."""
from copy import deepcopy
from datetime import datetime, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import operator
import re

import numpy as np
import pandas as pd

from .models import InputError, RESERVED_COLUMNS, _format
from .types import cast_scalar, missing

DISK_TYPES = {"string", "integer", "decimal", "datetime", "boolean"}
META = RESERVED_COLUMNS | {"_file_id", "_sheet", "_rule_id", "_before_value", "_action", "_final_action", "_source_record_number", "_removed_by_rule_id", "_attempt_id"}
MULTI = {"trim", "case", "cast", "validate_type", "missing", "fill_null", "fill_stat", "drop_null", "required", "select_fields", "drop_fields", "replace"}
SINGLE = {"range", "iqr", "enum", "length", "regex", "compare", "rename"}
OPTIONS = {
    "trim": ({"fieldIds"}, set()), "case": ({"fieldIds", "mode"}, set()),
    "cast": ({"fieldIds", "dtype"}, {"precision", "scale", "rounding", "dateFormat", "outputFormat", "onError"}),
    "validate_type": ({"fieldIds", "dtype"}, {"precision", "scale", "rounding", "dateFormat", "onError"}),
    "missing": ({"fieldIds", "markers"}, set()), "fill_null": ({"fieldIds", "value"}, set()),
    "fill_stat": ({"fieldIds", "strategy", "emptyPolicy"}, {"value", "precision", "scale", "rounding", "invalidPolicy"}),
    "drop_null": ({"fieldIds"}, {"onError"}), "required": ({"fieldIds"}, {"onError"}),
    "range": ({"fieldId"}, {"min", "max", "inclusiveMin", "inclusiveMax", "onError"}),
    "iqr": ({"fieldId", "emptyPolicy"}, {"factor", "onError"}),
    "enum": ({"fieldId", "values"}, {"onError"}), "length": ({"fieldId"}, {"min", "max", "onError"}),
    "regex": ({"fieldId", "pattern"}, {"onError"}), "compare": ({"fieldId", "operator", "value"}, {"onError"}),
    "deduplicate": (set(), {"fieldIds", "keep", "nullsEqual", "onError"}),
    "select_fields": ({"fieldIds"}, set()), "drop_fields": ({"fieldIds"}, set()), "rename": ({"fieldId", "target"}, set()),
    "replace": ({"fieldIds", "search", "replacement"}, {"mode", "count"}),
    "compare_fields": ({"leftFieldId", "rightFieldId", "operator", "valueType", "nullPolicy"}, {"onError"}),
}
OPS = {"eq": operator.eq, "ne": operator.ne, "gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}


def decimal_value(value, precision=38, scale=None):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("boolean is not decimal")
    try:
        number = Decimal(str(value).strip())
        if not number.is_finite() or number.adjusted() > 38 or number.adjusted() < -10000:
            raise ValueError("decimal is nonfinite or exceeds precision domain")
        with localcontext() as context:
            context.prec = 100
            if scale is not None:
                number = number.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
            digits = max(0, number.adjusted() + 1) + (scale if scale is not None else max(0, -number.as_tuple().exponent))
            digits = max(1, digits)
            if digits > precision or (scale is None and max(0, -number.as_tuple().exponent) > 12):
                raise ValueError("decimal exceeds declared precision/scale")
        return number
    except (InvalidOperation, OverflowError, TypeError) as exc:
        raise ValueError("expected finite decimal") from exc


def cast_v3(value, dtype, rule=None):
    rule = rule or {}
    if missing(value):
        return None
    if dtype == "decimal":
        return decimal_value(value, rule.get("precision", 38), rule.get("scale", 2))
    if dtype == "datetime":
        fmt = rule.get("dateFormat", "iso8601")
        if fmt == "iso8601":
            if isinstance(value, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?)?", value):
                raise ValueError("expected ISO-8601 date or timestamp")
            if not isinstance(value, (str, date, datetime, pd.Timestamp)):
                raise ValueError("expected ISO-8601 date or timestamp")
            return cast_scalar(value, dtype)
        return cast_scalar(value, dtype, fmt)
    return cast_scalar(value, dtype)


def typed_scalar(value, nullable=True):
    if not isinstance(value, dict) or set(value) - {"type", "value", "scale", "precision"} or not {"type", "value"} <= value.keys():
        raise InputError("Expected typed scalar {type,value}")
    dtype, raw = value["type"], value["value"]
    if dtype == "null" and raw is None and nullable:
        return None
    if not isinstance(dtype, str) or dtype not in DISK_TYPES or raw is None:
        raise InputError("Invalid typed scalar type/value")
    if dtype == "string" and (not isinstance(raw, str) or len(raw) > 10000):
        raise InputError("Typed string must contain at most 10000 characters")
    if dtype == "boolean" and type(raw) is not bool:
        raise InputError("Typed boolean must be a JSON boolean")
    if dtype == "integer" and not (type(raw) is int or isinstance(raw,str) and re.fullmatch(r"[+-]?[0-9]+",raw)):
        raise InputError("Typed integer requires an exact JSON integer or integer string")
    if dtype == "decimal" and (not isinstance(raw, str) or type(value.get("scale", 2)) is not int or not 0 <= value.get("scale", 2) <= 12):
        raise InputError("Typed decimal requires exact text and scale 0..12")
    if dtype == "decimal" and (type(value.get("precision",38)) is not int or not 1 <= value.get("precision",38) <= 38 or value.get("scale",2) > value.get("precision",38)):
        raise InputError("Typed decimal precision must be 1..38 and >= scale")
    if dtype != "decimal" and any(key in value for key in ("scale","precision")):
        raise InputError("Typed precision/scale require decimal")
    try:
        return cast_v3(raw, dtype, value)
    except ValueError as exc:
        raise InputError(str(exc)) from exc


def typed(value):
    if missing(value):
        return {"type": "null", "value": None}
    if isinstance(value, (bool, np.bool_)):
        return {"type": "boolean", "value": bool(value)}
    if isinstance(value, (int, np.integer)):
        return {"type": "integer", "value": int(value)}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value), "scale": max(0, -value.as_tuple().exponent)}
    if isinstance(value, (float, np.floating)):
        return {"type": "decimal", "value": str(value), "scale": max(0, -Decimal(str(value)).as_tuple().exponent)}
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return {"type": "datetime", "value": value.isoformat()}
    return {"type": "string", "value": str(value)}


def numeric(value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, Decimal, np.number)):
        return None
    try:
        return decimal_value(value)
    except ValueError:
        return None


def canonical_key(value):
    """Typed equality: exact numbers share a key, booleans/strings stay distinct."""
    import json
    if missing(value):
        return "null"
    number = numeric(value)
    if number is not None:
        # normalize() uses the active Decimal context and can round 38-digit values.
        exact = format(number, "f")
        if "." in exact: exact = exact.rstrip("0").rstrip(".")
        return "numeric:" + ("0" if number == 0 else exact)
    return json.dumps(typed(value), ensure_ascii=False, separators=(",", ":"))


def normalize_rules(rules):
    if not isinstance(rules, list) or len(rules) > 50:
        raise InputError("Rules must contain at most 50 entries")
    normalized, seen = [], set()
    for index, source in enumerate(rules, 1):
        prefix = f"Rule {index}: "
        def bad(message):
            raise InputError(prefix + message)
        if not isinstance(source, dict) or not isinstance(source.get("type"), str) or source["type"] not in OPTIONS:
            bad("unsupported v3-disk rule type")
        rule = deepcopy(source)
        if not isinstance(rule.get("id"), str) or not 1 <= len(rule["id"]) <= 128 or rule["id"] in seen or type(rule.get("enabled")) is not bool:
            bad("unique id and boolean enabled are required")
        seen.add(rule["id"])
        kind = rule["type"]
        required, optional = OPTIONS[kind]
        if not required <= rule.keys() or set(rule) - required - optional - {"id", "type", "enabled"}:
            bad("missing or unsupported v3-disk options")
        refs = rule.get("fieldIds", [])
        if "fieldIds" in rule and (not isinstance(refs, list) or len(refs) > 200 or (not refs and kind != "deduplicate") or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", v) for v in refs) or len(set(refs)) != len(refs)):
            bad("fieldIds must be distinct logical field IDs")
        for key in ("fieldId", "leftFieldId", "rightFieldId"):
            if key in rule and (not isinstance(rule[key], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", rule[key])):
                bad("invalid logical field ID")
        if "onError" in optional:
            rule.setdefault("onError", "drop")
            if not isinstance(rule["onError"], str) or rule["onError"] not in ({"drop", "mark", "null", "fail"} if kind in {"cast", "range", "iqr"} else {"drop", "mark", "fail"}):
                bad("unsupported error action")
        if kind == "case" and rule["mode"] not in ("lower", "upper"):
            bad("mode must be lower or upper")
        if kind in {"cast", "validate_type"}:
            if not isinstance(rule["dtype"], str) or rule["dtype"] not in DISK_TYPES:
                bad("dtype must be string/integer/decimal/datetime/boolean")
            if rule["dtype"] == "decimal":
                rule.setdefault("precision", 38); rule.setdefault("scale", 2); rule.setdefault("rounding", "HALF_UP")
            elif any(key in rule for key in ("precision", "scale", "rounding")):
                bad("precision/scale/rounding require decimal")
            for key in ("dateFormat", "outputFormat"):
                if key in rule:
                    if rule["dtype"] != "datetime": bad(key + " requires datetime")
                    if rule[key] != "iso8601": _format(rule[key], key)
        if kind == "fill_stat":
            if rule["strategy"] not in ("mean", "median", "mode") or rule["emptyPolicy"] not in ("fail", "leave", "constant"):
                bad("invalid strategy/emptyPolicy")
            rule.setdefault("invalidPolicy", "exclude")
            if rule["invalidPolicy"] != "exclude": bad("invalidPolicy must be exclude")
            if rule["strategy"] != "mode":
                rule.setdefault("precision", 38); rule.setdefault("scale", 2); rule.setdefault("rounding", "HALF_UP")
            if (rule["emptyPolicy"] == "constant") != ("value" in rule): bad("constant alone requires value")
        if any(k in rule for k in ("precision", "scale", "rounding")):
            if type(rule.get("precision", 38)) is not int or not 1 <= rule.get("precision", 38) <= 38 or type(rule.get("scale", 2)) is not int or not 0 <= rule.get("scale", 2) <= 12 or rule.get("scale", 2) > rule.get("precision", 38) or rule.get("rounding", "HALF_UP") != "HALF_UP":
                bad("decimal requires precision 1..38, scale 0..12 <= precision, HALF_UP")
        if kind == "fill_null" or (kind == "fill_stat" and rule["emptyPolicy"] == "constant"):
            typed_scalar(rule["value"], False)
            if kind == "fill_stat" and rule["strategy"] in {"mean","median"} and rule["value"]["type"] not in {"integer","decimal"}:
                bad("numeric fill constant must be integer or decimal")
        for key in ("markers", "values"):
            if key in rule:
                if not isinstance(rule[key], list) or not 1 <= len(rule[key]) <= (100 if key == "markers" else 1000): bad("invalid " + key)
                for value in rule[key]: typed_scalar(value)
        if kind == "compare":
            typed_scalar(rule["value"], False)
            if not isinstance(rule["operator"], str) or rule["operator"] not in set(OPS) | {"contains", "starts_with", "ends_with"}: bad("invalid operator")
        if kind == "compare_fields":
            if not isinstance(rule["operator"], str) or rule["operator"] not in OPS or not isinstance(rule["valueType"], str) or rule["valueType"] not in DISK_TYPES or rule["nullPolicy"] not in ("skip", "violation"): bad("invalid cross-field comparison")
            if rule["valueType"] == "boolean" and rule["operator"] not in {"eq", "ne"}: bad("boolean comparison only supports eq/ne")
        if kind in {"range", "length"}:
            if "min" not in rule and "max" not in rule: bad("at least one bound required")
            bounds = []
            for key in ("min", "max"):
                if key in rule:
                    if kind == "length":
                        if type(rule[key]) is not int or not 0 <= rule[key] <= 1000000: bad("length bounds must be integers 0..1000000")
                        bounds.append(rule[key])
                    else:
                        try: bounds.append(decimal_value(rule[key]))
                        except ValueError as exc: bad(str(exc))
            if len(bounds) == 2 and bounds[0] > bounds[1]: bad("min exceeds max")
            if kind == "range":
                for key in ("inclusiveMin", "inclusiveMax"):
                    rule.setdefault(key, True)
                    if type(rule[key]) is not bool: bad("inclusive bounds must be boolean")
        if kind == "iqr":
            rule.setdefault("factor", "1.5")
            try: factor = decimal_value(rule["factor"])
            except ValueError as exc: bad(str(exc))
            if not 0 <= factor <= 100 or rule["emptyPolicy"] not in ("fail", "leave"): bad("invalid IQR configuration")
        if kind == "deduplicate":
            rule.setdefault("keep", "first"); rule.setdefault("nullsEqual", True)
            if rule["keep"] not in ("first", "last") or type(rule["nullsEqual"]) is not bool: bad("invalid deduplicate options")
        if kind == "regex":
            # Reuse the existing safe-regex validator, not its execution ordering.
            from .models import validate_rules
            validate_rules([{"type": "regex", "column": "f0", "pattern": rule["pattern"]}])
        if kind == "replace":
            rule.setdefault("mode", "literal"); rule.setdefault("count", 0)
            if rule["mode"] != "literal" or not isinstance(rule["search"], str) or not 1 <= len(rule["search"]) <= 10000 or not isinstance(rule["replacement"], str) or len(rule["replacement"]) > 10000 or type(rule["count"]) is not int or not 0 <= rule["count"] <= 100000: bad("invalid literal replace parameters")
        if kind == "rename" and (not isinstance(rule["target"], str) or not 1 <= len(rule["target"]) <= 128 or rule["target"] in META): bad("invalid or reserved target name")
        normalized.append(rule)
    return normalized


def logical_definitions(logical_fields):
    if not isinstance(logical_fields, list) or not 1 <= len(logical_fields) <= 200:
        raise InputError("logicalFields must contain 1..200 definitions")
    declared = {}
    for field in logical_fields:
        if not isinstance(field, dict) or set(field) - {"id", "label"} or not isinstance(field.get("id"), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field["id"]) or field["id"] in declared or not isinstance(field.get("label"), str) or not 1 <= len(field["label"]) <= 128:
            raise InputError("logicalFields require unique id and nonempty label")
        declared[field["id"]] = field
    return declared


def validate_rule_set(logical_fields, rules, schema_version=3):
    if schema_version != 3: raise InputError("v3-disk requires schemaVersion=3")
    declared = logical_definitions(logical_fields)
    normalized = normalize_rules(rules)
    for rule in normalized:
        refs = rule.get("fieldIds", [rule["fieldId"]] if "fieldId" in rule else [rule["leftFieldId"], rule["rightFieldId"]] if rule["type"] == "compare_fields" else [])
        if any(fid not in declared for fid in refs): raise InputError(f"Rule {rule['id']}: undeclared logical field")
    return normalized


def preflight(fields, logical_fields, field_bindings, rules, schema_version=3):
    normalized = validate_rule_set(logical_fields, rules, schema_version)
    declared = logical_definitions(logical_fields)
    if not isinstance(fields, list) or not 1 <= len(fields) <= 200 or any(not isinstance(f, dict) or f.get("id") != f"f{i}" or not isinstance(f.get("name"), str) or not 1 <= len(f["name"]) <= 128 or f["name"] in META for i, f in enumerate(fields)) or len({f["name"] for f in fields}) != len(fields):
        raise InputError("Input fields must match a unique ordered fN/name parse snapshot")
    if not isinstance(field_bindings, dict) or set(field_bindings) - set(declared) or any(not isinstance(value, str) or value not in {f["id"] for f in fields} for value in field_bindings.values()) or len(set(field_bindings.values())) != len(field_bindings):
        raise InputError("fieldBindings must be an explicit one-to-one logical ID -> snapshot fN map")
    current = [f["id"] for f in fields]
    names = {f["id"]: f["name"] for f in fields}
    types = {f["id"]: f.get("logicalType", "unknown") for f in fields}
    if any(not isinstance(dtype,str) or dtype not in DISK_TYPES | {"unknown","mixed"} for dtype in types.values()):
        raise InputError("Input logicalType is unsupported")
    for index, rule in enumerate(normalized, 1):
        refs = rule.get("fieldIds", [rule["fieldId"]] if "fieldId" in rule else [rule["leftFieldId"], rule["rightFieldId"]] if rule["type"] == "compare_fields" else [])
        for logical in refs:
            if logical not in declared or logical not in field_bindings:
                raise InputError(f"Rule {index} ({rule['id']}): unbound logical field {logical}")
            if rule["enabled"] and field_bindings[logical] not in current:
                raise InputError(f"Rule {index} ({rule['id']}): field {logical} was removed by an earlier projection")
        if not rule["enabled"]: continue
        physical = [field_bindings[value] for value in refs]
        kind = rule["type"]
        if kind == "cast":
            for fid in physical: types[fid] = rule["dtype"]
        elif kind == "compare_fields":
            expected = rule["valueType"]
            allowed = {"integer", "decimal"} if expected == "decimal" else {expected}
            if any(types[fid] not in allowed | {"unknown","mixed"} for fid in physical):
                raise InputError(f"Rule {index}: compare_fields types require an explicit compatible cast")
        elif kind == "fill_stat" and rule["strategy"] in {"mean", "median"}:
            if any(types[fid] not in {"integer", "decimal", "unknown"} for fid in physical):
                raise InputError(f"Rule {index}: numeric fill requires an explicit numeric cast first")
        elif kind == "rename":
            fid = physical[0]
            if any(other != fid and names[other] == rule["target"] for other in current): raise InputError(f"Rule {index}: rename target conflicts")
            names[fid] = rule["target"]
        elif kind == "select_fields": current = physical
        elif kind == "drop_fields":
            current = [fid for fid in current if fid not in physical]
            if not current: raise InputError(f"Rule {index}: cannot remove every field")
        elif kind == "fill_null" or (kind == "fill_stat" and rule["emptyPolicy"] == "constant"):
            value_type = rule["value"]["type"]
            for fid in physical:
                if types[fid] != "unknown" and types[fid] != value_type and not {types[fid], value_type} <= {"integer", "decimal"}: raise InputError(f"Rule {index}: fill value type is incompatible with {fid}")
                if types[fid] == "integer" and value_type == "decimal": types[fid] = "decimal"
    return {"valid": True, "schemaVersion": 3, "engineVersion": "v3-disk", "normalizedRules": normalized, "outputFields": [{"id": fid, "name": names[fid], "logicalType": types[fid]} for fid in current], "warnings": []}
