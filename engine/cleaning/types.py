"""Scalar conversions shared by explicit parse overrides and rule execution."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import math
import re
from typing import Any

import numpy as np
import pandas as pd


def missing(value: Any) -> bool:
    return value is None or bool(pd.isna(value))


def typed_equal(left: Any, right: Any) -> bool:
    if missing(left) or missing(right):
        return missing(left) and missing(right)
    if isinstance(left, (bool, np.bool_)) or isinstance(right, (bool, np.bool_)):
        return (
            isinstance(left, (bool, np.bool_))
            and isinstance(right, (bool, np.bool_))
            and bool(left) == bool(right)
        )
    if isinstance(left, (int, float, Decimal, np.number)) and isinstance(
        right, (int, float, Decimal, np.number)
    ):
        return Decimal(str(left)) == Decimal(str(right))
    return type(left) is type(right) and left == right


def cast_scalar(
    value: Any, dtype: str, date_format: str | None = None, precision: int | None = None
) -> Any:
    if missing(value):
        return None
    if dtype == "string":
        return str(value)
    if dtype == "boolean":
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True
            if lowered in {"false", "0", "no"}:
                return False
        elif isinstance(value, (int, float, Decimal, np.number)) and value in {0, 1}:
            return bool(value)
        raise ValueError("expected boolean true/false, 1/0 or yes/no")
    if dtype in {"integer", "number"}:
        if isinstance(value, (bool, np.bool_)):
            raise ValueError("boolean is not a numeric value")
        try:
            number = Decimal(str(value).strip())
            if not number.is_finite():
                raise ValueError("nonfinite number")
            if dtype == "integer":
                if (
                    number != number.to_integral_value()
                    or not -(2**63) <= number < 2**63
                ):
                    raise ValueError("expected exact signed int64")
                return int(number)
            result = float(number)
            if not math.isfinite(result):
                raise ValueError("expected finite number")
            if precision is not None:
                with localcontext() as context:
                    context.prec = max(
                        40,
                        len(number.as_tuple().digits)
                        + max(0, number.adjusted())
                        + precision
                        + 4,
                    )
                    number = number.quantize(
                        Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP
                    )
                return number
            return result
        except (InvalidOperation, OverflowError, TypeError) as exc:
            raise ValueError("expected finite numeric value") from exc
    if dtype == "datetime":
        if not isinstance(value, (str, date, datetime, pd.Timestamp)):
            raise ValueError("expected date string or datetime")
        try:
            if date_format and isinstance(value, str):
                value = datetime.strptime(value, date_format)
            converted = pd.to_datetime(value, errors="raise", utc=True)
            if pd.isna(converted):
                raise ValueError("nonmissing value converted to a missing datetime")
            return converted
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("value does not match datetime format") from exc
    raise ValueError("unsupported dtype")


def inferred_type(series: pd.Series) -> str:
    values = [value for value in series if not missing(value)]
    if not values:
        return "string"
    for dtype in ("boolean", "integer", "number", "datetime"):
        if dtype == "boolean" and not all(
            isinstance(v, (bool, np.bool_))
            or (
                isinstance(v, str)
                and v.strip().lower() in {"true", "false", "yes", "no"}
            )
            for v in values
        ):
            continue
        if dtype == "datetime" and not all(
            isinstance(v, (date, datetime, pd.Timestamp))
            or (isinstance(v, str) and re.match(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", v))
            for v in values
        ):
            continue
        try:
            for value in values:
                cast_scalar(value, dtype)
            return dtype
        except ValueError:
            pass
    return "string"
