"""PGS subtitle conversion: ocr."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np
from PIL import Image

from .models import OCRCandidate, VisualEvent

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CJK_PUNCT = "，。！？；：、…—～·《》〈〉「」『』【】（）“”‘’"
OPEN_PUNCT = "《〈「『【（“‘"
CLOSE_PUNCT = "，。！？；：、…—～·》〉」』】）”’"


def resize_rgba(rgba: np.ndarray, scale: float) -> np.ndarray:
    return cv2.resize(
        rgba,
        None,
        fx=scale,
        fy=scale,
        interpolation=cv2.INTER_LANCZOS4,
    )


def add_border(
    image: np.ndarray, border: int, value: int | tuple[int, int, int]
) -> np.ndarray:
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
        int(ys.min()) : int(ys.max()) + 1,
        int(xs.min()) : int(xs.max()) + 1,
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
                raise TypeError(
                    f"Unsupported PaddleOCR result type: {type(result)!r}"
                ) from exc

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
            char.isascii() and (char.isalnum() or char in " .,!?;:'\"-–—()[]/%+&")
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
        box = (
            normalize_box(boxes[index], index)
            if index < len(boxes)
            else normalize_box([], index)
        )
        raw_items.append({"text": text, "score": score, "box": box})

    rows = group_items_into_lines(raw_items)
    lines = [join_row_tokens([item["text"] for item in row], lang) for row in rows]
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
        return [OCRCandidate("", 0.0, "none", -1e9, [], []) for _ in events]

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
