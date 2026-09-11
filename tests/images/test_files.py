"""Shared discovery must retain each consumer's formats and preserve source images."""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from bdrip.images.files import image_files, screenshot_files
from bdrip.images.thumbnails import main, make_thumbnail


class ImageTests(unittest.TestCase):
    def test_discovery_orders_numbers_and_ignores_hidden_and_nonimages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("10.PNG", "2.jpg", "1.tiff", ".1.png", "._2.jpg", "notes.txt"):
                (root / name).touch()
            (root / "3.png").mkdir()
            self.assertEqual(
                [p.name for p in image_files(root)], ["1.tiff", "2.jpg", "10.PNG"]
            )
            self.assertEqual(
                [p.name for p in screenshot_files(root)], ["2.jpg", "10.PNG"]
            )

    def test_optional_screenshot_sections_can_be_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "More"
            self.assertEqual(screenshot_files(missing, missing_ok=True), [])
            with self.assertRaises(FileNotFoundError):
                image_files(missing)

    def test_thumbnail_keeps_original_and_repeated_runs_do_not_nest_thumbnails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scene.png"
            Image.new("RGB", (1920, 804), "red").save(source)
            original = source.read_bytes()
            self.assertEqual(main([str(root)]), 0)
            self.assertEqual(main([str(root)]), 0)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(
                sorted(p.name for p in root.iterdir()), ["scene.png", "scene_thumb.png"]
            )
            with Image.open(root / "scene_thumb.png") as thumbnail:
                self.assertEqual(thumbnail.size, (300, 126))

    def test_small_images_are_not_enlarged(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "small.png"
            Image.new("RGB", (100, 100)).save(source)
            self.assertIsNone(make_thumbnail(source))
            self.assertEqual(list(source.parent.iterdir()), [source])
