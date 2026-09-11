"""Two-point CRF calibration: linear B-frame QP and log-linear video bitrate."""

from __future__ import annotations

import math


CRF_VALUES = (13, 20)
SAMPLE_SECONDS = 60.0
METHOD = "two_point"


def select_sample(duration: float) -> dict:
    if isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration must be finite and positive")
    length = min(SAMPLE_SECONDS, duration)
    return {"kind": "center", "start": (duration - length) / 2, "duration": length}


def fit_models(rows: list[dict]) -> dict:
    """Fit each metric independently; missing B-frames never prevent a bitrate fit."""
    points = {}
    for row in rows:
        crf = row["crf"]
        if crf not in CRF_VALUES or crf in points:
            raise ValueError("Calibration requires one measurement each at CRF 13 and 20")
        bitrate, qp = row["average_bitrate_mbps"], row["average_qp"]
        if not math.isfinite(bitrate) or bitrate <= 0 or (qp is not None and not math.isfinite(qp)):
            raise ValueError("Calibration needs positive finite bitrates and finite B-frame QPs")
        points[crf] = row
    models = {"crf_range": list(CRF_VALUES), "qp": None, "log_bitrate": None,
              "qp_status": "Waiting for measurements at CRF 13 and 20",
              "bitrate_status": "Waiting for measurements at CRF 13 and 20"}
    if len(points) < 2:
        return models
    low, high = (points[crf] for crf in CRF_VALUES)
    c0, c1 = CRF_VALUES
    log_low, log_high = math.log(low["average_bitrate_mbps"]), math.log(high["average_bitrate_mbps"])
    e = (log_high - log_low) / (c1 - c0)
    models["log_bitrate"] = {"d": log_low - e * c0, "e": e, "bitrate_unit": "Mbps"}
    models["bitrate_status"] = "ready"
    if low["average_qp"] is None or high["average_qp"] is None:
        models["qp_status"] = "B-frame QP unavailable at one or both measured CRFs"
    else:
        b = (high["average_qp"] - low["average_qp"]) / (c1 - c0)
        models["qp"] = {"a": low["average_qp"] - b * c0, "b": b}
        models["qp_status"] = "ready"
    return models


def predict(models: dict, crf: float) -> dict:
    if isinstance(crf, bool) or not math.isfinite(crf) or not 0 <= crf <= 51:
        raise ValueError("Prediction CRF must be finite and between 0 and 51")
    qp, bitrate = models.get("qp"), models.get("log_bitrate")
    value = None
    if bitrate:
        try:
            value = math.exp(bitrate["d"] + bitrate["e"] * crf)
        except OverflowError:
            pass
        if value is not None and (not math.isfinite(value) or value <= 0):
            value = None
    return {"crf": crf, "average_qp": qp["a"] + qp["b"] * crf if qp else None,
            "average_bitrate_mbps": value, "extrapolated": not CRF_VALUES[0] <= crf <= CRF_VALUES[1]}
