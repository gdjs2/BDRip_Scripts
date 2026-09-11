"""Select measured CRF trials and distinguish candidates from convergence."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_FLOOR
from typing import Callable


_EPSILON = 1e-9
_CONVERGED_REASONS = {"precision_reached", "upper_bound_passes", "no_passing_crf_in_bounds"}


def _observations(rows: list[dict]) -> list[dict]:
    """Repeated copies of a measurement do not constitute a comparison."""
    return sorted({float(row["crf"]): row for row in rows}.values(), key=lambda row: row["crf"])


def _gate(row: dict) -> float:
    stress = row.get("stress_qp")
    return max(row["qp95"], stress) if stress is not None else row["qp95"]


def measured_summary(rows: list[dict], reason: str, search: dict | None = None) -> dict:
    """Describe actual evidence without presenting an unfinished search as best."""
    measured = _observations(rows)
    passing = [row["crf"] for row in measured if row["qp_pass"]]
    best_tested = max(passing, default=None)
    compared = len(measured) >= 2
    converged = compared and reason in _CONVERGED_REASONS
    recommended = (best_tested if converged
                   and reason in {"precision_reached", "upper_bound_passes"} else None)
    if best_tested is None:
        status = "no_passing_crf"
    elif recommended is not None:
        status = "converged"
    elif not compared:
        status = "insufficient_comparisons"
    else:
        status = "provisional"
    summary = {
        "reason": reason,
        "trials": len(measured),
        "best_tested_crf": best_tested,
        "recommended_crf": recommended,
        "verified": recommended is not None,
        "converged": converged,
        "comparison_complete": compared,
        "recommendation_status": status,
    }
    if search is not None:
        summary.update(precision=search["precision"],
                       bounds=[search["min_crf"], search["max_crf"]])
    return summary


def _grid(search: dict) -> list[float]:
    """Retain exact bounds and a stable decimal grid, including an off-grid high."""
    low = Decimal(str(search["min_crf"]))
    high = Decimal(str(search["max_crf"]))
    precision = Decimal(str(search["precision"]))
    if not all(value.is_finite() for value in (low, high, precision)):
        raise ValueError("CRF bounds and precision must be finite")
    if low >= high or precision <= 0:
        raise ValueError("CRF bounds must increase and precision must be positive")
    count = int(((high - low) / precision).to_integral_value(rounding=ROUND_FLOOR))
    # Validated project settings have at most 5,101 points. Avoid accidental
    # unbounded work when this standalone helper receives an invalid config.
    if count > 100_000:
        raise ValueError("CRF search grid contains too many points")
    points = [float(low + index * precision) for index in range(count + 1)]
    if points[-1] != float(high):
        points.append(float(high))
    return points


def _nearest(points: list[float], value: float) -> float:
    return min(points, key=lambda point: (abs(point - value), point))


def next_trial(rows: list[dict], policy: dict, search: dict) -> tuple[float | None, dict | None]:
    """Choose a directional QP trial, then refine a measured passing/failing bracket.

    QP interpolation only chooses the next encode. All stopping decisions and
    recommendations use completed measurements supplied by the caller.
    """
    points = _grid(search)
    low, high = points[0], points[-1]
    precision = float(search["precision"])
    measured = _observations(rows)

    def done(reason: str) -> tuple[None, dict]:
        return None, measured_summary(measured, reason, search)

    if not measured:
        if search["max_trials"] <= 0:
            return done("trial_limit")
        return _nearest(points, policy["initial_crf"]), None

    passing = [row for row in measured if row["qp_pass"]]
    failing = [row for row in measured if not row["qp_pass"]]
    if passing and failing and failing[0]["crf"] < passing[-1]["crf"] - _EPSILON:
        return done("nonmonotonic_observations")
    if len(measured) >= 2:
        if passing and passing[-1]["crf"] >= high - _EPSILON:
            return done("upper_bound_passes")
        if not passing and failing[0]["crf"] <= low + _EPSILON:
            return done("no_passing_crf_in_bounds")
        if passing and failing and failing[0]["crf"] - passing[-1]["crf"] <= precision + _EPSILON:
            return done("precision_reached")
    if len(measured) >= search["max_trials"]:
        return done("trial_limit")

    untested = [point for point in points
                if not any(abs(point - row["crf"]) <= _EPSILON for row in measured)]
    if passing and failing:
        left, right = passing[-1], failing[0]
        interior = [point for point in untested if left["crf"] < point < right["crf"]]
        if not interior:
            return done("precision_reached")
        qleft, qright = _gate(left), _gate(right)
        ratio = ((policy["qp_target"] - qleft) / (qright - qleft)
                 if qright > qleft else 0.5)
        ratio = min(1.0, max(0.0, ratio)) if math.isfinite(ratio) else 0.5
        candidate = left["crf"] + ratio * (right["crf"] - left["crf"])
        # Project onto untested interior grid points instead of rejecting a
        # near-edge prediction: a target just above a passing point should
        # verify its next grid neighbor, even when the failing bound is far.
        return _nearest(interior, candidate), None

    direction = 1 if passing else -1
    edge = passing[-1] if passing else failing[0]
    forward = [point for point in untested if direction * (point - edge["crf"]) > _EPSILON]
    if not forward:
        # An initial encode at a bound still needs a second measured CRF before
        # the bound can be reported as a converged result.
        if len(measured) == 1 and untested:
            return _nearest(untested, edge["crf"]), None
        return done("trial_limit")

    slope = 1.0
    previous_distance = 0.0
    if len(measured) >= 2:
        previous = measured[-2] if passing else measured[1]
        distance = edge["crf"] - previous["crf"]
        previous_distance = abs(distance)
        slope = (_gate(edge) - _gate(previous)) / distance
    if math.isfinite(slope) and slope > _EPSILON:
        step = abs((policy["qp_target"] - _gate(edge)) / slope)
    else:
        # A flat or reversed local QP slope cannot support extrapolation.
        # Continue toward the untested bound with an expanding measured step.
        step = max(1.0, previous_distance * 2)
    step = min(4.0, max(precision, step))
    return _nearest(forward, edge["crf"] + direction * step), None


def auto_search(evaluate: Callable[[float], dict], policy: dict,
                search: dict) -> tuple[list[dict], dict]:
    """Run the same incremental optimizer with a synchronous encode callback."""
    rows: list[dict] = []
    while True:
        candidate, summary = next_trial(rows, policy, search)
        if summary is not None:
            return sorted(rows, key=lambda row: row["crf"]), summary
        row = evaluate(candidate)
        if row["crf"] != candidate:
            raise ValueError("CRF evaluator returned a result for a different trial")
        rows.append(row)
