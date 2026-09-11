"""Plot linear B-frame QP and exponential bitrate against CRF, with measured anchors."""

from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

if __package__:
    from .crf_model import CRF_VALUES, fit_models, predict
else:
    from crf_model import CRF_VALUES, fit_models, predict


PALETTE = {"x264": ("#2563eb", "o"), "x265": ("#d97706", "s")}


def make_figure(report: dict, figure: Figure | None = None) -> Figure:
    """Share one CRF axis, with QP on the left and Mbps on the right.

    Passing a figure lets the Qt viewer keep its canvas and event connections.
    The default remains an Agg canvas for background PNG/SVG exports.
    """
    if figure is None:
        figure = Figure(figsize=(12, 6), layout="constrained")
        FigureCanvasAgg(figure)
    else:
        figure.clear()
    qp_axes = figure.add_subplot()
    bitrate_axes = qp_axes.twinx()
    legend_handles = []
    grid = [CRF_VALUES[0] + (CRF_VALUES[1] - CRF_VALUES[0]) * i / 200 for i in range(201)]
    has_qp = has_bitrate = missing_b_frames = False
    for codec, analysis in report["codecs"].items():
        rows = sorted(analysis["rows"], key=lambda row: row["crf"])
        models = analysis.get("models") or fit_models(rows)
        color, marker = PALETTE[codec]
        estimates = [predict(models, crf) for crf in grid]
        for axes, metric, model, name, style in ((qp_axes, "average_qp", "qp", "QP", "-"),
                (bitrate_axes, "average_bitrate_mbps", "log_bitrate", "bitrate", "--")):
            measured = [row for row in rows if row[metric] is not None]
            if models[model]:
                line, = axes.plot(grid, [row[metric] for row in estimates], color=color, linewidth=1.8,
                                  linestyle=style, label=f"{codec} {name}")
                legend_handles.append(line)
            if measured:
                points = axes.scatter([row["crf"] for row in measured], [row[metric] for row in measured],
                                      edgecolors=color, facecolors=color if model == "qp" else "none",
                                      marker=marker, s=40, zorder=3, label=f"{codec} {name} measured")
                if not models[model]:
                    legend_handles.append(points)
        has_qp |= any(row["average_qp"] is not None for row in rows)
        has_bitrate |= bool(rows)
        missing_b_frames |= any(row["average_qp"] is None for row in rows)
    qp_axes.set_ylabel("Average B-frame QP")
    bitrate_axes.set_ylabel("Video bitrate (Mbps)")
    # Plot R itself on a linear scale: predict() evaluates exp(d + e*c) at
    # every grid point. A log y-axis would make this exponential look linear.
    bitrate_axes.set_yscale("linear")
    bitrate_axes.set_ylim(bottom=0)
    for axes in (qp_axes, bitrate_axes):
        axes.set_xlabel("CRF (c)")
        axes.set_xlim(CRF_VALUES[0] - 0.5, CRF_VALUES[1] + 0.5)
        axes.set_xticks(range(CRF_VALUES[0], CRF_VALUES[1] + 1))
        axes.spines["top"].set_visible(False)
    qp_axes.grid(True, alpha=0.22)
    if legend_handles:
        qp_axes.legend(handles=legend_handles, fontsize=9, ncols=2,
                       loc="lower center", bbox_to_anchor=(0.5, 1.01))
    if not has_qp:
        message = "B-frame QP unavailable" if missing_b_frames else "Waiting for measurements"
        qp_axes.text(0.02, 0.04, message, va="bottom", transform=qp_axes.transAxes)
    if not has_bitrate:
        if has_qp:
            bitrate_axes.text(0.98, 0.04, "Waiting for bitrate measurements", ha="right",
                              va="bottom", transform=bitrate_axes.transAxes)
    name = Path(report["source"]["path"]).name
    state = "" if report["state"] == "complete" else f" — {report['state']}"
    figure.suptitle(f"QP and bitrate vs CRF{state}\n{name}", fontsize=13)
    caption = ("Markers: measured CRF 13 and 20. Curves: two-point estimates.\n"
               "Solid: QP(c) = a + bc (left axis). Dashed: R(c) = exp(d + ec) Mbps (right axis).")
    if missing_b_frames:
        caption += "\nA QP curve requires B-frame measurements at both CRFs."
    figure.supxlabel(caption, fontsize=9)
    return figure


def write_plot(output: Path, report: dict) -> None:
    figure = make_figure(report)
    try:
        for extension in ("png", "svg"):
            target = output / f"qp-bitrate.{extension}"
            temporary = output / f".qp-bitrate.{extension}"
            try:
                figure.savefig(temporary, format=extension, dpi=160)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        figure.clear()
