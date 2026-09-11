# CRF sweep: average B-frame QP versus video bitrate

`scripts/crf_search.py` measures **CRF 14, 15, 16, 17, and 18** on random
samples from a movie and plots their average B-frame QP against average video bitrate.
It uses PyAV's bundled FFmpeg libraries for video decoding, cropping, and
encoding, and Matplotlib for the figure. No FFmpeg, FFprobe, HandBrake, or
standalone x264/x265 executable is needed.

This replaces the adaptive CRF optimizer. Every requested codec completes the
five-point sweep; there are no QP thresholds, recommendations, calibration
passes, or automatic time cutoff. Runtime depends on the selected samples,
source resolution, hardware, and encoding settings.

## Run

Install the project's Python dependencies and run:

```sh
uv sync
uv run scripts/crf_search.py "movie.mkv" --config crf_search.example.json
```

Both codecs run by default. Select one with `--codec x264` or `--codec x265`.
The filename remains `crf_search.py` so the existing invocation still works.
Use the updated example configuration; the old `runtime`, `search`, stress
sample, and per-codec QP-policy fields have been removed.

The default output directory beside the movie is `<movie-stem>.crf-sweep/`:

| File | Contents |
| --- | --- |
| `qp-bitrate.png` | B-frame QP–bitrate figure, suitable for viewing or sharing. |
| `qp-bitrate.svg` | The same figure as a scalable standalone vector graphic. |
| `summary.csv` | One row per codec and CRF: average B-frame QP, average Mbps, sample count, encoded frames, B-frame count, duration, and video bytes. |
| `results.json` | Configuration, source and library information, exact sample ranges, crop, I/P/B QPs and frame counts, per-sample averages, and aggregate results. |
| `logs/` | Native encoder logs. |
| `cache/` | Completed sample measurements for repeatable runs and resuming. |

The figure has **average B-frame QP on the x-axis** and **average video bitrate in Mbps
on the y-axis**. Each codec has a separate curve, and each point is labelled
with the CRF that was actually encoded. Lines connect measured points; they
are not a fitted model. The table and figure update after each complete CRF
measurement. The figure title identifies unfinished or interrupted runs.

Use `--output-dir PATH` to choose another output directory.

## Random sample selection

Default sampling configuration:

```json
{
  "sampling": {
    "count": 10,
    "min_seconds": 5.0,
    "max_seconds": 10.0,
    "seed": 0,
    "start": 0.0,
    "end": null
  }
}
```

The selected interval is divided into `count` equally sized sections. In each
section, the script uniformly draws a clip duration in the configured range,
then uniformly draws a valid start position. This gives random placement with
coverage across the movie and prevents overlapping requested sample ranges.
All five CRFs and both codecs use the **same** clips and crop.

A section shorter than `max_seconds` caps that section's maximum clip length.
If the interval cannot fit `count` clips of at least `min_seconds`, the script
reports an error; reduce the count or minimum length, or extend the interval.
It does not silently reduce your requested sample count. Set equal minimum
and maximum durations to use a fixed clip length.

The seed controls repeatable pseudorandom selection. The default seed is `0`;
choose a different nonnegative integer for a different set of clips. The seed
and exact requested start times and durations are printed and saved. Times
are relative to the selected video stream's start. Actual encoded timestamps,
frame counts, and durations are saved too, since frames have discrete
presentation times.

Override the configuration for one run:

```sh
uv run scripts/crf_search.py "movie.mkv" --config crf_search.example.json \
  --samples 8 --min-sample-seconds 5 --max-sample-seconds 10 --seed 42

uv run scripts/crf_search.py "movie.mkv" --config crf_search.example.json \
  --samples 6 --sample-seconds 8 --start 00:05:00 --end 01:45:00
```

`--sample-seconds` sets both duration bounds and cannot be combined with the
minimum/maximum CLI options. `start` and `end` accept seconds, `MM:SS`, or
`HH:MM:SS`, including fractions. Credits are not excluded automatically.

## Encoder settings and exact crop

The existing settings are preserved:

| Encoder | Profile | Level | Preset | Pixel format |
| --- | --- | --- | --- | --- |
| x264 | High | 4.1 | placebo | yuv420p (8-bit) |
| x265 | Main10 | auto | slower | yuv420p10le (10-bit) |

`crf_search.example.json` retains the supplied encoder argument strings.
`codecs.x264` and `codecs.x265` each accept `preset`, `tune`, `profile`, `level`,
`pixel_format`, `params`, and `options`. `params` accepts either a
colon-separated `key=value` string or an object. `options` accepts a mapping
of additional libavcodec options. Supplying `params` or `options` replaces
that entire mapping; other configuration objects merge with the defaults.
Unknown configuration keys are errors. CRF, statistics output, dimensions,
and timing are controlled by the sweep and cannot be overridden in those
mappings.

The effective encoder settings are printed before encoding starts and stay
fixed for all five CRFs. The script checks that the PyAV build supplies the
selected encoder and pixel format. It preserves source resolution apart from
cropping and rejects video dimensions/frame rates that exceed an explicitly
configured encoder level.

Black margins are detected once before the sweep using PyAV and FFmpeg's
`bbox` filter. Up to two seconds at the center of each sample are inspected.
The union of detected content bounds provides one crop for all measurements.
Unreliable or absent content bounds retain the full frame. Sampling can miss
content outside the inspected windows; an explicit crop is available when
the bounds are known.

Crop dimensions and offsets remain exact. For example, `1920:804:0:137`
produces 1920×804, including the odd vertical offset. Width and height must
be even for these 4:2:0 encoders; incompatible dimensions produce an error
instead of being rounded.

```sh
uv run scripts/crf_search.py "movie.mkv" --config crf_search.example.json --crop 1920:804:0:137
uv run scripts/crf_search.py "movie.mkv" --config crf_search.example.json --no-crop
```

`video.crop` accepts `"auto"` (default), `null`, or a `width:height:x:y` string.
`--crop none` is equivalent to `--no-crop`. `video.cropdetect.limit` defaults
to `24/255` of the raw luminance code range, scaled to source bit depth;
`video.cropdetect.seconds` defaults to `2.0`. `video.stream` or
`--video-stream` selects a zero-based video stream, excluding attached cover
pictures. Audio, subtitles, and other streams are ignored.

## How the averages are calculated

For each clip, the encoder reports average QP and the number of I, P, and B
frames. Only its B-frame QP contributes to the reported average:

```text
clip_average_qp = encoder_average_B_frame_qp
```

The sweep combines clips using their encoded B-frame counts:

```text
average_qp = sum(clip_average_qp * clip_B_frame_count) / total_B_frame_count
average_bitrate_mbps = 8 * total_encoded_video_bytes / total_presentation_seconds / 1_000_000
```

The QP average weights every encoded B-frame equally and excludes I- and
P-frames. A clip with no B-frames is excluded from the QP average but still
contributes its full bytes and duration to bitrate. If a whole CRF trial has
no B-frames, its QP is `null` in JSON, blank in CSV, and `N/A (no B-frames)`
on screen; the figure omits that point.

The `average_qp` field contains this B-frame-only average; `b_frames` records
its weighting count. The report uses schema version 3 and `qp_frame_type: "B"`
to identify this definition. Raw I/P/B statistics remain available in each
sample for inspection. Existing cached encodes can be reused to recalculate
the B-frame averages by rerunning the same command.

Bitrate includes all encoded video frames. It is the
duration-weighted average clip bitrate, so a longer clip contributes more
than a shorter one. Video bytes include all packets emitted when the encoder
flushes, but exclude audio, subtitles, and container overhead. QP is derived
from encoder statistics; x264's per-frame-type averages are rounded in its
native summary. QP values from different codecs are not a shared perceptual
quality scale, and these sampled bitrates estimate full-movie behavior.

A row is published only after **all** selected clips finish at that CRF.
Ctrl+C terminates the active encoding worker and saves the complete rows and
figure. Rerun with the same source, seed, and settings to reuse completed
sample encodes, including those from a partially completed row. Use
`--no-resume` to re-encode the same selected clips. Change `--seed` to select
new clips. An output directory's reports are replaced for the current run;
use separate directories to retain multiple experiments.
