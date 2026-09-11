"""PGS subtitle conversion: cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

from .models import OCRCandidate, VisualEvent


def pipeline_signature(args: argparse.Namespace) -> str:
    settings = {
        "lang": args.lang,
        "ocr_version": args.ocr_version,
        "variants": args.variants,
        "scale": args.scale,
        "border": args.border,
        "alpha_threshold": args.alpha_threshold,
        "rec_score_threshold": args.rec_score_threshold,
        "det_threshold": args.det_threshold,
        "det_box_threshold": args.det_box_threshold,
        "det_unclip_ratio": args.det_unclip_ratio,
    }
    return hashlib.sha256(
        json.dumps(settings, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def event_cache_key(event: VisualEvent, signature: str) -> str:
    rgba = np.asarray(event.rgba.convert("RGBA"))
    digest = hashlib.sha256()
    digest.update(signature.encode("ascii"))
    digest.update(f"{event.start_ms:.3f}:{event.end_ms:.3f}".encode("ascii"))
    digest.update(str(rgba.shape).encode("ascii"))
    digest.update(rgba.tobytes())
    return digest.hexdigest()


def load_cache(path: Path) -> dict[str, OCRCandidate]:
    cache: dict[str, OCRCandidate] = {}
    if not path.is_file():
        return cache

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                cache[record["key"]] = OCRCandidate(
                    text=record["text"],
                    confidence=float(record["confidence"]),
                    variant=record["variant"],
                    quality=float(record["quality"]),
                    lines=list(record.get("lines") or []),
                    raw_items=list(record.get("raw_items") or []),
                )
            except Exception:
                print(
                    f"Warning: ignored invalid cache line {line_number} in {path}",
                    file=sys.stderr,
                )
    return cache


def append_cache(path: Path, key: str, candidate: OCRCandidate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "key": key,
        "text": candidate.text,
        "confidence": candidate.confidence,
        "variant": candidate.variant,
        "quality": candidate.quality,
        "lines": candidate.lines,
        "raw_items": candidate.raw_items,
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
