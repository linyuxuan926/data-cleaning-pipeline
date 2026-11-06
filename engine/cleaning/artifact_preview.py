"""Bounded preview of trusted task artifacts; never accepts URLs or raw paths."""
import csv
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from openpyxl import load_workbook
import pyarrow.parquet as pq

from .models import StrictModel, InputError
from .tableio import json_value


class ArtifactPreviewRequest(StrictModel):
    objectKey: str = Field(min_length=5, max_length=512)
    format: Literal["csv", "xlsx", "parquet"]
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("objectKey")
    @classmethod
    def artifact_key(cls, value):
        if not value.startswith(("results/", "dirty/")) or ".." in value or "\\" in value or ":" in value:
            raise ValueError("Only task result and dirty-data objects are accepted")
        return value


def preview_artifact(path: Path, file_format: str, limit: int) -> dict:
    if file_format == "csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            columns = next(reader, [])
            rows = []
            for row in reader:
                rows.append(row)
                if len(rows) >= limit + 1:
                    break
    elif file_format == "xlsx":
        workbook = load_workbook(path, read_only=True, data_only=False)
        try:
            iterator = workbook.active.iter_rows(values_only=True)
            columns = [str(v) for v in next(iterator, ())]
            rows = []
            for row in iterator:
                rows.append(list(row))
                if len(rows) >= limit + 1:
                    break
        finally:
            workbook.close()
    else:
        source = pq.ParquetFile(path)
        columns = source.schema_arrow.names
        rows = []
        for batch in source.iter_batches(batch_size=limit + 1):
            for record in batch.to_pylist():
                rows.append([record[name] for name in columns])
                if len(rows) >= limit + 1:
                    break
            if len(rows) >= limit + 1:
                break
    if not columns:
        raise InputError("Artifact has no columns")
    sample = rows[:limit]
    return {
        "columns": [{"name": name, "dtype": "artifact", "nullCount": sum(row[i] is None or row[i] == "" for row in sample)} for i, name in enumerate(columns)],
        "rows": [{name: json_value(value) for name, value in zip(columns, row)} for row in sample],
        "rowCount": len(rows) if len(rows) <= limit else None,
        "previewRowCount": len(sample), "warnings": [],
    }
