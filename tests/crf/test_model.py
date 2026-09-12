"""Known two-point fits, missing B-frames, and measured-versus-estimated plots."""

import math
import unittest

from bdrip.crf.model import fit_models, predict, predict_bitrate
from bdrip.crf.plot import make_figure
from tests.fixtures.models import anchors


class ModelTests(unittest.TestCase):
    def test_qp_is_linear_and_bitrate_midpoint_is_geometric(self):
        rows = anchors()
        models = fit_models(rows)
        self.assertEqual(models["qp"], {"a": 5, "b": 1})
        self.assertAlmostEqual(models["log_bitrate"]["e"], math.log(0.25) / 7)
        for row in rows:
            estimated = predict(models, row["crf"])
            self.assertAlmostEqual(estimated["average_qp"], row["average_qp"])
            self.assertAlmostEqual(
                estimated["average_bitrate_mbps"], row["average_bitrate_mbps"]
            )
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

    def test_incomplete_endpoint_is_excluded_from_fitting(self):
        rows = anchors()
        rows[1]["complete"] = False
        model = fit_models(rows)
        self.assertIsNone(model["qp"])
        self.assertIsNone(model["log_bitrate"])

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
        for rows in (
            anchors(rates=(0, 4)),
            anchors(rates=(-1, 4)),
            anchors(rates=(float("inf"), 4)),
            anchors(qps=(float("nan"), 25)),
            anchors() + anchors()[:1],
            [{**anchors()[0], "crf": 14}],
        ):
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

    def test_bitrate_predictions_match_anchors_and_geometric_midpoint(self):
        model = fit_models(anchors())
        for rate, qp, crf in ((16, 18, 13), (8, 21.5, 16.5), (4, 25, 20)):
            estimate = predict_bitrate(model, rate)
            self.assertAlmostEqual(estimate["average_qp"], qp)
            self.assertAlmostEqual(estimate["crf"], crf)
            self.assertEqual(estimate["average_bitrate_mbps"], rate)
            self.assertFalse(estimate["extrapolated"])
        self.assertTrue(predict_bitrate(model, 2)["extrapolated"])
        self.assertTrue(predict_bitrate(model, 18)["extrapolated"])

    def test_bitrate_predictions_handle_missing_qp_and_ambiguous_inverse(self):
        for rows in ([], anchors()[:1], anchors(qps=(None, 25)), anchors(rates=(8, 8))):
            estimate = predict_bitrate(fit_models(rows), 8)
            self.assertIsNone(estimate["average_qp"])
            self.assertNotEqual(estimate["status"], "ready")
        estimate = predict_bitrate(fit_models(anchors(qps=(None, 25))), 8)
        self.assertAlmostEqual(estimate["crf"], 16.5)
        self.assertIsNone(predict_bitrate(fit_models(anchors(rates=(8, 8))), 8)["crf"])
        flat_qp = predict_bitrate(fit_models(anchors(qps=(20, 20))), 8)
        self.assertEqual(flat_qp["average_qp"], 20)
        for rate in (0, -1, True, float("inf"), float("nan")):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                predict_bitrate(fit_models(anchors()), rate)


class PlotTests(unittest.TestCase):
    def figure(self, x264, x265=None):
        report = {
            "state": "complete",
            "source": {"path": "movie.mkv"},
            "codecs": {"x264": {"rows": x264}},
        }
        if x265 is not None:
            report["codecs"]["x265"] = {"rows": x265}
        figure = make_figure(report)
        self.addCleanup(figure.clear)
        return figure

    def test_bitrate_and_qp_share_one_axis_pair_with_distinct_codecs(self):
        figure = self.figure(anchors(), anchors(qps=(22, 29), rates=(8, 2)))
        self.assertEqual(len(figure.axes), 1)
        axes = figure.axes[0]
        self.assertEqual(axes.get_xlabel(), "Video bitrate (Mbps)")
        self.assertEqual(axes.get_ylabel(), "Average B-frame QP")
        self.assertEqual(axes.get_xscale(), "linear")
        self.assertEqual(axes.get_yscale(), "linear")
        self.assertEqual([line.get_label() for line in axes.lines], ["x264", "x265"])
        for points, expected in zip(
            axes.collections, ([(16, 18), (4, 25)], [(8, 22), (2, 29)])
        ):
            self.assertEqual(
                points.get_offsets().tolist(), [list(pair) for pair in expected]
            )
        figure.canvas.draw()
        self.assertEqual(
            [text.get_text() for text in axes.get_legend().get_texts()],
            ["x264", "x265"],
        )

    def test_curve_evaluates_qp_as_logarithm_of_bitrate(self):
        curve = self.figure(anchors()).axes[0].lines[0]
        x, y = curve.get_xdata(), curve.get_ydata()
        self.assertGreater(len(x), 2)
        self.assertTrue(all(left < right for left, right in zip(x, x[1:])))
        for rate, qp in zip(x, y):
            self.assertAlmostEqual(qp, 18 + 7 * math.log(rate / 16) / math.log(0.25))
        self.assertAlmostEqual(y[0], 25)
        self.assertAlmostEqual(y[-1], 18)

    def test_partial_or_missing_qp_keeps_only_measured_points(self):
        for rows in (anchors()[:1], anchors(qps=(18, None))):
            axes = self.figure(rows).axes[0]
            self.assertEqual(len(axes.lines), 0)
            self.assertEqual(axes.collections[0].get_offsets().tolist(), [[16, 18]])
        axes = self.figure(anchors(qps=(None, None))).axes[0]
        self.assertFalse(axes.lines)
        self.assertFalse(axes.collections)
        self.assertIn("B-frame QP unavailable", axes.texts[0].get_text())

    def test_equal_bitrates_keep_points_without_inventing_a_curve(self):
        axes = self.figure(anchors(rates=(8, 8))).axes[0]
        self.assertFalse(axes.lines)
        self.assertEqual(axes.collections[0].get_offsets().tolist(), [[8, 18], [8, 25]])

    def test_incomplete_endpoint_is_not_drawn(self):
        rows = anchors()
        rows[1]["complete"] = False
        axes = self.figure(rows).axes[0]
        self.assertFalse(axes.lines)
        self.assertEqual(axes.collections[0].get_offsets().tolist(), [[16, 18]])


if __name__ == "__main__":
    unittest.main()
