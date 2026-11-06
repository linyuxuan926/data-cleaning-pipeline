from decimal import Decimal
import json

import pandas as pd
import pytest

from cleaning.models import InputError, validate_rules
from cleaning.pipeline import clean_frame
from cleaning.tableio import read_table, write_table


def test_all_validation_reasons_are_preserved_before_drop():
    original = pd.DataFrame(
        {"name": [" Alice ", "Bob"], "email": ["bad", "bob@x"], "state": ["X", "A"]}
    )
    rules = [
        {"type": "length", "column": "f0", "max": 3},
        {"type": "trim", "columns": ["f0"]},
        {"type": "regex", "column": "f1", "pattern": "[^@]+@[^@]+"},
        {"type": "enum", "column": "f2", "values": ["A", "B"]},
    ]
    result = clean_frame(original, rules)
    assert result.report["executionOrder"] == [2, 1, 3, 4]
    assert result.report["issueCount"] == 3
    assert result.report["dirtyRows"] == result.report["removedRows"] == 1
    assert result.report["outputRows"] == 1
    assert result.dirty["name"].tolist() == [" Alice "] * 3
    assert result.dirty["_rule_index"].tolist() == [1, 3, 4]
    assert all(r["inputRows"] == 2 for r in result.report["rules"] if r["phase"] == 50)


def test_mark_collects_multiple_errors_preserves_records_and_metadata():
    original = pd.DataFrame({"name": ["long", "ok"], "n": ["bad", "2"]})
    result = clean_frame(
        original,
        [
            {"type": "length", "column": "f0", "max": 2, "onError": "mark"},
            {
                "type": "validate_type",
                "columns": ["f1"],
                "dtype": "integer",
                "onError": "mark",
            },
        ],
    )
    assert (
        result.report["removedRows"] == 0
        and result.report["dirtyRows"] == result.report["flaggedRows"] == 1
    )
    assert result.report["issueCount"] == 2
    assert len(json.loads(result.data.iloc[0]["_quality_errors"])) == 2
    assert not bool(result.data.iloc[0]["_quality_valid"])
    assert bool(result.data.iloc[1]["_quality_valid"])
    assert (
        result.report["inputRows"]
        == result.report["outputRows"] + result.report["removedRows"]
    )


def test_cast_mark_keeps_original_invalid_value_and_later_validation_adds_reason():
    result = clean_frame(
        pd.DataFrame({"n": ["bad", "2"]}),
        [
            {"type": "cast", "columns": ["f0"], "dtype": "integer", "onError": "mark"},
            {
                "type": "validate_type",
                "columns": ["f0"],
                "dtype": "integer",
                "onError": "mark",
            },
        ],
    )
    assert result.data.iloc[0]["n"] == "bad"
    assert result.data.iloc[1]["n"] == 2
    assert result.report["issueCount"] == 2 and result.report["dirtyRows"] == 1


@pytest.mark.parametrize("policy", ["drop", "mark", "fail", "null"])
def test_outlier_error_policies(policy):
    original = pd.DataFrame({"n": ["-1", "10", "bad", None]})
    rule = {"type": "range", "column": "f0", "min": 0, "max": 10, "onError": policy}
    if policy == "fail":
        with pytest.raises(InputError, match="Rule 1.*field n.*data row 1"):
            clean_frame(original, [rule])
        return
    result = clean_frame(original, [rule])
    assert result.report["issueCount"] == result.report["dirtyRows"] == 2
    assert result.report["removedRows"] == (2 if policy == "drop" else 0)
    if policy == "null":
        assert pd.isna(result.data.iloc[0]["n"]) and pd.isna(result.data.iloc[2]["n"])
        assert result.report["changedCells"] == 2


def test_stable_ids_survive_phase_sorting_rename_projection_and_name_ambiguity():
    original = pd.DataFrame({"f1": ["10", "bad"], "amount": [" A ", " B "]})
    fields = [{"id": "f0", "name": "f1"}, {"id": "f1", "name": "amount"}]
    result = clean_frame(
        original,
        [
            {"type": "rename", "column": "f1", "target": "clean_name"},
            {"type": "cast", "columns": ["f0"], "dtype": "integer"},
            {"type": "trim", "columns": ["f1"]},
            {"type": "select_fields", "columns": ["f1", "f0"]},
        ],
        fields=fields,
    )
    assert result.data.columns.tolist() == ["clean_name", "f1"]
    assert result.data.iloc[0].to_dict() == {"clean_name": "A", "f1": 10}
    assert [c["id"] for c in result.report["columnsAfter"]] == ["f1", "f0"]
    assert result.dirty.iloc[0]["f1"] == "bad"
    with pytest.raises(InputError, match="immutable field snapshot"):
        clean_frame(original, [], fields=[{"id": "f0", "name": "changed"}])


@pytest.mark.parametrize(
    "strategy,expected", [("mean", 20), ("median", 20), ("mode", "10")]
)
def test_statistical_fill_uses_current_values_and_reports_invalid_samples(
    strategy, expected
):
    frame = pd.DataFrame({"n": ["10", "30", None, "invalid"]}, dtype=object)
    result = clean_frame(
        frame,
        [
            {
                "type": "fill_stat",
                "columns": ["f0"],
                "strategy": strategy,
                "emptyPolicy": "fail",
            }
        ],
    )
    assert result.data.iloc[2]["n"] == expected
    stats = result.report["rules"][0]["statistics"]["f0"]
    assert stats["invalidValues"] == (0 if strategy == "mode" else 1)
    assert stats["sampleCount"] == (3 if strategy == "mode" else 2)


@pytest.mark.parametrize("policy", ["fail", "leave", "constant"])
def test_all_null_statistical_fill_policies(policy):
    rule = {
        "type": "fill_stat",
        "columns": ["f0"],
        "strategy": "mean",
        "emptyPolicy": policy,
    }
    if policy == "constant":
        rule["value"] = 7
    if policy == "fail":
        with pytest.raises(InputError, match="no valid values"):
            clean_frame(pd.DataFrame({"n": [None, None]}), [rule])
    else:
        result = clean_frame(pd.DataFrame({"n": [None, None]}), [rule])
        assert (
            result.data["n"].isna().all()
            if policy == "leave"
            else result.data["n"].tolist() == [7, 7]
        )


def test_phase_drops_excluded_from_later_stats_and_mode_tie_is_first_original_order():
    frame = pd.DataFrame(
        {"n": ["bad", "10", "30", None], "category": ["bad", "B", "A", None]}
    )
    result = clean_frame(
        frame,
        [
            {
                "type": "fill_stat",
                "columns": ["f0"],
                "strategy": "mean",
                "emptyPolicy": "fail",
            },
            {"type": "cast", "columns": ["f0"], "dtype": "number"},
            {
                "type": "fill_stat",
                "columns": ["f1"],
                "strategy": "mode",
                "emptyPolicy": "fail",
            },
        ],
    )
    assert result.report["executionOrder"] == [2, 1, 3]
    assert result.data.iloc[-1]["n"] == 20
    assert result.data.iloc[-1]["category"] == "B"


@pytest.mark.parametrize("columns", [None, [], ["f0"]])
def test_global_deduplicate_keep_last_and_full_row(columns):
    frame = pd.DataFrame({"id": ["1", "2", "1"], "value": ["same", "other", "same"]})
    rule = {"type": "deduplicate", "keep": "last"}
    if columns is not None:
        rule["columns"] = columns
    result = clean_frame(frame, [rule])
    assert result.data.index.tolist() == [1, 2]
    assert result.dirty["_source_row"].tolist() == [1]


def test_iqr_thresholds_zero_spread_and_null_policy():
    result = clean_frame(
        pd.DataFrame({"n": ["2", "2", "2", "2", "100"]}),
        [{"type": "iqr", "column": "f0", "emptyPolicy": "fail", "onError": "null"}],
    )
    statistics = result.report["rules"][0]["statistics"]
    assert statistics["lower"] == statistics["upper"] == 2
    assert statistics["sampleCount"] == 5
    assert pd.isna(result.data.iloc[-1]["n"]) and result.report["removedRows"] == 0
    with pytest.raises(InputError, match="no valid numeric"):
        clean_frame(
            pd.DataFrame({"n": [None, "bad"]}),
            [{"type": "iqr", "column": "f0", "emptyPolicy": "fail"}],
        )


def test_boolean_dates_precision_and_all_real_export_formats(tmp_path):
    frame = pd.DataFrame(
        {
            "id": ["001", "002"],
            "flag": ["yes", "no"],
            "date": ["2026/11/06", "2026/11/07"],
            "n": ["1.235", "-2.345"],
        }
    )
    result = clean_frame(
        frame,
        [
            {"type": "cast", "columns": ["f1"], "dtype": "boolean"},
            {
                "type": "cast",
                "columns": ["f2"],
                "dtype": "datetime",
                "dateFormat": "%Y/%m/%d",
                "outputFormat": "%d-%m-%Y",
            },
            {"type": "cast", "columns": ["f3"], "dtype": "number", "precision": 2},
        ],
    )
    assert result.data.iloc[0]["flag"] == True
    assert result.data.iloc[0]["date"] == "06-11-2026"
    assert result.data["n"].tolist() == [Decimal("1.24"), Decimal("-2.35")]
    for file_format in ("csv", "xlsx", "parquet"):
        path = tmp_path / f"result.{file_format}"
        write_table(result.data, path, file_format)
        loaded, _, _ = read_table(path, file_format)
        assert loaded.iloc[0]["id"] == "001"
        assert str(loaded.iloc[0]["n"]).lstrip("'") == "1.24"
        assert str(loaded.iloc[1]["n"]) == "-2.35"


def test_missing_normalizes_trim_produced_empty_and_explicit_markers():
    frame = pd.DataFrame({"n": ["   ", "NA", "2", None]})
    result = clean_frame(
        frame,
        [
            {"type": "fill_null", "columns": ["f0"], "value": "filled"},
            {"type": "missing", "columns": ["f0"], "markers": ["", "NA"]},
            {"type": "trim", "columns": ["f0"]},
        ],
    )
    # Within the missing phase array order remains meaningful: fill then marker normalization.
    assert result.data["n"].tolist() == [None, None, "2", "filled"]


@pytest.mark.parametrize(
    "op,value,survivors",
    [
        ("gt", 2, ["3"]),
        ("gte", 2, ["2", "3"]),
        ("lt", 2, ["1"]),
        ("lte", 2, ["1", "2"]),
        ("eq", "2", ["2"]),
        ("ne", "2", ["1", "3"]),
    ],
)
def test_safe_numeric_and_typed_comparisons(op, value, survivors):
    result = clean_frame(
        pd.DataFrame({"n": ["1", "2", "3"]}),
        [{"type": "compare", "column": "f0", "operator": op, "value": value}],
    )
    assert result.data["n"].tolist() == survivors


def _v3_rule(rule_id, rule, enabled=True):
    return {"id": rule_id, "enabled": enabled, **rule}


def test_v3_runs_rules_in_configured_order_while_v2_keeps_phase_order():
    frame = pd.DataFrame({"n": ["1000", "1", None, "3"]}, dtype=object)
    v3_rules = [
        _v3_rule(
            "range-first",
            {"type": "range", "column": "f0", "max": 10, "onError": "drop"},
        ),
        _v3_rule(
            "fill-second",
            {
                "type": "fill_stat",
                "columns": ["f0"],
                "strategy": "mean",
                "emptyPolicy": "fail",
            },
        ),
    ]
    v3 = clean_frame(frame, v3_rules, engine_version="v3-sequential")
    v2 = clean_frame(
        frame,
        [
            {key: value for key, value in item.items() if key not in {"id", "enabled"}}
            for item in v3_rules
        ],
        engine_version="v2-phased",
    )

    assert v3.report["executionOrder"] == ["range-first", "fill-second"]
    assert v3.report["rules"][0]["inputRows"] == 4
    assert v3.report["rules"][1]["inputRows"] == 3
    assert v3.data.index.tolist() == [1, 2, 3]
    assert v3.data["n"].tolist() == ["1", 2.0, "3"]
    assert v2.report["executionOrder"] == [2, 1]
    assert v2.data.index.tolist() == [1, 3]


def test_v3_applies_null_and_drop_before_the_next_rule():
    result = clean_frame(
        pd.DataFrame({"n": ["-1", "2"]}),
        [
            _v3_rule(
                "null-outside-range",
                {"type": "range", "column": "f0", "min": 0, "onError": "null"},
            ),
            _v3_rule(
                "drop-null",
                {"type": "drop_null", "columns": ["f0"], "onError": "drop"},
            ),
        ],
        engine_version="v3-sequential",
    )

    assert result.data.index.tolist() == [1]
    assert result.report["rules"][1]["inputRows"] == 2
    assert result.report["rules"][1]["rejectedRows"] == 1
    assert result.report["removedRows"] == 1


def test_v3_global_rules_use_all_surviving_rows_across_large_input():
    rows = 1001
    frame = pd.DataFrame(
        {
            "id": ["high", "duplicate"]
            + [f"id-{i}" for i in range(2, rows - 1)]
            + ["duplicate"],
            "n": ["9999"] + ["1"] * (rows - 2) + [None],
            "iqr": ["10"] * (rows - 2) + ["100", "10"],
        },
        dtype=object,
    )
    result = clean_frame(
        frame,
        [
            _v3_rule(
                "drop-high",
                {"type": "range", "column": "f1", "max": 100, "onError": "drop"},
            ),
            _v3_rule(
                "global-mean",
                {
                    "type": "fill_stat",
                    "columns": ["f1"],
                    "strategy": "mean",
                    "emptyPolicy": "fail",
                },
            ),
            _v3_rule("global-dedup", {"type": "deduplicate", "columns": ["f0"], "keep": "last"}),
            _v3_rule(
                "global-iqr",
                {
                    "type": "iqr",
                    "column": "f2",
                    "emptyPolicy": "fail",
                    "onError": "drop",
                },
            ),
        ],
        engine_version="v3-sequential",
    )

    assert result.report["rules"][1]["statistics"]["f1"]["sampleCount"] == rows - 2
    assert result.report["rules"][2]["rejectedRows"] == 1
    assert result.report["rules"][3]["statistics"]["sampleCount"] == rows - 2
    assert result.report["rules"][3]["rejectedRows"] == 1
    assert result.data.index[-1] == rows - 1
    assert result.data["n"].iloc[-1] == 1.0


def test_v3_skips_disabled_rules_but_keeps_them_in_the_configuration_report():
    result = clean_frame(
        pd.DataFrame({"name": [" Alice "]}),
        [
            _v3_rule("disabled-trim", {"type": "trim", "columns": ["f0"]}, False),
            _v3_rule("uppercase", {"type": "case", "columns": ["f0"], "mode": "upper"}),
        ],
        engine_version="v3-sequential",
    )

    assert result.data.iloc[0]["name"] == " ALICE "
    assert result.report["executionOrder"] == ["uppercase"]
    assert result.report["rules"][0]["status"] == "DISABLED"


def test_v3_drop_prevents_later_validation_or_fail_on_that_record():
    rules = [
        _v3_rule("drop-invalid", {"type": "length", "column": "f0", "max": 2}),
        _v3_rule(
            "later-fail",
            {"type": "enum", "column": "f0", "values": ["ok"], "onError": "fail"},
        ),
    ]
    result = clean_frame(
        pd.DataFrame({"name": ["invalid", "ok"]}),
        rules,
        engine_version="v3-sequential",
    )
    assert result.data.index.tolist() == [1]
    assert result.report["issueCount"] == 1
    assert result.report["rules"][1]["inputRows"] == 1
    with pytest.raises(InputError, match="Rule 1.*field name.*data row 1"):
        clean_frame(
            pd.DataFrame({"name": ["invalid", "ok"]}),
            list(reversed(rules)),
            engine_version="v3-sequential",
        )


def test_v3_mark_preserves_bad_cast_value_and_accumulates_later_issues():
    result = clean_frame(
        pd.DataFrame({"n": ["bad", "2"]}),
        [
            _v3_rule(
                "mark-cast",
                {"type": "cast", "columns": ["f0"], "dtype": "integer", "onError": "mark"},
            ),
            _v3_rule(
                "mark-range",
                {"type": "range", "column": "f0", "min": 0, "onError": "mark"},
            ),
        ],
        engine_version="v3-sequential",
    )
    assert result.data.iloc[0]["n"] == "bad"
    assert result.data.iloc[1]["n"] == 2
    assert result.report["issueCount"] == 2
    assert len(json.loads(result.data.iloc[0]["_quality_errors"])) == 2


def test_v3_cast_null_is_visible_to_later_fill_and_v2_still_rejects_it():
    cast = {"type": "cast", "columns": ["f0"], "dtype": "integer", "onError": "null"}
    with pytest.raises(InputError, match="unsupported onError"):
        validate_rules([cast], "v2-phased")
    result = clean_frame(
        pd.DataFrame({"n": ["bad", "2"]}),
        [
            _v3_rule("null-cast", cast),
            _v3_rule("fill-after-null", {"type": "fill_null", "columns": ["f0"], "value": 7}),
        ],
        engine_version="v3-sequential",
    )
    assert result.data["n"].tolist() == [7, 2]
    assert result.report["rules"][0]["issueCount"] == 1
    assert result.report["rules"][1]["changedCells"] == 1


@pytest.mark.parametrize("rules", [
    [_v3_rule("same", {"type": "trim", "columns": ["f0"]})] * 2,
    [_v3_rule("not-boolean", {"type": "trim", "columns": ["f0"]}, 1)],
])
def test_v3_rejects_ambiguous_or_malformed_rule_identity(rules):
    with pytest.raises(InputError):
        clean_frame(pd.DataFrame({"n": [" A "]}), rules, engine_version="v3-sequential")


def test_v3_defaults_legacy_rule_metadata_deterministically():
    result = clean_frame(
        pd.DataFrame({"name": [" Alice "]}),
        [{"type": "trim", "columns": ["f0"]}],
        engine_version="v3-sequential",
    )
    assert result.report["executionOrder"] == ["rule-1"]
    assert result.report["ruleSnapshot"][0]["enabled"] is True



@pytest.mark.parametrize(
    "op,value", [("contains", "ell"), ("starts_with", "he"), ("ends_with", "lo")]
)
def test_safe_string_predicates(op, value):
    result = clean_frame(
        pd.DataFrame({"text": ["hello", "world", 123]}),
        [{"type": "compare", "column": "f0", "operator": op, "value": value}],
    )
    assert result.data["text"].tolist() == ["hello"]


@pytest.mark.parametrize(
    "rule",
    [
        {
            "type": "compare",
            "column": "f0",
            "operator": "eval",
            "value": "__import__('os')",
        },
        {"type": "cast", "columns": ["f0"], "dtype": "number", "precision": 13},
        {"type": "cast", "columns": ["f0"], "dtype": "integer", "precision": 2},
        {"type": "regex", "column": "f0", "pattern": "x", "onError": "null"},
        {"type": "select_fields", "columns": []},
        {
            "type": "fill_stat",
            "columns": ["f0"],
            "strategy": "mean",
            "emptyPolicy": "constant",
        },
        {"type": "missing", "columns": ["f0"], "markers": [{}]},
        {"type": "iqr", "column": "f0", "emptyPolicy": "leave", "factor": -1},
        {"type": "length", "column": "f0", "min": 1.5},
        {"type": "cast", "columns": ["f0"], "dtype": "datetime", "outputFormat": "%q"},
    ],
)
def test_authoritative_rule_validation_rejects_invalid_options(rule):
    with pytest.raises(InputError):
        validate_rules([rule])


def test_large_finite_statistics_do_not_overflow_in_intermediate_sum():
    for strategy in ("mean", "median"):
        result = clean_frame(
            pd.DataFrame({"n": [1e308, 1e308, None]}, dtype=object),
            [
                {
                    "type": "fill_stat",
                    "columns": ["f0"],
                    "strategy": strategy,
                    "emptyPolicy": "fail",
                }
            ],
        )
        assert result.data.iloc[-1]["n"] == 1e308


def test_numeric_precision_rejects_overflow_and_handles_tiny_exponents():
    result = clean_frame(
        pd.DataFrame({"n": ["1e99999999", "1e-99999999"]}),
        [{"type": "cast", "columns": ["f0"], "dtype": "number", "precision": 2}],
    )
    assert result.report["removedRows"] == 1
    assert result.data.iloc[0]["n"] == Decimal("0.00")


@pytest.mark.parametrize(
    "value", [9007199254740993, 9223372036854775807, -9223372036854775808]
)
def test_range_and_order_predicates_preserve_exact_int64_precision(value):
    frame = pd.DataFrame({"n": [str(value)]})
    for validator in [
        {"type": "range", "column": "f0", "min": value, "max": value},
        {"type": "compare", "column": "f0", "operator": "gt", "value": value - 1},
        {"type": "compare", "column": "f0", "operator": "lt", "value": value + 1},
    ]:
        result = clean_frame(
            frame, [{"type": "cast", "columns": ["f0"], "dtype": "integer"}, validator]
        )
        assert result.report["outputRows"] == 1, (value, validator, result.report)
        assert result.data.iloc[0]["n"] == value


def test_range_and_order_predicates_preserve_rounded_decimal_precision():
    value = Decimal("9007199254740993.25")
    frame = pd.DataFrame({"n": [value]})
    result = clean_frame(
        frame,
        [
            {
                "type": "compare",
                "column": "f0",
                "operator": "gt",
                "value": 9007199254740993,
            }
        ],
    )
    assert result.report["outputRows"] == 1


@pytest.mark.parametrize("invalid", ["NaN", "nan", "inf", "-Infinity"])
def test_marked_nonfinite_number_keeps_original_string_after_nullable_consolidation(
    invalid,
):
    frame = pd.DataFrame({"n": [invalid, "2"]}, dtype=object)
    result = clean_frame(
        frame,
        [
            {"type": "cast", "columns": ["f0"], "dtype": "number", "onError": "mark"},
            {
                "type": "validate_type",
                "columns": ["f0"],
                "dtype": "number",
                "onError": "mark",
            },
        ],
    )
    assert result.data.iloc[0]["n"] == invalid
    assert type(result.data.iloc[0]["n"]) is str
    assert result.data.iloc[1]["n"] == 2.0
    assert result.report["issueCount"] == 2 and result.report["removedRows"] == 0
    assert len(json.loads(result.data.iloc[0]["_quality_errors"])) == 2


@pytest.mark.parametrize("invalid", ["NaT", "nat", ""])
@pytest.mark.parametrize("kind", ["cast", "validate_type"])
def test_nonmissing_nat_tokens_never_silently_pass_datetime_conversion(invalid, kind):
    frame = pd.DataFrame({"date": [invalid, "2026-11-06", None]}, dtype=object)
    result = clean_frame(
        frame,
        [{"type": kind, "columns": ["f0"], "dtype": "datetime", "onError": "mark"}],
    )
    assert result.data.iloc[0]["date"] == invalid
    assert result.report["issueCount"] == 1 and result.report["dirtyRows"] == 1
    assert len(json.loads(result.data.iloc[0]["_quality_errors"])) == 1
    assert pd.isna(result.data.iloc[2]["date"])
