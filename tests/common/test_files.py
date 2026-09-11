"""Atomic JSON writes under transient and permanent Windows replacement failures."""

import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bdrip.common.files import read_json, write_json
from bdrip.common.progress import FrameProgress, Progress


def windows_error(code):
    error = PermissionError(errno.EACCES, "File temporarily locked")
    error.winerror = code
    return error


class AtomicJsonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "progress.json"
        self.before = {"completed": 1, "message": "Encoding 日本語"}
        write_json(self.path, self.before)

    def test_transient_windows_errors_retry_without_removing_previous_snapshot(self):
        original_replace = Path.replace
        after = {"completed": 2, "message": "Still encoding 日本語"}
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                write_json(self.path, self.before)
                attempted_sources = []

                def replace(source, destination):
                    attempted_sources.append(source)
                    self.assertEqual(destination, self.path)
                    self.assertEqual(read_json(destination), self.before)
                    self.assertEqual(read_json(source), after)
                    if len(attempted_sources) < 3:
                        raise windows_error(code)
                    return original_replace(source, destination)

                with (
                    mock.patch.object(Path, "replace", new=replace),
                    mock.patch("bdrip.common.files.time.sleep") as sleep,
                ):
                    write_json(self.path, after)
                self.assertEqual(len(attempted_sources), 3)
                self.assertEqual(len(set(attempted_sources)), 1)
                self.assertEqual(sleep.call_count, 2)
                self.assertEqual(read_json(self.path), after)
                self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_task_and_frame_progress_recover_after_a_sharing_conflict(self):
        original_replace = Path.replace
        for progress in (Progress(self.path), FrameProgress(str(self.path))):
            with self.subTest(progress=type(progress).__name__):
                attempts = []

                def replace(source, destination):
                    attempts.append(source)
                    if len(attempts) == 1:
                        raise windows_error(5)
                    return original_replace(source, destination)

                with (
                    mock.patch.object(Path, "replace", new=replace),
                    mock.patch("bdrip.common.files.time.sleep"),
                ):
                    if isinstance(progress, Progress):
                        progress.update(total=40, completed=7, stage="encoding")
                        self.assertEqual(read_json(self.path)["completed"], 7)
                        progress.update(state="complete", completed=40)
                        self.assertEqual(read_json(self.path)["state"], "complete")
                    else:
                        progress.update(120, 0.5, flushing=True)
                        self.assertEqual(
                            read_json(self.path),
                            {
                                "frames": 120,
                                "fraction": 0.5,
                                "flushing": True,
                            },
                        )

    def test_persistent_access_denied_is_bounded_and_keeps_previous_snapshot(self):
        error = windows_error(5)
        with (
            mock.patch.object(Path, "replace", side_effect=error) as replace,
            mock.patch("bdrip.common.files.time.sleep") as sleep,
            self.assertRaises(PermissionError) as raised,
        ):
            write_json(self.path, {"completed": 2})
        self.assertIs(raised.exception, error)
        self.assertGreater(replace.call_count, 1)
        self.assertLessEqual(replace.call_count, 10)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 1.0)
        self.assertEqual(read_json(self.path), self.before)
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_unrelated_io_errors_are_not_retried(self):
        for error in (
            PermissionError(errno.EACCES, "Permission denied"),
            OSError(errno.ENOSPC, "No space left"),
            windows_error(19),  # ERROR_WRITE_PROTECT
        ):
            with (
                self.subTest(error=error),
                mock.patch.object(Path, "replace", side_effect=error) as replace,
                mock.patch("bdrip.common.files.time.sleep") as sleep,
                self.assertRaises(OSError) as raised,
            ):
                write_json(self.path, {"completed": 2})
            self.assertIs(raised.exception, error)
            replace.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(read_json(self.path), self.before)
            self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_cleanup_failure_does_not_hide_original_error(self):
        original = windows_error(5)
        with (
            mock.patch.object(Path, "replace", side_effect=original),
            mock.patch.object(Path, "unlink", side_effect=windows_error(32)),
            mock.patch("bdrip.common.files.time.sleep"),
            self.assertRaises(PermissionError) as raised,
        ):
            write_json(self.path, {"completed": 2})
        self.assertIs(raised.exception, original)
        self.assertEqual(read_json(self.path), self.before)

    def test_interrupt_during_retry_preserves_previous_snapshot_and_cleans_up(self):
        with (
            mock.patch.object(Path, "replace", side_effect=windows_error(32)),
            mock.patch("bdrip.common.files.time.sleep", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            write_json(self.path, {"completed": 2})
        self.assertEqual(read_json(self.path), self.before)
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    @unittest.skipUnless(
        sys.platform == "win32", "Requires Windows file-sharing semantics"
    )
    def test_real_windows_read_handle_blocks_replace_until_closed(self):
        original_replace = Path.replace
        blocked = []
        with self.path.open("rb") as reader:

            def replace(source, destination):
                try:
                    return original_replace(source, destination)
                except OSError as exc:
                    blocked.append(exc.winerror)
                    reader.close()
                    raise

            with mock.patch.object(Path, "replace", new=replace):
                write_json(self.path, {"completed": 2})
        self.assertTrue(blocked)
        self.assertTrue(all(code in (5, 32, 33) for code in blocked))
        self.assertEqual(read_json(self.path), {"completed": 2})


if __name__ == "__main__":
    unittest.main()
