"""Bounded readers and explicit, spreadsheet-safe writers for the supported formats."""

import csv
from datetime import date, datetime
from decimal import Decimal
import math
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

import numpy as np
from openpyxl import Workbook, load_workbook
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import budgets
from .models import (
    InputError,
    RESERVED_COLUMNS,
    validate_limits,
    validate_parse_options,
)
from .storage import MAX_FILE_BYTES
from .types import cast_scalar, inferred_type, typed_equal

MAX_ROWS = 500_000
MAX_COLUMNS = 200
MAX_CELLS = 5_000_000
csv.field_size_limit(16 * 1024 * 1024)


def _headers(values: list[Any], maximum: int = MAX_COLUMNS) -> list[str]:
    if not values or len(values) > maximum:
        raise InputError(f"Input must have between 1 and {maximum} columns")
    if not all(isinstance(c, str) and c.strip() and len(c) <= 128 for c in values):
        raise InputError(
            "Every header must be a nonempty string of at most 128 characters"
        )
    if len({c.strip() for c in values}) != len(values):
        raise InputError("Duplicate column headers are not supported")
    if set(values) & RESERVED_COLUMNS:
        raise InputError(
            "Input headers conflict with reserved dirty-row metadata columns"
        )
    return values


def _check_xlsx(path: Path, maximum: int = 512 * 1024 * 1024) -> None:
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10_000 or sum(i.file_size for i in entries) > maximum:
                raise InputError(
                    f"Expanded XLSX exceeds the configured {maximum}-byte limit"
                )
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= {
                i.filename for i in entries
            }:
                raise InputError("ZIP content is not an XLSX workbook")
            if any(i.file_size > 256 * 1024 * 1024 for i in entries):
                raise InputError("XLSX contains an oversized XML entry")
    except BadZipFile as exc:
        raise InputError("Input is not a valid XLSX archive") from exc


def _csv_options(path: Path, options: dict) -> dict:
    with path.open("rb") as source:
        sample = source.read(65536)
    if sample.startswith((b"PK\x03\x04", b"PAR1", b"%PDF", b"\x1f\x8b")):
        raise InputError("File content is binary and does not match CSV format")
    encoding = options["encoding"]
    if encoding == "auto":
        if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        else:
            import codecs

            for candidate in ("utf-8-sig", "gb18030"):
                try:
                    codecs.getincrementaldecoder(candidate)(errors="strict").decode(
                        sample, final=False
                    )
                    encoding = candidate
                    break
                except UnicodeError:
                    continue
            if encoding == "auto":
                raise InputError(
                    "Cannot detect CSV encoding; choose an explicit encoding"
                )
    import codecs

    text = codecs.getincrementaldecoder(encoding)(errors="strict").decode(
        sample, final=False
    )
    if "\x00" in text or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
        raise InputError("CSV contains unsupported binary/control characters")
    delimiter = options["delimiter"]
    if delimiter == "auto":
        try:
            delimiter = csv.Sniffer().sniff(text, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    return options | {"encoding": encoding, "delimiter": delimiter}


def _normalize_values(values: list[Any], markers: list[Any]) -> list[Any]:
    return [
        (
            None
            if is_missing(value)
            or any(typed_equal(value, marker) for marker in markers)
            else value
        )
        for value in values
    ]


def read_table(
    path: Path,
    file_format: str,
    limit: int | None = None,
    parse_options: dict | None = None,
    limits: dict | None = None,
) -> tuple[pd.DataFrame, int | None, list[str]]:
    configured = validate_limits(limits)
    # Module constants remain overridable in focused capacity tests.
    max_rows = min(MAX_ROWS, configured["maxRows"])
    max_columns = min(MAX_COLUMNS, configured["maxColumns"])
    max_cells = min(MAX_CELLS, configured["maxCells"])
    options = validate_parse_options(parse_options)
    if path.stat().st_size <= 0 or path.stat().st_size > min(
        MAX_FILE_BYTES, configured["maxFileBytes"]
    ):
        raise InputError("Input exceeds the configured file byte limit or is empty")
    if limit is not None and not 1 <= limit <= configured["previewRows"]:
        raise InputError("Preview exceeds configured previewRows")
    row_limit = max_rows + 1 if limit is None else limit + 1
    warnings: list[str] = []
    sheets: list[dict] = []
    logical_text = 0
    try:
        if file_format == "csv":
            options = _csv_options(path, options)
            with path.open("r", encoding=options["encoding"], newline="") as source:
                iterator = csv.reader(
                    source, strict=True, delimiter=options["delimiter"]
                )
                headers = _headers(next(iterator, []), max_columns)
                records = []
                for row_number, row in enumerate(iterator, 1):
                    if limit is None and row_number * len(headers) > max_cells:
                        raise InputError(
                            "Input exceeds the 5000000-cell memory budget (rows × columns)"
                        )
                    if not row:
                        row = [""] * len(headers)
                    if len(row) != len(headers):
                        raise InputError(
                            f"CSV data row {row_number} has {len(row)} fields; expected {len(headers)}"
                        )
                    logical_text += sum(budgets.text_bytes(value) for value in row)
                    budgets.check_text_bytes(logical_text)
                    records.append(_normalize_values(row, options["missingValues"]))
                    if len(records) >= row_limit:
                        break
            frame = pd.DataFrame(records, columns=headers, dtype=object)
            row_count = len(frame) if len(frame) < row_limit else None
        elif file_format == "xlsx":
            _check_xlsx(path, configured["maxExcelExpandedBytes"])
            workbook = load_workbook(
                path, read_only=True, data_only=False, keep_links=False
            )
            try:
                sheets = [
                    {
                        "name": s.title,
                        "rows": max(0, (s.max_row or 1) - 1),
                        "columns": s.max_column or 0,
                    }
                    for s in workbook.worksheets
                ]
                name = options.get("sheetName", workbook.worksheets[0].title)
                if name not in workbook.sheetnames:
                    raise InputError(f"Worksheet does not exist: {name}")
                sheet = workbook[name]
                options = options | {"sheetName": name}
                if (sheet.max_column or 0) > max_columns:
                    raise InputError(f"XLSX exceeds configured {max_columns} columns")
                iterator = sheet.iter_rows(values_only=True)
                headers = _headers(list(next(iterator, ())), max_columns)
                records = []
                for values in iterator:
                    if limit is None and (len(records) + 1) * len(headers) > max_cells:
                        raise InputError(
                            "Input exceeds the 5000000-cell memory budget (rows × columns)"
                        )
                    if len(values) > len(headers):
                        raise InputError("XLSX row has more cells than its header")
                    logical_text += sum(budgets.text_bytes(value) for value in values)
                    budgets.check_text_bytes(logical_text)
                    records.append(
                        _normalize_values(
                            list(values) + [None] * (len(headers) - len(values)),
                            options["missingValues"],
                        )
                    )
                    if len(records) >= row_limit:
                        break
                frame = pd.DataFrame(records, columns=headers, dtype=object)
                row_count = len(frame) if len(frame) < row_limit else None
                if len(workbook.worksheets) > 1:
                    warnings.append(
                        f"Only the selected XLSX worksheet is processed: {name}"
                    )
            finally:
                workbook.close()
        elif file_format == "parquet":
            with path.open("rb") as source:
                magic = source.read(4)
                source.seek(-4, 2)
                tail = source.read(4)
            if magic != b"PAR1" or tail != b"PAR1":
                raise InputError("File content does not match Parquet magic")
            parquet = pq.ParquetFile(path)
            headers = _headers(parquet.schema_arrow.names, max_columns)
            row_count = parquet.metadata.num_rows
            if (
                sum(
                    parquet.metadata.row_group(i).total_byte_size
                    for i in range(parquet.metadata.num_row_groups)
                )
                > 512 * 1024 * 1024
            ):
                raise InputError("Expanded Parquet exceeds the 512 MiB safety limit")
            if row_count > max_rows:
                raise InputError(f"Input exceeds configured {max_rows} data rows")
            if row_count * len(headers) > max_cells:
                raise InputError(
                    "Input exceeds the 5000000-cell memory budget (rows × columns)"
                )
            # Nested parquet values do not form scalar spreadsheet cells.
            if any(
                pa.types.is_nested(field.type)
                or pa.types.is_binary(field.type)
                or pa.types.is_large_binary(field.type)
                for field in parquet.schema_arrow
            ):
                raise InputError("Parquet must contain scalar, non-binary columns")
            # Preserve dictionaries during decoding, then count repeated strings
            # before expanding the current batch into Python objects.
            dictionary_columns = [
                field.name
                for field in parquet.schema_arrow
                if pa.types.is_string(field.type)
                or pa.types.is_large_string(field.type)
                or (
                    pa.types.is_dictionary(field.type)
                    and (
                        pa.types.is_string(field.type.value_type)
                        or pa.types.is_large_string(field.type.value_type)
                    )
                )
            ]
            parquet = pq.ParquetFile(path, read_dictionary=dictionary_columns)
            frames = []
            count = 0
            frame_memory = 0
            for batch in parquet.iter_batches(batch_size=min(1024, row_limit)):
                if limit is not None:
                    batch = batch.slice(0, max(0, row_limit - count))
                logical_text += _arrow_text_bytes(batch)
                budgets.check_text_bytes(logical_text)
                # Decode only the used batch values. Converting a dictionary to
                # pandas categorical directly would retain its entire row-group
                # dictionary once for every small batch.
                decoded = [
                    (
                        pc.dictionary_decode(array)
                        if pa.types.is_dictionary(array.type)
                        else array
                    )
                    for array in batch.columns
                ]
                part = pa.RecordBatch.from_arrays(decoded, names=headers).to_pandas()
                frame_memory += int(part.memory_usage(index=True, deep=True).sum())
                if frame_memory > budgets.MAX_FRAME_MEMORY_BYTES:
                    raise InputError("DataFrame exceeds the 512 MiB memory budget")
                frames.append(part)
                count += batch.num_rows
                if limit is not None and count >= row_limit:
                    break
            frame = (
                pd.concat(frames, ignore_index=True)
                if frames
                else pa.Table.from_batches([], schema=parquet.schema_arrow).to_pandas()
            )
            for name in frame.columns:
                values = _normalize_values(
                    frame[name].tolist(), options["missingValues"]
                )
                if any(
                    is_missing(a) != is_missing(b) for a, b in zip(frame[name], values)
                ):
                    frame[name] = pd.Series(values, index=frame.index, dtype=object)
        else:
            raise InputError("Unsupported input format")
    except InputError:
        raise
    except (
        UnicodeDecodeError,
        csv.Error,
        pd.errors.ParserError,
        ValueError,
        KeyError,
        IndexError,
        pa.ArrowException,
    ) as exc:
        raise InputError(f"Cannot read {file_format}: {str(exc)[:300]}") from exc
    if len(frame.columns) > max_columns:
        raise InputError("Input exceeds 200 columns")
    if len(frame) > max_rows:
        raise InputError(
            f"Input exceeds configured {max_rows} data rows (hard cap 500000)"
        )
    if limit is not None:
        frame = frame.iloc[:limit].copy()
    frame.index = pd.RangeIndex(len(frame))
    for field_id, dtype in options.get("fieldTypes", {}).items():
        position = int(field_id[1:])
        if position >= len(frame.columns):
            raise InputError(f"Unknown fieldTypes ID: {field_id}")
        name = frame.columns[position]
        values = []
        for row_index, value in enumerate(frame[name]):
            try:
                values.append(cast_scalar(value, dtype))
            except ValueError as exc:
                raise InputError(
                    f"Parse field {field_id} ({name}), data row {row_index + 1}: {exc}"
                ) from exc
        frame[name] = pd.Series(values, dtype=object)
    frame.attrs["parseOptions"] = options
    frame.attrs["sheets"] = sheets
    budgets.check_frame(frame)
    return frame, row_count, warnings


def _arrow_text_bytes(batch: pa.RecordBatch) -> int:
    total = 0
    for array in batch.columns:
        if pa.types.is_dictionary(array.type):
            if pa.types.is_string(array.type.value_type) or pa.types.is_large_string(
                array.type.value_type
            ):
                lengths = pc.binary_length(array.dictionary).to_pylist()
                for record in pc.value_counts(array.indices).to_pylist():
                    if record["values"] is not None:
                        total += (lengths[record["values"]] or 0) * record["counts"]
        elif pa.types.is_string(array.type) or pa.types.is_large_string(array.type):
            total += pc.sum(pc.binary_length(array)).as_py() or 0
        budgets.check_text_bytes(total)
    return total


def is_missing(value: Any) -> bool:
    return value is None or bool(pd.isna(value))


def json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, np.ndarray):
        return json_value(value.tolist())
    if isinstance(value, np.generic):
        return json_value(value.item())
    if is_missing(value):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def profile(frame: pd.DataFrame, ids: list[str] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "id": ids[index] if ids else f"f{index}",
            "name": str(name),
            "dtype": str(frame[name].dtype),
            "inferredType": inferred_type(frame[name]),
            "nullCount": int(frame[name].isna().sum()),
        }
        for index, name in enumerate(frame.columns)
    ]


def preview_table(
    path: Path,
    file_format: str,
    limit: int,
    parse_options: dict | None = None,
    limits: dict | None = None,
) -> dict[str, Any]:
    frame, row_count, warnings = read_table(
        path, file_format, limit, parse_options, limits
    )
    return {
        "columns": profile(frame),
        "rows": [
            {str(k): json_value(v) for k, v in row.items()}
            for row in frame.to_dict(orient="records")
        ],
        "rowCount": row_count,
        "previewRowCount": len(frame),
        "warnings": warnings,
        "parseOptions": frame.attrs["parseOptions"],
        "sheets": frame.attrs["sheets"],
    }


def _export_value(value: Any, safe_spreadsheet: bool) -> tuple[Any, bool]:
    if isinstance(value, Decimal) and value.is_finite():
        # A rounded native number must remain numeric in XLSX, including negatives.
        return value, False
    # JSON previews use null for infinity; data exports retain an explicit literal.
    if isinstance(value, (float, np.floating)) and math.isinf(value):
        value = str(value)
    else:
        value = json_value(value)
    # Cells beginning with formula trigger characters become literal text in spreadsheet exports.
    if (
        safe_spreadsheet
        and isinstance(value, str)
        and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@"))
    ):
        return "'" + value, True
    return value, False


class _LimitedCsvOutput:
    def __init__(self, stream):
        self.stream = stream
        self.bytes = 3  # UTF-8 BOM, emitted on the first write.

    def write(self, value: str) -> int:
        remaining = budgets.MAX_OUTPUT_BYTES - self.bytes
        if len(value) > remaining:
            raise InputError("Output exceeds the 256 MiB artifact limit")
        size = len(value.encode("utf-8"))
        if size > remaining:
            raise InputError("Output exceeds the 256 MiB artifact limit")
        self.bytes += size
        return self.stream.write(value)


def write_table(frame: pd.DataFrame, path: Path, file_format: str) -> int:
    budgets.check_frame(frame)
    try:
        escaped = _write_table(frame, path, file_format)
        if path.stat().st_size > budgets.MAX_OUTPUT_BYTES:
            raise InputError("Output exceeds the 256 MiB artifact limit")
        return escaped
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _write_table(frame: pd.DataFrame, path: Path, file_format: str) -> int:
    escaped = 0
    if file_format == "csv":
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(_LimitedCsvOutput(output), lineterminator="\n")
            headers = [_export_value(str(c), True) for c in frame.columns]
            escaped += sum(flag for _, flag in headers)
            writer.writerow([v for v, _ in headers])
            for row in frame.itertuples(index=False, name=None):
                values = [_export_value(v, True) for v in row]
                escaped += sum(flag for _, flag in values)
                writer.writerow([v for v, _ in values])
    elif file_format == "xlsx":
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Cleaned data")
        # Header strings can also contain spreadsheet formulas.
        sheet.append([_export_value(str(c), True)[0] for c in frame.columns])
        for row in frame.itertuples(index=False, name=None):
            values = [_export_value(v, True) for v in row]
            if any(isinstance(v, str) and len(v) > 32767 for v, _ in values):
                raise InputError(
                    "XLSX output cell exceeds Excel's 32767-character limit; choose CSV or Parquet"
                )
            escaped += sum(flag for _, flag in values)
            sheet.append([v for v, _ in values])
        workbook.save(path)
    elif file_format == "parquet":
        try:
            # Arrow preserves typed columns. Mixed native scalar types are rejected rather than silently stringified.
            frame.to_parquet(path, engine="pyarrow", index=False, compression="snappy")
        except (pa.ArrowException, ValueError, TypeError) as exc:
            raise InputError(
                "Parquet requires a consistent type per column; add cast rules or choose CSV/XLSX"
            ) from exc
    else:
        raise InputError("Unsupported output format")
    return escaped
