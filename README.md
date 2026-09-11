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

## CRF sweep and B-frame QP–bitrate figure

`scripts/crf_search.py` uses PyAV to encode random video clips at **CRF 14,
15, 16, 17, and 18**, then writes a figure and CSV table relating average B-frame
QP to average video bitrate. It needs no FFmpeg or HandBrake executable. Matplotlib
creates PNG and SVG figures.

```sh
uv sync
uv run scripts/crf_search.py "movie.mkv" --config crf_search.example.json
```

Defaults are ten clips of 5–10 seconds, with one uniformly random clip in
each equal section of the movie. A configurable seed makes the selection
repeatable. All CRFs and both codecs encode the same clips. Configure
`sampling.count`, `sampling.min_seconds`, `sampling.max_seconds`, and
`sampling.seed`, or override them for a run:

```sh
uv run scripts/crf_search.py "movie.mkv" --config crf_search.example.json \
  --samples 8 --min-sample-seconds 5 --max-sample-seconds 10 --seed 42
```

Use `--sample-seconds 8` for fixed-length clips, `--start`/`--end` to restrict
the sampling interval, and `--codec x264` or `--codec x265` for one encoder.
The existing x264 High@4.1/placebo and x265 Main10/auto/slower defaults and
custom argument strings are preserved and printed before encoding begins.
Automatic black-margin detection runs once and keeps exact crop dimensions
and offsets. `--crop 1920:804:0:137` supplies a manual crop; `--no-crop`
preserves the full frame.

The `<movie-stem>.crf-sweep/` directory contains:

- `qp-bitrate.png` and `qp-bitrate.svg`: average B-frame QP on x, average video Mbps on
  y, separate curves for the codecs, and a CRF label on every point.
- `summary.csv`: the five measured rows per selected codec.
- `results.json`: exact sample ranges, settings, crop, and individual metrics.
- `logs/` and `cache/`: encoder logs and reusable completed measurements.

QP uses only B-frames, weighted by the B-frame count in each clip. Bitrate
includes all video frames and is weighted by clip duration. Trials without
B-frames report unavailable QP and are omitted from the figure.
Only video is measured. The complete five-point sweep replaces the previous
adaptive optimizer and its time cutoff, so runtime depends on sample count,
clip length, and encoding speed. Use the updated example config; the old
search/runtime/QP-policy fields have been removed.

Rerun with the same seed and settings to reuse cached samples, or use
`--no-resume` to encode them again. `--output-dir` selects another report
location. See [the sweep guide](docs/crf-search.md) for configuration details
and average definitions.
