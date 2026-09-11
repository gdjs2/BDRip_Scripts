"""Parse human-readable timestamps shared by video commands."""

from __future__ import annotations

import re
from typing import Any

from .validation import number


def parse_time(value: Any) -> float:
    """Parse nonnegative seconds, MM:SS, or HH:MM:SS, with fractional seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return number(value, "time")
    if not isinstance(value, str):
        raise ValueError("time must be seconds or HH:MM:SS")
    value = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return number(float(value), "time")
    parts = value.split(":")
    if (
        len(parts) not in (2, 3)
        or not all(re.fullmatch(r"\d+", part) for part in parts[:-1])
        or not re.fullmatch(r"\d+(?:\.\d+)?", parts[-1])
    ):
        raise ValueError("time must be nonnegative seconds, MM:SS, or HH:MM:SS")
    hours, minutes, seconds = ["0"] + parts if len(parts) == 2 else parts
    if int(minutes) >= 60 or float(seconds) >= 60:
        raise ValueError("minutes and seconds in a timestamp must be below 60")
    return number(int(hours) * 3600 + int(minutes) * 60 + float(seconds), "time")
