"""PyAV-only crop inspection and actual native two-pass encodes."""

import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock, patch

import av

from bdrip.video import cropdetect, transcode, two_pass
from bdrip.video.probe import EncodingError
from tests.fixtures.media import make_media


class CropCommandTests(unittest.TestCase):
    def test_detection_clamps_to_short_video_and_needs_no_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mkv"
            make_media(source, borders=True)
            with patch.dict(os.environ, {"PATH": ""}):
                self.assertEqual(cropdetect.detect_crop(source), "crop=96:64:16:16")
                self.assertEqual(cropdetect.main([str(source)]), 0)

    def test_outside_interval_and_missing_video_fail_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mkv"
            self.assertEqual(cropdetect.main([str(source)]), 1)
            make_media(source)
            for start, end in ((60, 61), (5, 1), (-1, 1)):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    cropdetect.detect_crop(source, start=start, end=end)


class TwoPassFailureTests(unittest.TestCase):
    def test_invalid_rates_and_crops_do_not_create_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mkv"
            make_media(source)
            output = source.parent / "output"
            for rate in (None, 0, -1, True, 1.5):
                with self.subTest(rate=rate), self.assertRaises(ValueError):
                    two_pass.encode_two_pass(source, output, x264_bitrate=rate)
            for crop in (
                "crop=95:64:0:0",
                "crop=96:64:0:5",
                "scale=48:32",
                "crop=iw:ih:0:0",
            ):
                with self.subTest(crop=crop), self.assertRaises(EncodingError):
                    two_pass.encode_two_pass(
                        source, output, x264_bitrate=200, crop_filter=crop
                    )
            self.assertFalse(output.exists())

    def test_second_pass_requires_native_first_pass_statistics(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            for codec in ("x264", "x265"):
                with (
                    self.subTest(codec=codec),
                    self.assertRaisesRegex(EncodingError, "first-pass statistics"),
                ):
                    transcode.encode_pass({"pass": 2, "codec": codec})

    def test_missing_stats_stops_before_second_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.mkv"
            make_media(source)
            result = {"frames": 12, "duration": 1, "video_bytes": 500}
            with patch.object(two_pass, "_run_pass", return_value=result) as run:
                with self.assertRaisesRegex(EncodingError, "first-pass statistics"):
                    two_pass.encode_two_pass(source, root / "output", x264_bitrate=200)
                run.assert_called_once()
            self.assertEqual(list((root / "output").glob("*.mkv")), [])

    def test_failed_or_cancelled_encode_retains_prior_output_and_removes_partial(self):
        for failure in (EncodingError("failed encode"), KeyboardInterrupt()):
            with (
                self.subTest(failure=type(failure).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = root / "movie.mkv"
                make_media(source)
                destination = root / "output"
                destination.mkdir()
                final = destination / "movie.x264.200k.mkv"
                final.write_bytes(b"previous movie")

                def fail(job, workspace, log_file, duration):
                    Path(job["output"]).write_bytes(b"partial movie")
                    log_file.write_text("failure diagnostics")
                    raise failure

                with patch.object(two_pass, "_run_pass", side_effect=fail) as run:
                    with self.assertRaises(type(failure)):
                        two_pass.encode_two_pass(
                            source, destination, x264_bitrate=200, x265_bitrate=200
                        )
                    run.assert_called_once()
                self.assertEqual(final.read_bytes(), b"previous movie")
                self.assertEqual(list(destination.glob("*.mkv")), [final])
                self.assertTrue(next((destination / "logs").iterdir()).read_text())

    def test_interrupt_kills_and_reaps_python_worker(self):
        process = Mock()
        process.communicate.side_effect = [KeyboardInterrupt, ("", None)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(two_pass.subprocess, "Popen", return_value=process),
                patch.object(two_pass, "tqdm"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    two_pass._run_pass(
                        {"codec": "x264", "pass": 1, "bitrate": 200},
                        root,
                        root / "encode.log",
                        1,
                    )
            process.kill.assert_called_once()
            self.assertEqual(process.communicate.call_count, 2)


class NativeTwoPassTests(unittest.TestCase):
    def test_both_encoders_with_empty_path_exact_crop_native_stats_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Unicode, spaces, and colons must not leak into native stats params.
            source = root / (
                "电影 [sample]:test.mkv" if os.name != "nt" else "电影 [sample].mkv"
            )
            make_media(source, borders=True)
            original = source.read_bytes()
            run = two_pass._run_pass
            measurements = []

            def record(job, workspace, log_file, duration):
                result = run(job, workspace, log_file, duration)
                measurements.append((job["codec"], job["pass"], result))
                self.assertTrue((workspace / "pass.stats").stat().st_size)
                if job["codec"] == "x265":
                    self.assertTrue((workspace / "pass.stats.cutree").is_file())
                return result

            with (
                patch.dict(os.environ, {"PATH": ""}),
                patch.object(two_pass, "_run_pass", side_effect=record),
                patch.object(two_pass, "tqdm"),
            ):
                outputs = two_pass.encode_two_pass(
                    source,
                    root / "output",
                    x264_bitrate=200,
                    x265_bitrate=200,
                    crop_filter="crop=96:64:17:17",
                )
            self.assertEqual(
                [(codec, phase) for codec, phase, _ in measurements],
                [("x264", 1), ("x264", 2), ("x265", 1), ("x265", 2)],
            )
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(all(data["frames"] == 12 for _, _, data in measurements))
            for path in outputs:
                with av.open(str(path)) as encoded:
                    self.assertEqual(len(encoded.streams), 1)
                    stream = encoded.streams.video[0]
                    self.assertEqual((stream.width, stream.height), (96, 64))
                    frames = list(encoded.decode(stream))
                    self.assertEqual(len(frames), 12)
                    self.assertEqual(frames[0].pts, 0)
            for path in (root / "output" / "logs").iterdir():
                self.assertTrue(path.name.endswith(".encode.log"))
                log = path.read_text()
                self.assertIn("pass 1", log)
                self.assertIn("pass 2", log)
                self.assertIn("veryslow", log)
                if "x265" in path.name:
                    self.assertIn("stats-write", log)
                    self.assertIn("stats-read", log)
                else:
                    self.assertIn("rc=2pass", log)
            self.assertFalse(list((root / "output").glob(".bdrip-*")))

    def test_vfr_timestamps_color_and_aspect_ratio_survive_full_encode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "vfr.mkv"
            timestamps = [
                5000,
                5040,
                5120,
                5160,
                5280,
                5400,
                5480,
                5520,
                5600,
                5720,
                5840,
                5920,
            ]
            with av.open(str(source), "w") as output:
                stream = output.add_stream("libx264", rate=25)
                stream.codec_context.options = {"crf": "0", "preset": "ultrafast"}
                stream.width, stream.height, stream.pix_fmt = 96, 64, "yuv420p10le"
                stream.time_base = Fraction(1, 1000)
                stream.sample_aspect_ratio = Fraction(4, 3)
                for name in ("color_primaries", "color_trc", "colorspace"):
                    setattr(stream.codec_context, name, 1)
                for timestamp in timestamps:
                    frame = av.VideoFrame(96, 64, "yuv420p10le")
                    for plane in frame.planes:
                        plane.update((b"\x00\x02") * (plane.buffer_size // 2))
                    frame.pts, frame.time_base = timestamp, Fraction(1, 1000)
                    for packet in stream.encode(frame):
                        output.mux(packet)
                for packet in stream.encode(None):
                    output.mux(packet)
            with patch.dict(os.environ, {"PATH": ""}), patch.object(two_pass, "tqdm"):
                paths = two_pass.encode_two_pass(
                    source, root / "output", x264_bitrate=100
                )
            with av.open(str(paths[0])) as encoded:
                stream = encoded.streams.video[0]
                self.assertEqual(stream.codec_context.format.name, "yuv420p10le")
                self.assertEqual(stream.sample_aspect_ratio, Fraction(4, 3))
                self.assertEqual(stream.codec_context.color_primaries, 1)
                actual = [
                    frame.pts * frame.time_base for frame in encoded.decode(stream)
                ]
                self.assertEqual(
                    actual, [Fraction(t - timestamps[0], 1000) for t in timestamps]
                )

    def test_rip_command_works_from_external_directory_without_executables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.mkv"
            make_media(source)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bdrip",
                    "rip",
                    str(source),
                    str(root / "output"),
                    "--x264_bitrate",
                    "200",
                ],
                cwd=root,
                env={**os.environ, "PATH": ""},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((root / "output" / "movie.x264.200k.mkv").stat().st_size)
