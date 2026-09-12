"""Mouse interaction, shared-axis estimates, and live model previews in Qt."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from bdrip.crf.gui.preview import FigureDialog, FigurePreview
except ImportError:
    QApplication = None

from bdrip.common.files import write_json
from bdrip.crf.model import fit_models, predict_bitrate
from tests.fixtures.models import anchors


def report(x264=None, x265=None):
    codecs = {"x264": {"rows": anchors() if x264 is None else x264}}
    if x265 is not None:
        codecs["x265"] = {"rows": x265}
    for analysis in codecs.values():
        analysis["models"] = fit_models(analysis["rows"])
    return {
        "method": "two_point",
        "state": "complete",
        "source": {"path": "Movie.mkv"},
        "codecs": codecs,
    }


@unittest.skipUnless(QApplication, "Install the gui extra to test interactive plots")
class InteractivePlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.preview = FigurePreview()
        self.preview.resize(1000, 700)
        self.preview.show()
        self.preview.set_report(report(x265=anchors(qps=(22, 29), rates=(8, 2))))
        self.chart = self.preview.chart
        self.app.processEvents()

    def tearDown(self):
        self.preview.close()
        self.preview.deleteLater()
        self.app.processEvents()

    def position(self, bitrate, height=0.5):
        canvas = self.chart.canvas
        axes = self.chart.axes[0]
        bottom, top = axes.get_ylim()
        x, y = axes.transData.transform((bitrate, bottom + (top - bottom) * height))
        ratio = canvas.device_pixel_ratio
        return QPoint(round(x / ratio), round((canvas.figure.bbox.height - y) / ratio))

    def hover(self, bitrate, height=0.5):
        QTest.mouseMove(self.chart.canvas, self.position(bitrate, height))
        self.app.processEvents()

    def click(self, bitrate, button=None):
        QTest.mouseClick(
            self.chart.canvas,
            button or Qt.MouseButton.LeftButton,
            pos=self.position(bitrate),
        )
        self.app.processEvents()

    def test_hover_uses_same_bitrate_for_both_codecs_at_any_mouse_height(self):
        with mock.patch(
            "subprocess.Popen", side_effect=AssertionError("Hover must not encode")
        ):
            for height in (0.2, 0.8):
                self.hover(7.37, height)
                bitrate = self.chart.hover_bitrate
                self.assertAlmostEqual(bitrate, 7.37, delta=0.03)
                for codec in ("x264", "x265"):
                    estimate = self.chart.estimates[codec]
                    self.assertEqual(
                        estimate, predict_bitrate(self.chart.models[codec], bitrate)
                    )
                    marker = self.chart.markers[codec]
                    self.assertEqual(list(marker.get_xdata()), [bitrate])
                    self.assertEqual(list(marker.get_ydata()), [estimate["average_qp"]])
                self.assertNotEqual(
                    self.chart.estimates["x264"]["crf"],
                    self.chart.estimates["x265"]["crf"],
                )
                self.assertEqual(
                    list(self.chart.cursor.get_xdata()), [bitrate, bitrate]
                )
                for text in ("Video bitrate", "x264", "x265", "B-frame QP", "Mbps"):
                    self.assertIn(text, self.chart.tooltip.get_text())
                self.assertNotIn("CRF", self.chart.tooltip.get_text())

    def test_cursor_clears_on_leave_and_task_switch(self):
        self.hover(8)
        QTest.mouseMove(self.chart.canvas, QPoint(1, 1))
        self.app.processEvents()
        self.assertIsNone(self.chart.hover_bitrate)
        self.assertTrue(all(not artist.get_visible() for artist in self.chart.artists))
        self.hover(7)
        self.preview.show_file(None)
        self.assertIs(self.preview.stack.currentWidget(), self.preview.image)
        self.assertEqual(self.chart.estimates, {})
        self.preview.set_report(report(x264=[]))
        self.app.processEvents()
        self.hover(8)
        self.assertEqual(set(self.chart.estimates), {"x264"})
        self.assertIsNone(self.chart.estimates["x264"]["average_qp"])

    def test_full_draw_does_not_request_a_recursive_qt_repaint(self):
        self.hover(8)
        with mock.patch.object(
            self.chart.canvas, "blit", wraps=self.chart.canvas.blit
        ) as repaint:
            self.chart.canvas.draw()
            repaint.assert_not_called()
            self.assertTrue(self.chart.tooltip.get_visible())
            self.hover(7)
            self.assertTrue(repaint.called)

    def test_click_pins_bitrate_until_moved_or_released(self):
        with mock.patch(
            "subprocess.Popen", side_effect=AssertionError("Pinning must not encode")
        ):
            self.click(7.37)
            pinned, estimates = self.chart.pinned_bitrate, dict(self.chart.estimates)
            self.assertAlmostEqual(pinned, 7.37, delta=0.03)
            self.assertEqual(
                estimates["x264"], predict_bitrate(self.chart.models["x264"], pinned)
            )
            self.assertIn("Pinned bitrate", self.chart.tooltip.get_text())
            self.assertTrue(self.chart.unpin_action.isEnabled())
            self.hover(12)
            QTest.mouseMove(self.chart.canvas, QPoint(1, 1))
            self.chart.clear_hover()
            self.app.processEvents()
            self.assertEqual(self.chart.pinned_bitrate, pinned)
            self.assertEqual(self.chart.estimates, estimates)
            self.assertTrue(self.chart.tooltip.get_visible())
            self.assertEqual(list(self.chart.cursor.get_xdata()), [pinned, pinned])
            self.click(12)
            self.assertAlmostEqual(self.chart.pinned_bitrate, 12, delta=0.03)
            self.chart.unpin_action.trigger()
            self.assertIsNone(self.chart.pinned_bitrate)
            self.assertFalse(self.chart.unpin_action.isEnabled())
            self.hover(7)
            self.assertAlmostEqual(self.chart.hover_bitrate, 7, delta=0.03)
            self.click(7)
            self.click(7.5, Qt.MouseButton.RightButton)
            self.assertIsNone(self.chart.pinned_bitrate)
            self.assertFalse(self.chart.tooltip.get_visible())

    def test_pin_survives_resize_zoom_and_new_models_but_clears_on_task_switch(self):
        self.preview.set_report(report(x264=anchors()[:1]))
        self.app.processEvents()
        self.click(8)
        pinned = self.chart.pinned_bitrate
        self.assertIsNone(self.chart.estimates["x264"]["average_qp"])
        self.chart.axes[0].set_xlim(4, 12)
        self.chart.canvas.draw()
        self.preview.resize(800, 580)
        self.app.processEvents()
        self.preview.set_report(report(x264=anchors(qps=(18, None))))
        self.app.processEvents()
        self.assertEqual(self.chart.pinned_bitrate, pinned)
        self.assertIsNone(self.chart.estimates["x264"]["average_qp"])
        self.preview.set_report(report(x265=anchors(qps=(22, 29), rates=(8, 2))))
        self.app.processEvents()
        self.assertEqual(self.chart.pinned_bitrate, pinned)
        self.assertEqual(self.chart.axes[0].get_xlim(), (4, 12))
        self.assertAlmostEqual(
            self.chart.estimates["x265"]["average_qp"], 22, delta=0.03
        )
        self.assertTrue(self.chart.tooltip.get_visible())
        self.preview.show_file(None)
        self.assertIsNone(self.chart.pinned_bitrate)
        self.assertEqual(self.chart.estimates, {})
        self.assertFalse(self.chart.unpin_action.isEnabled())

    def test_navigation_clicks_do_not_move_or_release_pin(self):
        self.click(8)
        pinned = self.chart.pinned_bitrate
        self.chart.toolbar.pan()
        self.click(7)
        self.click(7, Qt.MouseButton.RightButton)
        self.assertEqual(self.chart.pinned_bitrate, pinned)
        self.chart.toolbar.pan()
        self.chart.toolbar.zoom()
        self.click(7)
        self.assertEqual(self.chart.pinned_bitrate, pinned)
        self.chart.toolbar.zoom()
        self.chart.unpin_action.trigger()
        self.assertIsNone(self.chart.pinned_bitrate)

    def test_partial_missing_qp_and_constant_bitrate_do_not_invent_qp(self):
        for rows in (anchors()[:1], anchors(qps=(18, None)), anchors(rates=(8, 8))):
            self.preview.set_report(report(x264=rows))
            self.app.processEvents()
            QTest.mouseMove(self.chart.canvas, QPoint(1, 1))
            self.hover(8)
            self.assertIsNone(self.chart.estimates["x264"]["average_qp"])
            self.assertFalse(self.chart.markers["x264"].get_visible())
            self.assertIn("B-frame QP N/A", self.chart.tooltip.get_text())
        self.preview.set_report(report())
        self.app.processEvents()
        self.hover(8.1)
        self.assertIsNotNone(self.chart.estimates["x264"]["average_qp"])

    def test_resize_zoom_and_live_update_keep_bitrate_coordinates_correct(self):
        self.chart.axes[0].set_xlim(4, 12)
        self.chart.canvas.draw()
        self.preview.resize(800, 580)
        self.app.processEvents()
        self.hover(7.37)
        self.assertAlmostEqual(self.chart.hover_bitrate, 7.37, delta=0.03)
        self.preview.set_report(report())
        self.app.processEvents()
        self.assertEqual(self.chart.axes[0].get_xlim(), (4, 12))
        self.hover(8)
        self.assertAlmostEqual(
            self.chart.estimates["x264"]["average_qp"], 21.5, delta=0.03
        )
        self.chart.toolbar.home()
        self.app.processEvents()
        self.assertAlmostEqual(self.chart.axes[0].get_xlim()[1], 16 * 1.12)
        self.hover(2)
        self.assertIn("extrapolation", self.chart.tooltip.get_text())

    def test_larger_view_loads_and_refreshes_models_without_a_saved_png(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_json(path / "results.json", report(x264=anchors()[:1]))
            dialog = FigureDialog()
            try:
                dialog.set_file(path / "qp-bitrate.png", "Movie")
                dialog.show()
                self.app.processEvents()
                self.assertFalse(dialog.isModal())
                self.assertIs(
                    dialog.preview.stack.currentWidget(), dialog.preview.chart
                )
                self.assertIsNone(dialog.preview.chart.models["x264"]["qp"])
                canvas = dialog.preview.chart.canvas
                write_json(path / "results.json", report())
                dialog.refresh()
                self.app.processEvents()
                self.assertIs(dialog.preview.chart.canvas, canvas)
                self.assertIsNotNone(dialog.preview.chart.models["x264"]["qp"])
            finally:
                dialog.close()
                self.assertFalse(dialog.timer.isActive())
                dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
