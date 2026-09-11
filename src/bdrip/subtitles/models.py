"""PGS subtitle conversion: models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PIL import Image


@dataclass
class VisualEvent:
    source_index: int
    start_ms: float
    end_ms: float
    rgba: Image.Image
    forced: bool


@dataclass
class OCRCandidate:
    text: str
    confidence: float
    variant: str
    quality: float
    lines: list[str]
    raw_items: list[dict[str, Any]]


@dataclass
class Cue:
    start_ms: float
    end_ms: float
    text: str
    confidence: float
    variant: str
    source_indices: list[int]
