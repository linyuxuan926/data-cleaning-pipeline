from datetime import datetime
import json

from openpyxl import Workbook
import pandas as pd
import pytest

from cleaning.models import InputError, validate_parse_options
from cleaning.tableio import preview_table, read_table, write_table


@pytest.mark.parametrize(
    "encoding,delimiter", [("utf-8", ","), ("gb18030", ";"), ("utf-16", "\t")]
)
def test_actual_csv_detection_and_stable_fields(tmp_path, encoding, delimiter):
    path = tmp_path / "input.csv"
    path.write_bytes(
        delimiter.join(["邮编", "姓名", "日期"]).encode(encoding)
        + ("\n" + delimiter.join(["00101", "明日香", "2026-11-06"]) + "\n").encode(
            encoding
        )
    )
    # utf-16 requires one BOM for the entire text, not each appended block.
    if encoding == "utf-16":
        path.write_bytes(
            (
                delimiter.join(["邮编", "姓名", "日期"])
                + "\n"
                + delimiter.join(["00101", "明日香", "2026-11-06"])
                + "\n"
            ).encode(encoding)
        )
    preview = preview_table(path, "csv", 10)
    assert preview["parseOptions"]["delimiter"] == delimiter
    assert preview["parseOptions"]["encoding"] in {encoding, "utf-8-sig"}
    assert [c["id"] for c in preview["columns"]] == ["f0", "f1", "f2"]
    assert preview["columns"][2]["inferredType"] == "datetime"
    assert preview["rows"][0]["邮编"] == "00101"


def test_manual_csv_encoding_delimiter_markers_and_field_types(tmp_path):
    path = tmp_path / "input.csv"
    path.write_bytes(
        "id|name|amount|flag\n001|NA|1.25|yes\n002||2|no\n".encode("latin-1")
    )
    options = {
        "encoding": "latin-1",
        "delimiter": "|",
        "missingValues": ["NA"],
        "fieldTypes": {"f0": "string", "f2": "number", "f3": "boolean"},
    }
    preview = preview_table(path, "csv", 20, options)
    assert preview["rows"][0] == {
        "id": "001",
        "name": None,
        "amount": 1.25,
        "flag": True,
    }
    assert preview["rows"][1]["name"] == ""
    assert preview["parseOptions"]["fieldTypes"] == options["fieldTypes"]
    with pytest.raises(InputError, match="data row 2"):
        read_table(
            path, "csv", parse_options={**options, "fieldTypes": {"f1": "integer"}}
        )
    with pytest.raises(InputError, match="Unknown fieldTypes"):
        read_table(
            path, "csv", parse_options={**options, "fieldTypes": {"f9": "string"}}
        )


def test_actual_xlsx_sheet_selection_names_dimensions_and_type_override(tmp_path):
    path = tmp_path / "book.xlsx"
    workbook = Workbook()
    workbook.active.title = "First"
    workbook.active.append(["wrong"])
    workbook.active.append(["first"])
    sheet = workbook.create_sheet("数据")
    sheet.append(["id", "active"])
    sheet.append(["001", "yes"])
    sheet.append(["002", "no"])
    workbook.save(path)
    result = preview_table(
        path, "xlsx", 20, {"sheetName": "数据", "fieldTypes": {"f1": "boolean"}}
    )
    assert result["parseOptions"]["sheetName"] == "数据"
    assert result["rows"][0] == {"id": "001", "active": True}
    assert result["sheets"] == [
        {"name": "First", "rows": 1, "columns": 1},
        {"name": "数据", "rows": 2, "columns": 2},
    ]
    with pytest.raises(InputError, match="does not exist"):
        read_table(path, "xlsx", parse_options={"sheetName": "absent"})
    with pytest.raises(InputError, match="Expanded XLSX"):
        read_table(path, "xlsx", limits={"maxExcelExpandedBytes": 1})


@pytest.mark.parametrize("file_format", ["csv", "xlsx", "parquet"])
def test_actual_content_must_match_declared_format(tmp_path, file_format):
    path = tmp_path / "not-real"
    path.write_bytes(b"PK\x03\x04this is not an excel workbook")
    with pytest.raises((InputError, ValueError)):
        read_table(path, file_format)


@pytest.mark.parametrize(
    "name", ["_record_id", "_quality_errors", "_quality_valid", "_field", "_error_code"]
)
def test_all_quality_metadata_headers_are_reserved(tmp_path, name):
    path = tmp_path / "input.csv"
    path.write_text(f"id,{name}\n1,value\n", encoding="utf-8")
    with pytest.raises(InputError, match="reserved"):
        read_table(path, "csv")


def test_configured_limits_apply_beyond_bounded_preview(tmp_path):
    path = tmp_path / "input.csv"
    path.write_text("a,b\n1,x\n2,y\n3,z\n", encoding="utf-8")
    preview = preview_table(path, "csv", 1, limits={"maxRows": 2})
    assert preview["rowCount"] is None
    with pytest.raises(InputError, match="configured 2 data rows"):
        read_table(path, "csv", limits={"maxRows": 2})
    with pytest.raises(InputError, match="columns"):
        preview_table(path, "csv", 1, limits={"maxColumns": 1})
    with pytest.raises(InputError, match="configured previewRows"):
        preview_table(path, "csv", 2, limits={"previewRows": 1})
    with pytest.raises(InputError, match="configured file"):
        preview_table(path, "csv", 1, limits={"maxFileBytes": 2})


@pytest.mark.parametrize(
    "options",
    [
        {"encoding": []},
        {"delimiter": []},
        {"fieldTypes": {"f0": "bad"}},
        {"fieldTypes": {"bad": "string"}},
        {"missingValues": [{}]},
        {"sheetName": ""},
        {"unknown": 1},
    ],
)
def test_invalid_parse_options_fail_with_actionable_input_error(options):
    with pytest.raises(InputError):
        validate_parse_options(options)


def test_datetime_parse_override_rejects_nonmissing_nat_and_explicit_empty_string(
    tmp_path,
):
    path = tmp_path / "input.csv"
    for value in ("NaT", '""'):
        path.write_text(f"date\n{value}\n", encoding="utf-8")
        with pytest.raises(InputError, match="Parse field f0.*data row 1"):
            read_table(
                path,
                "csv",
                parse_options={"missingValues": [], "fieldTypes": {"f0": "datetime"}},
            )
