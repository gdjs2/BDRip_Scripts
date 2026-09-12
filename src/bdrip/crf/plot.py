"""Plot average B-frame QP against video bitrate, with measured anchors."""

from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from bdrip.crf.model import fit_models, predict_bitrate

PALETTE = {"x264": ("#2563eb", "o"), "x265": ("#d97706", "s")}


def make_figure(
    report: dict, figure: Figure | None = None, *, compact: bool = False
) -> Figure:
    """Plot video Mbps horizontally and average B-frame QP vertically.

    The desktop view omits the export title/caption to give the plot more room.
    Both views use the same measurements and logarithmic QP-versus-bitrate fit.
    """
    if figure is None:
        figure = Figure(figsize=(10, 6.5), layout="constrained")
        FigureCanvasAgg(figure)
    else:
        figure.clear()
    figure.set_facecolor("white")
    axes = figure.add_subplot()
    rates = []
    unavailable = []
    has_points = False
    for codec, analysis in report["codecs"].items():
        rows = sorted(
            (row for row in analysis["rows"] if row.get("complete", True)),
            key=lambda row: row["crf"],
        )
        rates.extend(row["average_bitrate_mbps"] for row in rows)
        models = analysis.get("models") or fit_models(rows)
        color, marker = PALETTE[codec]
        measured = [row for row in rows if row["average_qp"] is not None]
        curve = False
        if models.get("qp") and models.get("log_bitrate") and len(rows) == 2:
            low, high = sorted(row["average_bitrate_mbps"] for row in rows)
            grid = [low + (high - low) * index / 200 for index in range(201)]
            estimates = [predict_bitrate(models, rate)["average_qp"] for rate in grid]
            if all(qp is not None for qp in estimates):
                axes.plot(grid, estimates, color=color, linewidth=2, label=codec)
                curve = True
        if measured:
            axes.scatter(
                [row["average_bitrate_mbps"] for row in measured],
                [row["average_qp"] for row in measured],
                color=color,
                marker=marker,
                s=40,
                zorder=3,
                label="_nolegend_" if curve else codec,
            )
            has_points = True
        if any(row["average_qp"] is None for row in rows):
            unavailable.append(codec)
    axes.set_xlabel("Video bitrate (Mbps)", labelpad=10)
    axes.set_ylabel("Average B-frame QP", labelpad=10)
    axes.set_xlim(0, max(rates) * 1.12 if rates else 20)
    axes.margins(y=0.15)
    axes.grid(True, color="#e2e8f0", linewidth=0.8)
    axes.set_axisbelow(True)
    axes.tick_params(colors="#475569", labelsize=9, length=0, pad=7)
    axes.xaxis.label.set_color("#334155")
    axes.yaxis.label.set_color("#334155")
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        axes.spines[side].set_color("#cbd5e1")
    if has_points:
        axes.legend(loc="upper right", frameon=False, fontsize=10, ncols=2)
    if unavailable or not has_points:
        message = (
            f"{', '.join(unavailable)}: B-frame QP unavailable"
            if unavailable
            else "Waiting for B-frame QP measurements"
        )
        axes.text(
            0.03,
            0.04,
            message,
            color="#64748b",
            fontsize=9,
            va="bottom",
            transform=axes.transAxes,
        )
    if not compact:
        name = Path(report["source"]["path"]).name
        state = "" if report["state"] == "complete" else f" — {report['state']}"
        figure.suptitle(f"B-frame QP by video bitrate{state}\n{name}", fontsize=12)
        measured = (
            "sample means"
            if report.get("aggregation") == "sample_mean"
            else "measurements"
        )
        figure.supxlabel(
            f"Markers: {measured} at CRF 13 and 20. Lines: estimated QP versus video bitrate.",
            fontsize=9,
            color="#64748b",
        )
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
