# Two-point CRF calibration

`bdrip crf` selects **one 60-second clip centered in the video**
and encodes it at **CRF 13 and 20**. For each encoder it records the average
**B-frame QP** and video bitrate, then fits two models:

```text
QP(c)  = a + b*c
ln R(c) = d + e*c
R(c)   = exp(d + e*c)       # R in Mbps
```

PyAV's bundled FFmpeg libraries handle decoding, exact cropping, and encoding.
No FFmpeg, FFprobe, HandBrake, or standalone x264/x265 executable is needed.
Matplotlib draws the two curves. The GUI uses the optional PySide6 dependency.

When both codecs are selected, the order is **x264 CRF 13 → x264 CRF 20 →
x265 CRF 13 → x265 CRF 20**. Both encoders use the same sample and crop.
There are only two measurements per encoder; intermediate CRFs are estimated
without additional encoding. Runtime depends on resolution, hardware, and
encoder settings; there is no automatic time cutoff.

Implementation modules now live under `src/bdrip/crf/` and `src/bdrip/video/`.
Use `bdrip crf` and `bdrip gui` in place of the removed script launchers.
Existing configs and task folders keep their paths and formats. See
[project architecture](architecture.md) and [command migration](commands.md).

## Desktop task queue

```sh
uv run --extra gui bdrip gui --config crf_search.example.json

# Choose a persistent queue/results folder:
uv run --extra gui bdrip gui --workspace "/path/to/CRF tasks"
```

Choose a codec and crop, optionally edit **Encoder options…**, then click
**Add videos…** and **Start queue**. Each video becomes a separate task with
its own saved settings and output folder. Later edits affect newly added tasks.

Encoding, crop detection, and PNG/SVG export run in background Python
processes. The GUI renders the interactive plot from saved models and stays
responsive, showing the active codec, CRF, submitted frames, elapsed time,
and overall encoding progress. With both codecs selected,
there are four encodes; selecting one codec runs two. Frame progress updates
during each encode, including a flushing indication while delayed frames are
being completed. Progress reaches 100% when the task finishes successfully.

The **QP and bitrate curves** tab overlays both curves on **one interactive
plot with a shared CRF x-axis**:

- Left y-axis and solid lines: average B-frame QP.
- Right y-axis and dashed lines: video bitrate in Mbps.

Each codec has its own color. Filled markers show measured QPs and hollow
markers show measured bitrates at CRF 13 and 20. Curves are fitted estimates.
Bitrate is drawn as **R(c) = exp(d + e*c)** on a linear Mbps scale, with the
formula evaluated throughout the CRF interval. If the measured bitrates are
close, the exponential curve can look almost straight over that interval.

Move the mouse anywhere inside the plot to show a vertical CRF cursor,
highlight the corresponding points on each curve, and display a tooltip with
estimated B-frame QP and bitrate for each encoder. These values come directly
from the fitted equations at the mouse's CRF, including fractional values.
They do not snap to the two measured CRFs or launch additional encodes.
Missing estimates display `N/A`; values outside 13–20 are labeled as
extrapolation. Moving outside the plot clears an unpinned cursor and tooltip.

**Left-click inside the plot to pin the CRF.** The cursor, highlighted curve
points, and predicted QP/bitrate stay visible when you move the mouse away.
Click another position to move the pin. **Unpin** on the toolbar, or a
right-click inside the plot, releases it and restores mouse tracking. Pinning
works in both the main plot and larger preview. It survives resizing, zooming,
and new measurements for the same task; estimates update as each model becomes
available. Switching tasks clears the pin. Clicks while **Pan** or **Zoom**
is active control navigation, so turn those tools off before placing a pin.

The toolbar supports **Pan**, rectangular **Zoom**, **Home** to restore the
full view, and saving a figure. **Larger preview…** opens the same interactive
plot in a resizable, non-modal window. Mouse estimates follow the correct CRF
when zoomed or resized. The plot updates after each completed measurement and
preserves a manually zoomed view for that task. A codec's curves become
available after both CRFs finish, so x264's model can be explored while x265
runs. The GUI also loads existing two-point results without re-encoding them.

Older sweep results retain their saved-image preview with **Fit**, **100%**,
and **− / +** controls; they do not have the two-point models needed for hover
estimates.

The **Estimates** tab displays the fitted equations and estimated QP/bitrate
for a chosen CRF. Changing this value performs no encoding. Values outside
13–20 are explicitly labeled as extrapolation. **Logs** shows the task log
or individual encoder logs (last 64 KiB in the viewer; full files stay on disk).
**Task settings** shows the selected task's saved configuration.

| Control | Behavior |
| --- | --- |
| Start queue | Run one video at a time, in queue order. |
| Pause after current | Finish the active video and leave remaining videos queued. |
| Cancel task | Cancel waiting work or stop the active encoder and save complete measurements. |
| Retry task | Queue a failed, cancelled, or interrupted task again, reusing its completed encodes. |
| Remove task | Remove the list entry while retaining its output folder and all files. |

Failed or cancelled tasks do not block later videos. Closing the window pauses
the queue and requests cancellation of the active task; it remains responsive
until that worker has stopped. Reopening the same workspace restores tasks
without automatically starting them. Only one GUI can open a workspace at a
time. If the GUI was forcibly terminated and its worker is still running, wait
for that worker to finish before reopening the workspace.

The default workspace is `crf-tasks/` in the current working directory. Each
task uses a unique subdirectory, even for duplicate filenames:

```text
crf-tasks/
  queue.json
  Movie-<unique-task-id>/
    config.json             # Task settings snapshot
    task.log                # Combined output, appended on retry
    progress.json           # Task stage, codec, CRF, overall progress
    sample-progress.json    # Current encoder's frame progress
    results.json            # Measurements and model coefficients
    summary.csv             # Actual measurements only
    estimates.csv           # Estimated values at CRFs 13 through 20
    qp-bitrate.png          # Both curves overlaid with two y-axes
    qp-bitrate.svg
    logs/                   # Native encoder logs, including previous attempts
    cache/                  # Completed encodes for resuming
```

## Command line

```sh
uv sync
uv run bdrip crf "movie.mkv" --config crf_search.example.json

# Run one encoder and choose where to save the reports:
uv run bdrip crf "movie.mkv" --codec x264 --output-dir "movie-calibration"
```

Both codecs run by default. The default output directory beside the source is
`<movie-stem>.crf-model/`. The encoder options are printed before encoding,
followed by the sample start/duration, measured table, and fitted equations.

| File | Contents |
| --- | --- |
| `qp-bitrate.png`, `qp-bitrate.svg` | One shared-CRF plot with QP on the left y-axis and Mbps on the right, including measured points and fitted curves. |
| `summary.csv` | Two measured rows per codec, including QP, Mbps, frame/B-frame counts, actual duration, and video bytes. |
| `estimates.csv` | Model estimates at integer CRFs 13–20, explicitly separate from measurements. |
| `results.json` | Source/settings/crop, exact sample range, raw I/P/B statistics, measurements, and fitted coefficients for each codec. |
| `logs/`, `cache/` | Native logs and reusable completed measurements. |

Reports and the figure update after each measurement. Their state identifies
running, interrupted, failed, and complete runs. Ctrl+C stops the active worker
and retains completed results. Rerun with the same source and settings to reuse
cached encodes; `--no-resume` forces fresh encodes. Reports in an explicitly
reused output directory are replaced, so choose separate directories to keep
multiple experiments. The GUI handles this with unique task directories.

Task runners can use `--progress-file PATH` for atomic JSON progress and
`--cancel-file PATH` for a cancellation marker. Creating the marker requests
cancellation; remove it before a new run. The GUI manages these automatically.

## Sample and model definitions

For video duration `T` in seconds:

```text
sample_duration = min(60, T)
sample_start    = (T - sample_duration) / 2
```

A video shorter than 60 seconds uses its entire video stream. Times are relative
to the selected stream's start. The report saves both the requested range and
actual encoded timing because frame boundaries have discrete timestamps.

Let `Q13`, `Q20` be measured average B-frame QPs, and `R13`, `R20` be measured
video bitrates in Mbps. Each encoder receives its own model:

```text
b = (Q20 - Q13) / 7
a = Q13 - 13*b

e = (ln(R20) - ln(R13)) / 7
d = ln(R13) - 13*e
```

The logarithm is natural. For example, if the endpoint bitrates are 16 and
4 Mbps, the estimate at CRF 16.5 is 8 Mbps (the geometric midpoint). Both fits
pass through their two measured endpoints. No model is produced from a single
completed measurement.

Only B-frame QP contributes to `average_qp`. Raw I/P/B statistics remain saved
for inspection. If an encode has no B-frames, its QP is `null` in JSON, blank
in CSV, and shown as unavailable in the GUI/console. The QP fit requires B-frame
measurements at both CRFs; the bitrate fit remains available independently.
x264's native summary rounds its reported per-frame-type QPs.

Bitrate includes **all encoded video frames**, including packets emitted when
the encoder flushes:

```text
average_bitrate_mbps = 8 * encoded_video_bytes / presentation_seconds / 1_000_000
```

Audio, subtitles, and container overhead are excluded. Bitrate is measured
output, not an enforced target rate. The saved model is under each codec's
`models.qp` (`a`, `b`) and `models.log_bitrate` (`d`, `e`, `bitrate_unit`).
Reports use schema version 4, `method: "two_point"`, and `qp_frame_type: "B"`.

These are approximations for the selected minute and encoding settings.
VBV limits and content changes can make actual bitrate depart from an
exponential curve. A single minute does not measure the entire movie, and
QP values across x264 and x265 are not a shared perceptual quality scale.

## Encoder settings and exact crop

The existing defaults and supplied argument strings are preserved:

| Encoder | Profile | Level | Preset | Pixel format |
| --- | --- | --- | --- | --- |
| x264 | High | 4.1 | placebo | yuv420p (8-bit) |
| x265 | Main10 | auto | slower | yuv420p10le (10-bit) |

**Encoder options…** has separate codec tabs for preset, level (`auto` is
supported), tune, native parameters, and additional FFmpeg encoder options.
The High/8-bit and Main10/10-bit profiles are displayed for reference.
Parameters accept colon-separated `key=value` entries or one entry per line.
**Save** validates and applies edits to new tasks; **Cancel** discards them.
**Restore Defaults** fills in the original settings for review before saving.
**Load config…** and **Save config…** import/export video and encoder settings.

In JSON, `codecs.x264` and `codecs.x265` accept `preset`, `tune`, `profile`,
`level`, `pixel_format`, `params`, and `options`. `params` accepts a colon-separated
string or an object; `options` is a mapping of additional libavcodec options.
Supplying either mapping replaces that whole mapping; other objects merge with
defaults. CRF, statistics, dimensions, and timing are controlled by the program.
Conflicting arguments and unknown config keys are rejected. Encoder availability,
pixel format, and explicit level compatibility are checked before testing.

Automatic black-margin detection uses PyAV and FFmpeg's `bbox` filter to inspect
up to two seconds at the center of the chosen minute. The detected content union
provides one crop shared by every encode. Unreliable or absent bounds retain
the full frame. A manual crop can account for content missed by detection.

Crop dimensions and offsets remain exact: `1920:804:0:137` produces 1920×804,
including the odd vertical offset. Width/height must be even for these 4:2:0
encoders; incompatible dimensions produce an error instead of rounding.

```sh
uv run bdrip crf "movie.mkv" --config crf_search.example.json --crop 1920:804:0:137
uv run bdrip crf "movie.mkv" --config crf_search.example.json --no-crop
```

`video.crop` accepts `"auto"` (default), `null`, or `"width:height:x:y"`.
`--crop none` also disables cropping. `video.cropdetect.limit` defaults to
`24/255` of the raw luminance range, scaled to source bit depth;
`video.cropdetect.seconds` defaults to `2.0`. `video.stream` or `--video-stream`
selects a zero-based video stream, excluding attached cover pictures.

## Existing configurations and queues

The previous random sampling policy and CRF 14–18 sweep have been removed.
Sample count, duration, seed, and sampling interval controls no longer appear
in the GUI or CLI. Importing a previous JSON config keeps its encoder/video
settings and ignores its recognized `sampling` block. New exports omit that
block; the sample and measurement CRFs are fixed by this method.

Existing queue entries that have never run adopt the new method. Historical
sweep tasks retain their original settings, figures, and logs. Retrying a failed
or interrupted old sweep creates a new two-point task in a separate folder,
so its previous measurements remain available. Completed old tasks can be
viewed as before; add the video again to calibrate it using the new method.
