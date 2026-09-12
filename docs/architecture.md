# Project architecture

`bdrip` is an installable Python package using the `src/` layout. Run `uv sync`
after checking out the repository. Imports and background processes then work
independently of the current directory. `pyproject.toml` defines the package and
its console commands; `uv.lock` records dependency versions.

## Package boundaries

```text
src/bdrip/
  cli.py                  Command dispatch; load only the selected feature
  common/                 Shared persistence, environment, time, validation, progress
  video/
    probe.py              PyAV stream inspection and video errors
    crop.py               Exact PyAV crop detection and filtering
    encode.py             Native encoder settings, statistics, and worker entry point
    cropdetect.py         Crop command using the shared PyAV detector
    two_pass.py           Full-file two-pass coordinator, progress, and command
    transcode.py          Native PyAV pass worker, stats, and video-only muxing
  crf/
    config.py             Encoder defaults and config validation
    calibration.py        Encode samples at two CRFs, average measurements, cache/report
    model.py              Select samples, fit and evaluate QP/bitrate equations
    plot.py               Shared figure construction and PNG/SVG export
    gui/
      app.py              Main window and task controls
      style.py            Shared desktop colors and compact widget styling
      options.py          Encoder option editor
      estimates.py        Model loading and estimate presentation
      plot.py             Interactive canvas, hover, and pinning
      preview.py          Figure preview and legacy image viewer
      queue.py            Persistent queue and process lifecycle
      worker.py           Background task entry point
  release/
    pipeline.py           Prepare/build orchestration and release naming
    catalog.py            IMDb/TMDB title selection and translations
    media.py              MediaInfo metadata and common document formatting
    bbcode.py, nfo.py      Document rendering and their commands
    artwork.py, banner.py  NFO artwork and template extraction
    screenshots.py        Uploads, retries, and screenshot URL cache
    torrent.py            Torrent creation/verification and MD5 calculation
    progress.py           Release console and concurrent status display
  images/
    files.py              Visible-image discovery in natural filename order
    thumbnails.py         Thumbnail creation and command
  subtitles/
    cli.py                Arguments and conversion orchestration
    models.py             Shared visual-event, OCR-result, and cue records
    pgs.py                Parse and decode subtitle bitmaps
    ocr.py                Preprocessing, recognition, and candidate scoring
    cache.py              Resumable OCR cache
    srt.py                Cue merging, timestamps, SRT, and review reports
```

Keep reusable video operations in `video`; calibration policy belongs in `crf`.
The video backend imports no Qt or GUI code. Model calculations are independent
of the GUI, and both exported figures and the interactive view use the same
plot-building functions. GUI modules depend on those core modules, with PySide6
installed only when the `gui` extra is selected.

Release orchestration calls document, metadata, upload, and torrent helpers.
Shared image discovery serves both release screenshots and thumbnails, with
each caller's supported formats preserved. Audio codec abbreviations and aspect
ratios are formatted in one place. Shared atomic JSON persistence is used by
calibration reports, worker progress, and queue state.

Crop inspection and full-file encoding reuse PyAV stream inspection and exact
crop filters. `video.two_pass` runs `video.transcode` as an isolated Python
worker for each pass, capturing native logs and polling frame progress. All
FFmpeg work occurs through PyAV; no external FFmpeg/FFprobe process is launched.
See the [standalone video guide](video-tools.md).

Subtitle decoding, OCR, and SRT output have separate modules so cue processing
can be used and tested without starting the OCR engine. Optional OCR dependencies
are loaded when conversion starts. Importing the root package or requesting
command help does not start encoding, upload files, or initialize OCR models.

## Commands and workers

`bdrip.cli` dispatches `bdrip COMMAND` to the feature's `main(argv)` function.
`bdrip-crf` and `bdrip-gui` provide direct entry points, while `python -m bdrip`
uses the same dispatcher.

The desktop queue starts `python -m bdrip.crf.gui.worker` in the task workspace.
Calibration starts `python -m bdrip.video.encode` for each native encode. Neither
worker relies on locating a sibling script or changing `sys.path`. Encoders run
in the existing sequence: x264 at both CRFs, followed by x265 at both CRFs.

## Compatibility and development

The old `scripts/` launchers and root `main.py` have been removed. Use the
installed commands or module entry points; the [command migration table](commands.md)
lists replacements. Example configs and saved tasks remain readable.
Calibration schema 5 records sample means and their individual measurements.
Native encoder code changes invalidate cached sample measurements;
saved results and figures remain readable.

Put reusable functions in the relevant package and import them by package name:

```python
from bdrip.crf.config import load_config
from bdrip.crf.model import predict
from bdrip.video.probe import inspect_video
```

Tests follow feature boundaries under `tests/crf`, `tests/video`, `tests/gui`,
`tests/release`, `tests/images`, `tests/subtitles`, and `tests/common`.
Generated media and example model data are shared through `tests/fixtures`.
Run from the repository root:

```sh
uv run --extra gui python -m unittest discover -s tests -t . -v
uv build
```

The build produces a wheel and source archive for the package; application
modules run independently of the repository working directory.
