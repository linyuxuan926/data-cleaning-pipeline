from threading import Event

import pandas as pd
import pytest

from cleaning.models import CancelledError, InputError, validate_rules
from cleaning.pipeline import clean_frame


def test_ordered_rules_conserve_rows_and_keep_original_dirty_values():
    original = pd.DataFrame(
        {
            "id": ["1", "1", "2", "3", "4"],
            "name": [" Alice ", " OTHER ", " BOB ", None, "Eve"],
            "age": ["24", "10", "bad", "30", "200"],
        },
        dtype=object,
    )
    rules = [
        {"type": "trim", "columns": ["name"]},
        {"type": "case", "columns": ["name"], "mode": "lower"},
        {"type": "fill_null", "columns": ["name"], "value": "unknown"},
        {"type": "deduplicate", "columns": ["id"]},
        {"type": "cast", "columns": ["age"], "dtype": "integer"},
        {"type": "range", "column": "age", "min": 0, "max": 150},
    ]
    result = clean_frame(original, rules)
    assert list(result.data["id"]) == ["1", "3"]
    assert list(result.data["name"]) == ["alice", "unknown"]
    assert str(result.data["age"].dtype) == "Int64"
    assert result.dirty["_source_row"].tolist() == [2, 3, 5]
    assert result.dirty["_rule_index"].tolist() == [4, 5, 6]
    assert result.dirty.iloc[0]["name"] == " OTHER "
    assert (
        result.report["inputRows"]
        == result.report["outputRows"] + result.report["dirtyRows"]
    )
    assert original.iloc[0]["name"] == " Alice "


def test_cast_int64_does_not_round_large_integers_or_accept_fractional_values():
    original = pd.DataFrame(
        {
            "n": [
                "9223372036854775807",
                "9223372036854775808",
                "1.5",
                "-9223372036854775808",
                None,
            ]
        },
        dtype=object,
    )
    result = clean_frame(
        original, [{"type": "cast", "columns": ["n"], "dtype": "integer"}]
    )
    assert result.data.iloc[0]["n"] == 9223372036854775807
    assert result.data.iloc[1]["n"] == -9223372036854775808
    assert pd.isna(result.data.iloc[2]["n"])
    assert result.report["dirtyRows"] == 2
    assert result.report["changedCells"] == 2


def test_cast_changed_cells_excludes_values_in_rejected_rows():
    original = pd.DataFrame({"a": ["2", "bad"], "b": ["bad", "3"]})
    result = clean_frame(
        original, [{"type": "cast", "columns": ["a", "b"], "dtype": "integer"}]
    )
    assert result.report["outputRows"] == 0
    assert result.report["changedCells"] == 0
    assert result.report["dirtyRows"] == 2


def test_trim_preserves_non_string_cell_types_and_nulls():
    original = pd.DataFrame({"n": [2, None, " A "]}, dtype=object)
    result = clean_frame(original, [{"type": "trim", "columns": ["n"]}])
    assert type(result.data.iloc[0]["n"]) is int
    assert result.report["changedCells"] == 1


def test_rename_is_final_projection_and_stable_ids_keep_original_dirty_schema():
    original = pd.DataFrame({"old": ["10", "bad"]})
    result = clean_frame(
        original,
        [
            {"type": "rename", "column": "old", "target": "new"},
            {"type": "cast", "columns": ["f0"], "dtype": "integer"},
        ],
    )
    assert result.data.columns.tolist() == ["new"]
    assert "old" in result.dirty.columns and "new" not in result.dirty.columns
    with pytest.raises(InputError, match="missing columns"):
        clean_frame(original, [{"type": "trim", "columns": ["missing"]}])
    with pytest.raises(InputError, match="already exists"):
        clean_frame(
            pd.DataFrame({"a": [], "b": []}),
            [{"type": "rename", "column": "a", "target": "b"}],
        )


def test_range_regex_null_and_invalid_semantics():
    original = pd.DataFrame(
        {
            "n": ["0", "150", "151", "bad", None],
            "mail": ["a@b", "invalid", "a@b", "a@b", None],
        }
    )
    result = clean_frame(
        original,
        [
            {"type": "range", "column": "n", "min": 0, "max": 150},
            {"type": "regex", "column": "mail", "pattern": "[^@]+@[^@]+"},
        ],
    )
    assert result.data.index.tolist() == [0, 4]
    assert result.dirty["_source_row"].tolist() == [2, 3, 4]


def test_cancel_before_work_and_empty_pipeline():
    event = Event()
    event.set()
    with pytest.raises(CancelledError):
        clean_frame(pd.DataFrame({"a": [1]}), [], cancel=event)
    result = clean_frame(pd.DataFrame({"a": [1]}), [])
    assert result.report["inputRows"] == result.report["outputRows"] == 1
    assert result.report["dirtyRows"] == 0
    assert result.dirty.columns.tolist() == [
        "a",
        "_record_id",
        "_source_row",
        "_field",
        "_rule_index",
        "_error_code",
        "_reason",
    ]


@pytest.mark.parametrize(
    "rules",
    [
        [{"type": "nope"}],
        [{"type": "trim", "columns": ["a"], "extra": 1}],
        [{"type": "trim", "columns": []}],
        [{"type": "trim", "columns": ["a", "a"]}],
        [{"type": "range", "column": "a", "min": 2, "max": 1}],
        [{"type": "range", "column": "a", "min": float("inf")}],
        [{"type": "range", "column": "a", "min": True}],
        [{"type": "rename", "column": "a", "target": "_reason"}],
        [{"type": "regex", "column": "a", "pattern": "(a+)+"}],
        [{"type": "regex", "column": "a", "pattern": "(?=a)a"}],
        [{"type": "regex", "column": "a", "pattern": r"(a)\1"}],
        [{"type": "regex", "column": "a", "pattern": "["}],
        [{"type": "fill_null", "columns": ["a"], "value": {"bad": 1}}],
        [{"type": "fill_null", "columns": ["a"], "value": None}],
        [{"type": "fill_null", "columns": ["a"], "value": "x" * 10001}],
        [{"type": "fill_null", "columns": ["a"], "value": 10**1000}],
        [{"type": "range", "column": "a", "min": 10**1000}],
        [{"type": "regex", "column": "a", "pattern": r"\Qliteral\E"}],
        [{"type": "regex", "column": "a", "pattern": "[a-z&&[^b]]"}],
        [{"type": "case", "columns": ["a"], "mode": "title"}],
        [{"type": "cast", "columns": ["a"], "dtype": "bool"}],
        [{"type": []}],
        [{"type": "case", "columns": ["a"], "mode": []}],
        [{"type": "cast", "columns": ["a"], "dtype": {}}],
    ],
)
def test_invalid_rule_options_rejected(rules):
    with pytest.raises(InputError):
        validate_rules(rules)
