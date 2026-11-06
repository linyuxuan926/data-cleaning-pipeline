"""Bounded tabular readers and incremental artifact writers for v3-disk."""
import csv
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from openpyxl import Workbook, load_workbook

from . import budgets
from .models import InputError, validate_limits, validate_parse_options
from .tableio import _headers, _csv_options, _check_xlsx, _normalize_values, _export_value, _LimitedCsvOutput
from .types import cast_scalar


class _CsvRecordLines:
    """Bound physical input while csv.reader joins quoted logical records."""
    def __init__(self, resource, limit): self.resource,self.limit,self.size=resource,limit,0
    def __iter__(self): return self
    def __next__(self):
        line=self.resource.readline(self.limit+1)
        if not line: raise StopIteration
        self.size+=len(line.encode("utf-8"))
        if self.size>self.limit: raise InputError("CSV logical record exceeds maxRecordBytes")
        return line
    def reset(self): self.size=0


class TableStream:
    def __init__(self, path, file_format, parse_options=None, limits=None, batch_rows=1000):
        self.path, self.format = Path(path), file_format
        self.limits = validate_limits(limits)
        self.options = validate_parse_options(parse_options)
        self.batch_rows = batch_rows
        self.max_record = (limits or {}).get("maxRecordBytes", 4 * 1024 * 1024)
        self.max_batch = (limits or {}).get("maxBatchBytes", 32 * 1024 * 1024)
        self.warnings, self.sheets = [], []
        self.headers, self.initial_types = [], {}
        self.initial_specs = {}
        self.resource = None
        self.csv_lines = self.previous_csv_limit = None

    def __enter__(self):
        try: return self._open()
        except Exception:
            self.__exit__()
            raise

    def _open(self):
        if not 0 < self.path.stat().st_size <= self.limits["maxFileBytes"]:
            raise InputError("Input exceeds configured file byte limit or is empty")
        if self.format == "csv":
            self.options = _csv_options(self.path, self.options)
            self.resource = self.path.open(encoding=self.options["encoding"], newline="")
            self.previous_csv_limit=csv.field_size_limit(self.max_record)
            self.csv_lines=_CsvRecordLines(self.resource,self.max_record)
            self.iterator = csv.reader(self.csv_lines, delimiter=self.options["delimiter"], strict=True)
            self.headers = _headers(next(self.iterator, []), self.limits["maxColumns"])
            self.csv_lines.reset()
            self.initial_types = {f"f{i}": "string" for i in range(len(self.headers))}
        elif self.format == "xlsx":
            _check_xlsx(self.path, self.limits["maxExcelExpandedBytes"])
            self.resource = load_workbook(self.path, read_only=True, data_only=False, keep_links=False)
            self.sheets = [{"name": s.title, "rows": max(0, (s.max_row or 1)-1), "columns": s.max_column or 0} for s in self.resource.worksheets]
            name = self.options.get("sheetName", self.resource.worksheets[0].title)
            if name not in self.resource.sheetnames: raise InputError("Worksheet does not exist: " + name)
            sheet = self.resource[name]
            if (sheet.max_column or 0) > self.limits["maxColumns"]: raise InputError("XLSX exceeds configured column budget")
            self.options["sheetName"] = name
            self.iterator = sheet.iter_rows(values_only=True)
            self.headers = _headers(list(next(self.iterator, ())), self.limits["maxColumns"])
            self.initial_types = {f"f{i}": "unknown" for i in range(len(self.headers))}
            if len(self.sheets) > 1: self.warnings.append("Only selected XLSX sheet processed: " + name)
        elif self.format == "parquet":
            self.resource = pq.ParquetFile(self.path)
            self.headers = _headers(self.resource.schema_arrow.names, self.limits["maxColumns"])
            if any(pa.types.is_nested(f.type) or pa.types.is_binary(f.type) or pa.types.is_large_binary(f.type) for f in self.resource.schema_arrow): raise InputError("Parquet must contain scalar non-binary fields")
            if self.resource.metadata.num_rows > self.limits["maxRows"] or self.resource.metadata.num_rows * len(self.headers) > self.limits["maxCells"]: raise InputError("Parquet exceeds row/cell budgets")
            if sum(self.resource.metadata.row_group(i).total_byte_size for i in range(self.resource.metadata.num_row_groups)) > self.limits["maxExcelExpandedBytes"]: raise InputError("Expanded Parquet exceeds byte budget")
            self.iterator = self._parquet_rows()
            for i, field in enumerate(self.resource.schema_arrow):
                dtype = field.type
                self.initial_types[f"f{i}"] = "integer" if pa.types.is_integer(dtype) else "decimal" if pa.types.is_decimal(dtype) or pa.types.is_floating(dtype) else "boolean" if pa.types.is_boolean(dtype) else "datetime" if pa.types.is_timestamp(dtype) or pa.types.is_date(dtype) else "string"
                if pa.types.is_decimal(dtype):
                    if dtype.precision > 38 or dtype.scale > 12 or dtype.scale < 0: raise InputError("Parquet decimal requires precision<=38 and scale 0..12")
                    self.initial_specs[f"f{i}"] = {"dtype": "decimal", "precision": dtype.precision, "scale": dtype.scale}
                elif pa.types.is_floating(dtype): self.initial_specs[f"f{i}"] = {"dtype":"decimal","nativeFloat":dtype.bit_width}
        else: raise InputError("Unsupported input format")
        for fid, dtype in self.options.get("fieldTypes", {}).items():
            if int(fid[1:]) >= len(self.headers): raise InputError("Unknown parse field: " + fid)
            self.initial_types[fid] = "decimal" if dtype == "number" else dtype
            self.initial_specs.pop(fid, None)
            if dtype == "number": self.initial_specs[fid] = {"dtype":"decimal","nativeFloat":64}
        return self

    def _parquet_rows(self):
        for batch in self.resource.iter_batches(batch_size=self.batch_rows):
            if batch.nbytes > self.max_batch: raise InputError("Parquet batch exceeds maxBatchBytes")
            # Dictionary columns may expand to much more than batch.nbytes.
            # Materialize one logical row, then let the byte-aware batcher bound it.
            for index in range(batch.num_rows): yield [column[index].as_py() for column in batch.columns]

    def batches(self):
        count = text = size = 0
        batch = []
        try:
            for values in self.iterator:
                if self.csv_lines: self.csv_lines.reset()
                count += 1
                if count > self.limits["maxRows"] or count * len(self.headers) > self.limits["maxCells"]: raise InputError("Input exceeds configured row/cell budget")
                values = list(values) or [""] * len(self.headers)
                if len(values) != len(self.headers): raise InputError(f"Source record {count} field count differs from header")
                row_size = sum(budgets.text_bytes(value) + 32 for value in values)
                if row_size > self.max_record: raise InputError(f"Source record {count} exceeds maxRecordBytes")
                text += sum(budgets.text_bytes(value) for value in values)
                budgets.check_text_bytes(text)
                values = _normalize_values(values, self.options["missingValues"])
                for fid, dtype in self.options.get("fieldTypes", {}).items():
                    try: values[int(fid[1:])] = cast_scalar(values[int(fid[1:])], dtype)
                    except ValueError as exc: raise InputError(f"Parse field {fid}, source record {count}: {exc}") from exc
                if batch and (len(batch) >= self.batch_rows or size + row_size > self.max_batch):
                    yield batch
                    batch, size = [], 0
                batch.append(values); size += row_size
            if batch: yield batch
        except (UnicodeError, csv.Error, pa.ArrowException) as exc:
            raise InputError(f"Cannot read {self.format}: {str(exc)[:200]}") from exc

    def __exit__(self, *_args):
        if self.resource is not None: self.resource.close()
        if self.previous_csv_limit is not None: csv.field_size_limit(self.previous_csv_limit)


class StreamWriter:
    def __init__(self, path, file_format, headers, types=None):
        self.path, self.format, self.headers = Path(path), file_format, headers
        self.types, self.escaped = types or {}, 0
        self.resource = self.sheet = self.writer = None

    def __enter__(self):
        if self.format == "csv":
            self.resource = self.path.open("w", encoding="utf-8-sig", newline="")
            self.writer = csv.writer(_LimitedCsvOutput(self.resource), lineterminator="\n")
            self._spreadsheet_row(self.headers)
        elif self.format == "xlsx":
            self.resource = Workbook(write_only=True)
            self.sheet = self.resource.create_sheet("Cleaned data")
            self._spreadsheet_row(self.headers)
        elif self.format == "parquet":
            fields = []
            for name in self.headers:
                spec = self.types.get(name, {})
                dtype = spec.get("dtype", "string")
                if dtype == "integer": arrow=pa.int64()
                elif dtype == "boolean": arrow=pa.bool_()
                elif dtype == "datetime": arrow=pa.timestamp("us",tz="UTC")
                elif dtype == "decimal" and spec.get("nativeFloat"): arrow=pa.float32() if spec["nativeFloat"]==32 else pa.float64()
                elif dtype == "decimal": arrow=pa.decimal128(spec.get("precision",38),spec.get("scale",2))
                else: arrow=pa.string()
                fields.append(pa.field(name, arrow))
            self.schema = pa.schema(fields)
            self.resource = pq.ParquetWriter(self.path, self.schema, compression="snappy")
        else: raise InputError("Unsupported output format")
        return self

    def _spreadsheet_row(self, row):
        values = [_export_value(value, True) for value in row]
        self.escaped += sum(flag for _, flag in values)
        if self.format == "csv": self.writer.writerow([value for value, _ in values])
        else:
            if any(isinstance(value, str) and len(value) > 32767 for value, _ in values): raise InputError("XLSX cell exceeds 32767 characters")
            self.sheet.append([value for value, _ in values])

    def write_rows(self, rows):
        if self.format in {"csv", "xlsx"}:
            for row in rows: self._spreadsheet_row(row)
            if self.format == "csv": self.resource.flush()
        else:
            converted = []
            for row in rows:
                record = {}
                for field, value in zip(self.schema, row):
                    if value is not None and pa.types.is_string(field.type): value = str(value)
                    if value is not None and pa.types.is_decimal(field.type) and isinstance(value,float):
                        value=Decimal(str(value))
                    record[field.name] = value
                converted.append(record)
            try: self.resource.write_table(pa.Table.from_pylist(converted, schema=self.schema))
            except (pa.ArrowException, TypeError, ValueError) as exc: raise InputError("Parquet output types conflict; use cast mark/null/drop or CSV") from exc
        if self.path.exists() and self.path.stat().st_size > budgets.MAX_OUTPUT_BYTES: raise InputError("Output exceeds 256 MiB artifact limit")

    def __exit__(self, exc_type, *_args):
        try:
            if self.format == "xlsx":
                if exc_type is None: self.resource.save(self.path)
                else:
                    self.sheet.close()
                    if self.sheet._writer: self.sheet._writer.cleanup()
                    self.resource.close()
            else: self.resource.close()
            if exc_type is None and self.path.stat().st_size > budgets.MAX_OUTPUT_BYTES: raise InputError("Output exceeds 256 MiB artifact limit")
        except Exception:
            self.path.unlink(missing_ok=True)
            raise
        if exc_type is not None: self.path.unlink(missing_ok=True)
