"""Known two-point fits, missing B-frames, and measured-versus-estimated plots."""

import math
import unittest

from scripts.crf_model import fit_models, predict
from scripts.crf_plot import make_figure


def anchors(qps=(18, 25), rates=(16, 4)):
    return [{"crf": crf, "average_qp": qp, "average_bitrate_mbps": rate}
            for crf, qp, rate in zip((13, 20), qps, rates)]


class ModelTests(unittest.TestCase):
    def test_qp_is_linear_and_bitrate_midpoint_is_geometric(self):
        rows = anchors()
        models = fit_models(rows)
        self.assertEqual(models["qp"], {"a": 5, "b": 1})
        self.assertAlmostEqual(models["log_bitrate"]["e"], math.log(0.25) / 7)
        for row in rows:
            estimated = predict(models, row["crf"])
            self.assertAlmostEqual(estimated["average_qp"], row["average_qp"])
            self.assertAlmostEqual(estimated["average_bitrate_mbps"], row["average_bitrate_mbps"])
        middle = predict(models, 16.5)
        self.assertEqual(middle["average_qp"], 21.5)
        self.assertAlmostEqual(middle["average_bitrate_mbps"], 8)
        self.assertFalse(middle["extrapolated"])
        self.assertEqual(fit_models(rows[::-1]), models)

    def test_constant_measurements_produce_constant_models(self):
        models = fit_models(anchors(qps=(20, 20), rates=(10, 10)))
        self.assertEqual(models["qp"], {"a": 20, "b": 0})
        self.assertEqual(models["log_bitrate"]["e"], 0)
        self.assertAlmostEqual(predict(models, 16)["average_bitrate_mbps"], 10)

    def test_no_model_with_one_point_and_no_qp_fit_without_b_frames(self):
        for rows in ([], anchors()[:1]):
            model = fit_models(rows)
            self.assertIsNone(model["qp"])
            self.assertIsNone(model["log_bitrate"])
        for qps in ((None, 25), (18, None), (None, None)):
            model = fit_models(anchors(qps=qps))
            self.assertIsNone(model["qp"])
            self.assertIn("unavailable", model["qp_status"])
            self.assertAlmostEqual(predict(model, 16.5)["average_bitrate_mbps"], 8)

    def test_invalid_measurements_are_rejected(self):
        for rows in (anchors(rates=(0, 4)), anchors(rates=(-1, 4)),
                     anchors(rates=(float("inf"), 4)), anchors(qps=(float("nan"), 25)),
                     anchors() + anchors()[:1], [{**anchors()[0], "crf": 14}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                fit_models(rows)

    def test_extrapolation_is_identified_and_invalid_crfs_rejected(self):
        models = fit_models(anchors())
        for crf in (0, 12.9, 20.1, 51):
            self.assertTrue(predict(models, crf)["extrapolated"])
        for crf in (13, 20):
            self.assertFalse(predict(models, crf)["extrapolated"])
        for crf in (-1, 52, True, float("inf"), float("nan")):
            with self.subTest(crf=crf), self.assertRaises(ValueError):
                predict(models, crf)


class PlotTests(unittest.TestCase):
    def figure(self, x264, x265=None):
        report = {"state": "complete", "source": {"path": "movie.mkv"},
                  "codecs": {"x264": {"rows": x264}}}
        if x265 is not None:
            report["codecs"]["x265"] = {"rows": x265}
        figure = make_figure(report)
        self.addCleanup(figure.clear)
        return figure

    def test_qp_and_bitrate_share_one_plot_with_two_scales_and_distinct_line_styles(self):
        figure = self.figure(anchors(), anchors(qps=(22, 29), rates=(8, 2)))
        self.assertEqual(len(figure.axes), 2)
        qp, rate = figure.axes
        self.assertEqual(qp.get_ylabel(), "Average B-frame QP")
        self.assertEqual(rate.get_ylabel(), "Video bitrate (Mbps)")
        figure.canvas.draw()
        self.assertEqual(qp.get_position().bounds, rate.get_position().bounds)
        self.assertTrue(qp.get_shared_x_axes().joined(qp, rate))
        self.assertEqual([line.get_linestyle() for line in qp.lines], ["-", "-"])
        self.assertEqual([line.get_linestyle() for line in rate.lines], ["--", "--"])
        self.assertEqual(rate.yaxis.get_label_position(), "right")
        self.assertEqual(qp.yaxis.get_label_position(), "left")
        for axes, metric in ((qp, "QP"), (rate, "bitrate")):
            self.assertEqual(axes.get_xlabel(), "CRF (c)")
            self.assertEqual([line.get_label() for line in axes.lines], [f"x264 {metric}", f"x265 {metric}"])
            self.assertEqual([points.get_label() for points in axes.collections], [f"x264 {metric} measured", f"x265 {metric} measured"])
            self.assertEqual(list(axes.collections[0].get_offsets()[:, 0]), [13, 20])
        self.assertAlmostEqual(qp.lines[0].get_ydata()[100], 21.5)
        self.assertAlmostEqual(rate.lines[0].get_ydata()[100], 8)

    def test_missing_b_qp_suppresses_only_qp_curve(self):
        qp, rate = self.figure(anchors(qps=(18, None))).axes
        self.assertEqual(len(qp.lines), 0)
        self.assertEqual(list(qp.collections[0].get_offsets()[:, 0]), [13])
        self.assertEqual(len(rate.lines), 1)
        self.assertEqual(list(rate.collections[0].get_offsets()[:, 0]), [13, 20])

    def test_bitrate_curve_evaluates_exponential_on_linear_axis(self):
        rate = self.figure(anchors()).axes[1]
        self.assertEqual(rate.get_yscale(), "linear")
        curve = rate.lines[0]
        x, y = curve.get_xdata(), curve.get_ydata()
        self.assertGreater(len(x), 2)
        for crf, bitrate in zip(x, y):
            self.assertAlmostEqual(bitrate, 16 * 0.25 ** ((crf - 13) / 7))
        middle = len(x) // 2
        self.assertEqual(x[middle], 16.5)
        self.assertAlmostEqual(y[middle], 8)
        self.assertNotAlmostEqual(y[middle], (y[0] + y[-1]) / 2)
        self.assertGreater(y[0] - y[middle], y[middle] - y[-1])

    def test_partial_report_has_measured_markers_but_no_fitted_curves(self):
        for axes in self.figure(anchors()[:1]).axes:
            self.assertEqual(len(axes.lines), 0)
            self.assertEqual(list(axes.collections[0].get_offsets()[:, 0]), [13])


if __name__ == "__main__":
    unittest.main()
