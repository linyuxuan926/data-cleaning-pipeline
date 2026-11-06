"""JSON Schema 2020-12 contracts for all schema-three rules."""
from copy import deepcopy
from .bindings import OPTIONS


def rule_schemas():
    fid = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"}
    scalar = {"oneOf": [
        {"type": "object", "additionalProperties": False, "required": ["type", "value"], "properties": {"type": {"const": "null"}, "value": {"type": "null"}}},
        *[{"type": "object", "additionalProperties": False, "required": ["type", "value"], "properties": {"type": {"const": kind}, "value": {"type": raw, **({"maxLength": 10000} if kind == "string" else {})}}} for kind, raw in [("string", "string"), ("integer", "integer"), ("boolean", "boolean"), ("datetime", "string")]],
        {"type": "object", "additionalProperties": False, "required": ["type", "value"], "properties": {"type": {"const": "decimal"}, "value": {"type": "string", "pattern": "^[+-]?[0-9]+(?:\\.[0-9]+)?$"}, "precision": {"type": "integer", "minimum": 1, "maximum": 38}, "scale": {"type": "integer", "minimum": 0, "maximum": 12}}}
    ]}
    scalar["oneOf"][2]["properties"]["value"] = {"oneOf": [{"type":"integer"},{"type":"string","pattern":"^[+-]?[0-9]+$"}]}
    props = {
        "id": {"type": "string", "minLength": 1, "maxLength": 128}, "enabled": {"type": "boolean"},
        "fieldIds": {"type": "array", "items": fid, "minItems": 1, "maxItems": 200, "uniqueItems": True},
        "fieldId": fid, "leftFieldId": fid, "rightFieldId": fid,
        "dtype": {"enum": ["string", "integer", "decimal", "datetime", "boolean"]},
        "valueType": {"enum": ["string", "integer", "decimal", "datetime", "boolean"]},
        "precision": {"type": "integer", "minimum": 1, "maximum": 38},
        "scale": {"type": "integer", "minimum": 0, "maximum": 12}, "rounding": {"const": "HALF_UP"},
        "dateFormat": {"type": "string", "minLength": 1, "maxLength": 128}, "outputFormat": {"type": "string", "minLength": 1, "maxLength": 128},
        "onError": {"enum": ["drop", "mark", "fail"]}, "value": scalar,
        "markers": {"type": "array", "items": scalar, "minItems": 1, "maxItems": 100},
        "values": {"type": "array", "items": scalar, "minItems": 1, "maxItems": 1000},
        "strategy": {"enum": ["mean", "median", "mode"]}, "emptyPolicy": {"enum": ["fail", "leave", "constant"]},
        "invalidPolicy": {"const": "exclude"}, "min": {"type": ["string", "number"]}, "max": {"type": ["string", "number"]},
        "inclusiveMin": {"type": "boolean"}, "inclusiveMax": {"type": "boolean"},
        "factor": {"type": ["string", "number"]}, "pattern": {"type": "string", "minLength": 1, "maxLength": 256},
        "operator": {"enum": ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "starts_with", "ends_with"]},
        "keep": {"enum": ["first", "last"]}, "nullsEqual": {"type": "boolean"},
        "target": {"type": "string", "minLength": 1, "maxLength": 128},
        "search": {"type": "string", "minLength": 1, "maxLength": 10000},
        "replacement": {"type": "string", "maxLength": 10000}, "count": {"type": "integer", "minimum": 0, "maximum": 100000},
        "mode": {"enum": ["lower", "upper"]}, "nullPolicy": {"enum": ["skip", "violation"]},
    }
    rules = {}
    for kind, (required, optional) in OPTIONS.items():
        keys = required | optional | {"id", "type", "enabled"}
        properties = {key: deepcopy(props[key]) for key in keys if key != "type"}
        properties["type"] = {"const": kind}
        if kind in {"cast", "range", "iqr"}: properties["onError"] = {"enum": ["drop", "mark", "null", "fail"]}
        if kind == "deduplicate": properties["fieldIds"]["minItems"] = 0
        if kind == "replace": properties["mode"] = {"const": "literal"}
        if kind == "compare_fields": properties["operator"] = {"enum": ["eq", "ne", "gt", "gte", "lt", "lte"]}
        if kind in {"range", "length"}:
            properties.setdefault("min", {}); properties.setdefault("max", {})
            if kind == "length":
                for key in ("min", "max"): properties[key] = {"type": "integer", "minimum": 0, "maximum": 1000000}
        if kind == "iqr": properties["emptyPolicy"] = {"enum": ["fail", "leave"]}
        definition = {"type": "object", "additionalProperties": False, "required": sorted(required | {"id", "type", "enabled"}), "properties": properties}
        if kind in {"range", "length"}: definition["anyOf"] = [{"required": ["min"]}, {"required": ["max"]}]
        rules[kind] = definition
    return {"schemaVersion": 3, "engineVersion": "v3-disk", "ruleCount": len(rules), "rules": rules,
            "jsonSchema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "array", "maxItems": 50, "items": {"oneOf": [{"$ref": f"#/$defs/{kind}"} for kind in rules]}, "$defs": rules}}
