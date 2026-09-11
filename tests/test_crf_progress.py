"""Atomic progress publication and cooperative cancellation."""

import json
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from scripts.crf_progress import FrameProgress, Progress, read_json, write_json


class ProgressTests(unittest.TestCase):
    def test_progress_cannot_overwrite_input_or_report_files(self):
        from scripts.crf_search import main
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mkv"
            source.write_bytes(b"original video bytes")
            output = Path(directory) / "results"
            for path in (source, output / "results.json"):
                with self.subTest(path=path), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main([str(source), "--output-dir", str(output), "--progress-file", str(path)])
                self.assertEqual(error.exception.code, 2)
            self.assertEqual(source.read_bytes(), b"original video bytes")

    def test_cancel_marker_stops_work_but_allows_terminal_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            cancel = Path(directory) / "cancel.request"
            progress = Progress(path, cancel)
            progress.update(total=10, completed=3, stage="encoding")
            self.assertEqual(read_json(path)["completed"], 3)
            cancel.touch()
            with self.assertRaises(KeyboardInterrupt):
                progress.update(completed=4)
            progress.update(check=False, state="interrupted")
            self.assertEqual(read_json(path)["state"], "interrupted")
            self.assertEqual(read_json(path)["completed"], 3)

    def test_incomplete_or_invalid_json_does_not_replace_last_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            write_json(path, {"completed": 3})
            with self.assertRaises(ValueError):
                write_json(path, {"completed": float("nan")})
            self.assertEqual(json.loads(path.read_text()), {"completed": 3})
            self.assertEqual(list(Path(directory).iterdir()), [path])
            self.assertEqual(read_json(path.with_name("absent.json")), {})

    def test_frame_progress_reserves_completion_for_encoder_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.json"
            progress = FrameProgress(str(path))
            progress.update(100, 1.1, flushing=True)
            self.assertEqual(read_json(path), {"frames": 100, "fraction": 0.99, "flushing": True})


if __name__ == "__main__":
    unittest.main()
