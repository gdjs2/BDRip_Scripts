"""Behavioral tests for directional CRF search and recommendation evidence."""

from __future__ import annotations

import unittest

from scripts.crf_optimizer import auto_search, measured_summary, next_trial


def policy(**changes):
    return {"initial_crf": 17.5, "qp_target": 19.5, **changes}


def settings(**changes):
    return {"min_crf": 14.0, "max_crf": 23.0, "precision": 0.1,
            "max_trials": 6, **changes}


def measurement(crf, qp, *, target=19.5, stress=None):
    gate = max(qp, stress) if stress is not None else qp
    return {"crf": crf, "qp95": qp, "stress_qp": stress,
            "qp_pass": gate <= target, "bitrate_mbps": 10 * 2 ** ((17.5 - crf) / 6)}


class DirectionalSearchTests(unittest.TestCase):
    def test_user_x264_measurement_predicts_nearby_higher_crf_and_converges(self):
        rows = [measurement(17.5, 19.306)]
        rows[0].update(bitrate_mbps=7.9233088, worst_qp=19.45)
        self.assertEqual(next_trial(rows, policy(), settings()), (17.7, None))
        rows.append(measurement(17.7, 19.506))
        self.assertEqual(next_trial(rows, policy(), settings()), (17.6, None))
        rows.append(measurement(17.6, 19.406))
        candidate, summary = next_trial(rows, policy(), settings())
        self.assertIsNone(candidate)
        self.assertEqual(summary["recommended_crf"], 17.6)
        self.assertEqual(summary["reason"], "precision_reached")
        self.assertTrue(summary["verified"])

    def test_user_x265_measurement_predicts_lower_crf_and_checks_next_grid_point(self):
        codec_policy = policy(initial_crf=18.5, qp_target=20.5)
        rows = [measurement(18.5, 22.99050595, target=20.5)]
        rows[0].update(bitrate_mbps=4.3079197, worst_qp=23.065)
        self.assertEqual(next_trial(rows, codec_policy, settings()), (16.0, None))
        rows.append(measurement(16.0, 20.49050595, target=20.5))
        self.assertEqual(next_trial(rows, codec_policy, settings()), (16.1, None))
        rows.append(measurement(16.1, 20.59050595, target=20.5))
        candidate, summary = next_trial(rows, codec_policy, settings())
        self.assertIsNone(candidate)
        self.assertEqual(summary["recommended_crf"], 16.0)
        self.assertEqual(summary["reason"], "precision_reached")

    def test_stress_qp_controls_search_direction(self):
        rows = [measurement(17.5, 18.0, stress=22.0)]
        self.assertEqual(next_trial(rows, policy(), settings()), (15.0, None))

    def test_exact_target_tests_one_higher_grid_point(self):
        self.assertEqual(next_trial([measurement(17.5, 19.5)], policy(), settings()),
                         (17.6, None))

    def test_positive_measured_slope_guides_same_side_extrapolation(self):
        rows = [measurement(17.5, 17.0), measurement(19.5, 18.0)]
        self.assertEqual(next_trial(rows, policy(), settings()), (22.5, None))

    def test_flat_slope_expands_toward_untested_bound(self):
        rows = [measurement(17.5, 18.5), measurement(18.5, 18.5)]
        self.assertEqual(next_trial(rows, policy(), settings()), (20.5, None))

    def test_search_only_recommends_real_measured_passing_boundary(self):
        observed = []

        def evaluate(crf):
            observed.append(crf)
            return measurement(crf, crf + 1.67)

        rows, summary = auto_search(evaluate, policy(), settings())
        self.assertEqual(summary["recommended_crf"], 17.8)
        self.assertEqual(summary["reason"], "precision_reached")
        self.assertEqual({row["crf"] for row in rows}, set(observed))
        self.assertEqual(len(observed), len(set(observed)))
        self.assertTrue(any(row["crf"] == 17.9 and not row["qp_pass"] for row in rows))

    def test_native_x265_style_qp_plateau_converges_with_twelve_trial_budget(self):
        observed = []

        def evaluate(crf):
            observed.append(crf)
            if crf <= 14.5:
                qp = 19.81
            elif crf <= 14.6:
                qp = 20.00
            elif crf <= 15.2:
                qp = 20.81
            else:
                qp = max(20.81, crf + 5.32)
            return measurement(crf, qp, target=20.5)

        rows, summary = auto_search(evaluate, policy(initial_crf=18.5, qp_target=20.5),
                                    settings(max_trials=12))
        self.assertGreater(len(observed), 6)
        self.assertLessEqual(len(observed), 12)
        self.assertEqual(len(observed), len(set(observed)))
        self.assertEqual([row["crf"] for row in rows], sorted(observed))
        self.assertEqual(summary["reason"], "precision_reached")
        self.assertEqual(summary["recommended_crf"], 14.6)
        self.assertTrue(summary["converged"])
        self.assertTrue(any(row["crf"] == 14.6 and row["qp_pass"] for row in rows))
        self.assertTrue(any(row["crf"] == 14.7 and not row["qp_pass"] for row in rows))

    def test_nonmonotonic_observations_keep_only_provisional_candidate(self):
        rows = [measurement(17.5, 20), measurement(18.5, 19)]
        candidate, summary = next_trial(rows, policy(), settings())
        self.assertIsNone(candidate)
        self.assertEqual(summary["reason"], "nonmonotonic_observations")
        self.assertEqual(summary["best_tested_crf"], 18.5)
        self.assertIsNone(summary["recommended_crf"])
        self.assertFalse(summary["converged"])

    def test_upper_bound_requires_a_comparison(self):
        codec_policy = policy(initial_crf=23.0)
        initial = measurement(23.0, 19)
        candidate, summary = next_trial([initial], codec_policy, settings())
        self.assertEqual(candidate, 22.9)
        self.assertIsNone(summary)
        _, summary = next_trial([initial, measurement(candidate, 18.9)], codec_policy, settings())
        self.assertEqual(summary["recommended_crf"], 23.0)
        self.assertEqual(summary["reason"], "upper_bound_passes")

    def test_lower_bound_failure_requires_a_comparison(self):
        codec_policy = policy(initial_crf=14.0)
        initial = measurement(14.0, 20)
        self.assertEqual(next_trial([initial], codec_policy, settings()), (14.1, None))
        _, summary = next_trial([initial, measurement(14.1, 20.1)], codec_policy, settings())
        self.assertEqual(summary["reason"], "no_passing_crf_in_bounds")
        self.assertTrue(summary["converged"])
        self.assertFalse(summary["verified"])
        self.assertIsNone(summary["best_tested_crf"])

    def test_off_grid_upper_bound_is_tested_exactly(self):
        rows, summary = auto_search(lambda crf: measurement(crf, 10), policy(),
                                    settings(max_crf=20.03))
        self.assertEqual(summary["recommended_crf"], 20.03)
        self.assertIn(20.03, [row["crf"] for row in rows])

    def test_non_round_lower_bound_is_stable_and_not_repeated(self):
        low, high = 25.118768723996514, 26.945218159464417
        threshold = 25.32228644328755
        codec_policy = policy(initial_crf=(low + high) / 2, qp_target=threshold)
        search = settings(min_crf=low, max_crf=high, precision=0.5, max_trials=12)
        observed = []

        def evaluate(crf):
            observed.append(crf)
            return measurement(crf, crf, target=threshold)

        rows, summary = auto_search(evaluate, codec_policy, search)
        self.assertEqual(observed.count(low), 1)
        self.assertEqual(len(observed), len(set(observed)))
        self.assertTrue(all(low <= row["crf"] <= high for row in rows))
        self.assertEqual(summary["recommended_crf"], low)
        self.assertEqual(summary["reason"], "precision_reached")


class RecommendationEvidenceTests(unittest.TestCase):
    def test_lone_passing_measurement_is_not_recommended(self):
        rows, summary = auto_search(lambda crf: measurement(crf, 19), policy(),
                                    settings(max_trials=1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(summary["best_tested_crf"], 17.5)
        self.assertIsNone(summary["recommended_crf"])
        self.assertFalse(summary["verified"])
        self.assertFalse(summary["comparison_complete"])
        self.assertEqual(summary["recommendation_status"], "insufficient_comparisons")

    def test_time_limit_and_sweep_keep_best_tested_candidate_provisional(self):
        rows = [measurement(17.5, 19), measurement(18.0, 19.4)]
        for reason in ("time_limit", "trial_limit", "explicit_sweep", "interrupted"):
            with self.subTest(reason=reason):
                summary = measured_summary(rows, reason, settings())
                self.assertEqual(summary["best_tested_crf"], 18.0)
                self.assertIsNone(summary["recommended_crf"])
                self.assertFalse(summary["verified"])
                self.assertFalse(summary["converged"])
                self.assertTrue(summary["comparison_complete"])
                self.assertEqual(summary["recommendation_status"], "provisional")
                self.assertEqual(summary["bounds"], [14.0, 23.0])
                self.assertEqual(summary["precision"], 0.1)

    def test_repeated_measurement_does_not_satisfy_comparison(self):
        row = measurement(23.0, 19)
        summary = measured_summary([row, dict(row)], "upper_bound_passes")
        self.assertEqual(summary["trials"], 1)
        self.assertFalse(summary["converged"])
        self.assertFalse(summary["comparison_complete"])
        self.assertIsNone(summary["recommended_crf"])

    def test_no_passing_observations_never_create_a_candidate(self):
        summary = measured_summary([measurement(18.5, 23)], "time_limit")
        self.assertIsNone(summary["best_tested_crf"])
        self.assertIsNone(summary["recommended_crf"])
        self.assertEqual(summary["recommendation_status"], "no_passing_crf")

    def test_bad_evaluator_cannot_invent_an_unmeasured_recommendation(self):
        with self.assertRaisesRegex(ValueError, "different trial"):
            auto_search(lambda crf: measurement(crf + 0.1, 19), policy(), settings())


if __name__ == "__main__":
    unittest.main()
