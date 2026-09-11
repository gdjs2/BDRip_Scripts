"""Qt canvas with a shared CRF cursor and estimates from the saved models."""

import math

from matplotlib.backend_bases import MouseButton
from matplotlib.backends.backend_qt import NavigationToolbar2QT
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from bdrip.crf.model import CRF_VALUES, fit_models, predict
from bdrip.crf.plot import PALETTE, make_figure

HOVER_HINT = "Move the mouse to estimate B-frame QP and bitrate. Click to pin a CRF; right-click or Unpin to release."


class InteractivePlot(QWidget):
    def __init__(self):
        super().__init__()
        self.figure = Figure(figsize=(12, 6), layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self, coordinates=False)
        self.toolbar.addSeparator()
        self.unpin_action = self.toolbar.addAction("Unpin")
        self.unpin_action.setToolTip(
            "Release the selected CRF and resume mouse estimates"
        )
        self.unpin_action.setEnabled(False)
        self.unpin_action.triggered.connect(self.unpin)
        self.readout = QLabel(HOVER_HINT)
        self.readout.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.readout)
        self.models = {}
        self.axes = ()
        self.artists = []
        self.background = None
        self.hover_crf = None
        self.pinned_crf = None
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
        self.hover_crf, self.estimates = None, {}
        make_figure(report, self.figure)
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
        qp_axes, bitrate_axes = self.axes
        self.cursor = bitrate_axes.axvline(
            13,
            color="#475569",
            linewidth=1,
            linestyle=":",
            visible=False,
            animated=True,
        )
        self.tooltip = bitrate_axes.text(
            0.02,
            0.98,
            "",
            transform=bitrate_axes.transAxes,
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
            for axes, metric in (
                (qp_axes, "average_qp"),
                (bitrate_axes, "average_bitrate_mbps"),
            ):
                (marker,) = axes.plot(
                    [],
                    [],
                    marker=shape,
                    color=color,
                    linestyle="none",
                    markersize=7,
                    markerfacecolor=color if metric == "average_qp" else "white",
                    visible=False,
                    animated=True,
                )
                self.markers[codec, metric] = marker
        self.artists = [self.cursor, *self.markers.values(), self.tooltip]
        if self.pinned_crf is not None:
            # Recalculate at the pinned CRF as each encoder finishes, including
            # a pin placed before both calibration points were available.
            self.show_estimate(self.pinned_crf)
        else:
            self.readout.setText(HOVER_HINT)
        self.canvas.draw_idle()

    def reset(self) -> None:
        self.pinned_crf = None
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
        if self.pinned_crf is not None:
            return
        self.hover_crf, self.estimates = None, {}
        for artist in self.artists:
            artist.set_visible(False)
        self.readout.setText(HOVER_HINT)
        if self.background is not None:
            self.paint_hover()

    def on_motion(self, event) -> None:
        if self.pinned_crf is not None:
            return
        crf = self.event_crf(event)
        if crf is None:
            self.clear_hover()
        else:
            self.show_estimate(crf)

    def event_crf(self, event) -> float | None:
        crf = event.xdata
        if (
            event.inaxes not in self.axes
            or crf is None
            or not math.isfinite(crf)
            or not 0 <= crf <= 51
            or self.toolbar.mode
        ):
            return None
        return crf

    def on_click(self, event) -> None:
        crf = self.event_crf(event)
        if crf is None:
            return
        if event.button == MouseButton.RIGHT:
            self.unpin()
        elif event.button == MouseButton.LEFT:
            self.pinned_crf = crf
            self.unpin_action.setEnabled(True)
            self.show_estimate(crf)

    def unpin(self) -> None:
        self.pinned_crf = None
        self.unpin_action.setEnabled(False)
        self.clear_hover()

    def show_estimate(self, crf: float) -> None:
        self.hover_crf = crf
        self.estimates = {
            codec: predict(model, crf) for codec, model in self.models.items()
        }
        title = f"{'Pinned CRF' if self.pinned_crf is not None else 'CRF'} {crf:.2f} · estimates"
        if not CRF_VALUES[0] <= crf <= CRF_VALUES[1]:
            title += " (extrapolation)"
        lines = [title]
        for codec, estimate in self.estimates.items():
            qp, rate = estimate["average_qp"], estimate["average_bitrate_mbps"]
            qp_text = f"{qp:.3f}" if qp is not None else "N/A"
            rate_text = f"{rate:.4g} Mbps" if rate is not None else "N/A"
            lines.append(f"{codec}: B-frame QP {qp_text} · bitrate {rate_text}")
            for metric in ("average_qp", "average_bitrate_mbps"):
                marker = self.markers[codec, metric]
                value = estimate[metric]
                marker.set_visible(value is not None)
                marker.set_data([crf], [value] if value is not None else [])
        self.cursor.set_xdata([crf, crf])
        self.cursor.set_linestyle("-" if self.pinned_crf is not None else ":")
        self.cursor.set_visible(True)
        self.tooltip.set_text("\n".join(lines))
        left, right = self.axes[0].get_xlim()
        on_left = crf >= (left + right) / 2
        self.tooltip.set_position((0.02 if on_left else 0.98, 0.98))
        self.tooltip.set_ha("left" if on_left else "right")
        self.tooltip.set_visible(True)
        self.readout.setText("   |   ".join(lines))
        self.paint_hover()
