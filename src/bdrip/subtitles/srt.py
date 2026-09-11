"""PGS subtitle conversion: srt."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Sequence

from .models import Cue


def normalized_for_merge(text: str) -> str:
    return re.sub(r"[\s\u200b]+", "", text).strip()


def merge_cues(
    cues: Sequence[Cue], merge_gap: float, update_window: float
) -> list[Cue]:
    merged: list[Cue] = []

    for cue in cues:
        if not cue.text.strip():
            continue

        if not merged:
            merged.append(cue)
            continue

        previous = merged[-1]
        gap = cue.start_ms - previous.end_ms
        prev_norm = normalized_for_merge(previous.text)
        curr_norm = normalized_for_merge(cue.text)

        # Same subtitle across multiple PGS display updates.
        if prev_norm == curr_norm and gap <= merge_gap:
            previous.end_ms = max(previous.end_ms, cue.end_ms)
            previous.confidence = min(previous.confidence, cue.confidence)
            previous.source_indices.extend(cue.source_indices)
            continue

        # Incremental update, e.g. first line appears and a second line is
        # added a fraction of a second later.
        near_update = cue.start_ms - previous.start_ms <= update_window
        if near_update and prev_norm and curr_norm:
            if prev_norm in curr_norm:
                previous.text = cue.text
                previous.end_ms = max(previous.end_ms, cue.end_ms)
                previous.confidence = min(previous.confidence, cue.confidence)
                previous.variant = cue.variant
                previous.source_indices.extend(cue.source_indices)
                continue

            if curr_norm in prev_norm:
                previous.end_ms = max(previous.end_ms, cue.end_ms)
                previous.confidence = min(previous.confidence, cue.confidence)
                previous.source_indices.extend(cue.source_indices)
                continue

        # Prevent overlap after rounding or malformed PGS timestamps.
        if previous.end_ms > cue.start_ms:
            previous.end_ms = max(previous.start_ms + 1.0, cue.start_ms)

        merged.append(cue)

    return merged


def ms_to_srt(ms: float) -> str:
    total_ms = max(0, int(round(ms)))
    milliseconds = total_ms % 1000
    total_seconds = total_ms // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def render_srt(cues: Sequence[Cue]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n"
            f"{ms_to_srt(cue.start_ms)} --> {ms_to_srt(cue.end_ms)}\n"
            f"{cue.text}"
        )
    return "\n\n".join(blocks) + "\n"


def write_review_tsv(path: Path, cues: Sequence[Cue], threshold: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(
            [
                "cue",
                "start",
                "end",
                "confidence",
                "needs_review",
                "variant",
                "source_display_sets",
                "text",
            ]
        )
        for index, cue in enumerate(cues, start=1):
            writer.writerow(
                [
                    index,
                    ms_to_srt(cue.start_ms),
                    ms_to_srt(cue.end_ms),
                    f"{cue.confidence:.4f}",
                    "YES" if cue.confidence < threshold else "",
                    cue.variant,
                    ",".join(map(str, cue.source_indices)),
                    cue.text.replace("\n", " / "),
                ]
            )
