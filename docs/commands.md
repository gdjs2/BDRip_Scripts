# Command migration

All implementation code now lives under `src/bdrip/`. The redundant `scripts/`
launchers and root `main.py` have been removed. Install the package with
`uv sync` (`uv sync --extra gui` for the desktop interface).

Use `uv run bdrip COMMAND ...` from the repository, or `bdrip COMMAND ...` in
an environment where the package is installed. `python -m bdrip` is equivalent.
Existing example JSON filenames and task folders retain their paths.

| Removed script | Replacement command |
| --- | --- |
| `scripts/bbcode_generator.py` | `bdrip bbcode` |
| `scripts/release_pipeline.py` | `bdrip release` |
| `scripts/nfo_generator.py` | `bdrip nfo` |
| `scripts/nfo_banner2b64.py` | `bdrip nfo-banner` |
| `scripts/check_torrent.py` | `bdrip torrent-check` |
| `scripts/create_thumb.py` | `bdrip thumbnails` |
| `scripts/pgs2srt.py` | `bdrip subtitles` |
| `scripts/rip.py` | `bdrip rip` |
| `scripts/detect_cropfilter.py` | `bdrip crop` |
| `scripts/crf_search.py` | `bdrip crf` or `bdrip-crf` |
| `scripts/crf_gui.py` | `bdrip gui` or `bdrip-gui` |
| `main.py` | `bdrip` or `python -m bdrip` |

All commands support `--help`. Crop arguments use numeric `width:height:x:y`
values with an optional `crop=` prefix. Arbitrary FFmpeg command/filter strings
are not accepted by the PyAV ripper. Crop and rip now use PyAV's bundled
libraries, so an FFmpeg/FFprobe executable is unnecessary.
