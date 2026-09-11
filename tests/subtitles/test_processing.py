"""Subtitle text processing and resumable records work independently of OCR models."""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from bdrip.subtitles.cache import append_cache, event_cache_key, load_cache
from bdrip.subtitles.models import Cue, OCRCandidate, VisualEvent
from bdrip.subtitles.srt import merge_cues, render_srt, write_review_tsv


class SubtitleTests(unittest.TestCase):
    def test_repeated_display_updates_merge_and_keep_multiline_text(self):
        cues = [
            Cue(1000, 1600, "第一行", 0.9, "dark", [1]),
            Cue(1100, 2000, "第一行\n第二行", 0.8, "binary", [2]),
            Cue(2001, 2800, "第一行 第二行", 0.95, "dark", [3]),
            Cue(3000, 4000, "下一句", 0.9, "alpha", [4]),
        ]
        merged = merge_cues(cues, merge_gap=50, update_window=200)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].source_indices, [1, 2, 3])
        self.assertEqual(merged[0].confidence, 0.8)
        self.assertEqual(
            render_srt(merged),
            "1\n00:00:01,000 --> 00:00:02,800\n第一行\n第二行\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n下一句\n",
        )

    def test_distinct_overlapping_cues_are_trimmed(self):
        cues = [
            Cue(0, 1500, "one", 1, "dark", [1]),
            Cue(1000, 2000, "two", 1, "dark", [2]),
        ]
        merged = merge_cues(cues, merge_gap=50, update_window=200)
        self.assertEqual(merged[0].end_ms, merged[1].start_ms)

    def test_cache_round_trip_tolerates_interrupted_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            candidate = OCRCandidate(
                "字幕", 0.9, "dark", 1.5, ["字幕"], [{"text": "字幕"}]
            )
            self.assertEqual(load_cache(path), {})
            append_cache(path, "event", candidate)
            with path.open("a") as handle:
                handle.write('{"key":')
            self.assertEqual(load_cache(path), {"event": candidate})

    def test_cache_key_changes_with_timing_image_and_settings(self):
        event = VisualEvent(1, 1000, 2000, Image.new("RGBA", (2, 2), "white"), False)
        key = event_cache_key(event, "settings-a")
        self.assertNotEqual(key, event_cache_key(event, "settings-b"))
        event.start_ms = 1001
        self.assertNotEqual(key, event_cache_key(event, "settings-a"))
        event.start_ms = 1000
        event.rgba.putpixel((0, 0), (0, 0, 0, 0))
        self.assertNotEqual(key, event_cache_key(event, "settings-a"))

    def test_review_report_retains_confidence_and_source_indices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.tsv"
            write_review_tsv(path, [Cue(1000, 2000, "a\nb", 0.6, "dark", [1, 3])], 0.8)
            report = path.read_text(encoding="utf-8-sig")
            self.assertIn("0.6000\tYES\tdark\t1,3\ta / b", report)
