import math
import re
from typing import Any, Literal
from uuid import UUID

import regex
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORMATS = {"csv", "xlsx", "parquet"}
DTYPES = {"integer", "number", "string", "datetime", "boolean"}
RESERVED_COLUMNS = {
    "_source_row",
    "_rule_index",
    "_reason",
    "_field",
    "_error_code",
    "_record_id",
    "_quality_errors",
    "_quality_valid",
}
PHASES = {
    "trim": 10,
    "case": 10,
    "cast": 20,
    "missing": 30,
    "fill_null": 30,
    "fill_stat": 30,
    "drop_null": 30,
    "range": 40,
    "iqr": 40,
    "required": 50,
    "enum": 50,
    "length": 50,
    "regex": 50,
    "validate_type": 50,
    "compare": 50,
    "deduplicate": 60,
    "select_fields": 70,
    "drop_fields": 70,
    "rename": 70,
}
RULE_FIELDS = {
    "trim": ({"type", "columns"}, set()),
    "case": ({"type", "columns", "mode"}, set()),
    "cast": (
        {"type", "columns", "dtype"},
        {"dateFormat", "precision", "outputFormat", "onError"},
    ),
    "validate_type": ({"type", "columns", "dtype"}, {"dateFormat", "onError"}),
    "missing": ({"type", "columns", "markers"}, set()),
    "fill_null": ({"type", "columns", "value"}, set()),
    "fill_stat": ({"type", "columns", "strategy", "emptyPolicy"}, {"value"}),
    "drop_null": ({"type", "columns"}, {"onError"}),
    "required": ({"type", "columns"}, {"onError"}),
    "range": (
        {"type", "column"},
        {"min", "max", "inclusiveMin", "inclusiveMax", "onError"},
    ),
    "iqr": ({"type", "column", "emptyPolicy"}, {"factor", "onError"}),
    "enum": ({"type", "column", "values"}, {"onError"}),
    "length": ({"type", "column"}, {"min", "max", "onError"}),
    "regex": ({"type", "column", "pattern"}, {"onError"}),
    "compare": ({"type", "column", "operator", "value"}, {"onError"}),
    "deduplicate": ({"type"}, {"columns", "keep", "onError"}),
    "select_fields": ({"type", "columns"}, set()),
    "drop_fields": ({"type", "columns"}, set()),
    "rename": ({"type", "column", "target"}, set()),
}
HARD_LIMITS = {
    "maxFileBytes": 104857600,
    "maxRows": 500000,
    "maxColumns": 200,
    "maxCells": 5000000,
    "maxExcelExpandedBytes": 536870912,
    "previewRows": 1000,
}


class InputError(ValueError):
    """An input that cannot be processed safely or according to its declared schema."""


class CancelledError(Exception):
    pass


def _finite_number(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def scalar(value: Any, nullable: bool = False) -> bool:
    return (
        (nullable and value is None)
        or isinstance(value, (str, bool))
        or _finite_number(value)
    )


def column_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value not in RESERVED_COLUMNS
    )


def validate_limits(values: dict | None) -> dict[str, int]:
    result = dict(HARD_LIMITS)
    for key, value in (values or {}).items():
        if key in HARD_LIMITS:
            if type(value) is not int or not 1 <= value <= HARD_LIMITS[key]:
                raise InputError(
                    f"Limit {key} must be an integer from 1 to {HARD_LIMITS[key]}"
                )
            result[key] = value
    return result


def validate_parse_options(values: dict | None) -> dict:
    values = values or {}
    if not isinstance(values, dict) or set(values) - {
        "encoding",
        "delimiter",
        "sheetName",
        "missingValues",
        "fieldTypes",
    }:
        raise InputError("Unsupported parse options")
    result = {"encoding": "auto", "delimiter": "auto", "missingValues": [""]} | values
    if not isinstance(result["encoding"], str) or result["encoding"] not in {
        "auto",
        "utf-8-sig",
        "utf-8",
        "gb18030",
        "utf-16",
        "latin-1",
    }:
        raise InputError("Unsupported CSV encoding")
    if result["delimiter"] == "\\t":
        result["delimiter"] = "\t"
    if not isinstance(result["delimiter"], str) or result["delimiter"] not in {
        "auto",
        ",",
        ";",
        "\t",
        "|",
    }:
        raise InputError("Unsupported CSV delimiter")
    markers = result["missingValues"]
    if (
        not isinstance(markers, list)
        or len(markers) > 100
        or not all(
            scalar(v, True) and (not isinstance(v, str) or len(v) <= 10000)
            for v in markers
        )
    ):
        raise InputError("missingValues must contain at most 100 finite scalar markers")
    if "sheetName" in result and (
        not isinstance(result["sheetName"], str)
        or not 1 <= len(result["sheetName"]) <= 128
    ):
        raise InputError("Invalid worksheet name")
    types = result.get("fieldTypes", {})
    if (
        not isinstance(types, dict)
        or len(types) > 200
        or not all(
            re.fullmatch(r"f[0-9]+", k) and isinstance(v, str) and v in DTYPES
            for k, v in types.items()
        )
    ):
        raise InputError("fieldTypes must map stable field IDs to supported types")
    return result


def _format(value: Any, label: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise InputError(f"{label} must be 1..128 characters")
    # Only portable datetime directives. Reject platform extensions and unresolved directives.
    allowed = set("aAbBcdHIjmMpSUwWxXyYfzZGVu%")
    for match in re.finditer(r"%(.)", value):
        if match.group(1) not in allowed:
            raise InputError(f"Unsupported {label} directive")
    if value.endswith("%") and not value.endswith("%%"):
        raise InputError(f"Invalid {label}")


def validate_rules(
    rules: Any, engine_version: str = "v2-phased"
) -> list[dict[str, Any]]:
    if engine_version == "v3-disk":
        from .bindings import normalize_rules
        return normalize_rules(rules)
    if not isinstance(rules, list) or len(rules) > 50:
        raise InputError("Rules must contain at most 50 entries")
    if engine_version not in {"v2-phased", "v3-sequential"}:
        raise InputError("Unsupported engine version")
    if engine_version == "v3-sequential":
        seen_ids = set()
        execution_rules = []
        validation_rules = []
        for index, rule in enumerate(rules, 1):
            if not isinstance(rule, dict):
                raise InputError(f"Rule {index}: rule must be an object")
            rule = {"id": f"rule-{index}", "enabled": True, **rule}
            if (
                not isinstance(rule["id"], str)
                or not 1 <= len(rule["id"]) <= 128
                or type(rule["enabled"]) is not bool
            ):
                raise InputError(
                    f"Rule {index}: id must be 1..128 characters and enabled must be boolean"
                )
            if rule["id"] in seen_ids:
                raise InputError(f"Rule {index}: duplicate rule id")
            seen_ids.add(rule["id"])
            executable = {
                key: value
                for key, value in rule.items()
                if key not in {"id", "enabled"}
            }
            validation_rule = executable
            # V3 extends cast's error policy without widening the frozen V2 API.
            if (
                executable.get("type") == "cast"
                and executable.get("onError") == "null"
            ):
                validation_rule = executable | {"onError": "mark"}
            execution_rules.append((rule["id"], rule["enabled"], executable))
            validation_rules.append(validation_rule)
        validate_rules(validation_rules, "v2-phased")
        return [
            {"id": rule_id, "enabled": enabled, **executable}
            for rule_id, enabled, executable in execution_rules
        ]
    for index, rule in enumerate(rules, 1):
        prefix = f"Rule {index}: "

        def bad(message):
            raise InputError(prefix + message)

        if (
            not isinstance(rule, dict)
            or not isinstance(rule.get("type"), str)
            or rule["type"] not in RULE_FIELDS
        ):
            bad("unsupported rule type")
        kind = rule["type"]
        required, optional = RULE_FIELDS[kind]
        if not required <= rule.keys() or rule.keys() - required - optional:
            bad("missing or unsupported options")
        if "columns" in rule:
            cols = rule["columns"]
            if (
                not isinstance(cols, list)
                or len(cols) > 200
                or (not cols and kind != "deduplicate")
                or not all(column_name(c) for c in cols)
                or len(set(cols)) != len(cols)
            ):
                bad("columns must be distinct nonempty names")
        if "column" in rule and not column_name(rule["column"]):
            bad("invalid column")
        if "onError" in rule and (
            not isinstance(rule["onError"], str)
            or rule["onError"]
            not in (
                {"drop", "mark", "fail", "null"}
                if kind in {"range", "iqr"}
                else {"drop", "mark", "fail"}
            )
        ):
            bad("unsupported onError policy")
        if kind == "case" and rule["mode"] not in ("lower", "upper"):
            bad("mode must be lower or upper")
        if kind in {"cast", "validate_type"}:
            if not isinstance(rule["dtype"], str) or rule["dtype"] not in DTYPES:
                bad("unsupported dtype")
            for key in ("dateFormat", "outputFormat"):
                if key in rule:
                    if rule["dtype"] != "datetime":
                        bad(f"{key} requires datetime")
                    _format(rule[key], key)
            if "precision" in rule and (
                rule["dtype"] != "number"
                or type(rule["precision"]) is not int
                or not 0 <= rule["precision"] <= 12
            ):
                bad("precision requires number and integer 0..12")
        if kind == "fill_null" or (
            kind == "fill_stat" and rule.get("emptyPolicy") == "constant"
        ):
            if (
                "value" not in rule
                or not scalar(rule["value"])
                or (isinstance(rule["value"], str) and len(rule["value"]) > 10000)
            ):
                bad("value must be a non-null finite scalar, text <=10000 characters")
        if kind == "fill_stat":
            if rule["strategy"] not in ("mean", "median", "mode") or rule[
                "emptyPolicy"
            ] not in ("fail", "leave", "constant"):
                bad("invalid statistical fill strategy or empty policy")
            if "value" in rule and rule["emptyPolicy"] != "constant":
                bad("value only applies to constant empty policy")
        if kind == "missing":
            markers = rule["markers"]
            if (
                not isinstance(markers, list)
                or not 1 <= len(markers) <= 100
                or not all(
                    scalar(v, True) and (not isinstance(v, str) or len(v) <= 10000)
                    for v in markers
                )
            ):
                bad("markers must contain 1..100 finite scalars")
        if kind == "deduplicate" and rule.get("keep", "first") not in ("first", "last"):
            bad("keep must be first or last")
        if kind == "rename" and not column_name(rule["target"]):
            bad("invalid rename target")
        if kind in {"range", "length"}:
            bounds = [rule[k] for k in ("min", "max") if k in rule]
            if not bounds or not all(_finite_number(n) for n in bounds):
                bad("at least one finite numeric bound required")
            if kind == "length" and not all(
                type(n) is int and 0 <= n <= 1000000 for n in bounds
            ):
                bad("length bounds must be integers 0..1000000")
            if "min" in rule and "max" in rule and rule["min"] > rule["max"]:
                bad("min must not exceed max")
            for key in ("inclusiveMin", "inclusiveMax"):
                if key in rule and type(rule[key]) is not bool:
                    bad("inclusive bounds must be booleans")
        if kind == "iqr":
            if (
                rule["emptyPolicy"] not in ("fail", "leave")
                or not _finite_number(rule.get("factor", 1.5))
                or not 0 <= rule.get("factor", 1.5) <= 100
            ):
                bad("invalid IQR factor or empty policy")
        if kind == "enum" and (
            not isinstance(rule["values"], list)
            or not 1 <= len(rule["values"]) <= 1000
            or not all(scalar(v, True) for v in rule["values"])
        ):
            bad("values must contain 1..1000 finite scalars")
        if kind == "compare":
            if rule["operator"] not in (
                "eq",
                "ne",
                "gt",
                "gte",
                "lt",
                "lte",
                "contains",
                "starts_with",
                "ends_with",
            ) or not scalar(rule["value"]):
                bad("invalid comparison operator or scalar")
        if kind == "regex":
            pattern = rule["pattern"]
            if not isinstance(pattern, str) or not 1 <= len(pattern) <= 256:
                bad("pattern must contain 1..256 characters")
            if (
                "(?" in pattern
                or "&&" in pattern
                or "\\Q" in pattern
                or "\\E" in pattern
                or "\\z" in pattern
                or re.search(r"\\[1-9]", pattern)
                or re.search(r"\)[*+?{]", pattern)
            ):
                bad(
                    "lookaround, special groups, backreferences and repeated groups are unsupported"
                )
            try:
                regex.compile(pattern)
            except regex.error as exc:
                bad(f"invalid regex: {exc}")
    return rules


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreviewRequest(StrictModel):
    objectKey: str = Field(min_length=5, max_length=512)
    format: Literal["csv", "xlsx", "parquet"]
    limit: int = Field(default=20, ge=1, le=1000)
    parseOptions: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)

    @field_validator("objectKey")
    @classmethod
    def raw_object(cls, value):
        if not value.startswith("raw/") or ".." in value or "\\" in value:
            raise ValueError("Only raw input objects are accepted")
        return value


class RuleValidationRequest(StrictModel):
    rules: list[dict[str, Any]] = Field(max_length=50)
    engineVersion: Literal["v2-phased", "v3-sequential", "v3-disk"] = "v2-phased"
    schemaVersion: int = 1
    logicalFields: list[dict[str, Any]] = Field(default_factory=list)


class TaskInput(StrictModel):
    fileId: UUID | None = None
    parseVersion: int | None = Field(default=None, ge=1)
    objectKey: str = Field(min_length=5, max_length=512)
    format: Literal["csv", "xlsx", "parquet"]
    name: str = Field(min_length=1, max_length=255)
    parseOptions: dict[str, Any] = Field(default_factory=dict)
    fields: list[dict[str, Any]] | None = None
    _raw_object = field_validator("objectKey")(PreviewRequest.raw_object.__func__)


class TaskMessage(StrictModel):
    taskId: UUID
    attempt: int = Field(ge=1, le=1000000)
    engineVersion: Literal["v2-phased", "v3-sequential", "v3-disk"] = "v2-phased"
    schemaVersion: int = 1
    logicalFields: list[dict[str, Any]] = Field(default_factory=list)
    fieldBindings: dict[str, str] = Field(default_factory=dict)
    outputFields: list[dict[str, Any]] | None = None
    attemptId: UUID | None = None
    requestId: str | None = Field(default=None, min_length=1, max_length=128)
    input: TaskInput
    rules: list[dict[str, Any]] = Field(max_length=50)
    outputFormat: Literal["csv", "xlsx", "parquet"]
    limits: dict[str, int] = Field(default_factory=dict)

    @field_validator("requestId")
    @classmethod
    def safe_request_id(cls, value):
        if value is not None and any(
            not 33 <= ord(character) <= 126 for character in value
        ):
            raise ValueError(
                "requestId must contain printable ASCII characters without spaces"
            )
        return value

    @model_validator(mode="after")
    def v3_attempt_identity(self):
        if self.engineVersion in {"v3-sequential", "v3-disk"} and self.attemptId is None:
            raise ValueError("V3 task messages require attemptId")
        if self.engineVersion == "v3-disk" and self.schemaVersion != 3:
            raise ValueError("v3-disk requires schemaVersion=3")
        return self


class PreflightRequest(StrictModel):
    input: TaskInput
    schemaVersion: Literal[3] = 3
    engineVersion: Literal["v3-disk"] = "v3-disk"
    logicalFields: list[dict[str, Any]] = Field(max_length=200)
    fieldBindings: dict[str, str]
    rules: list[dict[str, Any]] = Field(max_length=50)
