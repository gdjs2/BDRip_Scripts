"""Queue persistence, native background work, cancellation, and GUI responsiveness."""

import csv
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from bdrip.crf.gui.app import EncoderSettingsDialog, Window
    from bdrip.crf.gui.queue import TaskQueue
except ImportError:
    QApplication = None

from bdrip.crf.config import load_config, validate_config
from bdrip.crf.model import predict
from tests.fixtures.media import make_media


@unittest.skipUnless(QApplication, "Install the gui extra to test the desktop queue")
class QueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crf-gui-tests-")
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "Movie with spaces 日本語.mkv"
        make_media(self.source, audio=False)
        self.config = validate_config(
            {
                "video": {"crop": None},
                "codecs": {
                    "x264": {
                        "preset": "ultrafast",
                        "params": {
                            "threads": 1,
                            "bframes": 2,
                            "b-adapt": 0,
                            "rc-lookahead": 4,
                        },
                    },
                    "x265": {
                        "preset": "ultrafast",
                        "params": {
                            "pools": "none",
                            "frame-threads": 1,
                            "bframes": 2,
                            "b-adapt": 0,
                            "rc-lookahead": 4,
                        },
                    },
                },
            }
        )
        self.window = Window(self.directory / "queue")
        self.queue = self.window.queue
        self.queue.timer.setInterval(20)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.queue.pause()
        if self.queue.active:
            self.queue.cancel(self.queue.active)
            self.wait_until(lambda: self.queue.active is None)
        self.window.close()
        self.app.processEvents()
        self.temporary.cleanup()

    def wait_until(self, predicate, timeout=45):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(
            predicate(),
            "Timed out: " + str([(t.state, t.error) for t in self.queue.tasks]),
        )

    def test_saved_queue_snapshots_configs_and_keeps_artifacts_on_remove(self):
        first, second = self.queue.add(
            [str(self.source), str(self.source)], self.config, "both"
        )
        self.assertNotEqual(self.queue.output(first), self.queue.output(second))
        self.config["codecs"]["x264"]["preset"] = "fast"
        self.assertEqual(first.config["codecs"]["x264"]["preset"], "ultrafast")
        first.config["video"]["cropdetect"]["seconds"] = 3
        self.assertEqual(second.config["video"]["cropdetect"]["seconds"], 2)
        self.queue.save()
        with self.assertRaisesRegex(ValueError, "already open"):
            TaskQueue(self.queue.workspace)
        self.queue.close()
        restored = TaskQueue(self.queue.workspace)
        try:
            self.assertEqual(
                [task.id for task in restored.tasks], [first.id, second.id]
            )
            self.assertEqual(
                restored.tasks[0].config["video"]["cropdetect"]["seconds"], 3
            )
            self.assertFalse(restored.enabled)
            restored.cancel(restored.tasks[0])
            self.assertEqual(restored.tasks[0].state, "cancelled")
            restored.remove(restored.tasks[0])
            self.assertTrue((restored.output(first) / "config.json").is_file())
        finally:
            restored.close()

    def test_encoder_edits_are_saved_and_used_by_background_encoders(self):
        self.window.config = validate_config(self.config)
        self.window.fill_settings()
        original = self.queue.add([str(self.source)], self.config, "both")[0]
        self.assertEqual(self.window.sample_count.value(), 10)
        self.assertEqual(self.window.sample_seconds.value(), 10)
        self.window.sample_count.setValue(2)
        self.window.sample_seconds.setValue(0.5)
        dialog = EncoderSettingsDialog(self.window.config, self.window)
        x264, x265 = dialog.editors["x264"], dialog.editors["x265"]
        x264.preset.setCurrentText("superfast")
        x264.level.setText("4.0")
        x264.tune.setText("grain")
        x264.params.setPlainText("threads=1\nbframes=2:rc-lookahead=4:b-adapt=0")
        x264.add_option("threads", "1")
        x265.preset.setCurrentText("veryfast")
        x265.level.setText("auto")
        dialog.accept()
        self.assertEqual(
            dialog.result(), dialog.DialogCode.Accepted, dialog.error.text()
        )
        # Crop edits made before opening the encoder dialog are preserved.
        self.window.crop.setText("94:62:1:1")
        with (
            mock.patch("bdrip.crf.gui.app.EncoderSettingsDialog", return_value=dialog),
            mock.patch.object(dialog, "exec", return_value=dialog.DialogCode.Accepted),
        ):
            self.window.edit_encoders()
        self.assertEqual(self.window.crop.text(), "94:62:1:1")
        with mock.patch(
            "bdrip.crf.gui.app.QFileDialog.getOpenFileNames",
            return_value=([str(self.source)], ""),
        ):
            self.window.add_videos()
        task = self.queue.tasks[-1]
        self.assertEqual(original.config, self.config)
        self.assertEqual(task.config["video"]["crop"], "94:62:1:1")
        self.assertEqual(
            task.config["sampling"], {"count": 2, "seconds": 0.5, "seed": 0}
        )
        saved = self.directory / "edited.json"
        with mock.patch(
            "bdrip.crf.gui.app.QFileDialog.getSaveFileName",
            return_value=(str(saved), ""),
        ):
            self.window.save_settings()
        self.assertEqual(load_config(saved), task.config)
        self.queue.remove(original)
        observed_samples = set()
        self.queue.changed.connect(
            lambda: observed_samples.add(task.progress.get("sample_index"))
        )
        self.queue.start()
        self.wait_until(lambda: not self.queue.enabled and self.queue.active is None)
        self.assertEqual(task.state, "completed", task.error)
        report = json.loads((self.queue.output(task) / "results.json").read_text())
        self.assertEqual(report["config"], task.config)
        self.assertEqual((task.progress["completed"], task.progress["total"]), (8, 8))
        self.assertTrue({1, 2} <= observed_samples)
        self.assertEqual(len(report["sample_plan"]), 2)
        for codec, analysis in report["codecs"].items():
            for row in analysis["rows"]:
                self.assertEqual(row["sample_count"], 2)
                self.assertTrue(row["complete"])
                options = row["samples"][0]["encoder_options"]
                self.assertEqual(
                    options["preset"], "superfast" if codec == "x264" else "veryfast"
                )
                self.assertIn("bframes=2", options[f"{codec}-params"])
                if codec == "x264":
                    self.assertEqual(
                        (options["level"], options["tune"], options["threads"]),
                        ("4.0", "grain", "1"),
                    )

    def test_invalid_encoder_options_are_rejected_and_cancel_preserves_settings(self):
        original = validate_config(self.window.config)
        dialog = EncoderSettingsDialog(original, self.window)
        dialog.editors["x264"].params.setPlainText("crf=30")
        dialog.accept()
        self.assertNotEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertIn("crf", dialog.error.text())
        dialog.restore_defaults()
        dialog.editors["x265"].add_option("threads", "2")
        dialog.editors["x265"].add_option("threads", "3")
        dialog.accept()
        self.assertIn("duplicate", dialog.error.text())
        dialog.reject()
        self.assertEqual(self.window.config, original)
        self.assertEqual(dialog.config, original)
        dialog.restore_defaults()
        dialog.accept()
        self.assertEqual(dialog.config["codecs"], load_config()["codecs"])
        dialog.close()

    def test_larger_preview_fits_zooms_and_refreshes_without_blocking_window(self):
        task = self.queue.add([str(self.source)], self.config, "x264")[0]
        path = self.queue.output(task) / "qp-bitrate.png"
        image = QImage(1440, 960, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFF)
        self.assertTrue(image.save(str(path)))
        task.state = "completed"
        self.window.refresh()
        self.assertFalse(self.window.figure.image.original.isNull())
        self.window.preview_figure()
        preview = self.window.figure_dialog
        self.app.processEvents()
        self.assertFalse(preview.isModal())
        self.assertTrue(self.window.isEnabled())
        self.assertLess(preview.preview.image.scale, 1)
        preview.preview.image.set_zoom(1)
        self.app.processEvents()
        self.assertEqual(preview.preview.image.canvas.pixmap().size(), image.size())
        self.assertGreater(
            preview.preview.image.scroll.horizontalScrollBar().maximum(), 0
        )
        previous = preview.preview.image.original.cacheKey()
        image.fill(0x123456)
        self.assertTrue(image.save(str(path)))
        preview.refresh()
        self.assertNotEqual(preview.preview.image.original.cacheKey(), previous)
        self.assertEqual(preview.preview.image.zoom, 1)
        self.assertEqual(
            preview.preview.image.original.toImage().pixelColor(0, 0).name(), "#123456"
        )
        preview.preview.image.set_zoom(None)
        self.app.processEvents()
        self.assertLess(preview.preview.image.scale, 1)
        preview.close()
        self.assertFalse(preview.timer.isActive())
        waiting = self.queue.add([str(self.source)], self.config, "x264")[0]
        self.window.tree.setCurrentItem(self.window.items[waiting.id])
        self.assertTrue(self.window.figure.image.original.isNull())
        self.assertFalse(self.window.figure_button.isEnabled())
        self.window.tree.setCurrentItem(self.window.items[task.id])
        self.window.preview_figure()
        self.window.close()
        self.assertFalse(preview.isVisible())
        self.assertFalse(preview.timer.isActive())

    def test_failure_does_not_block_later_videos_and_gui_remains_responsive(self):
        invalid = self.directory / "broken.mkv"
        invalid.write_text("not a video")
        failed = self.queue.add([str(invalid)], self.config, "x264")[0]
        first = self.queue.add([str(self.source)], self.config, "both")[0]
        second = self.queue.add([str(self.source)], self.config, "x264")[0]
        ticks, observed, partial = [], [], []
        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()

        def observe():
            if self.queue.active:
                task = self.queue.active
                if not observed or observed[-1] != task.id:
                    observed.append(task.id)
                if 0 < task.percent < 100:
                    partial.append(task.percent)

        self.queue.changed.connect(observe)
        self.queue.start()
        try:
            self.wait_until(
                lambda: not self.queue.enabled and self.queue.active is None
            )
        finally:
            timer.stop()
        self.assertEqual(
            [task.state for task in self.queue.tasks],
            ["failed", "completed", "completed"],
        )
        self.assertEqual(observed, [failed.id, first.id, second.id])
        self.assertGreater(len(ticks), 50)
        self.assertTrue(partial)
        self.assertGreaterEqual(first.started, failed.finished)
        self.assertGreaterEqual(second.started, first.finished)
        for task, count in ((first, 4), (second, 2)):
            output = self.queue.output(task)
            for filename in (
                "config.json",
                "progress.json",
                "sample-progress.json",
                "results.json",
                "summary.csv",
                "estimates.csv",
                "qp-bitrate.png",
                "qp-bitrate.svg",
                "task.log",
            ):
                self.assertGreater((output / filename).stat().st_size, 0, filename)
            self.assertEqual(len(list((output / "logs").glob("*.log"))), count)
            with (output / "summary.csv").open() as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), count)
            self.assertEqual(task.progress["completed"], count)
            self.assertEqual(task.percent, 100)
            self.assertEqual(
                json.loads((output / "results.json").read_text())["qp_frame_type"], "B"
            )
        self.window.tree.setCurrentItem(self.window.items[first.id])
        self.window.refresh_details()
        self.assertFalse(self.window.figure.image.original.isNull())
        self.assertIs(
            self.window.figure.stack.currentWidget(), self.window.figure.chart
        )
        self.assertIn("profile=high", self.window.log.toPlainText())
        self.assertEqual(self.window.progress_bar.value(), 100)
        self.assertEqual(self.window.models.table.rowCount(), 2)
        report = json.loads((self.queue.output(first) / "results.json").read_text())
        self.window.models.crf.setValue(16.5)
        for index, codec in enumerate(("x264", "x265")):
            estimated = predict(report["codecs"][codec]["models"], 16.5)
            self.assertAlmostEqual(
                float(self.window.models.table.item(index, 1).text()),
                estimated["average_qp"],
                delta=0.0005,
            )
            self.assertAlmostEqual(
                float(self.window.models.table.item(index, 2).text()),
                estimated["average_bitrate_mbps"],
                delta=0.0005,
            )
        self.window.models.crf.setValue(21)
        self.assertIn("Extrapolation", self.window.models.message.text())
        self.assertIn("ln R(c)", self.window.models.equations.text())

    def test_cancellation_reaps_encoder_preserves_results_and_retry_reuses_cache(self):
        self.config["sampling"].update(count=2, seconds=0.5)
        task = self.queue.add([str(self.source)], self.config, "x264")[0]
        self.queue.start()
        self.wait_until(
            lambda: (
                task.progress.get("crf") == 13
                and task.progress.get("sample_index") == 2
                and task.progress.get("stage") == "encoding"
                and task.progress.get("encoder_pid")
            )
        )
        encoder_pid = task.progress["encoder_pid"]
        self.queue.pause()
        self.queue.cancel(task)
        self.wait_until(lambda: self.queue.active is None)
        self.assertEqual(task.state, "cancelled")
        if os.name == "posix":
            with self.assertRaises(ProcessLookupError):
                os.kill(encoder_pid, 0)
        output = self.queue.output(task)
        interrupted = json.loads((output / "results.json").read_text())
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertEqual(interrupted["codecs"]["x264"]["rows"][0]["crf"], 13)
        # The tiny second clip may finish while Qt delivers cancellation.
        # Retain every completed clip, including one that won that race.
        saved_row = interrupted["codecs"]["x264"]["rows"][0]
        self.assertIn(saved_row["sample_count"], (1, 2))
        self.assertEqual(saved_row["complete"], saved_row["sample_count"] == 2)
        self.assertIsNone(interrupted["codecs"]["x264"]["models"]["log_bitrate"])
        self.assertTrue((output / "qp-bitrate.png").is_file())
        saved_logs = {
            path.name: path.read_bytes() for path in (output / "logs").glob("*.log")
        }
        self.queue.retry(task)
        self.queue.start()
        self.wait_until(lambda: self.queue.active is None and not self.queue.enabled)
        self.assertEqual(task.state, "completed", task.error)
        self.assertEqual(task.attempts, 2)
        report = json.loads((output / "results.json").read_text())
        self.assertEqual(report["sample_plan"], interrupted["sample_plan"])
        self.assertEqual((task.progress["completed"], task.progress["total"]), (4, 4))
        self.assertTrue(
            all(
                row["complete"] and row["sample_count"] == 2
                for row in report["codecs"]["x264"]["rows"]
            )
        )
        self.assertTrue(report["codecs"]["x264"]["rows"][0]["samples"][0]["cached"])
        resumed_samples = report["codecs"]["x264"]["rows"][0]["samples"]
        for saved_sample, resumed_sample in zip(saved_row["samples"], resumed_samples):
            self.assertEqual(resumed_sample, {**saved_sample, "cached": True})
        for filename, contents in saved_logs.items():
            self.assertEqual((output / "logs" / filename).read_bytes(), contents)
        self.assertIn("Attempt 2", (output / "task.log").read_text())

    def test_closing_window_cancels_active_task_and_preserves_waiting_queue(self):
        task, waiting = self.queue.add(
            [str(self.source), str(self.source)], self.config, "x264"
        )
        self.queue.start()
        self.wait_until(
            lambda: (
                task.progress.get("stage") == "encoding"
                and task.progress.get("encoder_pid")
            )
        )
        self.window.close()
        self.assertTrue(self.window.closing)
        self.wait_until(
            lambda: self.queue.active is None and not self.window.isVisible()
        )
        self.assertEqual(task.state, "cancelled")
        self.assertEqual(waiting.state, "queued")
        restored = TaskQueue(self.queue.workspace)
        try:
            self.assertEqual(
                [item.state for item in restored.tasks], ["cancelled", "queued"]
            )
            self.assertFalse(restored.enabled)
        finally:
            restored.close()

    def test_recovery_and_corrupt_queue_do_not_discard_saved_tasks(self):
        task = self.queue.add([str(self.source)], self.config, "x264")[0]
        task.state = "running"
        task.started = 1000.0
        self.queue.save()
        self.queue.close()
        restored = TaskQueue(self.queue.workspace)
        self.assertEqual(restored.tasks[0].state, "interrupted")
        restored.close()
        (self.queue.output(task) / "progress.json").write_text(
            json.dumps(
                {
                    "state": "complete",
                    "message": "Completed",
                    "elapsed_seconds": 42.5,
                }
            )
        )
        restored = TaskQueue(self.queue.workspace)
        self.assertEqual(restored.tasks[0].state, "completed")
        self.assertEqual(restored.tasks[0].finished, 1042.5)
        self.assertEqual(restored.tasks[0].error, "")
        restored.close()
        path = self.queue.workspace / "queue.json"
        path.write_text("invalid json")
        with self.assertRaisesRegex(ValueError, "left intact"):
            TaskQueue(self.queue.workspace)
        self.assertEqual(path.read_text(), "invalid json")

    def test_old_queue_migrates_waiting_tasks_and_retains_historical_results_on_retry(
        self,
    ):
        waiting, history = self.queue.add(
            [str(self.source), str(self.source)], self.config, "x264"
        )
        history.state, history.attempts = "cancelled", 1
        self.queue.save()
        path = self.queue.workspace / "queue.json"
        data = json.loads(path.read_text())
        data["schema_version"] = 1
        for item in data["tasks"]:
            item.pop("method")
            item["config"]["sampling"] = {"count": 10, "seed": 42}
        path.write_text(json.dumps(data))
        output = self.queue.output(history)
        original = {
            "results.json": '{"schema_version":3}',
            "task.log": "old sweep log",
            "qp-bitrate.png": "old figure",
        }
        for filename, contents in original.items():
            (output / filename).write_text(contents)
        self.queue.close()
        restored = TaskQueue(self.queue.workspace)
        try:
            waiting, history = restored.tasks
            self.assertEqual(waiting.method, "two_point")
            self.assertEqual(
                waiting.config["sampling"], {"count": 10, "seconds": 10, "seed": 42}
            )
            self.assertEqual(history.method, "sweep")
            self.assertEqual(history.config["sampling"], {"count": 10, "seed": 42})
            retried = restored.retry(history)
            self.assertEqual(retried.method, "two_point")
            self.assertNotEqual(restored.output(retried), output)
            self.assertEqual(retried.config, validate_config(history.config))
            self.assertEqual(history.state, "cancelled")
            for filename, contents in original.items():
                self.assertEqual((output / filename).read_text(), contents)
            self.assertEqual(json.loads(path.read_text())["schema_version"], 2)
        finally:
            restored.close()

    def test_retry_keeps_old_single_clip_artifacts_in_original_folder(self):
        history = self.queue.add([str(self.source)], self.config, "x264")[0]
        history.state, history.attempts = "cancelled", 1
        history.config.pop("sampling")
        original_config = json.loads(json.dumps(history.config))
        output = self.queue.output(history)
        artifact = output / "results.json"
        artifact.write_text('{"schema_version":4,"sample_plan":[{"duration":60}]}')
        original = artifact.read_bytes()
        self.queue.save()
        self.queue.close()
        restored = TaskQueue(self.queue.workspace)
        try:
            history = restored.tasks[0]
            self.assertEqual(history.config, original_config)
            retried = restored.retry(history)
            self.assertNotEqual(restored.output(retried), output)
            self.assertEqual(
                retried.config["sampling"], {"count": 10, "seconds": 10, "seed": 0}
            )
            self.assertEqual(artifact.read_bytes(), original)
        finally:
            restored.close()


if __name__ == "__main__":
    unittest.main()
