"""Process deadline, cancellation, and isolated native-probe regressions."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock, call, patch

from scripts import crf_runtime as runtime


class ProcessRuntimeTests(unittest.TestCase):
    def test_stdout_is_drained_and_decoded_as_utf8(self):
        stdout = runtime.run_process(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(('片段' + 'x'*100000).encode('utf-8'))"],
            timeout=5.0, stderr=subprocess.DEVNULL,
        )
        self.assertEqual(stdout, "片段" + "x" * 100000)

    def test_unbounded_timeout_remains_supported_for_standalone_callers(self):
        stdout = runtime.run_process(
            [sys.executable, "-c", "print('done')"], timeout=None, stderr=subprocess.DEVNULL,
        )
        self.assertEqual(stdout, "done\n")

    def test_expired_budget_does_not_start_a_child(self):
        with patch.object(runtime.subprocess, "Popen") as spawn:
            for timeout in (0, -0.1):
                with self.subTest(timeout=timeout), self.assertRaises(runtime.BudgetExpired):
                    runtime.run_process([sys.executable, "-c", "print('unexpected')"], timeout=timeout, stderr=None)
            spawn.assert_not_called()

    def test_sleeping_child_is_killed_and_reaped_without_grace_period(self):
        real_popen = subprocess.Popen
        children = []

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        before = time.monotonic()
        with patch.object(runtime.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(runtime.BudgetExpired):
                runtime.run_process(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout=0.15, stderr=subprocess.DEVNULL,
                )
        self.assertLess(time.monotonic() - before, 2.0)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertNotEqual(children[0].returncode, 0)
        self.assertTrue(children[0].stdout.closed)

    def test_failed_child_preserves_exit_code_and_stderr_destination(self):
        with tempfile.TemporaryFile() as stderr:
            with self.assertRaisesRegex(RuntimeError, "code 7"):
                runtime.run_process(
                    [sys.executable, "-c", "import sys; print('native failure', file=sys.stderr); sys.exit(7)"],
                    timeout=5.0, stderr=stderr,
                )
            stderr.seek(0)
            self.assertEqual(stderr.read(), b"native failure\n")

    def test_process_startup_time_is_deducted_from_the_budget(self):
        process = Mock(returncode=0)
        process.communicate.return_value = ("done", None)
        with patch.object(runtime.subprocess, "Popen", return_value=process):
            with patch.object(runtime.time, "monotonic", side_effect=[10.0, 10.2]):
                self.assertEqual(runtime.run_process([sys.executable], timeout=0.5, stderr=None), "done")
        self.assertAlmostEqual(process.communicate.call_args.kwargs["timeout"], 0.3)

    def test_process_that_exhausts_its_budget_during_startup_is_reaped(self):
        process = Mock()
        process.communicate.return_value = ("", None)
        process.poll.return_value = -9
        with patch.object(runtime.subprocess, "Popen", return_value=process):
            with patch.object(runtime.time, "monotonic", side_effect=[10.0, 10.6]):
                with self.assertRaises(runtime.BudgetExpired):
                    runtime.run_process([sys.executable], timeout=0.5, stderr=None)
        process.kill.assert_called_once_with()
        process.communicate.assert_called_once_with()

    def test_cancellation_kills_and_reaps_before_reraising(self):
        for interruption in (KeyboardInterrupt(), SystemExit(3)):
            process = Mock()
            process.communicate.side_effect = [interruption, ("", None)]
            process.poll.return_value = -9
            with self.subTest(interruption=type(interruption).__name__):
                with patch.object(runtime.subprocess, "Popen", return_value=process):
                    with self.assertRaises(type(interruption)) as caught:
                        runtime.run_process([sys.executable], timeout=0.5, stderr=None)
                self.assertIs(caught.exception, interruption)
                process.kill.assert_called_once_with()
                self.assertEqual(len(process.communicate.call_args_list), 2)
                timeout = process.communicate.call_args_list[0].kwargs["timeout"]
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 0.5)
                self.assertEqual(process.communicate.call_args_list[1], call())


class ProbeRuntimeTests(unittest.TestCase):
    def test_probe_failure_includes_a_bounded_native_stderr_tail(self):
        def failure(command, *, timeout, stderr):
            stderr.write("".join(f"native detail {index}\n" for index in range(30)).encode("utf-8"))
            stderr.flush()
            raise RuntimeError("Python worker exited with code 2")

        with patch.object(runtime, "run_process", side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                runtime.run_probe("inspect", {"input": "missing.mkv"}, 5.0)
        message = str(caught.exception)
        self.assertIn("code 2", message)
        self.assertIn("native detail 29", message)
        self.assertNotIn("native detail 0\n", message)
        self.assertEqual(len(message.splitlines()), 21)

    def test_probe_timeout_remains_distinguishable_from_a_native_failure(self):
        error = runtime.BudgetExpired("deadline")
        with patch.object(runtime, "run_process", side_effect=error):
            with self.assertRaises(runtime.BudgetExpired) as caught:
                runtime.run_probe("inspect", {"input": "movie.mkv"}, 0.1)
        self.assertIs(caught.exception, error)

    def test_probe_rejects_invalid_or_nonobject_json(self):
        for stdout in ("not json", "[]"):
            with self.subTest(stdout=stdout), patch.object(runtime, "run_process", return_value=stdout):
                with self.assertRaisesRegex(RuntimeError, "JSON"):
                    runtime.run_probe("inspect", {"input": "movie.mkv"}, 5.0)

    def test_real_probe_failure_reports_its_input_error(self):
        with tempfile.TemporaryDirectory(prefix="test-crf-missing-") as temporary:
            with self.assertRaisesRegex(RuntimeError, "Input video does not exist"):
                runtime.run_probe("inspect", {"input": str(Path(temporary) / "missing.mkv")}, 5.0)

    def test_inspect_and_crop_run_in_real_python_workers(self):
        try:
            import av
        except ImportError as exc:
            raise unittest.SkipTest("PyAV is not installed") from exc
        if "bbox" not in av.filter.filters_available:
            raise unittest.SkipTest("PyAV has no native bbox filter")
        with tempfile.TemporaryDirectory(prefix="test-crf-probe-") as temporary:
            source = Path(temporary) / "bordered.mkv"
            with av.open(str(source), "w") as output:
                stream = output.add_stream("ffv1", rate=10)
                stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
                for index in range(2):
                    frame = av.VideoFrame(64, 64, "yuv420p")
                    for plane_index, plane in enumerate(frame.planes):
                        plane.update(bytes([16 if plane_index == 0 else 128]) * plane.buffer_size)
                    plane = frame.planes[0]
                    pixels = bytearray(bytes(plane))
                    for row in range(8, 56):
                        start = row * plane.line_size + 8
                        pixels[start:start + 48] = bytes([120]) * 48
                    plane.update(pixels)
                    frame.pts, frame.time_base, frame.duration = index, Fraction(1, 10), 1
                    for packet in stream.encode(frame):
                        output.mux(packet)
                for packet in stream.encode(None):
                    output.mux(packet)
            info = runtime.run_probe("inspect", {"input": str(source), "video_index": 0}, 5.0)
            self.assertEqual((info["width"], info["height"]), (64, 64))
            self.assertAlmostEqual(info["duration"], 0.2)
            crop = runtime.run_probe("crop", {
                "input": str(source), "video_index": 0, "source": info,
                "samples": [{"start": 0.0, "duration": 0.2, "kind": "representative"}],
                "settings": {"limit": 24 / 255, "seconds": 0.1},
            }, 5.0)
            self.assertEqual(crop["crop"], "48:48:8:8")
            self.assertEqual(crop["content_frames"], 2)

            # Also exercise relative imports when the worker is invoked as a module.
            job_path = Path(temporary) / "inspect.json"
            job_path.write_text(json.dumps({"operation": "inspect", "payload": {"input": str(source)}}), encoding="utf-8")
            stdout = runtime.run_process(
                [sys.executable, "-m", "scripts.crf_runtime", "--job", str(job_path)],
                timeout=5.0, stderr=subprocess.DEVNULL,
            )
            self.assertEqual(json.loads(stdout)["width"], 64)


if __name__ == "__main__":
    unittest.main()
