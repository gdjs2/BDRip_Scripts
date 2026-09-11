#!/usr/bin/env python3
"""
PGS/SUP -> SRT using PaddleOCR 3.x.

Pipeline
--------
1. Parse Blu-ray PGS display sets with sup2srt.
2. Carry palettes/objects across display-set updates.
3. Reconstruct subtitle bitmaps and preserve PGS timing.
4. Generate subtitle-friendly image variants.
5. Run PaddleOCR on CPU or GPU, in batches.
6. Select the best OCR variant by confidence and language plausibility.
7. Merge duplicate/incremental PGS display updates.
8. Write SRT, a confidence-review TSV, and a resumable JSONL cache.

Recommended commands
--------------------
Chinese, NVIDIA GPU:
    python pgs_to_srt_paddle.py Chinese.sup Chinese.srt \
        --lang ch --device gpu:0 --batch-size 16 \
        --variants dark,binary,alpha --debug-dir debug_chinese

English:
    python pgs_to_srt_paddle.py English.sup English.srt \
        --lang en --device gpu:0 --batch-size 16

Traditional Chinese:
    python pgs_to_srt_paddle.py ChineseTraditional.sup output.srt \
        --lang chinese_cht --device gpu:0

The first run downloads PaddleOCR models. Subsequent runs use the local cache.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import cv2
import numpy as np
from PIL import Image
from tqdm import tqdm


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def comma_list(value: str) -> list[str]:
    result = [part.strip().lower() for part in value.split(",") if part.strip()]
    allowed = {"dark", "binary", "alpha"}
    invalid = sorted(set(result) - allowed)
    if invalid:
        raise argparse.ArgumentTypeError(
            f"Unknown variants: {', '.join(invalid)}; allowed: dark,binary,alpha"
        )
    if not result:
        raise argparse.ArgumentTypeError("at least one variant is required")
    return result


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def unit_float(value: str) -> float:
    number = float(value)
    if not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a Blu-ray PGS .sup subtitle to SRT using PaddleOCR.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="Input Blu-ray PGS .sup file.")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help="Output SRT path. Defaults to INPUT with a .srt extension.",
    )
    parser.add_argument(
        "--lang",
        default="ch",
        help=(
            "PaddleOCR language: ch=Simplified Chinese, "
            "chinese_cht=Traditional Chinese, en=English."
        ),
    )
    parser.add_argument(
        "--ocr-version",
        default="PP-OCRv5",
        choices=["PP-OCRv6", "PP-OCRv5", "PP-OCRv4"],
        help="PaddleOCR model generation.",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Inference device, for example cpu or gpu:0.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=12,
        help="Number of subtitle events processed in one OCR batch.",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=8,
        help="Paddle CPU inference threads; ignored by most GPU work.",
    )
    parser.add_argument(
        "--variants",
        type=comma_list,
        default=["dark", "binary", "alpha"],
        help="Comma-separated preprocessing variants.",
    )
    parser.add_argument(
        "--scale",
        type=positive_float,
        default=3.0,
        help="Bitmap upscale factor before OCR.",
    )
    parser.add_argument(
        "--border",
        type=int,
        default=24,
        help="Border added around the processed subtitle bitmap.",
    )
    parser.add_argument(
        "--alpha-threshold",
        type=int,
        default=8,
        help="Alpha value above which a PGS pixel counts as visible.",
    )
    parser.add_argument(
        "--rec-score-threshold",
        type=unit_float,
        default=0.30,
        help="PaddleOCR recognition result filter.",
    )
    parser.add_argument(
        "--review-threshold",
        type=unit_float,
        default=0.80,
        help="Cues below this confidence are marked for manual review.",
    )
    parser.add_argument(
        "--det-threshold",
        type=unit_float,
        default=0.20,
        help="Text detector pixel threshold.",
    )
    parser.add_argument(
        "--det-box-threshold",
        type=unit_float,
        default=0.35,
        help="Text detector box threshold.",
    )
    parser.add_argument(
        "--det-unclip-ratio",
        type=positive_float,
        default=1.6,
        help="Expansion ratio for detected text boxes.",
    )
    parser.add_argument(
        "--default-duration",
        type=positive_float,
        default=3000.0,
        metavar="MS",
        help="Fallback duration for a final cue without a later display set.",
    )
    parser.add_argument(
        "--minimum-duration",
        type=positive_float,
        default=100.0,
        metavar="MS",
        help="Minimum generated cue duration.",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=250.0,
        metavar="MS",
        help="Maximum gap for merging identical adjacent OCR cues.",
    )
    parser.add_argument(
        "--update-window",
        type=float,
        default=900.0,
        metavar="MS",
        help=(
            "Window for merging incremental PGS updates when one recognized "
            "text is contained in the next."
        ),
    )
    parser.add_argument(
        "--forced-only",
        action="store_true",
        help="OCR only composition objects carrying the PGS forced flag.",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        help="JSONL OCR cache. Defaults to OUTPUT.ocr-cache.jsonl.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable loading and writing the resumable OCR cache.",
    )
    parser.add_argument(
        "--review-file",
        type=Path,
        help="Confidence TSV. Defaults to OUTPUT.review.tsv.",
    )
    parser.add_argument(
        "--debug-dir",
        type=Path,
        help="Save source/preprocessed images for low-confidence cues.",
    )
    parser.add_argument(
        "--debug-all",
        action="store_true",
        help="Save debug images for every cue rather than low-confidence cues.",
    )
    parser.add_argument(
        "--no-bom",
        action="store_true",
        help="Write plain UTF-8 instead of UTF-8 with BOM.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing output SRT.",
    )
    parser.add_argument(
        "--verbose-errors",
        action="store_true",
        help="Print Python tracebacks when a cue fails.",
    )
    return parser


# ---------------------------------------------------------------------------
# PGS parsing and state materialization
# ---------------------------------------------------------------------------

def import_pgs_components():
    try:
        from sup2srt.sup_parser import DisplaySet, SupParser
        from sup2srt.sup_decoder import decode_display_set
    except ImportError as exc:
        raise RuntimeError(
            "sup2srt is not installed. Run:\n"
            "  python -m pip install sup2srt==0.1.3"
        ) from exc
    return DisplaySet, SupParser, decode_display_set


def materialize_display_sets(display_sets: Sequence[Any], DisplaySet: Any) -> list[Any]:
    """
    PGS may reuse an earlier palette or object in a later PCS. The upstream
    decoder expects each display set to contain its own PDS/ODS, so carry the
    most recently defined resources forward.
    """
    palette_cache: dict[int, Any] = {}
    object_cache: dict[int, Any] = {}
    latest_palette: Any | None = None
    output: list[Any] = []

    for ds in display_sets:
        for pds in ds.pds_list:
            palette_cache[pds.palette_id] = pds
            latest_palette = pds

        for ods in ds.ods_list:
            object_cache[ods.object_id] = ods

        if ds.pcs is None or not ds.pcs.objects:
            output.append(ds)
            continue

        palette = palette_cache.get(ds.pcs.palette_id, latest_palette)
        objects = [
            object_cache[obj.object_id]
            for obj in ds.pcs.objects
            if obj.object_id in object_cache
        ]

        effective = DisplaySet(
            pcs=ds.pcs,
            wds=ds.wds,
            pds_list=[palette] if palette is not None else [],
            ods_list=objects,
        )
        output.append(effective)

    return output


def select_forced_objects(ds: Any, DisplaySet: Any) -> Any:
    if ds.pcs is None:
        return ds

    forced_objects = [obj for obj in ds.pcs.objects if obj.forced]
    if not forced_objects:
        return DisplaySet(pcs=replace(ds.pcs, objects=[]))

    wanted = {obj.object_id for obj in forced_objects}
    return DisplaySet(
        pcs=replace(ds.pcs, objects=forced_objects),
        wds=ds.wds,
        pds_list=ds.pds_list,
        ods_list=[ods for ods in ds.ods_list if ods.object_id in wanted],
    )


def compose_decoded_images(decoded: Sequence[Any]) -> Image.Image | None:
    if not decoded:
        return None

    left = min(item.x for item in decoded)
    top = min(item.y for item in decoded)
    right = max(item.x + item.image.width for item in decoded)
    bottom = max(item.y + item.image.height for item in decoded)

    if right <= left or bottom <= top:
        return None

    canvas = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
    for item in sorted(decoded, key=lambda d: (d.y, d.x)):
        canvas.alpha_composite(item.image.convert("RGBA"), (item.x - left, item.y - top))

    return crop_alpha(canvas, threshold=1)


def crop_alpha(image: Image.Image, threshold: int) -> Image.Image | None:
    rgba = np.asarray(image.convert("RGBA"))
    alpha = rgba[:, :, 3]
    ys, xs = np.where(alpha > threshold)
    if len(xs) == 0:
        return None

    x1, x2 = int(xs.min()), int(xs.max()) + 1
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    return image.crop((x1, y1, x2, y2))


def build_visual_events(
    display_sets: Sequence[Any],
    decode_display_set: Any,
    DisplaySet: Any,
    *,
    forced_only: bool,
    default_duration_ms: float,
    minimum_duration_ms: float,
) -> tuple[list[VisualEvent], int]:
    events: list[VisualEvent] = []
    decode_failures = 0

    pcs_indices = [i for i, ds in enumerate(display_sets) if ds.pcs is not None]

    for position, ds_index in enumerate(pcs_indices):
        ds = display_sets[ds_index]
        if not ds.pcs.objects:
            continue

        effective = select_forced_objects(ds, DisplaySet) if forced_only else ds
        if not effective.pcs.objects:
            continue

        start_ms = float(effective.pcs.pts_ms)

        if position + 1 < len(pcs_indices):
            next_ds = display_sets[pcs_indices[position + 1]]
            end_ms = float(next_ds.pcs.pts_ms)
        else:
            end_ms = start_ms + default_duration_ms

        if end_ms - start_ms < minimum_duration_ms:
            end_ms = start_ms + minimum_duration_ms

        decoded = decode_display_set(effective)
        rgba = compose_decoded_images(decoded)
        if rgba is None:
            decode_failures += 1
            continue

        is_forced = any(obj.forced for obj in effective.pcs.objects)
        events.append(
            VisualEvent(
                source_index=ds_index,
                start_ms=start_ms,
                end_ms=end_ms,
                rgba=rgba,
                forced=is_forced,
            )
        )

    return events, decode_failures


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def resize_rgba(rgba: np.ndarray, scale: float) -> np.ndarray:
    return cv2.resize(
        rgba,
        None,
        fx=scale,
        fy=scale,
        interpolation=cv2.INTER_LANCZOS4,
    )


def add_border(image: np.ndarray, border: int, value: int | tuple[int, int, int]) -> np.ndarray:
    if border <= 0:
        return image
    return cv2.copyMakeBorder(
        image,
        border,
        border,
        border,
        border,
        cv2.BORDER_CONSTANT,
        value=value,
    )


def preprocess_variants(
    image: Image.Image,
    *,
    variants: Sequence[str],
    scale: float,
    border: int,
    alpha_threshold: int,
) -> dict[str, np.ndarray]:
    rgba = np.asarray(image.convert("RGBA"))
    alpha = rgba[:, :, 3]

    ys, xs = np.where(alpha > alpha_threshold)
    if len(xs) == 0:
        return {}

    rgba = rgba[
        int(ys.min()): int(ys.max()) + 1,
        int(xs.min()): int(xs.max()) + 1,
    ]
    rgba = resize_rgba(rgba, scale)

    rgb = rgba[:, :, :3].astype(np.float32)
    a = rgba[:, :, 3:4].astype(np.float32) / 255.0

    # Original colors on black. For typical white/yellow subtitle fills this
    # removes the transparent area and makes the dark outline unobtrusive.
    dark_rgb = np.clip(rgb * a, 0, 255).astype(np.uint8)

    outputs: dict[str, np.ndarray] = {}

    if "dark" in variants:
        dark = add_border(dark_rgb, border, (0, 0, 0))
        outputs["dark"] = cv2.cvtColor(dark, cv2.COLOR_RGB2BGR)

    if "binary" in variants:
        gray = cv2.cvtColor(dark_rgb, cv2.COLOR_RGB2GRAY)
        # Bright subtitle fill -> black glyphs; transparent/dark outline -> white.
        _, binary = cv2.threshold(
            gray,
            0,
            255,
            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
        )
        # Repair tiny holes without aggressively joining neighboring characters.
        kernel = np.ones((2, 2), dtype=np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        binary = add_border(binary, border, 255)
        outputs["binary"] = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)

    if "alpha" in variants:
        alpha_mask = 255 - rgba[:, :, 3]
        alpha_mask = add_border(alpha_mask, border, 255)
        outputs["alpha"] = cv2.cvtColor(alpha_mask, cv2.COLOR_GRAY2BGR)

    return outputs


# ---------------------------------------------------------------------------
# OCR result parsing, sorting and cleanup
# ---------------------------------------------------------------------------

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CJK_PUNCT = "，。！？；：、…—～·《》〈〉「」『』【】（）“”‘’"
OPEN_PUNCT = "《〈「『【（“‘"
CLOSE_PUNCT = "，。！？；：、…—～·》〉」』】）”’"


def result_to_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        payload = result
    else:
        payload = getattr(result, "json", None)
        if callable(payload):
            payload = payload()
        if payload is None:
            try:
                payload = dict(result)
            except Exception as exc:
                raise TypeError(f"Unsupported PaddleOCR result type: {type(result)!r}") from exc

    if not isinstance(payload, dict):
        raise TypeError(f"Unexpected PaddleOCR JSON payload: {type(payload)!r}")

    inner = payload.get("res", payload)
    if not isinstance(inner, dict):
        raise TypeError("PaddleOCR result does not contain a dictionary result.")
    return inner


def normalize_box(box: Any, fallback_index: int) -> tuple[float, float, float, float]:
    arr = np.asarray(box, dtype=float)
    if arr.size >= 4:
        if arr.ndim == 1:
            x1, y1, x2, y2 = arr.flat[:4]
            return float(x1), float(y1), float(x2), float(y2)

        points = arr.reshape(-1, 2)
        return (
            float(points[:, 0].min()),
            float(points[:, 1].min()),
            float(points[:, 0].max()),
            float(points[:, 1].max()),
        )

    # Preserve OCR-return order when no geometry is available.
    y = float(fallback_index * 100)
    return 0.0, y, 1.0, y + 1.0


def group_items_into_lines(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not items:
        return []

    items = sorted(
        items,
        key=lambda item: (
            (item["box"][1] + item["box"][3]) / 2,
            item["box"][0],
        ),
    )

    heights = [max(1.0, item["box"][3] - item["box"][1]) for item in items]
    median_height = float(np.median(heights))
    tolerance = max(6.0, median_height * 0.65)

    rows: list[dict[str, Any]] = []
    for item in items:
        yc = (item["box"][1] + item["box"][3]) / 2
        best_row = None
        best_distance = math.inf

        for row in rows:
            distance = abs(yc - row["center"])
            if distance <= tolerance and distance < best_distance:
                best_row = row
                best_distance = distance

        if best_row is None:
            rows.append({"center": yc, "items": [item]})
        else:
            best_row["items"].append(item)
            best_row["center"] = float(
                np.mean(
                    [
                        (row_item["box"][1] + row_item["box"][3]) / 2
                        for row_item in best_row["items"]
                    ]
                )
            )

    rows.sort(key=lambda row: row["center"])
    for row in rows:
        row["items"].sort(key=lambda item: item["box"][0])

    return [row["items"] for row in rows]


def is_ascii_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "_%+'")


def join_row_tokens(tokens: Sequence[str], lang: str) -> str:
    tokens = [token.strip() for token in tokens if token and token.strip()]
    if not tokens:
        return ""

    if lang == "en":
        text = " ".join(tokens)
        text = re.sub(r"\s+([,.;:!?%)\]])", r"\1", text)
        text = re.sub(r"([(\[])\s+", r"\1", text)
        text = re.sub(r"\s+(['’]s)\b", r"\1", text, flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", text).strip()

    output = tokens[0]
    for token in tokens[1:]:
        previous = output[-1] if output else ""
        current = token[0] if token else ""

        # Keep a space only between separately detected ASCII words.
        needs_space = is_ascii_word_char(previous) and is_ascii_word_char(current)
        output += (" " if needs_space else "") + token

    return output


def clean_text(text: str, lang: str) -> str:
    text = text.replace("\r", "")
    text = text.replace("\u200b", "")
    text = text.replace("\ufeff", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if lang != "en":
        # Remove OCR-inserted spaces around CJK characters and punctuation.
        text = re.sub(
            r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])",
            "",
            text,
        )
        text = re.sub(
            rf" +([{re.escape(CLOSE_PUNCT)}])",
            r"\1",
            text,
        )
        text = re.sub(
            rf"([{re.escape(OPEN_PUNCT)}]) +",
            r"\1",
            text,
        )
        text = re.sub(
            r"(?<=[\u3400-\u9fff]) +(?=[A-Za-z0-9])",
            "",
            text,
        )
        text = re.sub(
            r"(?<=[A-Za-z0-9]) +(?=[\u3400-\u9fff])",
            "",
            text,
        )
        text = text.replace("...", "……")

    return text.strip()


def language_plausibility(text: str, lang: str) -> float:
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return 0.0

    if lang == "en":
        good = sum(
            char.isascii()
            and (char.isalnum() or char in " .,!?;:'\"-–—()[]/%+&")
            for char in chars
        )
        return good / len(chars)

    good = 0
    for char in chars:
        if CJK_RE.match(char):
            good += 1
        elif char.isascii() and (char.isalnum() or char in " .,!?:;'-/%+&"):
            good += 1
        elif char in CJK_PUNCT:
            good += 1
    return good / len(chars)


def candidate_quality(text: str, confidence: float, lang: str) -> float:
    if not text:
        return -1e9

    plausibility = language_plausibility(text, lang)
    visible_length = len(re.sub(r"\s+", "", text))
    length_bonus = min(visible_length, 40) * 0.0015

    # Penalize excessive isolated ASCII garbage in Chinese OCR.
    garbage_penalty = 0.0
    if lang != "en":
        isolated_ascii = re.findall(r"(?<![A-Za-z0-9])[A-Za-z](?![A-Za-z0-9])", text)
        garbage_penalty = min(0.20, len(isolated_ascii) * 0.025)

    return confidence + 0.18 * plausibility + length_bonus - garbage_penalty


def parse_ocr_result(result: Any, variant: str, lang: str) -> OCRCandidate:
    payload = result_to_dict(result)

    texts = list(payload.get("rec_texts") or [])
    scores = list(payload.get("rec_scores") or [])
    boxes = payload.get("rec_boxes")
    if boxes is None:
        boxes = payload.get("rec_polys")
    if boxes is None:
        boxes = []

    raw_items: list[dict[str, Any]] = []
    for index, text in enumerate(texts):
        text = str(text or "").strip()
        if not text:
            continue

        score = float(scores[index]) if index < len(scores) else 0.0
        box = normalize_box(boxes[index], index) if index < len(boxes) else normalize_box([], index)
        raw_items.append({"text": text, "score": score, "box": box})

    rows = group_items_into_lines(raw_items)
    lines = [
        join_row_tokens([item["text"] for item in row], lang)
        for row in rows
    ]
    lines = [line for line in lines if line]
    text = clean_text("\n".join(lines), lang)

    weights = [max(1, len(item["text"])) for item in raw_items]
    if raw_items and sum(weights):
        confidence = float(
            sum(item["score"] * weight for item, weight in zip(raw_items, weights))
            / sum(weights)
        )
    else:
        confidence = 0.0

    return OCRCandidate(
        text=text,
        confidence=confidence,
        variant=variant,
        quality=candidate_quality(text, confidence, lang),
        lines=lines,
        raw_items=raw_items,
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# OCR processing
# ---------------------------------------------------------------------------

def initialize_ocr(args: argparse.Namespace):
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(
            "PaddleOCR is not installed. Install PaddlePaddle first, then run:\n"
            "  python -m pip install paddleocr==3.7.0"
        ) from exc

    return PaddleOCR(
        lang=args.lang,
        ocr_version=args.ocr_version,
        device=args.device,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_det_limit_side_len=64,
        text_det_limit_type="min",
        text_det_thresh=args.det_threshold,
        text_det_box_thresh=args.det_box_threshold,
        text_det_unclip_ratio=args.det_unclip_ratio,
        text_rec_score_thresh=args.rec_score_threshold,
        cpu_threads=args.cpu_threads,
    )


def process_event_batch(
    ocr: Any,
    events: Sequence[VisualEvent],
    args: argparse.Namespace,
) -> list[OCRCandidate]:
    images: list[np.ndarray] = []
    metadata: list[tuple[int, str]] = []

    for event_index, event in enumerate(events):
        variants = preprocess_variants(
            event.rgba,
            variants=args.variants,
            scale=args.scale,
            border=args.border,
            alpha_threshold=args.alpha_threshold,
        )
        for variant, image in variants.items():
            images.append(image)
            metadata.append((event_index, variant))

    if not images:
        return [
            OCRCandidate("", 0.0, "none", -1e9, [], [])
            for _ in events
        ]

    results = ocr.predict(
        images,
        text_rec_score_thresh=args.rec_score_threshold,
    )
    if len(results) != len(images):
        raise RuntimeError(
            f"PaddleOCR returned {len(results)} results for {len(images)} images."
        )

    candidates_by_event: list[list[OCRCandidate]] = [[] for _ in events]
    for result, (event_index, variant) in zip(results, metadata):
        candidate = parse_ocr_result(result, variant, args.lang)
        candidates_by_event[event_index].append(candidate)

    selected: list[OCRCandidate] = []
    for candidates in candidates_by_event:
        valid = [candidate for candidate in candidates if candidate.text]
        if not valid:
            selected.append(OCRCandidate("", 0.0, "none", -1e9, [], []))
        else:
            selected.append(max(valid, key=lambda candidate: candidate.quality))

    return selected


def save_debug_images(
    debug_dir: Path,
    event: VisualEvent,
    candidate: OCRCandidate,
    args: argparse.Namespace,
) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    prefix = (
        f"{event.source_index:05d}_"
        f"{int(round(event.start_ms)):010d}_"
        f"{candidate.confidence:.3f}"
    )

    event.rgba.save(debug_dir / f"{prefix}_source.png")
    variants = preprocess_variants(
        event.rgba,
        variants=args.variants,
        scale=args.scale,
        border=args.border,
        alpha_threshold=args.alpha_threshold,
    )
    for name, image in variants.items():
        cv2.imwrite(str(debug_dir / f"{prefix}_{name}.png"), image)

    (debug_dir / f"{prefix}.json").write_text(
        json.dumps(
            {
                "start_ms": event.start_ms,
                "end_ms": event.end_ms,
                "text": candidate.text,
                "confidence": candidate.confidence,
                "variant": candidate.variant,
                "quality": candidate.quality,
                "raw_items": candidate.raw_items,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Cue merging and SRT output
# ---------------------------------------------------------------------------

def normalized_for_merge(text: str) -> str:
    return re.sub(r"[\s\u200b]+", "", text).strip()


def merge_cues(cues: Sequence[Cue], merge_gap: float, update_window: float) -> list[Cue]:
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def chunked(sequence: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(sequence), size):
        yield sequence[start: start + size]


def main() -> int:
    args = build_parser().parse_args()

    if args.batch_size < 1:
        print("Error: --batch-size must be at least 1.", file=sys.stderr)
        return 2
    if args.border < 0:
        print("Error: --border cannot be negative.", file=sys.stderr)
        return 2
    if not 0 <= args.alpha_threshold <= 255:
        print("Error: --alpha-threshold must be between 0 and 255.", file=sys.stderr)
        return 2

    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return 2
    if input_path.suffix.lower() != ".sup":
        print("Error: input must be a .sup PGS subtitle.", file=sys.stderr)
        return 2

    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else input_path.with_suffix(".srt")
    )
    if output_path.exists() and not args.overwrite:
        print(
            f"Error: output already exists: {output_path}\n"
            "Use --overwrite to replace it.",
            file=sys.stderr,
        )
        return 2

    cache_path = (
        args.cache.expanduser().resolve()
        if args.cache
        else output_path.with_suffix(".ocr-cache.jsonl")
    )
    review_path = (
        args.review_file.expanduser().resolve()
        if args.review_file
        else output_path.with_suffix(".review.tsv")
    )
    debug_dir = args.debug_dir.expanduser().resolve() if args.debug_dir else None

    try:
        DisplaySet, SupParser, decode_display_set = import_pgs_components()

        print(f"Input:          {input_path}")
        print(f"Output:         {output_path}")
        print(f"Language:       {args.lang}")
        print(f"OCR model:      {args.ocr_version}")
        print(f"Device:         {args.device}")
        print(f"Variants:       {', '.join(args.variants)}")
        print()

        print("Parsing PGS...")
        parsed = SupParser(input_path).parse()
        materialized = materialize_display_sets(parsed, DisplaySet)
        events, decode_failures = build_visual_events(
            materialized,
            decode_display_set,
            DisplaySet,
            forced_only=args.forced_only,
            default_duration_ms=args.default_duration,
            minimum_duration_ms=args.minimum_duration,
        )

        print(f"Display sets:   {len(parsed)}")
        print(f"Visual events:  {len(events)}")
        print(f"Decode failures:{decode_failures:>5}")
        if not events:
            raise RuntimeError("No decodable subtitle events were found.")

        signature = pipeline_signature(args)
        cache = {} if args.no_cache else load_cache(cache_path)
        cached_count = 0
        candidates: list[OCRCandidate | None] = [None] * len(events)
        pending_indices: list[int] = []

        for index, event in enumerate(events):
            key = event_cache_key(event, signature)
            if key in cache:
                candidates[index] = cache[key]
                cached_count += 1
            else:
                pending_indices.append(index)

        print(f"Cached OCR:     {cached_count}")
        print(f"Pending OCR:    {len(pending_indices)}")
        print()

        ocr = initialize_ocr(args) if pending_indices else None

        batches = list(chunked(pending_indices, args.batch_size))
        progress = tqdm(batches, desc="PaddleOCR batches", unit="batch")

        for index_batch in progress:
            event_batch = [events[index] for index in index_batch]
            try:
                selected = process_event_batch(ocr, event_batch, args)
            except Exception:
                if args.verbose_errors:
                    traceback.print_exc()
                raise

            for event_index, candidate in zip(index_batch, selected):
                candidates[event_index] = candidate
                event = events[event_index]

                if not args.no_cache:
                    key = event_cache_key(event, signature)
                    append_cache(cache_path, key, candidate)

                if debug_dir and (
                    args.debug_all
                    or candidate.confidence < args.review_threshold
                    or not candidate.text
                ):
                    save_debug_images(debug_dir, event, candidate, args)

        raw_cues: list[Cue] = []
        empty_ocr = 0

        for event, candidate in zip(events, candidates):
            assert candidate is not None
            if not candidate.text:
                empty_ocr += 1
                continue

            raw_cues.append(
                Cue(
                    start_ms=event.start_ms,
                    end_ms=event.end_ms,
                    text=candidate.text,
                    confidence=candidate.confidence,
                    variant=candidate.variant,
                    source_indices=[event.source_index],
                )
            )

        cues = merge_cues(
            raw_cues,
            merge_gap=args.merge_gap,
            update_window=args.update_window,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        encoding = "utf-8" if args.no_bom else "utf-8-sig"
        output_path.write_text(render_srt(cues), encoding=encoding, newline="\n")
        write_review_tsv(review_path, cues, args.review_threshold)

        low_confidence = sum(cue.confidence < args.review_threshold for cue in cues)

        print()
        print("Finished.")
        print(f"Raw OCR cues:       {len(raw_cues)}")
        print(f"Merged SRT cues:    {len(cues)}")
        print(f"Empty OCR events:   {empty_ocr}")
        print(f"Low-confidence:     {low_confidence}")
        print(f"SRT:                {output_path}")
        print(f"Review TSV:         {review_path}")
        if not args.no_cache:
            print(f"Resume cache:       {cache_path}")
        if debug_dir:
            print(f"Debug images:       {debug_dir}")

        return 0

    except KeyboardInterrupt:
        print("\nInterrupted. Completed OCR remains in the cache.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\nError: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.verbose_errors:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
