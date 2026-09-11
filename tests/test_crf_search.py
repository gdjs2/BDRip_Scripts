"""Random sample selection, averaged metrics, plotting and native CRF sweeps."""

import csv
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import av
from PIL import Image

from scripts import crf_search
from scripts.crf_plot import make_figure

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
    def plan(self, duration=600, **overrides):
        sampling = {**crf_search.load_config()["sampling"], **overrides}
        return crf_search.select_samples(duration, sampling)

    def test_seeded_clips_are_random_and_uniformly_distributed_without_overlap(self):
        samples = self.plan(start=30, end=570, seed=17)
        self.assertEqual(len(samples), 10)
        self.assertEqual(samples, self.plan(start=30, end=570, seed=17))
        self.assertNotEqual(samples, self.plan(start=30, end=570, seed=18))
        lengths = set()
        previous_end = 30
        for index, sample in enumerate(samples):
            left, right = 30 + index * 54, 30 + (index + 1) * 54
            self.assertGreaterEqual(sample["start"], left)
            self.assertLessEqual(sample["start"] + sample["duration"], right + 1e-9)
            self.assertGreaterEqual(sample["duration"], 5)
            self.assertLessEqual(sample["duration"], 10)
            self.assertGreaterEqual(sample["start"], previous_end)
            previous_end = sample["start"] + sample["duration"]
            lengths.add(sample["duration"])
        self.assertGreater(len(lengths), 1)

    def test_random_positions_cover_each_bin_instead_of_always_using_centers(self):
        relative = []
        durations = []
        for seed in range(200):
            sample = self.plan(duration=100, count=1, seed=seed)[0]
            relative.append(sample["start"] / (100 - sample["duration"]))
            durations.append(sample["duration"])
        self.assertAlmostEqual(sum(relative) / len(relative), 0.5, delta=0.06)
        self.assertLess(min(relative), 0.05)
        self.assertGreater(max(relative), 0.95)
        self.assertAlmostEqual(sum(durations) / len(durations), 7.5, delta=0.3)

    def test_fixed_duration_and_short_strata(self):
        self.assertTrue(all(s["duration"] == 7 for s in self.plan(min_seconds=7, max_seconds=7)))
        samples = self.plan(duration=60)
        self.assertEqual(len(samples), 10)
        self.assertTrue(all(5 <= sample["duration"] <= 6 for sample in samples))
        with self.assertRaisesRegex(ValueError, "cannot fit"):
            self.plan(duration=49)

    def test_invalid_bounds_do_not_silently_change_the_requested_interval(self):
        for overrides in ({"start": 700}, {"start": 20, "end": 10}, {"end": 601}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.plan(**overrides)


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

    def test_plot_uses_average_qp_on_x_and_average_mbps_on_y_and_labels_crfs(self):
        report = {"state": "complete", "source": {"path": "movie.mkv"}, "codecs": {
            "x264": {"rows": [{"crf": 14, "average_qp": 17, "average_bitrate_mbps": 12},
                               {"crf": 15, "average_qp": 18, "average_bitrate_mbps": 9}]},
            "x265": {"rows": [{"crf": 14, "average_qp": 19, "average_bitrate_mbps": 8}]},
        }}
        figure = make_figure(report)
        try:
            axes = figure.axes[0]
            self.assertEqual(axes.get_xlabel(), "Average B-frame QP (weighted by B-frame count)")
            self.assertIn("bitrate", axes.get_ylabel())
            self.assertEqual(list(axes.lines[0].get_xdata()), [17, 18])
            self.assertEqual(list(axes.lines[0].get_ydata()), [12, 9])
            self.assertEqual([line.get_label() for line in axes.lines], ["x264", "x265"])
            self.assertEqual([text.get_text() for text in axes.texts], ["CRF 14", "CRF 15", "CRF 14"])
        finally:
            figure.clear()

    def test_plot_omits_measurements_without_b_frame_qp(self):
        report = {"state": "complete", "source": {"path": "movie.mkv"}, "codecs": {
            "x264": {"rows": [{"crf": 14, "average_qp": 17, "average_bitrate_mbps": 12},
                               {"crf": 15, "average_qp": None, "average_bitrate_mbps": 9},
                               {"crf": 16, "average_qp": 19, "average_bitrate_mbps": 8}]},
            "x265": {"rows": [{"crf": 14, "average_qp": None, "average_bitrate_mbps": 7}]},
        }}
        figure = make_figure(report)
        try:
            axes = figure.axes[0]
            self.assertEqual(len(axes.lines), 1)
            self.assertEqual(list(axes.lines[0].get_xdata()), [17, 19])
            self.assertEqual(list(axes.lines[0].get_ydata()), [12, 8])
            self.assertEqual([text.get_text() for text in axes.texts], ["CRF 14", "CRF 16"])
        finally:
            figure.clear()


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


class SweepIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="crf-sweep-tests-")
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
            "sampling": {"count": 2, "min_seconds": 0.3, "max_seconds": 0.4, "seed": 123},
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

    def test_both_encoders_complete_exactly_five_crfs_and_write_table_and_figures(self):
        output = self.directory / "both-codecs"
        completed = self.cli(self.source, output)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertLess(completed.stderr.index("profile=high"), completed.stderr.index("x264 CRF 14:"))
        self.assertLess(completed.stderr.index("profile=main10"), completed.stderr.index("x264 CRF 14:"))
        report = json.loads((output / "results.json").read_text())
        self.assertEqual(report["state"], "complete")
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["qp_frame_type"], "B")
        self.assertEqual(report["crf_values"], [14, 15, 16, 17, 18])
        for codec, analysis in report["codecs"].items():
            self.assertEqual([row["crf"] for row in analysis["rows"]], [14, 15, 16, 17, 18])
            for row in analysis["rows"]:
                self.assertEqual(row["sample_count"], 2)
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
        self.assertEqual(len(rows), 10)
        self.assertNotIn("recommended", rows[0])
        self.assertTrue(all(int(row["b_frames"]) > 0 for row in rows))
        with Image.open(output / "qp-bitrate.png") as figure:
            self.assertEqual(figure.format, "PNG")
            self.assertGreater(figure.width, 1000)
            self.assertGreater(figure.height, 600)
        svg = (output / "qp-bitrate.svg").read_text()
        self.assertIn("Average B-frame QP", svg)
        self.assertIn("Average video bitrate", svg)
        self.assertIn("CRF 18", svg)
        with (mock.patch.object(crf_search.subprocess, "Popen", side_effect=AssertionError("Cached sweep must not encode")),
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

    def test_sweep_without_b_frames_reports_missing_qp_and_preserves_bitrate(self):
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
        self.assertEqual(len(report["codecs"]["x264"]["rows"]), 5)
        for row in report["codecs"]["x264"]["rows"]:
            self.assertIsNone(row["average_qp"])
            self.assertEqual(row["b_frames"], 0)
            self.assertGreater(row["average_bitrate_mbps"], 0)
            self.assertGreater(row["frames"], 0)
        with (output / "summary.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(all(row["average_qp"] == "" and row["b_frames"] == "0" for row in rows))
        svg = (output / "qp-bitrate.svg").read_text()
        self.assertIn("No B-frames in completed measurements", svg)
        self.assertNotIn("<!-- CRF 14 -->", svg)

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

    def test_partial_sample_set_is_not_reported_as_a_complete_average(self):
        output = self.directory / "interrupted"
        encoded = metrics(0.3, 1200, {"I": 18, "P": 20}, {"I": 1, "P": 3})
        with (mock.patch.object(crf_search, "run_sample", side_effect=[encoded, KeyboardInterrupt]),
              mock.patch.object(crf_search.CONSOLE, "quiet", True)):
            code = crf_search.main([str(self.source), "--config", str(self.config),
                                   "--codec", "x264", "--output-dir", str(output)])
        self.assertEqual(code, 130)
        report = json.loads((output / "results.json").read_text())
        self.assertEqual(report["state"], "interrupted")
        self.assertEqual(report["codecs"]["x264"]["rows"], [])
        self.assertTrue((output / "qp-bitrate.png").exists())

    def test_audio_only_and_too_many_samples_fail_clearly(self):
        completed = self.cli(self.audio_only, self.directory / "no-video")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("video", completed.stderr.lower())
        self.assertNotIn("Traceback", completed.stderr)
        completed = self.cli(self.source, self.directory / "too-short", "--samples", "10", "--sample-seconds", "5")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("cannot fit", completed.stderr)


if __name__ == "__main__":
    unittest.main()
