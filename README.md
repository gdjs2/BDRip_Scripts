# BDRip tools

Tools for CRF calibration, Blu-ray release preparation, screenshot thumbnails,
and PGS subtitle conversion. The implementation lives in the installable
`bdrip` package under `src/`. All video inspection, cropping, and encoding use
PyAV and its bundled FFmpeg libraries; no FFmpeg/FFprobe executable is needed.

## Quick start

Python 3.13 or later is required. From this repository:

```sh
uv sync --extra gui
uv run --extra gui bdrip gui --config crf_search.example.json
```

For calibration without the desktop GUI:

```sh
uv sync
uv run bdrip crf "movie.mkv" --config crf_search.example.json
```

Calibration uses PyAV to encode one centered minute at CRF 13 and 20, recording
B-frame QP and video bitrate. The GUI queues videos in background processes and
shows the fitted QP and exponential bitrate curves with hover estimates and
click-to-pin selection. Encoder defaults, exact cropping, report formats, and
saved queues are unchanged by the package reorganization.

See the [calibration and GUI guide](docs/crf-search.md) for encoder options,
progress, saved figures, logs, and result interpretation.

## Commands

Use `uv run bdrip --help` or `uv run bdrip COMMAND --help` for usage.

| Command | Purpose |
| --- | --- |
| `bdrip gui` | Desktop calibration queue and interactive curves. Requires the `gui` extra. |
| `bdrip crf VIDEO` | Calibrate a video and save figures, measurements, and logs. |
| `bdrip release prepare ROOT` / `build ROOT` | Prepare release folders or build their documents, checksums, and torrents. |
| `bdrip bbcode CONFIG` | Render a BBCode release post; screenshot uploads use `--upload`. |
| `bdrip nfo IMDB_ID VIDEO --source SOURCE` | Generate an NFO document. |
| `bdrip nfo-banner TEMPLATE` | Extract embedded NFO artwork. |
| `bdrip torrent-check TORRENT CONTENT` | Verify a torrent against its content directory or single file. |
| `bdrip thumbnails FOLDER` | Create screenshot thumbnails. |
| `bdrip subtitles INPUT.sup [OUTPUT.srt]` | Convert PGS subtitles with the separate PaddleOCR setup. |
| `bdrip rip VIDEO ...` | Full-file two-pass PyAV encoder. |
| `bdrip crop VIDEO` | Exact crop detection with PyAV. |

`bdrip-crf`, `bdrip-gui`, and `python -m bdrip` are also supported. The old
`scripts/*.py` launchers and root `main.py` have been removed; use the
[command migration table](docs/commands.md) to update saved commands.
Root-level example configs retain their existing paths.

The [standalone video guide](docs/video-tools.md) covers crop inspection,
two-pass encoding, progress, and retained native logs. Both codecs run entirely
through PyAV, including first-pass statistics and final Matroska muxing.
Release tools retain their MediaInfo and metadata-service requirements; subtitle
OCR retains its separate PaddleOCR runtime and model requirements.

## Project layout

| Location | Responsibility |
| --- | --- |
| `src/bdrip/video/` | Video inspection, crop detection, and encoding. |
| `src/bdrip/crf/` | Encoder configuration, calibration, models, and figure export. |
| `src/bdrip/crf/gui/` | Desktop views, interactive plots, persistent queue, and task worker. |
| `src/bdrip/release/` | Metadata, BBCode/NFO, screenshot uploads, torrents, and release workflow. |
| `src/bdrip/images/` | Image discovery and thumbnail generation. |
| `src/bdrip/subtitles/` | PGS decoding, OCR, cache, cue merging, and SRT output. |
| `src/bdrip/common/` | Shared filesystem, JSON, environment, validation, time, and progress helpers. |
| `tests/` | Tests grouped by feature, with shared media and model fixtures. |

Read the [architecture guide](docs/architecture.md) for module boundaries and
where to add code, the [release guide](docs/release-pipeline.md) for publishing
workflows, and the [subtitle guide](docs/subtitles.md) for OCR usage.

## Development

```sh
uv sync --extra gui
uv run --extra gui python -m unittest discover -s tests -t . -v
uv build
```

On a machine without a display, set `QT_QPA_PLATFORM=offscreen` for GUI tests.
The tests use generated media, temporary output directories, and mocked external
metadata; they do not upload screenshots or download OCR models.
Video integration tests run native x264/x265 encodes with an empty `PATH`,
including two-pass statistics, exact crop, timing, and output recovery checks.
