"""PGS subtitle conversion: cli."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any, Iterable, Sequence

from tqdm import tqdm

from .models import Cue, OCRCandidate


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


def chunked(sequence: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(sequence), size):
        yield sequence[start : start + size]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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
        from .cache import append_cache, event_cache_key, load_cache, pipeline_signature
        from .ocr import initialize_ocr, process_event_batch, save_debug_images
        from .pgs import (
            build_visual_events,
            import_pgs_components,
            materialize_display_sets,
        )
        from .srt import merge_cues, render_srt, write_review_tsv

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
