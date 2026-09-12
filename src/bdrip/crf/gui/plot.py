"""Qt canvas with a bitrate cursor and B-frame QP estimates from saved models."""

import math

from matplotlib.backend_bases import MouseButton
from matplotlib.backends.backend_qt import NavigationToolbar2QT
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from bdrip.crf.model import fit_models, predict_bitrate
from bdrip.crf.plot import PALETTE, make_figure

HOVER_HINT = "Move to estimate QP · Click to pin bitrate · Right-click to release"


class InteractivePlot(QWidget):
    def __init__(self):
        super().__init__()
        self.figure = Figure(figsize=(8, 6), layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self, coordinates=False)
        self.toolbar.setIconSize(QSize(16, 16))
        self.toolbar.setStyleSheet("QToolButton { padding: 4px; }")
        self.toolbar.addSeparator()
        self.unpin_action = self.toolbar.addAction("Unpin")
        self.unpin_action.setToolTip(
            "Release the selected bitrate and resume mouse estimates"
        )
        self.unpin_action.setEnabled(False)
        self.unpin_action.triggered.connect(self.unpin)
        self.readout = QLabel(HOVER_HINT)
        self.readout.setWordWrap(True)
        self.readout.setMinimumHeight(34)
        self.readout.setMaximumHeight(44)
        self.readout.setStyleSheet("color: #475569; font-size: 11px; padding: 4px 8px;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.readout)
        self.models = {}
        self.axes = ()
        self.artists = []
        self.background = None
        self.hover_bitrate = None
        self.pinned_bitrate = None
        self.estimates = {}
        self.canvas.mpl_connect("motion_notify_event", self.on_motion)
        self.canvas.mpl_connect("button_press_event", self.on_click)
        self.canvas.mpl_connect("figure_leave_event", self.clear_hover)
        self.canvas.mpl_connect("draw_event", self.on_draw)
        self.canvas.mpl_connect("resize_event", self.clear_background)

    def set_report(self, report: dict) -> None:
        # Preserve a user's zoom on updates to the same task. New tasks reset
        # through FigurePreview before loading their report.
        limits = [axes.get_xlim() + axes.get_ylim() for axes in self.axes]
        zoomed = bool(self.axes and getattr(self, "default_limits", []) != limits)
        self.background = None
        self.hover_bitrate, self.estimates = None, {}
        make_figure(report, self.figure, compact=True)
        self.axes = tuple(self.figure.axes)
        self.models = {
            codec: analysis.get("models") or fit_models(analysis["rows"])
            for codec, analysis in report["codecs"].items()
        }
        self.toolbar.update()
        self.default_limits = [axes.get_xlim() + axes.get_ylim() for axes in self.axes]
        self.toolbar.push_current()
        if zoomed:
            for axes, (left, right, bottom, top) in zip(self.axes, limits):
                axes.set_xlim(left, right)
                axes.set_ylim(bottom, top)
            self.toolbar.push_current()
        axes = self.axes[0]
        self.cursor = axes.axvline(
            1,
            color="#475569",
            linewidth=1,
            linestyle=":",
            visible=False,
            animated=True,
        )
        self.tooltip = axes.text(
            0.02,
            0.98,
            "",
            transform=axes.transAxes,
            va="top",
            fontsize=10,
            visible=False,
            animated=True,
            bbox={
                "boxstyle": "round,pad=0.5",
                "fc": "white",
                "ec": "#94a3b8",
                "alpha": 0.95,
            },
        )
        self.tooltip.set_in_layout(False)
        self.markers = {}
        for codec in self.models:
            color, shape = PALETTE[codec]
            (marker,) = axes.plot(
                [],
                [],
                marker=shape,
                color=color,
                linestyle="none",
                markersize=7,
                visible=False,
                animated=True,
            )
            self.markers[codec] = marker
        self.artists = [self.cursor, *self.markers.values(), self.tooltip]
        if self.pinned_bitrate is not None:
            # Recalculate at the pinned bitrate as each encoder finishes, including
            # a pin placed before both calibration points were available.
            self.show_estimate(self.pinned_bitrate)
        else:
            self.readout.setText(HOVER_HINT)
        self.canvas.draw_idle()

    def reset(self) -> None:
        self.pinned_bitrate = None
        self.unpin_action.setEnabled(False)
        self.clear_hover()
        self.axes = ()
        self.models = {}
        self.background = None

    def clear_background(self, event=None) -> None:
        self.background = None

    def on_draw(self, event) -> None:
        # Redraw only cursor overlays on mouse movement; encoding/progress and
        # Qt's event loop never wait for a full Matplotlib render per event.
        if event.canvas is self.canvas and not self.canvas.is_saving():
            self.background = self.canvas.copy_from_bbox(self.figure.bbox)
            # This callback can run inside Qt's paint event. Draw into Agg's
            # buffer, but let that event finish without requesting a repaint.
            self.draw_hover_artists()

    def draw_hover_artists(self) -> None:
        renderer = self.canvas.get_renderer()
        for artist in self.artists:
            if artist.get_visible():
                artist.draw(renderer)

    def paint_hover(self) -> None:
        if self.background is None:
            self.canvas.draw_idle()
            return
        self.canvas.restore_region(self.background)
        self.draw_hover_artists()
        self.canvas.blit(self.figure.bbox)

    def clear_hover(self, event=None) -> None:
        if self.pinned_bitrate is not None:
            return
        self.hover_bitrate, self.estimates = None, {}
        for artist in self.artists:
            artist.set_visible(False)
        self.readout.setText(HOVER_HINT)
        if self.background is not None:
            self.paint_hover()

    def on_motion(self, event) -> None:
        if self.pinned_bitrate is not None:
            return
        bitrate = self.event_bitrate(event)
        if bitrate is None:
            self.clear_hover()
        else:
            self.show_estimate(bitrate)

    def event_bitrate(self, event) -> float | None:
        bitrate = event.xdata
        if (
            event.inaxes not in self.axes
            or bitrate is None
            or not math.isfinite(bitrate)
            or bitrate <= 0
            or self.toolbar.mode
        ):
            return None
        return bitrate

    def on_click(self, event) -> None:
        bitrate = self.event_bitrate(event)
        if bitrate is None:
            return
        if event.button == MouseButton.RIGHT:
            self.unpin()
        elif event.button == MouseButton.LEFT:
            self.pinned_bitrate = bitrate
            self.unpin_action.setEnabled(True)
            self.show_estimate(bitrate)

    def unpin(self) -> None:
        self.pinned_bitrate = None
        self.unpin_action.setEnabled(False)
        self.clear_hover()

    def show_estimate(self, bitrate: float) -> None:
        self.hover_bitrate = bitrate
        self.estimates = {
            codec: predict_bitrate(model, bitrate)
            for codec, model in self.models.items()
        }
        title = f"{'Pinned bitrate' if self.pinned_bitrate is not None else 'Video bitrate'} {bitrate:.3f} Mbps"
        lines, readout = (
            [title],
            [
                f"{'Pinned · ' if self.pinned_bitrate is not None else ''}{bitrate:.3f} Mbps"
            ],
        )
        for codec, estimate in self.estimates.items():
            qp = estimate["average_qp"]
            qp_text = f"{qp:.3f}" if qp is not None else "N/A"
            suffix = " (extrapolation)" if estimate["extrapolated"] else ""
            lines.append(f"{codec}: B-frame QP {qp_text}{suffix}")
            if qp is None:
                lines.append(estimate["status"])
            readout.append(f"{codec} QP {qp_text}{suffix}")
            marker = self.markers[codec]
            marker.set_visible(qp is not None)
            marker.set_data([bitrate], [qp] if qp is not None else [])
        self.cursor.set_xdata([bitrate, bitrate])
        self.cursor.set_linestyle("-" if self.pinned_bitrate is not None else ":")
        self.cursor.set_visible(True)
        self.tooltip.set_text("\n".join(lines))
        left, right = self.axes[0].get_xlim()
        on_left = bitrate >= (left + right) / 2
        self.tooltip.set_position((0.02 if on_left else 0.98, 0.98))
        self.tooltip.set_ha("left" if on_left else "right")
        self.tooltip.set_visible(True)
        self.readout.setText("   ·   ".join(readout))
        self.paint_hover()
