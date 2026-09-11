"""CRF search correctness and PyAV smoke tests.

Run with ``uv run python -m unittest discover -s tests -v``.
The integration fixture is generated through PyAV; no media files or FFmpeg
executable are needed.
"""

from __future__ import annotations

import argparse
import csv
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import av

from scripts import crf_search


ROOT = Path(__file__).resolve().parents[1]


def sampling(**overrides):
    return {"count": 10, "seconds": 45.0, "start": 0.0, "end": None,
            "stress_starts": [], **overrides}


def policy(**overrides):
    return {"initial_crf": 17.5, "qp_target": 19.5,
            "qp_limit": 20.0, "normal_bitrate_mbps": [8.0, 15.0],
            "emergency_bitrate_mbps": 28.0, **overrides}


def result(duration, video_bytes, qp, *, kind="representative", start=0.0):
    return {"sample": {"kind": kind, "start": start, "duration": duration},
            "duration": duration, "video_bytes": video_bytes,
            "frames": max(1, round(duration * 24)), "qp": qp}


def make_media(path, *, audio=True, video=True, borders=False):
    """Write one second of moving image and optional silent audio."""
    width, height = (128, 96) if borders else (96, 64)
    with av.open(str(path), mode="w") as output:
        video_stream = None
        audio_stream = None
        if video:
            video_stream = output.add_stream("ffv1", rate=12)
            video_stream.width = width
            video_stream.height = height
            video_stream.pix_fmt = "yuv420p"
            video_stream.time_base = Fraction(1, 12)
        if audio:
            audio_stream = output.add_stream("pcm_s16le", rate=12000)
            audio_stream.layout = "mono"
        for index in range(12):
            if video_stream is not None:
                frame = av.VideoFrame(width, height, "yuv420p")
                for plane_index, plane in enumerate(frame.planes):
                    if borders:
                        values = bytearray([16 if plane_index == 0 else 128]) * plane.buffer_size
                        if plane_index == 0:
                            for y in range(16, height - 16):
                                for x in range(16, width - 16):
                                    values[y * plane.line_size + x] = 64 + (index * 17 + x * 3 + y * 5) % 150
                        plane.update(values)
                    else:
                        plane.update(bytes(
                            (index * 17 + offset // 11 + plane_index * 81) % 256
                            for offset in range(plane.buffer_size)
                        ))
                frame.pts = index
                frame.time_base = Fraction(1, 12)
                for packet in video_stream.encode(frame):
                    output.mux(packet)
            if audio_stream is not None:
                sound = av.AudioFrame(format="s16", layout="mono", samples=1000)
                sound.sample_rate = 12000
                sound.pts = index * 1000
                sound.time_base = Fraction(1, 12000)
                sound.planes[0].update(bytes(sound.planes[0].buffer_size))
                for packet in audio_stream.encode(sound):
                    output.mux(packet)
        for stream in (video_stream, audio_stream):
            if stream is not None:
                for packet in stream.encode(None):
                    output.mux(packet)


class PercentileTests(unittest.TestCase):
    def test_linear_quantile_is_order_independent(self):
        self.assertAlmostEqual(crf_search.percentile([4, 1, 3, 2], 0.95), 3.85)

    def test_single_value_and_endpoints(self):
        self.assertEqual(crf_search.percentile([7], 0.95), 7)
        self.assertEqual(crf_search.percentile([3, 1, 2], 0), 1)
        self.assertEqual(crf_search.percentile([3, 1, 2], 1), 3)

    def test_empty_values_rejected(self):
        with self.assertRaises(ValueError):
            crf_search.percentile([], 0.95)


class SamplingTests(unittest.TestCase):
    def assert_ranges_valid(self, samples, start, end):
        representative = sorted(
            (item for item in samples if item["kind"] == "representative"),
            key=lambda item: item["start"],
        )
        self.assertTrue(representative)
        previous_end = start
        for item in representative:
            self.assertGreater(item["duration"], 0)
            self.assertGreaterEqual(item["start"] + 1e-9, previous_end)
            self.assertLessEqual(item["start"] + item["duration"], end + 1e-9)
            previous_end = item["start"] + item["duration"]

    def test_short_source_reduces_sampling_without_overlap(self):
        samples = crf_search.select_samples(15, sampling())
        self.assertLessEqual(len(samples), 10)
        self.assert_ranges_valid(samples, 0, 15)

    def test_subsecond_source_is_still_sampled(self):
        self.assert_ranges_valid(crf_search.select_samples(0.02, sampling()), 0, 0.02)

    def test_selected_interval_is_respected(self):
        samples = crf_search.select_samples(
            600, sampling(count=4, seconds=20, start=30, end=570))
        self.assertEqual(len(samples), 4)
        self.assert_ranges_valid(samples, 30, 570)
        self.assertEqual(samples, crf_search.select_samples(
            600, sampling(count=4, seconds=20, start=30, end=570)))

    def test_invalid_sampling_is_rejected(self):
        for overrides in ({"count": 0}, {"seconds": 0}, {"start": -1},
                          {"start": 20, "end": 10}, {"start": 100}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                crf_search.select_samples(100, sampling(**overrides))


class SummaryTests(unittest.TestCase):
    def test_bitrate_is_duration_weighted_and_excludes_stress_scenes(self):
        samples = [result(1, 1_000_000, {"I": 16, "P": 17, "B": 18}),
                   result(3, 6_000_000, {"I": 18, "P": 19, "B": 20}, start=5),
                   result(10, 100_000_000, {"I": 30}, kind="stress", start=30)]
        summary = crf_search.summarize_trial(18, samples, policy())
        self.assertEqual(summary["crf"], 18)
        self.assertAlmostEqual(summary["bitrate_mbps"], 14.0)
        self.assertAlmostEqual(summary["qp95"], 19.9)
        self.assertEqual(summary["stress_qp"], 30)
        self.assertFalse(summary["qp_pass"])

    def test_missing_frame_types_are_valid(self):
        summary = crf_search.summarize_trial(
            18, [result(1, 500_000, {"I": 18})], policy())
        self.assertEqual(summary["qp95"], 18)
        self.assertTrue(summary["qp_pass"])

    def test_emergency_bitrate_does_not_override_qp_priority(self):
        summary = crf_search.summarize_trial(
            18, [result(1, 5_000_000, {"I": 18})], policy())
        self.assertEqual(summary["bitrate_mbps"], 40)
        self.assertTrue(summary["qp_pass"])

    def test_stress_scene_can_fail_an_otherwise_passing_trial(self):
        summary = crf_search.summarize_trial(18, [
            result(1, 500_000, {"I": 18}),
            result(1, 500_000, {"I": 20}, kind="stress"),
        ], policy())
        self.assertEqual(summary["qp95"], 18)
        self.assertEqual(summary["worst_qp"], 18)
        self.assertEqual(summary["stress_qp"], 20)
        self.assertFalse(summary["qp_pass"])

    def test_incomplete_metrics_cannot_produce_recommendations(self):
        for item in (result(0, 1000, {"I": 18}), result(1, 0, {"I": 18}),
                     result(1, 1000, {}), result(1, 1000, {"I": math.nan})):
            with self.subTest(item=item), self.assertRaises(ValueError):
                crf_search.summarize_trial(18, [item], policy())


class SearchTests(unittest.TestCase):
    def run_search(self, qp_function, *, max_trials=12, **policy_overrides):
        observations = []
        search_policy = policy(**policy_overrides)

        def evaluate(crf):
            observations.append(crf)
            return crf_search.summarize_trial(crf, [
                result(1, round(10_000_000 * 2 ** (-crf / 6)), {"I": qp_function(crf)}),
            ], search_policy)

        rows, summary = crf_search.auto_search(evaluate, search_policy, {
            "min_crf": 14.0, "max_crf": 23.0,
            "precision": 0.1, "max_trials": max_trials,
        })
        self.assertEqual(len(observations), len(set(observations)))
        self.assertLessEqual(len(observations), max_trials)
        self.assertTrue(all(14 <= crf <= 23 for crf in observations))
        self.assertEqual([row["crf"] for row in rows], sorted(observations))
        passing = [row["crf"] for row in rows if row["qp_pass"]]
        self.assertEqual(summary["recommended_crf"], max(passing, default=None))
        self.assertEqual(summary["trials"], len(observations))
        return rows, summary

    def test_brackets_and_refines_fractional_crf_threshold(self):
        rows, summary = self.run_search(lambda crf: crf + 1.67)
        self.assertEqual(summary["recommended_crf"], 17.8)
        self.assertEqual(summary["reason"], "precision_reached")
        self.assertTrue(any(row["crf"] == 17.9 and not row["qp_pass"] for row in rows))

    def test_finds_upper_bound_when_every_trial_passes(self):
        _, summary = self.run_search(lambda crf: 10)
        self.assertEqual(summary["recommended_crf"], 23)
        self.assertEqual(summary["reason"], "upper_bound_passes")

    def test_finds_passing_lower_bound(self):
        _, summary = self.run_search(lambda crf: crf + 5.5)
        self.assertEqual(summary["recommended_crf"], 14)
        self.assertEqual(summary["reason"], "precision_reached")

    def test_no_feasible_crf_is_reported_without_inventing_one(self):
        rows, summary = self.run_search(lambda crf: 30)
        self.assertIsNone(summary["recommended_crf"])
        self.assertFalse(summary["verified"])
        self.assertEqual(min(row["crf"] for row in rows), 14)
        self.assertEqual(summary["reason"], "no_passing_crf_in_bounds")

    def test_trial_budget_preserves_only_verified_recommendations(self):
        _, summary = self.run_search(lambda crf: crf + 1, max_trials=1)
        self.assertEqual(summary["recommended_crf"], 17.5)
        self.assertEqual(summary["reason"], "trial_limit")

    def test_nonmonotonic_observations_stop_refinement(self):
        _, summary = self.run_search(lambda crf: 25 if crf < 17 else 15)
        self.assertEqual(summary["reason"], "nonmonotonic_observations")
        self.assertEqual(summary["recommended_crf"], 18.5)

    def test_upper_bound_need_not_be_a_precision_multiple(self):
        def evaluate(crf):
            return {"crf": crf, "qp95": 10, "qp_pass": True}

        rows, summary = crf_search.auto_search(evaluate, policy(), {
            "min_crf": 14.0, "max_crf": 20.03, "precision": 0.1, "max_trials": 12,
        })
        self.assertEqual(summary["recommended_crf"], 20.03)
        self.assertIn(20.03, [row["crf"] for row in rows])
        self.assertEqual(summary["reason"], "upper_bound_passes")

    def test_non_round_lower_bound_is_sampled_once_and_search_terminates(self):
        low = 25.118768723996514
        high = 26.945218159464417
        threshold = 25.32228644328755
        observations = []

        def evaluate(crf):
            observations.append(crf)
            return {"crf": crf, "qp95": crf, "qp_pass": crf <= threshold}

        rows, summary = crf_search.auto_search(
            evaluate, policy(initial_crf=(low + high) / 2, qp_target=threshold),
            {"min_crf": low, "max_crf": high, "precision": 0.5, "max_trials": 12},
        )
        self.assertEqual(observations.count(low), 1)
        self.assertEqual(len(observations), len(set(observations)))
        self.assertEqual(summary["recommended_crf"], low)
        self.assertEqual(summary["reason"], "precision_reached")
        self.assertTrue(all(low <= row["crf"] <= high for row in rows))


class SweepArgumentTests(unittest.TestCase):
    def test_fractional_range_includes_exact_endpoint(self):
        self.assertEqual(crf_search.parse_crf_range("17:17.3:0.1"), [17, 17.1, 17.2, 17.3])

    def test_range_does_not_invent_off_grid_endpoint(self):
        self.assertEqual(crf_search.parse_crf_range("17:17.25:0.1"), [17, 17.1, 17.2])

    def test_invalid_ranges_are_rejected(self):
        for value in ("18:17:.1", "17:18:0", "nan:18:1", "17:inf:1", "0:51:.001", "17:18"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                crf_search.parse_crf_range(value)


class CropResolutionTests(unittest.TestCase):
    def test_manual_and_disabled_crop_do_not_run_detection(self):
        for requested, expected, dimensions in (
            ("96:64:16:16", "96:64:16:16", (96, 64)),
            (None, None, (128, 96)),
        ):
            with self.subTest(crop=requested):
                config = crf_search.load_config()
                config["video"]["crop"] = requested
                with mock.patch.object(crf_search, "detect_crop") as detector:
                    resolved = crf_search.resolve_crop(
                        Path("unused.mkv"), {"width": 128, "height": 96}, config, [],
                    )
                detector.assert_not_called()
                self.assertEqual(resolved["crop"], expected)
                self.assertEqual((resolved["width"], resolved["height"]), dimensions)

    def test_automatic_resolution_uses_detected_rectangle(self):
        config = crf_search.load_config()
        self.assertEqual(config["video"]["crop"], "auto")
        samples = [{"kind": "representative", "start": 0.0, "duration": 1.0}]
        detected = {"mode": "auto", "crop": "96:64:16:16", "width": 96,
                    "height": 64, "reason": "detected black borders"}
        with mock.patch.object(crf_search, "detect_crop", return_value=detected) as detector:
            resolved = crf_search.resolve_crop(
                Path("unused.mkv"), {"width": 128, "height": 96}, config, samples,
            )
        detector.assert_called_once()
        self.assertEqual(resolved["crop"], "96:64:16:16")
        self.assertEqual((resolved["width"], resolved["height"]), (96, 64))


class PyAVIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="crf-search-tests-")
        cls.directory = Path(cls.temporary.name)
        cls.source = cls.directory / "video-and-audio.mkv"
        cls.video_only = cls.directory / "video-only.mkv"
        cls.audio_only = cls.directory / "audio-only.mkv"
        cls.bordered = cls.directory / "bordered.mkv"
        make_media(cls.source)
        make_media(cls.video_only, audio=False)
        make_media(cls.audio_only, video=False)
        make_media(cls.bordered, borders=True)
        cls.config = cls.directory / "config.json"
        cls.config.write_text(json.dumps({
            "sampling": {"count": 1, "seconds": 1},
            "codecs": {
                "x264": {"preset": "ultrafast", "params": {
                    "threads": 1, "bframes": 2, "rc-lookahead": 4,
                    "aq-mode": 1, "deblock": "-3,-3",
                }},
                "x265": {"preset": "ultrafast", "params": {
                    "pools": "none", "frame-threads": 1, "bframes": 2,
                    "rc-lookahead": 4, "deblock": "-3,-3",
                }},
            },
        }), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def cli(self, source, output, *extra):
        return subprocess.run([
            sys.executable, str(ROOT / "scripts" / "crf_search.py"), str(source),
            "--config", str(self.config), "--output-dir", str(output),
            "--crf-values", "16,23", *extra,
        ], cwd=ROOT, text=True, capture_output=True, timeout=60)

    def test_both_codecs_sweep_reports_settings_and_resumes_without_encoding(self):
        output = self.directory / "both-codecs"
        completed = self.cli(self.source, output, "--codec", "both")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads((output / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(report["state"], "complete")
        self.assertIsNone(report["crop_detection"]["crop"])
        self.assertEqual(set(report["codecs"]), {"x264", "x265"})
        for codec in ("x264", "x265"):
            with self.subTest(codec=codec):
                self.assertEqual(report["config"]["codecs"][codec]["preset"], "ultrafast")
                self.assertEqual(report["config"]["codecs"][codec]["params"]["deblock"], "-3,-3")
                analysis = report["codecs"][codec]
                self.assertEqual([row["crf"] for row in analysis["rows"]], [16, 23])
                for row in analysis["rows"]:
                    self.assertIsInstance(row["bitrate_mbps"], (int, float))
                    self.assertGreater(row["bitrate_mbps"], 0)
                    self.assertTrue(math.isfinite(row["qp95"]))
                    self.assertEqual(row["measurement"], "sample encode")
                    self.assertEqual(len(row["samples"]), 1)
                    sample = row["samples"][0]
                    self.assertEqual(sample["frames"], 12)
                    self.assertAlmostEqual(sample["duration"], 1, places=2)
                    self.assertAlmostEqual(row["bitrate_mbps"],
                                           sample["video_bytes"] * 8 / sample["duration"] / 1e6)
                    self.assertFalse(sample["cached"])
                    options = sample["encoder_options"]
                    self.assertEqual(options["preset"], "ultrafast")
                    self.assertEqual(float(options["crf"]), row["crf"])
                    self.assertIn("deblock=-3,-3", options[f"{codec}-params"])
        with (output / "summary.csv").open(encoding="utf-8", newline="") as handle:
            table = list(csv.DictReader(handle))
        self.assertEqual(len(table), 4)
        self.assertEqual({row["codec"] for row in table}, {"x264", "x265"})
        log_times = {path: path.stat().st_mtime_ns for path in (output / "logs").glob("*.log")}
        self.assertEqual(len(log_times), 4)
        with (mock.patch.object(crf_search.subprocess, "Popen",
                                side_effect=AssertionError("Cached samples must not start workers")),
              mock.patch.object(crf_search.CONSOLE, "quiet", True)):
            self.assertEqual(crf_search.main([
                str(self.source), "--config", str(self.config), "--output-dir", str(output),
                "--codec", "both", "--crf-values", "16,23",
            ]), 0)
        resumed = json.loads((output / "results.json").read_text(encoding="utf-8"))
        self.assertTrue(all(sample["cached"] for analysis in resumed["codecs"].values()
                            for row in analysis["rows"] for sample in row["samples"]))
        self.assertEqual(log_times, {path: path.stat().st_mtime_ns for path in log_times})

    def test_audio_is_excluded_from_video_measurement(self):
        reports = []
        for source in (self.source, self.video_only):
            output = self.directory / (source.stem + "-comparison")
            completed = self.cli(source, output, "--codec", "x264", "--crf-values", "18")
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            reports.append(json.loads((output / "results.json").read_text(encoding="utf-8")))
        rows = [report["codecs"]["x264"]["rows"][0] for report in reports]
        self.assertEqual(rows[0]["bitrate_mbps"], rows[1]["bitrate_mbps"])
        self.assertEqual(rows[0]["qp95"], rows[1]["qp95"])
        self.assertEqual(rows[0]["samples"][0]["frames"], 12)

    def test_audio_only_source_fails_clearly(self):
        completed = self.cli(self.audio_only, self.directory / "no-video", "--codec", "x264")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("video", completed.stderr.lower())
        self.assertNotIn("Traceback", completed.stderr)

    def test_automatic_crop_is_detected_once_and_shared_by_all_trials(self):
        output = self.directory / "auto-cropped"
        with (mock.patch.object(crf_search, "detect_crop", wraps=crf_search.detect_crop) as detector,
              mock.patch.object(crf_search.CONSOLE, "quiet", True)):
            exit_code = crf_search.main([
                str(self.bordered), "--config", str(self.config), "--output-dir", str(output),
                "--codec", "both", "--crf-values", "16,23",
            ])
        self.assertEqual(exit_code, 0)
        detector.assert_called_once()
        report = json.loads((output / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(report["state"], "complete")
        self.assertEqual(report["config"]["video"]["crop"], "auto")
        self.assertEqual(report["crop_detection"]["mode"], "auto")
        self.assertEqual(report["crop_detection"]["crop"], "96:64:16:16")
        samples = [sample for analysis in report["codecs"].values()
                   for row in analysis["rows"] for sample in row["samples"]]
        self.assertEqual(len(samples), 4)
        for sample in samples:
            self.assertEqual((sample["width"], sample["height"]), (96, 64))
            cache = json.loads((output / "cache" / f"{sample['cache_key']}.json").read_text(encoding="utf-8"))
            self.assertEqual(cache["fingerprint"]["job"]["crop"], "96:64:16:16")

    def test_no_crop_preserves_frame_and_auto_crop_invalidates_its_cache(self):
        output = self.directory / "crop-cache-invalidation"
        uncropped = self.cli(self.bordered, output, "--codec", "x264", "--crf-values", "18", "--no-crop")
        self.assertEqual(uncropped.returncode, 0, uncropped.stdout + uncropped.stderr)
        report = json.loads((output / "results.json").read_text(encoding="utf-8"))
        original = report["codecs"]["x264"]["rows"][0]["samples"][0]
        self.assertIsNone(report["crop_detection"]["crop"])
        self.assertEqual((original["width"], original["height"]), (128, 96))

        cropped = self.cli(self.bordered, output, "--codec", "x264", "--crf-values", "18")
        self.assertEqual(cropped.returncode, 0, cropped.stdout + cropped.stderr)
        report = json.loads((output / "results.json").read_text(encoding="utf-8"))
        updated = report["codecs"]["x264"]["rows"][0]["samples"][0]
        self.assertEqual(report["crop_detection"]["crop"], "96:64:16:16")
        self.assertEqual((updated["width"], updated["height"]), (96, 64))
        self.assertFalse(updated["cached"])
        self.assertNotEqual(original["cache_key"], updated["cache_key"])


if __name__ == "__main__":
    unittest.main()
