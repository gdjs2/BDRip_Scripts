"""Known calibration anchors with a geometric bitrate midpoint."""


def anchors(qps=(18, 25), rates=(16, 4)):
    return [
        {"crf": crf, "average_qp": qp, "average_bitrate_mbps": rate}
        for crf, qp, rate in zip((13, 20), qps, rates)
    ]
