from datetime import datetime, timezone

from openpyxl import Workbook, load_workbook
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from cleaning.models import InputError
from cleaning.tableio import preview_table, read_table, write_table


@pytest.mark.parametrize("file_format", ["csv", "xlsx", "parquet"])
def test_all_formats_round_trip_and_bounded_preview(tmp_path, file_format):
    path = tmp_path / f"input.{file_format}"
    frame = pd.DataFrame({"id": ["001", "002", "003"], "name": ["张三", "李四", None]})
    write_table(frame, path, file_format)
    loaded, count, _ = read_table(path, file_format)
    assert loaded.iloc[0]["id"] == "001"
    assert count == 3
    preview = preview_table(path, file_format, 2)
    assert preview["previewRowCount"] == 2
    assert preview["rows"][0] == {"id": "001", "name": "张三"}
    assert preview["rowCount"] == (3 if file_format == "parquet" else None)


def test_csv_duplicate_reserved_and_malformed_headers_rejected(tmp_path):
    for content, message in [
        ("a,a\n1,2", "Duplicate"),
        ("a, a \n1,2", "Duplicate"),
        ("a,   \n1,2", "nonempty"),
        ("a,_reason\n1,x", "reserved"),
        ("a,b\n1,2,3", "3 fields"),
        ("a,b\n1", "1 fields"),
    ]:
        path = tmp_path / "input.csv"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(InputError, match=message):
            read_table(path, "csv")


def test_csv_preserves_literal_na_multiline_and_blank_data_rows(tmp_path):
    path = tmp_path / "input.csv"
    path.write_bytes('id,note\n001,"line one\nline two"\nNA,\n\n'.encode("utf-8"))
    frame, count, _ = read_table(path, "csv")
    assert count == 3
    assert frame.iloc[0]["note"] == "line one\nline two"
    assert frame.iloc[1]["id"] == "NA"
    assert frame.iloc[2].isna().all()


@pytest.mark.parametrize("file_format", ["csv", "xlsx"])
def test_spreadsheet_formula_protection_in_values_and_headers(tmp_path, file_format):
    path = tmp_path / f"result.{file_format}"
    count = write_table(
        pd.DataFrame({"=header": ["=1+1", " \t@command", -3]}), path, file_format
    )
    assert count >= 2
    if file_format == "csv":
        text = path.read_text(encoding="utf-8-sig")
        assert "'=header" in text and "'=1+1" in text
    else:
        workbook = load_workbook(path, data_only=False)
        assert workbook.active["A1"].data_type != "f"
        assert workbook.active["A2"].data_type != "f"
        assert workbook.active["A4"].value == -3


def test_xlsx_formula_input_is_literal_not_silently_discarded(tmp_path):
    path = tmp_path / "input.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["formula", "date"])
    sheet.append(["=1+1", datetime(2020, 1, 2)])
    workbook.create_sheet("ignored")
    workbook.save(path)
    frame, _, warnings = read_table(path, "xlsx")
    assert frame.iloc[0]["formula"] == "=1+1"
    assert warnings == ["Only the selected XLSX worksheet is processed: Sheet"]


def test_nested_parquet_and_ambiguous_mixed_types_rejected(tmp_path):
    path = tmp_path / "nested.parquet"
    pq.write_table(pa.table({"nested": [[1, 2]]}), path)
    with pytest.raises(InputError, match="scalar"):
        read_table(path, "parquet")
    with pytest.raises(InputError, match="consistent type"):
        write_table(
            pd.DataFrame({"mixed": ["one", 2]}, dtype=object),
            tmp_path / "mixed.parquet",
            "parquet",
        )


def test_limits_apply_before_silent_truncation(tmp_path, monkeypatch):
    import cleaning.tableio as module

    monkeypatch.setattr(module, "MAX_ROWS", 2)
    path = tmp_path / "input.csv"
    path.write_text("a\n1\n2\n3\n", encoding="utf-8")
    with pytest.raises(InputError, match="500000"):
        read_table(path, "csv")
    assert preview_table(path, "csv", 1)["previewRowCount"] == 1


def test_preview_json_null_and_datetime(tmp_path):
    path = tmp_path / "input.parquet"
    pq.write_table(
        pa.table(
            {
                "n": [float("inf"), None],
                "dt": [datetime(2020, 1, 2, tzinfo=timezone.utc), None],
            }
        ),
        path,
    )
    preview = preview_table(path, "parquet", 2)
    assert preview["rows"][0]["n"] is None
    assert preview["rows"][1] == {"n": None, "dt": None}
    assert preview["rows"][0]["dt"].startswith("2020-01-02")


def test_recursive_json_normalization_preserves_numeric_and_boolean_types():
    import json
    import numpy as np
    from cleaning.tableio import json_value

    value = {
        "count": np.int64(4),
        "nested": [{"valid": np.bool_(True), "mean": np.float64(1.25)}],
        "values": np.array([1, 2], dtype=np.int64),
    }
    normalized = json_value(value)
    assert json.loads(json.dumps(normalized, allow_nan=False)) == {
        "count": 4,
        "nested": [{"valid": True, "mean": 1.25}],
        "values": [1, 2],
    }
    assert (
        type(normalized["count"]) is int
        and type(normalized["nested"][0]["valid"]) is bool
    )


def test_empty_result_parquet_preserves_cast_types_and_column_names(tmp_path):
    from cleaning.pipeline import clean_frame

    frame = pd.DataFrame({"id": ["001"], "amount": ["-1"]})
    result = clean_frame(
        frame,
        [
            {"type": "cast", "columns": ["amount"], "dtype": "integer"},
            {"type": "range", "column": "amount", "min": 0},
        ],
    )
    path = tmp_path / "empty.parquet"
    write_table(result.data, path, "parquet")
    schema = pq.read_schema(path)
    assert schema.field("amount").type == pa.int64()
    assert schema.names == ["id", "amount"]
    loaded, count, _ = read_table(path, "parquet")
    assert count == 0 and loaded.empty and loaded.columns.tolist() == ["id", "amount"]


@pytest.mark.parametrize("file_format", ["csv", "xlsx", "parquet"])
def test_total_cell_budget_rejected_before_materializing_oversized_frame(
    tmp_path, monkeypatch, file_format
):
    import cleaning.tableio as module

    monkeypatch.setattr(module, "MAX_CELLS", 5)
    path = tmp_path / f"input.{file_format}"
    write_table(pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]}), path, file_format)
    with pytest.raises(InputError, match="5000000-cell"):
        read_table(path, file_format)
    # Sampling remains possible, so users can inspect an input that needs partitioning.
    if file_format == "parquet":
        with pytest.raises(InputError, match="5000000-cell"):
            preview_table(path, file_format, 1)
    else:
        assert preview_table(path, file_format, 1)["previewRowCount"] == 1
