"""Logical budgets count repeated values, including shared XLSX/Parquet strings."""

from dataclasses import dataclass
import sys
from typing import Any

import pandas as pd

from .models import InputError

MAX_LOGICAL_TEXT_BYTES = 256 * 1024 * 1024
MAX_FRAME_MEMORY_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024


def text_bytes(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    return len(value) if value.isascii() else len(value.encode("utf-8"))


def check_text_bytes(size: int) -> None:
    if size > MAX_LOGICAL_TEXT_BYTES:
        raise InputError("Logical text payload exceeds the 256 MiB processing budget")


@dataclass
class FrameBudget:
    text: int
    memory: int

    def replace_text(self, old: Any, new: Any) -> None:
        prospective = self.text - text_bytes(old) + text_bytes(new)
        check_text_bytes(prospective)
        self.text = prospective

    def preflight_fill(self, count: int, value: Any) -> None:
        check_text_bytes(self.text + count * text_bytes(value))
        # Conservative: do not deduct the missing objects being replaced.
        if self.memory + count * (sys.getsizeof(value) + 8) > MAX_FRAME_MEMORY_BYTES:
            raise InputError(
                "Filled data would exceed the 512 MiB DataFrame memory budget"
            )


def check_frame(frame: pd.DataFrame) -> FrameBudget:
    memory = int(frame.memory_usage(index=True, deep=True).sum())
    if memory > MAX_FRAME_MEMORY_BYTES:
        raise InputError("DataFrame exceeds the 512 MiB memory budget")
    total = 0
    for name in frame.columns:
        series = frame[name]
        if isinstance(series.dtype, pd.CategoricalDtype):
            counts = series.value_counts(dropna=True)
            total += sum(
                text_bytes(value) * int(count) for value, count in counts.items()
            )
        elif pd.api.types.is_object_dtype(series.dtype) or pd.api.types.is_string_dtype(
            series.dtype
        ):
            for value in series:
                total += text_bytes(value)
                check_text_bytes(total)
        check_text_bytes(total)
    return FrameBudget(total, memory)
