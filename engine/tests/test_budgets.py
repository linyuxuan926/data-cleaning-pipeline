from threading import Event
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from cleaning import budgets
from cleaning.models import InputError, TaskMessage
from cleaning.pipeline import clean_frame, execute_task
from cleaning.tableio import preview_table, read_table, write_table


def test_fill_amplification_is_rejected_before_candidate_copy(monkeypatch):
    frame = pd.DataFrame({"value": [None] * 100})
    monkeypatch.setattr(budgets, "MAX_LOGICAL_TEXT_BYTES", 1000)
    original_copy = pd.DataFrame.copy
    copies = []

    def tracked_copy(self, *args, **kwargs):
        copies.append(len(self))
        return original_copy(self, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "copy", tracked_copy)
    with pytest.raises(InputError, match="Logical text"):
        clean_frame(
            frame, [{"type": "fill_null", "columns": ["value"], "value": "x" * 100}]
        )
    assert copies == [
        100,
        100,
    ], "fill must be rejected before allocating a candidate frame (only shallow snapshot and working copy exist)"
    assert frame["value"].isna().all()


def test_fill_scalar_memory_amplification_is_preflighted(monkeypatch):
    frame = pd.DataFrame({"value": [None] * 100})
    current = int(frame.memory_usage(index=True, deep=True).sum())
    monkeypatch.setattr(budgets, "MAX_FRAME_MEMORY_BYTES", current + 10)
    with pytest.raises(InputError, match="Filled data"):
        clean_frame(frame, [{"type": "fill_null", "columns": ["value"], "value": 1}])


def test_unicode_case_expansion_is_stopped_incrementally(monkeypatch):
    calls = []

    class CountedString(str):
        def upper(self):
            calls.append(1)
            return super().upper()

    frame = pd.DataFrame({"value": [CountedString("ΐ")] * 10}, dtype=object)
    monkeypatch.setattr(budgets, "MAX_LOGICAL_TEXT_BYTES", 40)
    with pytest.raises(InputError, match="Logical text"):
        clean_frame(frame, [{"type": "case", "columns": ["value"], "mode": "upper"}])
    assert 0 < len(calls) < len(frame)


@pytest.mark.parametrize("file_format", ["csv", "xlsx", "parquet"])
def test_input_text_budget_counts_each_repeated_cell(
    tmp_path, monkeypatch, file_format
):
    path = tmp_path / f"input.{file_format}"
    write_table(pd.DataFrame({"value": ["x" * 100] * 10}), path, file_format)
    monkeypatch.setattr(budgets, "MAX_LOGICAL_TEXT_BYTES", 500)
    with pytest.raises(InputError, match="Logical text"):
        read_table(path, file_format)
    assert preview_table(path, file_format, 1)["previewRowCount"] == 1


def test_parquet_dictionary_budget_counts_used_repeated_values_not_unused_dictionary(
    tmp_path, monkeypatch
):
    path = tmp_path / "dictionary.parquet"
    dictionary = pa.array([f"{i:05d}" + "x" * 59 for i in range(1000)])
    values = pa.DictionaryArray.from_arrays(
        pa.array([0] * 10, type=pa.int32()), dictionary
    )
    pq.write_table(pa.table({"value": values}), path)
    monkeypatch.setattr(budgets, "MAX_LOGICAL_TEXT_BYTES", 700)
    frame, count, _ = read_table(path, "parquet")
    assert len(frame) == count == 10
    assert frame.iloc[0]["value"] == "00000" + "x" * 59
    monkeypatch.setattr(budgets, "MAX_LOGICAL_TEXT_BYTES", 500)
    with pytest.raises(InputError, match="Logical text"):
        read_table(path, "parquet")


@pytest.mark.parametrize("file_format", ["csv", "xlsx", "parquet"])
def test_actual_artifact_size_limit_removes_partial_output(
    tmp_path, monkeypatch, file_format
):
    monkeypatch.setattr(budgets, "MAX_OUTPUT_BYTES", 32)
    path = tmp_path / f"output.{file_format}"
    with pytest.raises(InputError, match="artifact limit"):
        write_table(pd.DataFrame({"value": ["x" * 100]}), path, file_format)
    assert not path.exists()


def test_frame_memory_limit_applies_to_numeric_data_without_strings(monkeypatch):
    frame = pd.DataFrame({"value": list(range(100))})
    monkeypatch.setattr(budgets, "MAX_FRAME_MEMORY_BYTES", 100)
    with pytest.raises(InputError, match="DataFrame"):
        clean_frame(frame, [])


def test_business_rule_validation_happens_before_download(tmp_path):
    task = TaskMessage.model_validate(
        {
            "taskId": str(uuid4()),
            "attempt": 1,
            "input": {
                "objectKey": "raw/test/data.csv",
                "name": "data.csv",
                "format": "csv",
            },
            "rules": [{"type": "regex", "column": "x", "pattern": r"\e"}],
            "outputFormat": "csv",
        }
    )
    # No storage adapter is used: invalid rules must be rejected first.
    with pytest.raises(InputError, match="invalid regex"):
        execute_task(task, object(), tmp_path, Event(), lambda *_: None)


def test_diagnostic_issue_cap_applies_while_collecting_violations(monkeypatch):
    import cleaning.pipeline as module

    monkeypatch.setattr(module, "MAX_ISSUES", 2)
    with pytest.raises(InputError, match="2-issue"):
        clean_frame(
            pd.DataFrame({"a": [None] * 20, "b": [None] * 20}),
            [{"type": "required", "columns": ["f0", "f1"]}],
        )


def test_wide_numeric_dirty_output_is_budgeted_before_dataframe_construction(
    monkeypatch,
):
    import cleaning.pipeline as module

    monkeypatch.setattr(module, "MAX_DIRTY_CELLS", 15)
    with pytest.raises(InputError, match="Dirty output.*cell"):
        clean_frame(
            pd.DataFrame({"a": [-1, -2], "b": [2, 3]}),
            [{"type": "range", "column": "f0", "min": 0}],
        )


def test_dirty_deep_memory_preflight_counts_numeric_values_and_metadata(monkeypatch):
    frame = pd.DataFrame({"a": [-1, -2]})
    # Original/working frames fit; diagnostics with six metadata columns do not.
    monkeypatch.setattr(budgets, "MAX_FRAME_MEMORY_BYTES", 1000)
    with pytest.raises(InputError, match="Dirty output would exceed"):
        clean_frame(frame, [{"type": "range", "column": "f0", "min": 0}])
