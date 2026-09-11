"""Installed commands and module entry points must not depend on the working directory."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bdrip.cli import COMMANDS


class CommandTests(unittest.TestCase):
    def run_command(self, *arguments):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, *arguments],
                cwd=directory,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(list(Path(directory).iterdir()), [], result.stderr)
        return result

    def test_root_import_and_help_do_not_load_feature_dependencies(self):
        result = self.run_command(
            "-c",
            "import sys; import bdrip; from bdrip.cli import main; "
            "main(['--help']); "
            "assert not {'av', 'PySide6', 'paddleocr', 'cv2', 'imdbinfo'} & sys.modules.keys()",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("torrent-check", result.stdout)

    def test_every_command_help_works_outside_repository(self):
        for command in COMMANDS:
            with self.subTest(command=command):
                result = self.run_command("-m", "bdrip", command, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_module_entry_points_support_help_without_running_jobs(self):
        for module in ("bdrip.crf", "bdrip.crf.gui"):
            with self.subTest(module=module):
                result = self.run_command("-m", module, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_unknown_command_returns_usage_error(self):
        result = self.run_command("-m", "bdrip", "missing-command")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown command", result.stderr)

    def test_invalid_crop_timestamp_returns_usage_error(self):
        result = self.run_command(
            "-m", "bdrip", "crop", "video.mkv", "--start", "bad", "--end", "10"
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
