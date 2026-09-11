"""Numeric validation shared by configuration and timestamp parsing."""

from __future__ import annotations

import math
from typing import Any


def number(
    value: Any, field: str, *, minimum: float = 0.0, positive: bool = False
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < minimum or (positive and value == minimum):
        relation = "greater than" if positive else "at least"
        raise ValueError(f"{field} must be finite and {relation} {minimum:g}")
    return value


def integer(value: Any, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer of at least {minimum}")
    return value
