"""Real PyAV fixtures for black margins, pixel depth, and sample coverage."""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import av

from scripts.crf_crop import _LumaBounds, detect_crop
from scripts.crf_encode import EncodingError, inspect_video


def frame_with_bounds(
    bounds: tuple[int, int, int, int] | None,
    *, depth: int = 8, width: int = 128, height: int = 96,
    black: int = 16, content: int = 128,
) -> av.VideoFrame:
    """Write exact raw Y values into lossless fixtures without NumPy."""
    frame = av.VideoFrame(width, height, "yuv420p" if depth == 8 else "yuv420p10le")
    sample_bytes = 1 if depth == 8 else 2
    scale = 1 << (depth - 8)
    for number, plane in enumerate(frame.planes):
        value = (black if number == 0 else 128) * scale
        pixel = value.to_bytes(sample_bytes, "little")
        plane.update(pixel * (plane.buffer_size // sample_bytes))
    if bounds is not None:
        x1, y1, x2, y2 = bounds
        plane = frame.planes[0]
        buffer = bytearray(bytes(plane))
        row = (content * scale).to_bytes(sample_bytes, "little") * (x2 - x1 + 1)
        for y in range(y1, y2 + 1):
            offset = y * plane.line_size + x1 * sample_bytes
            buffer[offset:offset + len(row)] = row
        plane.update(buffer)
    return frame


def make_video(
    path: Path, bounds: list[tuple[int, int, int, int] | None],
    *, depth: int = 8, start_ms: int = 0, duration_ms: int = 100,
    black: int = 16, content: int = 128, width: int = 128, height: int = 96,
) -> None:
    with av.open(str(path), "w") as output:
        stream = output.add_stream("ffv1", rate=10)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p" if depth == 8 else "yuv420p10le"
        stream.time_base = Fraction(1, 1000)
        stream.codec_context.time_base = Fraction(1, 1000)

        def mux(packets):
            for packet in packets:
                packet.duration = duration_ms
                output.mux(packet)

        for index, rectangle in enumerate(bounds):
            frame = frame_with_bounds(rectangle, depth=depth, black=black, content=content,
                                      width=width, height=height)
            frame.pts = start_ms + index * duration_ms
            frame.time_base = Fraction(1, 1000)
            frame.duration = duration_ms
            mux(stream.encode(frame))
        mux(stream.encode(None))


class CropDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "bbox" not in av.filter.filters_available:
            raise unittest.SkipTest("PyAV build has no bbox filter")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-crf-crop-")
        self.path = Path(self.temporary.name) / "source.mkv"

    def tearDown(self):
        self.temporary.cleanup()

    def detect(self, samples=None, *, seconds=2.0):
        source = inspect_video(self.path)
        samples = samples or [{"start": 0.0, "duration": source["duration"], "kind": "representative"}]
        return detect_crop(self.path, 0, samples, source, {"limit": 24 / 255, "seconds": seconds})

    def test_letterbox_pillarbox_and_both(self):
        for rectangle, expected in (
            ((0, 12, 127, 83), "128:72:0:12"),
            ((16, 0, 111, 95), "96:96:16:0"),
            ((16, 16, 111, 79), "96:64:16:16"),
        ):
            with self.subTest(rectangle=rectangle):
                make_video(self.path, [rectangle] * 4)
                result = self.detect()
                self.assertEqual(result["crop"], expected)
                self.assertEqual(result["detector"], "pyav-bbox-luma")
                self.assertEqual(result["content_frames"], 4)

    def test_8_and_10_bit_thresholds_preserve_raw_luminance(self):
        for depth, threshold in ((8, 24), (10, 96)):
            with self.subTest(depth=depth):
                # Border values equal the integer threshold; content is just
                # above it, so implicit limited/full-range conversion is visible.
                make_video(self.path, [(16, 16, 111, 79)] * 3, depth=depth, black=24, content=25)
                result = self.detect()
                self.assertEqual(result["crop"], "96:64:16:16")
                self.assertEqual(result["windows"][0]["bit_depth"], depth)
                self.assertEqual(result["windows"][0]["threshold"], threshold)

    def test_detected_bounds_remain_exact_with_odd_origin(self):
        make_video(self.path, [(9, 7, 118, 86)] * 3)
        result = self.detect()
        self.assertEqual(result["crop"], "110:80:9:7")
        self.assertEqual((result["width"], result["height"]), (110, 80))
        self.assertEqual(result["bounds"], {"x1": 9, "y1": 7, "x2": 118, "y2": 86})

    def test_1920_by_804_content_keeps_exact_height_at_even_and_odd_origins(self):
        for y in (137, 138, 139):
            with self.subTest(y=y):
                make_video(self.path, [(0, y, 1919, y + 803)] * 2,
                           width=1920, height=1080)
                result = self.detect()
                self.assertEqual(result["crop"], f"1920:804:0:{y}")
                self.assertEqual((result["width"], result["height"]), (1920, 804))
                self.assertEqual(result["bounds"], {"x1": 0, "y1": y,
                                                    "x2": 1919, "y2": y + 803})

    def test_odd_detected_dimensions_are_returned_without_padding_or_trimming(self):
        for rectangle, expected, dimensions in (
            ((9, 7, 117, 86), "109:80:9:7", (109, 80)),
            ((9, 7, 118, 85), "110:79:9:7", (110, 79)),
        ):
            with self.subTest(rectangle=rectangle):
                make_video(self.path, [rectangle] * 3)
                result = self.detect()
                self.assertEqual(result["crop"], expected)
                self.assertEqual((result["width"], result["height"]), dimensions)

    def test_variable_aspect_ratios_use_union_of_representative_and_stress_windows(self):
        make_video(self.path, [(0, 16, 127, 79)] * 10 + [(0, 8, 127, 87)] * 10)
        samples = [
            {"start": 0.0, "duration": 1.0, "kind": "representative"},
            {"start": 1.0, "duration": 1.0, "kind": "stress"},
        ]
        result = self.detect(samples, seconds=0.2)
        self.assertEqual(result["crop"], "128:80:0:8")
        self.assertAlmostEqual(result["windows"][0]["start"], 0.4)
        self.assertAlmostEqual(result["windows"][1]["start"], 1.4)
        self.assertEqual(result["windows"][1]["kinds"], ["stress"])

    def test_full_frame_scene_prevents_cropping_other_scenes(self):
        make_video(self.path, [(0, 16, 127, 79)] * 4 + [(0, 0, 127, 95)] * 4)
        result = self.detect()
        self.assertIsNone(result["crop"])
        self.assertEqual(result["reason_code"], "full_frame_content")

    def test_no_border_keeps_full_frame(self):
        make_video(self.path, [(0, 0, 127, 95)] * 3)
        result = self.detect()
        self.assertIsNone(result["crop"])
        self.assertEqual((result["width"], result["height"]), (128, 96))

    def test_black_and_tiny_dark_scene_content_keep_full_frame(self):
        for rectangle, reason in ((None, "no_content"), ((60, 40, 67, 55), "small_content")):
            with self.subTest(rectangle=rectangle):
                make_video(self.path, [rectangle] * 3)
                result = self.detect()
                self.assertIsNone(result["crop"])
                self.assertEqual(result["reason_code"], reason)
                if rectangle is None:
                    self.assertEqual(result["black_frames"], 3)

    def test_black_frames_do_not_override_visible_content_bounds(self):
        make_video(self.path, [None, (0, 12, 127, 83), None])
        result = self.detect()
        self.assertEqual(result["crop"], "128:72:0:12")
        self.assertEqual(result["black_frames"], 2)

    def test_single_frame_with_nonzero_timestamp_is_detected(self):
        make_video(self.path, [(16, 16, 111, 79)], start_ms=500, duration_ms=100)
        result = self.detect()
        self.assertEqual(result["crop"], "96:64:16:16")
        self.assertEqual(result["frames"], 1)

    def test_long_held_frame_overlapping_centered_window_is_detected(self):
        make_video(self.path, [(16, 16, 111, 79)], duration_ms=2000)
        result = self.detect(seconds=0.25)
        self.assertEqual(result["crop"], "96:64:16:16")
        self.assertEqual(result["frames"], 1)
        self.assertAlmostEqual(result["windows"][0]["start"], 0.875)

    def test_duplicate_detection_windows_are_not_decoded_twice(self):
        make_video(self.path, [(16, 16, 111, 79)] * 10)
        result = self.detect([
            {"start": 0.0, "duration": 1.0, "kind": "representative"},
            {"start": 0.0, "duration": 1.0, "kind": "stress"},
        ])
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(result["windows"][0]["kinds"], ["representative", "stress"])
        self.assertEqual(result["frames"], 10)

    def test_inconsistent_dimensions_are_rejected(self):
        make_video(self.path, [(0, 12, 127, 83)])
        source = inspect_video(self.path)
        source["height"] = 80
        with self.assertRaisesRegex(EncodingError, "changes video dimensions"):
            detect_crop(self.path, 0, [{"start": 0, "duration": 0.1}], source, {})

    def test_rgb_is_not_silently_converted_to_a_different_luminance_range(self):
        reader = _LumaBounds(24 / 255)
        frame = av.VideoFrame(128, 96, "rgb24")
        with self.assertRaisesRegex(ValueError, "native luminance plane"):
            reader.read(frame)


if __name__ == "__main__":
    unittest.main()
