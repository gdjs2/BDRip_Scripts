"""Centered sample selection, B-frame metrics and native two-point calibration."""

import csv
from fractions import Fraction
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import av
from PIL import Image

from scripts import crf_search
from scripts.crf_model import predict, select_sample

ROOT = Path(__file__).resolve().parents[1]


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


class SamplingTests(unittest.TestCase):
    def test_minute_is_centered_in_video(self):
        for duration in (60, 90, 600, 7200, 105.75):
            sample = select_sample(duration)
            self.assertEqual(sample["duration"], 60)
            self.assertAlmostEqual(sample["start"] + 30, duration / 2)

    def test_short_video_uses_whole_duration(self):
        for duration in (0.2, 1, 59.9):
            self.assertEqual(select_sample(duration), {"kind": "center", "start": 0, "duration": duration})

    def test_invalid_duration_is_rejected(self):
        for duration in (0, -1, True, float("inf"), float("nan")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                select_sample(duration)


def metrics(duration, video_bytes, qps, counts):
    return {"duration": duration, "video_bytes": video_bytes,
            "qp": qps, "frame_counts": counts, "frames": sum(counts.values())}


class AverageTests(unittest.TestCase):
    def test_qp_uses_only_b_frame_counts_and_bitrate_weights_duration(self):
        first = metrics(5, 1_000_000, {"I": 10.0, "P": 20.0, "B": 30.0}, {"I": 1, "P": 3, "B": 6})
        second = metrics(10, 4_000_000, {"I": 40.0, "P": 45.0, "B": 20.0}, {"I": 1, "P": 27, "B": 2})
        row = crf_search.summarize_trial(14, [first, second])
        self.assertEqual(row["samples"][0]["average_qp"], 30)
        self.assertEqual(row["samples"][0]["b_frames"], 6)
        self.assertEqual(row["samples"][1]["average_qp"], 20)
        self.assertEqual(row["average_qp"], 27.5)
        self.assertEqual(row["b_frames"], 8)
        self.assertAlmostEqual(row["average_bitrate_mbps"], 40 / 15)
        self.assertEqual((row["frames"], row["duration_seconds"], row["sample_count"]), (40, 15, 2))
        self.assertNotIn("qp95", row)
        self.assertNotIn("recommended_crf", row)

    def test_clip_without_b_frames_contributes_to_bitrate_but_not_qp(self):
        first = metrics(5, 1_000_000, {"I": 10.0, "P": 20.0, "B": 30.0}, {"I": 1, "P": 3, "B": 6})
        second = metrics(10, 4_000_000, {"I": 40.0, "P": 45.0}, {"I": 1, "P": 29})
        row = crf_search.summarize_trial(14, [first, second])
        self.assertIsNone(row["samples"][1]["average_qp"])
        self.assertEqual(row["samples"][1]["b_frames"], 0)
        self.assertEqual(row["samples"][1]["average_bitrate_mbps"], 3.2)
        self.assertEqual(row["average_qp"], 30)
        self.assertEqual(row["b_frames"], 6)
        self.assertEqual(row["frames"], 40)
        self.assertAlmostEqual(row["average_bitrate_mbps"], 40 / 15)

    def test_trial_without_b_frames_has_no_qp_but_still_reports_bitrate(self):
        first = metrics(5, 1_000_000, {"I": 10.0, "P": 20.0}, {"I": 1, "P": 9})
        second = metrics(10, 4_000_000, {"I": 40.0}, {"I": 30})
        row = crf_search.summarize_trial(14, [first, second])
        self.assertIsNone(row["average_qp"])
        self.assertEqual(row["b_frames"], 0)
        self.assertTrue(all(sample["average_qp"] is None for sample in row["samples"]))
        self.assertEqual((row["frames"], row["duration_seconds"], row["sample_count"]), (40, 15, 2))
        self.assertAlmostEqual(row["average_bitrate_mbps"], 40 / 15)

    def test_bad_or_incomplete_frame_metrics_are_rejected(self):
        good = metrics(5, 1000, {"I": 15.0, "P": 20.0}, {"I": 1, "P": 9})
        for change in ({"duration": 0}, {"video_bytes": 0}, {"frames": 11},
                       {"frame_counts": {"I": 1}}, {"qp": {"I": 15}},
                       {"qp": {"I": float("nan"), "P": 20}}, {"frame_counts": {"I": -1, "P": 11}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                crf_search.sample_metrics({**good, **change})
        with self.assertRaises(ValueError):
            crf_search.summarize_trial(14, [])


class WorkerCleanupTests(unittest.TestCase):
    def test_interrupt_kills_and_reaps_worker_without_caching_partial_metrics(self):
        process = mock.Mock()
        process.communicate.side_effect = [KeyboardInterrupt, ("", None)]
        job = {"codec": "x264", "crf": 14, "start": 0, "duration": 5}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with mock.patch.object(crf_search.subprocess, "Popen", return_value=process):
                with self.assertRaises(KeyboardInterrupt):
                    crf_search.run_sample(job, {}, {}, output, True)
            process.kill.assert_called_once()
            self.assertEqual(process.communicate.call_count, 2)
            self.assertFalse((output / "cache").exists())


class CalibrationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="crf-model-tests-")
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
            "codecs": {
                "x264": {"preset": "ultrafast", "params": {"threads": 1, "bframes": 2,
                                                           "b-adapt": 0, "rc-lookahead": 4}},
                "x265": {"preset": "ultrafast", "params": {"pools": "none", "frame-threads": 1,
                                                           "bframes": 2, "b-adapt": 0, "rc-lookahead": 4}},
            },
        }))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def cli(self, source, output, *extra):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "crf_search.py"), str(source),
                               "--config", str(self.config), "--output-dir", str(output), *extra],
                              cwd=ROOT, text=True, capture_output=True, timeout=60)

    def test_both_encoders_finish_in_order_and_save_measurements_models_and_figures(self):
        output = self.directory / "both-codecs"
        completed = self.cli(self.source, output, "--progress-file", str(output / "progress.json"))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(re.findall(r"^(x26[45]) CRF (\d+):", completed.stderr, re.MULTILINE),
                         [(codec, str(crf)) for codec in ("x264", "x265") for crf in (13, 20)])
        progress = json.loads((output / "progress.json").read_text())
        self.assertEqual((progress["state"], progress["completed"], progress["total"]),
                         ("complete", 4, 4))
        self.assertLess(completed.stderr.index("profile=high"), completed.stderr.index("x264 CRF 13:"))
        self.assertLess(completed.stderr.index("profile=main10"), completed.stderr.index("x264 CRF 13:"))
        report = json.loads((output / "results.json").read_text())
        self.assertEqual(report["state"], "complete")
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["qp_frame_type"], "B")
        self.assertEqual(report["crf_values"], [13, 20])
        self.assertEqual(report["method"], "two_point")
        self.assertEqual(report["sample_plan"], [{"kind": "center", "start": 0, "duration": 1}])
        for codec, analysis in report["codecs"].items():
            self.assertEqual([row["crf"] for row in analysis["rows"]], [13, 20])
            for row in analysis["rows"]:
                fitted = predict(analysis["models"], row["crf"])
                self.assertAlmostEqual(fitted["average_qp"], row["average_qp"])
                self.assertAlmostEqual(fitted["average_bitrate_mbps"], row["average_bitrate_mbps"])
                self.assertEqual(row["sample_count"], 1)
                self.assertEqual([sample["sample"] for sample in row["samples"]], report["sample_plan"])
                b_frames = sum(sample["frame_counts"].get("B", 0) for sample in row["samples"])
                self.assertGreater(b_frames, 0)
                self.assertEqual(row["b_frames"], b_frames)
                total_qp = sum(sample["qp"].get("B", 0) * sample["frame_counts"].get("B", 0)
                               for sample in row["samples"])
                self.assertAlmostEqual(row["average_qp"], total_qp / b_frames)
                self.assertAlmostEqual(row["average_bitrate_mbps"], row["video_bytes"] * 8 / row["duration_seconds"] / 1e6)
                for sample in row["samples"]:
                    self.assertEqual(sample["average_qp"], sample["qp"].get("B"))
                    self.assertEqual(sample["b_frames"], sample["frame_counts"].get("B", 0))
                    self.assertEqual(float(sample["encoder_options"]["crf"]), row["crf"])
                    self.assertEqual(sample["encoder_options"]["preset"], "ultrafast")
                    self.assertFalse(sample["cached"])
        with (output / "summary.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 4)
        self.assertNotIn("recommended", rows[0])
        self.assertTrue(all(int(row["b_frames"]) > 0 for row in rows))
        with (output / "estimates.csv").open() as handle:
            estimates = list(csv.DictReader(handle))
        self.assertEqual([(row["codec"], int(row["crf"])) for row in estimates],
                         [(codec, crf) for codec in ("x264", "x265") for crf in range(13, 21)])
        with Image.open(output / "qp-bitrate.png") as figure:
            self.assertEqual(figure.format, "PNG")
            self.assertGreater(figure.width, 1000)
            self.assertGreater(figure.height, 600)
        svg = (output / "qp-bitrate.svg").read_text()
        self.assertIn("Average B-frame QP", svg)
        self.assertIn("Video bitrate (Mbps)", svg)
        self.assertIn("Markers: measured CRF 13 and 20", svg)
        with (mock.patch.object(crf_search.subprocess, "Popen", side_effect=AssertionError("Cached calibration must not encode")),
              mock.patch.object(crf_search.CONSOLE, "quiet", True)):
            self.assertEqual(crf_search.main([str(self.source), "--config", str(self.config),
                                              "--output-dir", str(output)]), 0)
        resumed = json.loads((output / "results.json").read_text())
        self.assertEqual(resumed["sample_plan"], report["sample_plan"])
        self.assertTrue(all(sample["cached"] for analysis in resumed["codecs"].values()
                            for row in analysis["rows"] for sample in row["samples"]))

    def test_audio_is_excluded_and_exact_manual_crop_survives_all_crfs(self):
        reports = []
        for source in (self.source, self.video_only):
            output = self.directory / (source.stem + "-video-comparison")
            completed = self.cli(source, output, "--codec", "x264", "--crop", "94:62:1:1")
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            reports.append(json.loads((output / "results.json").read_text()))
        for first, second in zip(reports[0]["codecs"]["x264"]["rows"], reports[1]["codecs"]["x264"]["rows"]):
            self.assertEqual(first["average_qp"], second["average_qp"])
            self.assertEqual(first["average_bitrate_mbps"], second["average_bitrate_mbps"])
            self.assertTrue(all((sample["width"], sample["height"]) == (94, 62) for sample in first["samples"]))

    def test_calibration_without_b_frames_reports_missing_qp_and_preserves_bitrate(self):
        config = json.loads(self.config.read_text())
        config["codecs"]["x264"]["params"]["bframes"] = 0
        no_b_config = self.directory / "no-b-config.json"
        no_b_config.write_text(json.dumps(config))
        output = self.directory / "no-b-frames"
        completed = self.cli(self.source, output, "--codec", "x264", "--config", str(no_b_config))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("N/A (no B-frames)", completed.stderr)
        self.assertIn("Average B-frame QP", completed.stderr)
        report = json.loads((output / "results.json").read_text())
        self.assertEqual(report["state"], "complete")
        self.assertEqual(len(report["codecs"]["x264"]["rows"]), 2)
        for row in report["codecs"]["x264"]["rows"]:
            self.assertIsNone(row["average_qp"])
            self.assertEqual(row["b_frames"], 0)
            self.assertGreater(row["average_bitrate_mbps"], 0)
            self.assertGreater(row["frames"], 0)
        with (output / "summary.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(all(row["average_qp"] == "" and row["b_frames"] == "0" for row in rows))
        svg = (output / "qp-bitrate.svg").read_text()
        self.assertIn("B-frame QP unavailable", svg)
        self.assertIsNone(report["codecs"]["x264"]["models"]["qp"])
        self.assertIsNotNone(report["codecs"]["x264"]["models"]["log_bitrate"])

    def test_crop_is_detected_once_and_shared_by_every_crf(self):
        output = self.directory / "cropped"
        with (mock.patch.object(crf_search, "detect_crop", wraps=crf_search.detect_crop) as detector,
              mock.patch.object(crf_search.CONSOLE, "quiet", True)):
            self.assertEqual(crf_search.main([str(self.bordered), "--config", str(self.config),
                                              "--codec", "x264", "--output-dir", str(output)]), 0)
        detector.assert_called_once()
        report = json.loads((output / "results.json").read_text())
        self.assertEqual(report["crop_detection"]["crop"], "96:64:16:16")
        for row in report["codecs"]["x264"]["rows"]:
            self.assertTrue(all((sample["width"], sample["height"]) == (96, 64) for sample in row["samples"]))

    def test_interrupted_second_anchor_preserves_first_measurement_without_fitting(self):
        output = self.directory / "interrupted"
        encoded = metrics(0.3, 1200, {"I": 18, "P": 20}, {"I": 1, "P": 3})
        with (mock.patch.object(crf_search, "run_sample", side_effect=[encoded, KeyboardInterrupt]),
              mock.patch.object(crf_search.CONSOLE, "quiet", True)):
            code = crf_search.main([str(self.source), "--config", str(self.config),
                                   "--codec", "x264", "--output-dir", str(output)])
        self.assertEqual(code, 130)
        report = json.loads((output / "results.json").read_text())
        self.assertEqual(report["state"], "interrupted")
        analysis = report["codecs"]["x264"]
        self.assertEqual([row["crf"] for row in analysis["rows"]], [13])
        self.assertIsNone(analysis["models"]["qp"])
        self.assertIsNone(analysis["models"]["log_bitrate"])
        self.assertTrue((output / "qp-bitrate.png").exists())

    def test_audio_only_fails_clearly(self):
        completed = self.cli(self.audio_only, self.directory / "no-video")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("video", completed.stderr.lower())
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
