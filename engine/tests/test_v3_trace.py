import io
import json
from pathlib import Path
from threading import Event
from uuid import uuid4

import pandas as pd
import pytest

from cleaning.models import InputError, TaskMessage
from cleaning.pipeline import DIRTY_META, V3_DIRTY_META, clean_frame, execute_task


def rule(rule_id, kind, **options):
    return {"id": rule_id, "enabled": True, "type": kind, **options}


def test_v3_report_oracle_reconciles_unique_removals_and_preserves_issue_history():
    original = pd.DataFrame(
        {
            "id": ["A", "B", "C", "D", "A", "E", "F", "G"],
            "name": [" Alice ", "Bob", " Carol ", " Dan ", "Alice last", " Eve ", " Frank ", " Grace "],
            "amount": ["10", None, "bad", "1000", "12", "30", "20", "40"],
            "state": [" BAD ", "OK", "BAD", "OK", "OK", "OK", " BAD ", "OK"],
            "category": [" BAD ", "OK", "BAD", "OK", "OK", "OK", " BAD ", "OK"],
            "required_a": ["x"] * 5 + [None] + ["x"] * 2,
            "required_b": ["x"] * 5 + [None] + ["x"] * 2,
        },
        dtype=object,
    )
    file_id = str(uuid4())
    result = clean_frame(
        original,
        [
            rule("trim", "trim", columns=["f1", "f3", "f4"]),
            rule("cast", "cast", columns=["f2"], dtype="number", precision=2),
            rule("fill", "fill_stat", columns=["f2"], strategy="mean", emptyPolicy="fail"),
            rule("range", "range", column="f2", min=0, max=200),
            rule("required", "required", columns=["f5", "f6"]),
            rule("state", "enum", column="f3", values=["OK"], onError="mark"),
            rule("category", "enum", column="f4", values=["OK"], onError="mark"),
            rule("dedup", "deduplicate", columns=["f0"], keep="last"),
        ],
        engine_version="v3-sequential",
        file_id=file_id,
    )
    expected = {
        "inputRows": 8, "outputRows": 4, "validOutputRows": 3,
        "flaggedOutputRows": 1, "finalRemovedRows": 4, "issueRows": 5,
        "issueCount": 9, "duplicateRemovedRows": 1, "outlierHandledRows": 1,
        "missingFilledCells": 1, "typeConversionFailedCells": 1,
    }
    assert {key: result.report[key] for key in expected} == expected
    assert all(type(result.report[key]) is int for key in expected)
    assert result.data.index.tolist() == [1, 4, 6, 7]
    assert result.data.iloc[0]["amount"] == pytest.approx(1112 / 6)
    assert result.report["rules"][2]["statistics"]["f2"]["sampleCount"] == 6
    assert result.report["finalRemovalByRule"] == {"cast": 1, "range": 1, "required": 1, "dedup": 1}
    assert sum(result.report["finalRemovalByRule"].values()) == result.report["finalRemovedRows"]
    assert sum(item["removedRows"] for item in result.report["rules"]) == 4
    assert result.report["removedRows"] == result.report["finalRemovedRows"]
    assert result.report["flaggedRows"] == result.report["flaggedOutputRows"]
    assert result.report["dirtyRows"] == result.report["issueRows"]
    assert result.dirty.columns.tolist() == list(original.columns) + DIRTY_META + V3_DIRTY_META
    assert result.dirty["_file_id"].tolist() == [file_id] * 9
    assert result.dirty["_sheet"].isna().all()

    # Two missing fields are two issues, but the source row has one final removal.
    required_issues = result.dirty[result.dirty["_source_row"] == 6]
    assert required_issues["_field"].tolist() == ["required_a", "required_b"]
    assert required_issues["_final_action"].tolist() == ["REMOVED_BY_THIS_RULE"] * 2

    # Early marks survive in the audit even though a later dedup removes this row.
    first_row_issues = result.dirty[result.dirty["_source_row"] == 1]
    assert first_row_issues["_rule_id"].tolist() == ["state", "category", "dedup"]
    assert first_row_issues["_final_action"].tolist() == [
        "REMOVED_BY_LATER_RULE", "REMOVED_BY_LATER_RULE", "REMOVED_BY_THIS_RULE"
    ]
    assert first_row_issues.iloc[0]["state"] == " BAD "
    assert json.loads(first_row_issues.iloc[0]["_before_value"]) == {"type": "string", "value": "BAD"}
    assert json.loads(first_row_issues.iloc[2]["_before_value"])["value"]["f0"] == {"type": "string", "value": "A"}
    range_issue = result.dirty[result.dirty["_rule_id"] == "range"].iloc[0]
    assert range_issue["amount"] == "1000"
    assert json.loads(range_issue["_before_value"]) == {"type": "decimal", "value": "1000.00", "scale": 2}
    flagged = result.data[result.data.index == 6].iloc[0]
    assert not bool(flagged["_quality_valid"])
    assert len(json.loads(flagged["_quality_errors"])) == 2
    assert json.loads(json.dumps(result.report, allow_nan=False)) == result.report


def test_v3_nulls_stay_flagged_and_fill_counters_count_processing_events():
    result = clean_frame(
        pd.DataFrame({"a": ["bad", "2"], "b": ["bad", "3"]}),
        [
            rule("cast", "cast", columns=["f0", "f1"], dtype="integer", onError="null"),
            rule("fill-first", "fill_null", columns=["f0", "f1"], value=1),
            rule("range-null", "range", column="f0", max=0, onError="null"),
            rule("fill-second", "fill_null", columns=["f0"], value=9),
            rule("range-mark", "range", column="f0", min=10, onError="mark"),
        ],
        engine_version="v3-sequential",
    )
    assert result.report["missingFilledCells"] == 4
    assert result.report["typeConversionFailedCells"] == 2
    assert result.report["outlierHandledRows"] == 2
    assert sum(item["outlierHandledRows"] for item in result.report["rules"]) == 4
    assert result.report["issueRows"] == 2
    assert result.report["issueCount"] == 6
    assert result.report["validOutputRows"] == 0
    assert result.report["flaggedOutputRows"] == result.report["outputRows"] == 2
    assert result.report["finalRemovedRows"] == 0
    assert result.report["finalRemovalByRule"] == {}
    assert not result.data["_quality_valid"].any()
    assert set(result.dirty["_final_action"]) == {"OUTPUT_FLAGGED"}
    assert result.data["a"].tolist() == [9, 9]


def test_v3_repeated_cast_failures_count_each_rule_field_event():
    result = clean_frame(
        pd.DataFrame({"a": ["bad"]}),
        [rule(name, "cast", columns=["f0"], dtype="integer", onError="mark") for name in ["first", "second"]],
        engine_version="v3-sequential",
    )
    assert result.report["typeConversionFailedCells"] == 2
    assert result.report["issueRows"] == 1
    assert result.report["flaggedOutputRows"] == 1


def test_v2_dirty_columns_and_report_fields_remain_unchanged():
    original = pd.DataFrame({"a": ["bad"]})
    result = clean_frame(
        original, [{"type": "cast", "columns": ["f0"], "dtype": "integer"}],
        engine_version="v2-phased", file_id=str(uuid4()), sheet_name="sheet",
    )
    assert result.dirty.columns.tolist() == ["a"] + DIRTY_META
    assert "validOutputRows" not in result.report
    assert "finalRemovalByRule" not in result.report


def test_v3_extra_trace_columns_are_included_in_dirty_cell_budget(monkeypatch):
    import cleaning.pipeline as module
    monkeypatch.setattr(module, "MAX_DIRTY_CELLS", 10)
    original = pd.DataFrame({"a": ["bad"]})
    rules = [{"type": "cast", "columns": ["f0"], "dtype": "integer"}]
    assert len(clean_frame(original, rules).dirty) == 1
    with pytest.raises(InputError, match="Dirty output.*cell"):
        clean_frame(original, rules, engine_version="v3-sequential")


def test_execute_propagates_file_id_and_resolved_excel_sheet_to_trace(tmp_path):
    source = tmp_path / "original.xlsx"
    pd.DataFrame({"amount": ["bad", "2"]}).to_excel(source, index=False, sheet_name="账单")

    class Storage:
        def __init__(self):
            self.objects = {"raw/user/source.xlsx": source.read_bytes()}

        def download_input(self, key: str, target: Path):
            target.write_bytes(self.objects[key])

        def upload(self, key: str, path: Path, content_type: str):
            self.objects[key] = path.read_bytes()

    storage = Storage()
    file_id = str(uuid4())
    message = TaskMessage.model_validate({
        "taskId": str(uuid4()), "attempt": 1, "attemptId": str(uuid4()),
        "engineVersion": "v3-sequential",
        "input": {"fileId": file_id, "objectKey": "raw/user/source.xlsx", "format": "xlsx", "name": "source.xlsx"},
        "rules": [rule("cast", "cast", columns=["f0"], dtype="integer")],
        "outputFormat": "csv",
    })
    report, artifacts = execute_task(message, storage, tmp_path, Event(), lambda *_: None)
    dirty = pd.read_csv(io.BytesIO(storage.objects[artifacts["dirty"]]), dtype=str)
    assert dirty["_file_id"].tolist() == [file_id]
    assert dirty["_sheet"].tolist() == ["账单"]
    assert dirty["_source_row"].tolist() == ["1"]
    assert report["finalRemovedRows"] == 1
    assert report["typeConversionFailedCells"] == 1
    assert json.loads(storage.objects[artifacts["report"]]) == report
