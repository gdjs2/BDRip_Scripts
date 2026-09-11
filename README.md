# BBCode release generator

`scripts/bbcode_generator.py` builds a UTF-8 WiKi-style BBCode release post. It
fetches title, plot,
poster, genre, rating, and link data with `imdbinfo`; it reads the release file
with MediaInfo; and it enumerates images in the `Comparison` and `More`
subdirectories of `screenshots_dir`.

The `Comparison` and `More` subdirectories are optional. If either directory is
missing or contains no supported images, its BBCode section is omitted and no
upload request is made for it.

Copy `bbcode_config.example.json` to `bbcode_config.json`, edit it, then run:

```powershell
uv run python scripts/bbcode_generator.py
```

Pass another config and/or output path when needed:

```powershell
uv run python scripts/bbcode_generator.py release.json -o post.bbcode.txt
```

Upload every screenshot and embed the URLs returned by TTG:

```powershell
uv run python scripts/bbcode_generator.py release.json --upload
```

The token is read from `TU_TTG_TOKEN` in the process environment or `.env` and
sent through the API's `api_token` URL parameter. No `whoami` or `list` request
is made. Uploads use `overwrite=overwrite` and `thumbnails=1`. The default remote root is
the media filename without its extension, with dots replaced by underscores.
For example, `Movie.Name.2026.mkv` uses `/Movie_Name_2026/Comparison` and
`/Movie_Name_2026/More`. Original screenshot filenames are retained. Set
`upload_folder` in the JSON config to override the remote root.

Relative paths in a config are resolved relative to that config file. Long
`encoder_info` and `movie_description` values may be written directly in JSON,
or stored in UTF-8 files through `encoder_info_file` and
`movie_description_file`. Without `--upload`, `screenshot_url_prefix_env`
identifies an environment variable containing the public account-level prefix,
for example
`TTG_SCREENSHOT_URL_PREFIX=https://tu.totheglory.im/picture/<account-id>/`.
The generator infers the release folder from the final MKV filename, replacing
dots with underscores, then appends `Comparison`/`More`, safely percent-encoded
filenames, and `_thumb`. The prefix must use HTTPS and cannot contain
credentials, query parameters, fragments, control characters, or BBCode
delimiters. API tokens remain separate in `TU_TTG_TOKEN` and are never included
in generated URLs.

Optional `imdb_overrides` and `media_overrides` JSON objects can replace any
automatically discovered field when an upstream value needs correction.

## Two-stage release pipeline

Prepare both x264 and x265 release trees from a Blu-ray working directory:

```powershell
uv run python scripts/release_pipeline.py prepare "D:\Movie.Source.Directory" `
  --imdb-id tt1234567 --douban-id 12345678
```

This validates the single MKV under `Streams`, retrieves the canonical IMDb
title/year, prompts you to select when IMDb supplies multiple movie names,
collects Chinese IMDb AKAs plus every Chinese TMDB translation and alternative
title, and writes the deterministic de-duplicated result to
`common.chinese_name` in `[中文名/香港译名/台湾译名]` form. Set either a TMDB
API Read Access Token or v3 API key in the project or Blu-ray-root `.env`:

```dotenv
TMDB_API_TOKEN=your_tmdb_read_access_token
# Alternatively: TMDB_API_KEY=your_tmdb_v3_api_key
```

The generated config records only the environment-variable names, never the
secret itself. If neither credential is available, preparation still uses
qualified Chinese IMDb AKAs. An existing non-empty `chinese_name` is preserved;
`--force` refreshes it from IMDb/TMDB.

Preparation also selects the core/lossy source audio with the highest channel count
(using bitrate as a tie-breaker), and creates `Ripped`,
`Logs`, `Artifacts`, and `Artifacts5`. Each artifact directory receives optional
`Screenshots/Comparison` and `Screenshots/More` directories, its computed BT
directory. The Blu-ray root receives one editable `release_pipeline.json` with
shared movie settings under `common` and codec-specific paths, encoder settings,
and screenshot upload state under `variants.x264` and `variants.x265`. Existing
legacy `*.bbcode.json` files are imported automatically the first time `prepare`
creates the unified config; they are left in place. Existing variant settings are
preserved unless `--force` is passed. Use `--variant x264` or `--variant x265`
to prepare only one variant; the default is `all`.

For automation, bypass the title prompt explicitly:

```powershell
uv run python scripts/release_pipeline.py prepare "D:\Movie.Source.Directory" `
  --imdb-id tt1234567 --douban-id 12345678 --title "IMDb Movie Name"
```

Release-name audio tokens omit channel counts for lossy/core codecs (`DTS`,
`DDP`, and `AAC`). Plain AC-3/Dolby Digital is treated as the default and is
omitted entirely. LPCM uses the `LPCM` token without a channel count. TrueHD
and DTS-HD tokens retain their channel layout.

After editing the configs and placing each final MKV in its BT directory, build
all release files:

```powershell
uv run python scripts/release_pipeline.py build "D:\Movie.Source.Directory"
```

The build selects the newest matching final HandBrake log (or the explicit
`handbrake_log` config), extracts the final encoder summary, obtains a formatted
Douban description through the configurable PT-Gen endpoint with an IMDb
fallback, optionally uploads screenshots, generates BBCode and NFO files, and creates an
MD5 manifest in `<complete filename>.mkv <lowercase MD5>` format. It then creates a
private BitTorrent v1 torrent from the complete BT directory and re-reads every piece
to verify the stored hashes before publishing the final `.torrent` beside that directory.
Set the announce URL in the unified config before building:

```json
{
  "common": {
    "torrent": {
      "tracker": "https://tracker.example/announce",
      "source": "TTG",
      "piece_size": null,
      "verify": true
    }
  }
}
```

`piece_size` is expressed in bytes; `null` lets torf choose it. Each variant's
`torrent_output` controls its output path. The torrent is always marked private,
regardless of config, and its output must remain outside the BT directory. Set
`TU_TTG_TOKEN` in `.env`. A protected PT-Gen deployment can use the environment
variable named by `description_api_key_env` (default `PT_GEN_API_KEY`). NFO output
defaults to DOS CP437; set `common.nfo_encoding` to `utf-8` only when required.

Screenshot upload is opt-in:

```powershell
uv run python scripts/release_pipeline.py build "D:\Movie.Source.Directory" `
  --variant x265 --upload
```

The release pipeline keeps console output concise and in English. A Rich `Status`
spinner shows the active build stage plus real screenshot or torrent-piece
progress, such as `(3/12)` or `(428/1024)`, instead of a short seven-item progress
bar. It clears itself when work completes and is disabled automatically when output
is redirected. Detailed paths, retries, warnings, info hashes, and exception
tracebacks are appended to `release_pipeline.log` in the Blu-ray working root.
When both variants are selected, x264 and x265 run concurrently and appear as two
independent Rich spinner rows. Shared movie metadata is generated once. Writes to
the unified screenshot-upload cache are serialized so one variant cannot overwrite
the other variant's URLs.

Automatically discovered files whose basename starts with `.` are ignored. This
includes macOS metadata such as `.DS_Store` and `._movie.mkv`; such files are also
excluded from generated torrents.

## Two-point CRF calibration and desktop queue

The program encodes one **60-second clip centered in the video** at **CRF 13
and 20** using PyAV. It records average **B-frame QP** and video bitrate for
each encoder, then fits `QP(c) = a + b*c` and `ln R(c) = d + e*c` (R in Mbps).
Videos shorter than 60 seconds use their whole video stream.

```sh
uv run --extra gui scripts/crf_gui.py --config crf_search.example.json
```

Use **Encoder options…** to edit the preset, level, tune, parameters, and
additional FFmpeg options. **Add videos…** creates one task per video;
**Start queue** processes them in background processes. The GUI shows live
encoding progress, a shared-CRF plot with both fitted curves, and complete
logs. Hover over the plot to see the estimated B-frame QP and Mbps for each
encoder at the mouse’s CRF. QP uses the left y-axis and bitrate the right;
solid/dashed lines distinguish the metrics. Bitrate follows `R(c) = exp(d + e*c)`.
Click to pin the CRF and keep its predictions visible; click elsewhere to move
the pin, or use **Unpin** to resume hovering. The **Estimates** tab calculates
QP/bitrate at a chosen CRF instantly.
**Larger preview…** opens the same interactive plot in a resizable viewer with pan and zoom controls.

Each video finishes x264 at both CRFs before x265 starts. The queue supports
pause, cancel, and retry, and saves each task's reports, figure, settings,
and logs under `crf-tasks/`. Use `--workspace PATH` for another persistent
queue folder. Closing the GUI cancels active work and saves waiting tasks.

The CLI needs no GUI dependency or external FFmpeg/HandBrake executable:

```sh
uv run scripts/crf_search.py "movie.mkv" --config crf_search.example.json
```

The x264 High@4.1/placebo and x265 Main10/auto/slower defaults and supplied
argument strings are preserved and printed before encoding. Automatic
black-margin detection keeps exact crop dimensions and offsets. Use
`--codec x264` or `--codec x265` to select one encoder, `--crop 1920:804:0:137`
for a manual crop, or `--no-crop` to use the full frame.

The default `<movie-stem>.crf-model/` output contains:

- `qp-bitrate.png` and `.svg`: both curves on a shared CRF axis, with separate QP/Mbps scales.
- `summary.csv`: two actual measurements per codec.
- `estimates.csv`: fitted estimates at integer CRFs 13–20.
- `results.json`: sample, crop, encoder settings, measurements, and coefficients.
- `logs/` and `cache/`: native logs and completed encodes for resuming.

QP excludes I/P frames; bitrate includes all video frames and excludes audio,
subtitles, and container overhead. Missing B-frames prevent the QP fit while
preserving the bitrate model. The two-point curves approximate the selected
minute; actual full-movie behavior can differ.

Rerun with the same settings to reuse cached measurements, or use `--no-resume`
to encode again. `--output-dir PATH` selects another report folder. Previous
JSON configs can still supply encoder options; their old random sampling
settings are ignored. See [the calibration and GUI guide](docs/crf-search.md)
for model definitions, progress reporting, and existing queue migration.
