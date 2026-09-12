# Two-point CRF calibration

`bdrip crf` selects **ten 10-second clips spread across the video**
and encodes every clip at **CRF 13 and 20**. At each CRF, it takes the
arithmetic mean of the clips' average **B-frame QPs** and video bitrates.
These two mean endpoints define each encoder's models:

```text
QP(c)  = a + b*c
ln R(c) = d + e*c
R(c)   = exp(d + e*c)       # R in Mbps
QP(R)  = a + (b/e)*(ln R - d)
```

PyAV's bundled FFmpeg libraries handle decoding, exact cropping, and encoding.
No FFmpeg, FFprobe, HandBrake, or standalone x264/x265 executable is needed.
Matplotlib plots video bitrate on the x-axis against average B-frame QP on the
y-axis, with one curve per encoder. The GUI uses the optional PySide6 dependency.

When both codecs are selected, the order is **x264 CRF 13 → x264 CRF 20 →
x265 CRF 13 → x265 CRF 20**, completing all samples at each CRF before advancing.
Both encoders use the same sample ranges and crop.
There are two mean endpoints per encoder; intermediate CRFs are estimated
without additional encoding. Runtime depends on resolution, hardware, and
encoder settings; there is no automatic time cutoff.

Implementation modules now live under `src/bdrip/crf/` and `src/bdrip/video/`.
Use `bdrip crf` and `bdrip gui` in place of the removed script launchers.
Existing configs and task folders remain readable. See
[project architecture](architecture.md) and [command migration](commands.md).

## Desktop task queue

```sh
uv run --extra gui bdrip gui --config crf_search.example.json

# Choose a persistent queue-state folder:
uv run --extra gui bdrip gui --workspace "/path/to/CRF tasks"

# Optionally store all new task results in one location instead of beside each video:
uv run --extra gui bdrip gui --output-dir "/path/to/CRF results"
```

Choose the **Samples** count, **Seconds each**, codec, and crop, optionally edit
**Encoder options…**, then click
**Add videos…** and **Start queue**. Each video becomes a separate task with
its own saved settings and output folder. Later edits affect newly added tasks.
Settings and the task queue share a compact left sidebar; the selected video's
chart occupies the taller right pane. Drag the divider to adjust their widths.
The **Config** menu loads or saves a configuration file.

Encoding, crop detection, and PNG/SVG export run in background Python
processes. The GUI renders the interactive plot from saved models and stays
responsive, showing the active codec, CRF, sample number, submitted frames,
elapsed time, and overall encoding progress. With ten samples and both codecs
selected, there are **40 encodes**; selecting one codec runs 20. Frame progress updates
during each encode, including a flushing indication while delayed frames are
being completed. Progress reaches 100% when the task finishes successfully.

The **Bitrate / QP** tab shows one interactive plot:

- **X-axis:** video bitrate in Mbps, on a linear scale.
- **Y-axis:** average B-frame QP, on a linear scale.

Each codec has its own color and marker shape. The two markers are measured
pairs of mean bitrate and mean B-frame QP at CRF 13 and 20. The line between
them evaluates **QP(R) = a + (b/e)*(ln R - d)** continuously. CRF is eliminated
from the displayed relationship, using the same saved endpoint models.

Move the mouse inside the plot to show a vertical bitrate cursor, highlight
the corresponding point on each encoder's curve, and display its estimated
B-frame QP. Both codecs are evaluated at the **same bitrate**, which generally
corresponds to different CRFs. Hover does not snap to measurements or run more
encodes. Missing estimates display `N/A`. A bitrate outside an encoder's two
measured endpoint rates is labeled as extrapolation for that encoder.
Moving outside the plot clears an unpinned cursor and tooltip.

**Left-click inside the plot to pin the bitrate.** The cursor, highlighted curve
points, and predicted QPs stay visible when you move the mouse away.
Click another position to move the pin. **Unpin** on the toolbar, or a
right-click inside the plot, releases it and restores mouse tracking. Pinning
works in both the main plot and larger preview. It survives resizing, zooming,
and new measurements for the same task; estimates update as each model becomes
available. Switching tasks clears the pin. Clicks while **Pan** or **Zoom**
is active control navigation, so turn those tools off before placing a pin.

The toolbar supports **Pan**, rectangular **Zoom**, **Home** to restore the
full view, and saving a figure. **Larger preview…** opens the same interactive
plot in a resizable, non-modal window. Mouse estimates follow the correct bitrate
when zoomed or resized. An endpoint appears after all its samples finish.
Each refresh preserves a manually zoomed view for that task. A codec's curve becomes
available after both CRFs finish, so x264's model can be explored while x265
runs. Existing two-point JSON results also open with the new axes without
re-encoding. Previously saved image files stay unchanged until regenerated.

Older sweep results retain their saved-image preview with **Fit**, **100%**,
and **− / +** controls; they do not have the two-point models needed for hover
estimates.

The **Estimates** tab accepts a bitrate in Mbps and shows each encoder's estimated
B-frame QP, QP-versus-bitrate equation, and **approximate CRF**. CRF remains a
model estimate; changing the chart axes does not correct a bias between sampled
and full-movie encoding. Changing this value performs no encoding. Extrapolation
is identified separately for each encoder. **Logs** shows the task log
or individual encoder logs (last 64 KiB in the viewer; full files stay on disk).
**Task settings** shows the selected task's saved configuration.

| Control | Behavior |
| --- | --- |
| Start queue | Run one video at a time, in queue order. |
| Pause | Finish the active video and leave remaining videos queued. |
| Cancel | Cancel waiting work or stop the active encoder and save complete measurements. |
| Retry | Queue a failed, cancelled, or interrupted task again, reusing its completed encodes. |
| Remove | Remove the list entry while retaining its output folder and all files. |

Failed or cancelled tasks do not block later videos. Closing the window pauses
the queue and requests cancellation of the active task; it remains responsive
until that worker has stopped. Reopening the same workspace restores tasks
without automatically starting them. Only one GUI can open a workspace at a
time. If the GUI was forcibly terminated and its worker is still running, wait
for that worker to finish before reopening the workspace.

The queue workspace defaults to `crf-tasks/` in the current working directory;
it holds `queue.json` and process locks. **New task logs and results default to
`crf-tasks/` beside the input video**, with a unique task subdirectory even for
duplicate filenames. For an input at `/movies/Movie/Streams/movie.mkv`:

```text
/movies/Movie/Streams/crf-tasks/
  movie-<unique-task-id>/
    config.json             # Task settings snapshot
    task.log                # Combined output, appended on retry
    progress.json           # Task stage, codec, CRF, overall progress
    sample-progress.json    # Current encoder's frame progress
    results.json            # Measurements and model coefficients
    summary.csv             # Endpoint means and sample counts
    samples.csv             # Individual sample measurements and ranges
    estimates.csv           # Estimated values at CRFs 13 through 20
    qp-bitrate.png          # Bitrate x-axis, average B-frame QP y-axis
    qp-bitrate.svg
    logs/                   # Native encoder logs, including previous attempts
    cache/                  # Completed encodes for resuming
```

**Open results folder** opens this task directory. `task.log` contains the
combined task output, while `logs/` holds individual x264/x265 encoder logs.
`--output-dir` overrides the parent directory for newly added tasks; `--workspace`
only changes the queue-state location. Each task saves its output path, so
reopening the GUI or retrying a task keeps using that path. Existing queues
retain their original result folders; no saved logs or figures are moved.

## Command line

```sh
uv sync
uv run bdrip crf "movie.mkv" --config crf_search.example.json

# Run one encoder and choose where to save the reports:
uv run bdrip crf "movie.mkv" --codec x264 --output-dir "movie-calibration"

# Override the sample plan (the defaults are 10 clips of 10 seconds):
uv run bdrip crf "movie.mkv" --samples 10 --sample-seconds 10 --seed 0
```

Both codecs run by default. The default output directory beside the source is
`<movie-stem>.crf-model/`, with encoder logs under `logs/`. Use `--output-dir`
to choose another location. The encoder options are printed before encoding,
followed by all sample starts/durations, the mean endpoint table, and fitted equations.

| File | Contents |
| --- | --- |
| `qp-bitrate.png`, `qp-bitrate.svg` | Video bitrate (Mbps) on x and average B-frame QP on y, with measured points and one fitted curve per encoder. |
| `summary.csv` | Two endpoint rows per codec: mean QP/Mbps, sample counts, completion flag, and totals for frames, B-frames, duration, and video bytes. |
| `samples.csv` | One row per completed encode: codec, CRF, sample index/range, average B-frame QP, video Mbps, counts, bytes, and cache status. |
| `estimates.csv` | Model estimates at integer CRFs 13–20, explicitly separate from measurements. |
| `results.json` | Source/settings/crop, exact sample plan, raw per-sample I/P/B statistics, endpoint means, and fitted coefficients for each codec. |
| `logs/`, `cache/` | Native logs and reusable completed measurements. |

JSON and CSV reports update after every completed sample. Figures update after
each completed endpoint and when a task finishes or stops. Partial endpoints
retain their sample measurements and carry `complete: false`; they are excluded
from fitting and plotting until all planned samples finish. Report state identifies
running, interrupted, failed, and complete runs. Ctrl+C stops the active worker
and retains completed results. Rerun with the same source and settings to reuse
cached encodes; `--no-resume` forces fresh encodes. Reports in an explicitly
reused output directory are replaced, so choose separate directories to keep
multiple experiments. The GUI handles this with unique task directories.

Task runners can use `--progress-file PATH` for atomic JSON progress and
`--cancel-file PATH` for a cancellation marker. Creating the marker requests
cancellation; remove it before a new run. The GUI manages these automatically.

On Windows, progress readers or antivirus scanners can briefly block replacement
of a JSON snapshot. The writer retries access/sharing errors with short delays
(up to 0.71 seconds total), keeping the previous complete snapshot available.
Persistent access errors are still reported; use a writable output directory
if the destination does not permit replacement. After updating the program,
restart the GUI and retry an interrupted task to reuse its completed encodes.

## Sample and model definitions

The default config contains:

```json
{"sampling": {"count": 10, "seconds": 10.0, "seed": 0}}
```

Count and duration can also be changed in the GUI or with `--samples` and
`--sample-seconds`. The seed is configurable in JSON or with `--seed`.
For video duration `T`, requested count `N`, and clip length `L` in seconds:

```text
n = min(N, max(1, floor(T / L)))
sample_duration = min(L, T)
section_duration = T / n
# For each i from 0 to n - 1, using one seeded random generator:
sample_start[i] = i * section_duration + uniform(0, section_duration - sample_duration)
```

This places one random clip inside each equal section, without overlap. The
same duration, configuration, and seed produce the same plan, including on retry.
Short videos use fewer clips: a 35-second video uses three 10-second clips; a
video shorter than 10 seconds uses its whole video stream. Times are relative
to the selected stream's start. The report saves requested ranges and actual
encoded timing because frame boundaries have discrete timestamps.

For each clip, QP uses only B-frames, while bitrate includes **all encoded video
frames**, including packets emitted when the encoder flushes:

```text
clip_bitrate_mbps = 8 * encoded_video_bytes / presentation_seconds / 1_000_000
endpoint_bitrate = sum(clip_bitrate_mbps) / sample_count
endpoint_qp      = sum(clip_average_b_frame_qp) / qp_sample_count
```

Every clip contributes equally to each mean; clips are not weighted by duration
or B-frame count. `qp_sample_count` counts clips with a B-frame QP. A clip without
B-frames still contributes to the bitrate mean, but is excluded from the QP mean.
Its QP is `null` in JSON and blank in CSV. If no clip at an endpoint has B-frames,
that endpoint has no QP; the bitrate model remains available independently.
Raw I/P/B statistics remain saved for inspection. x264's native summary rounds
its reported per-frame-type QPs.

Let `Q13`, `Q20` be the mean B-frame QP endpoints, and `R13`, `R20` the mean
video bitrate endpoints in Mbps. Each encoder receives its own model:

```text
b = (Q20 - Q13) / 7
a = Q13 - 13*b

e = (ln(R20) - ln(R13)) / 7
d = ln(R13) - 13*e
```

The logarithm is natural. For example, if the endpoint bitrates are 16 and
4 Mbps, the estimate at CRF 16.5 is 8 Mbps (the geometric midpoint). Both fits
pass through their two measured endpoints. No model is produced from a single
completed endpoint.

The displayed QP-versus-bitrate curve eliminates `c` from those equations:

```text
c(R)  = (ln(R) - d) / e          # Approximate CRF, shown in the Estimates tab
QP(R) = (a - b*d/e) + (b/e)*ln(R)
```

Bitrate must be positive. Equal endpoint bitrates provide no unique inverse,
so the figure retains measured points without inventing a curve or QP estimate.
Missing B-frame QP at either endpoint also prevents a fitted QP curve. A single
completed endpoint is shown as a point until the other endpoint finishes.

Audio, subtitles, and container overhead are excluded. Bitrate is measured
output, not an enforced target rate. The saved model is under each codec's
`models.qp` (`a`, `b`) and `models.log_bitrate` (`d`, `e`, `bitrate_unit`).
Reports use schema version 5, `method: "two_point"`, `aggregation: "sample_mean"`,
`sampling_method: "stratified"`, and `qp_frame_type: "B"`.

These are approximations for the selected clips and encoding settings.
VBV limits and content changes can make actual bitrate depart from an
exponential curve. Sampling does not measure the entire movie, and
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
**Config → Load config…** and **Config → Save config…** import/export sampling,
video, and encoder settings.

In JSON, `codecs.x264` and `codecs.x265` accept `preset`, `tune`, `profile`,
`level`, `pixel_format`, `params`, and `options`. `params` accepts a colon-separated
string or an object; `options` is a mapping of additional libavcodec options.
Supplying either mapping replaces that whole mapping; other objects merge with
defaults. CRF, statistics, dimensions, and timing are controlled by the program.
Conflicting arguments and unknown config keys are rejected. Encoder availability,
pixel format, and explicit level compatibility are checked before testing.

Automatic black-margin detection uses PyAV and FFmpeg's `bbox` filter to inspect
up to two seconds at the center of each selected clip. The detected content union
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

The CRF endpoints stay fixed at 13 and 20. Configs without `sampling` receive
the new 10-by-10-second defaults. Older sampling configs retain `count` and
`seed`; a null seed becomes 0. Retired `min_seconds`, `max_seconds`, `start`,
and `end` fields are ignored; use `seconds` for the fixed clip duration.
Encoder and video settings are preserved.

Existing queue entries that have never run adopt the new defaults. Historical
single-clip and sweep tasks retain their settings, figures, and logs. Retrying
a failed or interrupted historical task creates a new task in a separate folder.
Completed old tasks can be viewed as before; add the video again to calibrate
it using sample means. New tasks resume their own completed clips on retry.
