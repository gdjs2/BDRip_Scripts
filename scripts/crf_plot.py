"""Export the measured B-frame QP–bitrate relationship as PNG and standalone SVG."""

from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure


def make_figure(report: dict) -> Figure:
    figure = Figure(figsize=(9, 6), layout="constrained")
    FigureCanvasAgg(figure)
    axes = figure.add_subplot()
    palette = {"x264": ("#2563eb", "o"), "x265": ("#d97706", "s")}
    has_points = False
    missing_b_frames = False
    for codec, analysis in report["codecs"].items():
        missing_b_frames |= any(row["average_qp"] is None for row in analysis["rows"])
        rows = sorted((row for row in analysis["rows"] if row["average_qp"] is not None),
                      key=lambda row: row["crf"])
        if not rows:
            continue
        has_points = True
        color, marker = palette[codec]
        axes.plot([row["average_qp"] for row in rows],
                  [row["average_bitrate_mbps"] for row in rows],
                  color=color, marker=marker, linewidth=1.8, markersize=6, label=codec)
        for index, row in enumerate(rows):
            axes.annotate(f"CRF {row['crf']}",
                          (row["average_qp"], row["average_bitrate_mbps"]),
                          xytext=(7, 9 if index % 2 == 0 else -15), textcoords="offset points",
                          color=color, fontsize=9)
    axes.set_xlabel("Average B-frame QP (weighted by B-frame count)")
    axes.set_ylabel("Average video bitrate (Mbps)")
    name = Path(report["source"]["path"]).name
    state = "" if report["state"] == "complete" else f" — {report['state']}"
    axes.set_title(f"B-frame QP–bitrate relationship{state}\n{name}", fontsize=13)
    axes.grid(True, alpha=0.22)
    axes.spines[["top", "right"]].set_visible(False)
    axes.margins(x=0.18, y=0.20)
    axes.set_ylim(bottom=0)
    if has_points:
        axes.legend(title="Encoder")
    else:
        message = "No B-frames in completed measurements" if missing_b_frames else "No complete CRF measurements yet"
        axes.text(0.5, 0.5, message, ha="center", va="center",
                  transform=axes.transAxes)
    caption = "Bitrate includes all video frames, weighted by clip duration. Labels identify measured CRFs."
    if missing_b_frames:
        caption += "\nPoints without B-frames are omitted."
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
