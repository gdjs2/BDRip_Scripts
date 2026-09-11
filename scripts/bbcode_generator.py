"""Generate a WiKi-style BBCode release post from JSON configuration."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import mimetypes
import os
import uuid
import math
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import imdbinfo
from pymediainfo import MediaInfo


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
TTG_API_URL = "https://tu.totheglory.im/api.php"
HTTP_HEADERS = {"User-Agent": "curl/8.12.1", "Accept": "application/json"}
LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", path.name)]


def text_value(config: dict[str, Any], name: str, base: Path) -> str:
    """Read either `name` or the UTF-8 file named by `name_file`."""
    file_name = config.get(f"{name}_file")
    if file_name:
        return (base / file_name).resolve().read_text(encoding="utf-8").strip()
    return str(config.get(name, "")).strip()


def required(config: dict[str, Any], name: str) -> Any:
    value = config.get(name)
    if value in (None, ""):
        raise ValueError(f"Missing required config key: {name}")
    return value


def first(value: Any, default: str = "Unknown") -> str:
    if isinstance(value, list):
        return str(value[0]) if value else default
    return str(value) if value not in (None, "") else default


def bitrate(track: Any) -> str:
    values = getattr(track, "other_bit_rate", None)
    if values:
        return str(values[0])
    raw = getattr(track, "bit_rate", None)
    return f"{int(raw) / 1000:.0f} kb/s" if raw else "Unknown bitrate"


def video_codec_text(track: Any) -> str:
    encoder = (getattr(track, "encoded_library_name", None)
               or getattr(track, "commercial_name", None)
               or getattr(track, "format", None)
               or getattr(track, "codec_id", None) or "Unknown")
    profile = str(getattr(track, "format_profile", None) or "")
    level_match = re.search(r"@\s*(L?[\d.]+)", profile, flags=re.IGNORECASE)
    level = level_match.group(1) if level_match else ""
    if level and not level.upper().startswith("L"):
        level = f"L{level}"
    codec = f"{encoder}_{level}" if level else str(encoder)
    raw_bitrate = getattr(track, "bit_rate", None)
    if raw_bitrate:
        rate = f"{float(raw_bitrate) / 1_000_000:.1f} Mbps"
    else:
        rate = bitrate(track).replace("Mb/s", "Mbps")
    return f"{codec} @ {rate}"


def audio_channel_text(track: Any) -> str:
    """Convert MediaInfo's discrete count/positions to a layout such as 7.1."""
    positions = getattr(track, "other_channel_positions", None)
    if positions:
        try:
            count = sum(float(part) for part in str(positions[0]).split("/"))
            return f"{count:.1f}"
        except ValueError:
            pass
    channels = getattr(track, "channel_s", None)
    if channels:
        count = float(channels)
        # MediaInfo's raw count includes the LFE channel; use the conventional
        # surround-layout notation when no position string is available.
        if count in {6.0, 8.0}:
            return f"{count - 1:.0f}.1"
        return f"{count:.1f}"
    return "Unknown"


def compact_audio_codec(name: str) -> str:
    replacements = {
        "Dolby TrueHD with Dolby Atmos": "Atmos/ TrueHD",
        "Dolby Digital Plus with Dolby Atmos": "Atmos/ E-AC-3",
        "DTS-HD Master Audio": "DTS-HD MA",
        "DTS-HD High Resolution Audio": "DTS-HD HRA",
        "Dolby Digital Plus": "E-AC-3",
        "Dolby Digital": "AC-3",
    }
    return replacements.get(name, name)


def audio_text(track: Any, max_length: int = 52) -> str:
    language = first(getattr(track, "other_language", None), track.language or "Unknown")
    codec = track.commercial_name or track.format or track.codec_id or "Unknown"
    channels = audio_channel_text(track)

    def render(codec_name: str) -> str:
        return f"{language} {codec_name} {channels} @ {bitrate(track)}"

    value = render(codec)
    return render(compact_audio_codec(codec)) if len(value) > max_length else value


def aspect_ratio_text(value: Any) -> str:
    """Normalize a numeric or a:b display aspect ratio to x:1."""
    text = str(value or "").strip()
    if not text or text.casefold() == "unknown":
        return "Unknown"
    try:
        if ":" in text:
            width, height = text.split(":", 1)
            ratio = float(width.strip()) / float(height.strip())
        else:
            ratio = float(text)
    except (ValueError, ZeroDivisionError):
        return text if text.endswith(":1") else f"{text}:1"
    truncated = math.floor(ratio * 100) / 100
    formatted = f"{truncated:.2f}"
    return f"{formatted}:1"


def media_metadata(file_path: Path) -> dict[str, Any]:
    info = MediaInfo.parse(file_path)
    general = next((t for t in info.tracks if t.track_type == "General"), None)
    video = next((t for t in info.tracks if t.track_type == "Video"), None)
    audios = [t for t in info.tracks if t.track_type == "Audio"]
    texts = [t for t in info.tracks if t.track_type == "Text"]
    if general is None or video is None:
        raise RuntimeError("MediaInfo did not find both General and Video tracks")

    size = int(general.file_size or file_path.stat().st_size)
    duration = first(getattr(general, "other_duration", None))
    aspect = aspect_ratio_text(
        getattr(video, "display_aspect_ratio", None)
        or first(getattr(video, "other_display_aspect_ratio", None))
    )

    audio_lines = []
    languages = []
    for track in audios:
        language = first(getattr(track, "other_language", None), track.language or "Unknown")
        languages.append(language)
        audio_lines.append(audio_text(track))

    subtitle_groups: dict[str, set[str]] = {}
    for track in texts:
        codec = track.codec_id or track.format or "Text"
        subtitle_groups.setdefault(codec, set()).add(track.language or "und")
    subtitles = " | ".join(
        f"{codec} ({'&'.join(sorted(langs))})" for codec, langs in subtitle_groups.items()
    ) or "None"

    chapters = any(t.track_type == "Menu" for t in info.tracks)
    return {
        "file_size": f"{size / (1 << 30):.2f} GiB",
        "duration": duration,
        "video_codec": video_codec_text(video),
        "resolution": f"{video.width}x{video.height}",
        "aspect_ratio": aspect,
        "frame_rate": f"{video.frame_rate} fps" if video.frame_rate else "Unknown",
        "audios": audio_lines or ["None"],
        "subtitles": subtitles,
        "languages": " | ".join(dict.fromkeys(languages)) or "Unknown",
        "chapters": "Yes" if chapters else "None",
    }


def imdb_metadata(imdb_id: str) -> dict[str, Any]:
    movie = imdbinfo.get_movie(imdb_id)
    if movie is None:
        raise RuntimeError(f"IMDb returned no metadata for {imdb_id}")
    rating = "N/A"
    if movie.rating is not None:
        votes = f" ({movie.votes:,} votes)" if movie.votes is not None else ""
        rating = f"{movie.rating}/10{votes}"
    return {
        "name": movie.title_localized or movie.title,
        "year": movie.year or "",
        "genre": " | ".join(movie.genres) or "Unknown",
        "rating": rating,
        "imdb_url": movie.url or f"https://www.imdb.com/title/{movie.imdbId}/",
        "release_date": movie.release_date or "Unknown",
        "plot": movie.plot or (movie.summaries[0] if movie.summaries else ""),
        "poster_url": movie.cover_url or "",
    }


def image_files(directory: Path, *, missing_ok: bool = False) -> list[Path]:
    if not directory.is_dir():
        if missing_ok:
            return []
        raise FileNotFoundError(f"Screenshot directory not found: {directory}")
    return sorted((p for p in directory.iterdir()
                   if p.is_file() and not p.name.startswith(".")
                   and p.suffix.casefold() in IMAGE_EXTENSIONS), key=natural_key)


def image_url(path: Path, base_url: str, thumb_suffix: str = "_thumb") -> tuple[str, str]:
    stem = quote(path.stem, safe="")
    suffix = quote(path.suffix, safe=".")
    base = base_url.rstrip("/")
    return f"{base}/{stem}{suffix}", f"{base}/{stem}{thumb_suffix}{suffix}"


def remote_release_folder(file_path: str | Path) -> str:
    """Convert a media filename to its TTG folder; preserve hyphens."""
    return Path(file_path).stem.replace(".", "_")


def screenshot_url_base(config: dict[str, Any]) -> str:
    """Resolve a safe account prefix and append the inferred release folder."""
    prefix_env = str(config.get("screenshot_url_prefix_env", "")).strip()
    prefix = os.environ.get(prefix_env, "").strip() if prefix_env else ""
    if not prefix:
        prefix = str(config.get("screenshot_url_prefix") or "").strip()

    # Old configs supplied the complete release-directory URL. Preserve that
    # behavior while new configs only store an account-level prefix.
    complete_base = False
    if not prefix:
        old_env = str(config.get("screenshot_url_base_env", "")).strip()
        prefix = os.environ.get(old_env, "").strip() if old_env else ""
        prefix = prefix or str(
            config.get("screenshot_url_base")
            or config.get("temporary_url_base")
            or ""
        ).strip()
        complete_base = bool(prefix)
    value = prefix
    if not value:
        raise ValueError(
            "Set the environment variable named by screenshot_url_prefix_env "
            "when not using --upload"
        )
    if any(character in value for character in "\r\n[]"):
        raise ValueError("screenshot_url_base contains unsafe BBCode/control characters")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ValueError("screenshot_url_base must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("screenshot_url_base must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("screenshot URL prefix must not contain a query string or fragment")
    base = value.rstrip("/")
    if complete_base:
        return base
    movie_stem = remote_release_folder(str(required(config, "file_path")))
    return f"{base}/{quote(movie_stem, safe='-_')}"


def load_env(path: Path) -> None:
    """Load simple KEY=VALUE entries without replacing existing variables."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def api_json(request: Request) -> Any:
    try:
        with urlopen(request, timeout=240) as response:
            return json.loads(response.read().decode("utf-8-sig"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"TTG API HTTP {exc.code}: {detail[:500]}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"TTG API request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("TTG API returned invalid JSON") from exc


def multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----Codex{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"), b"\r\n",
        ))
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    safe_name = file_path.name.replace('"', "")
    chunks.extend((
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode("utf-8"),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        file_path.read_bytes(), b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ))
    return b"".join(chunks), boundary


def find_url_pair(payload: Any, filename: str) -> tuple[str, str] | None:
    """Find original/thumbnail URLs in upload or list responses."""
    if isinstance(payload, dict):
        name = str(payload.get("filename") or payload.get("name") or "")
        original = next((str(payload[k]) for k in
                         ("url", "file_url", "original_url", "full_url")
                         if payload.get(k)), "")
        thumbnail = next((str(payload[k]) for k in
                          ("thumbnail_url", "thumb_url", "thumbnail", "thumb")
                          if payload.get(k)), "")
        if original and (not name or name == filename):
            return original, thumbnail
        for value in payload.values():
            found = find_url_pair(value, filename)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_url_pair(value, filename)
            if found:
                return found
    return None


def upload_image(path: Path, folder: str, token: str) -> tuple[str, str]:
    upload_options = {
        "folder": folder,
        "overwrite": "overwrite",
        "thumbnails": "1",
    }
    body, boundary = multipart(upload_options, path)
    # This server reliably honors upload options when they are present in the
    # query string. Keep them in the multipart body too for API compatibility.
    query = urlencode({"action": "upload", "api_token": token, **upload_options})
    request = Request(
        f"{TTG_API_URL}?{query}", data=body, method="POST",
        headers={
            **HTTP_HEADERS,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    payload = api_json(request)
    pair = find_url_pair(payload, path.name)
    if not pair or not pair[0] or not pair[1]:
        uploaded_items = payload.get("uploaded", []) if isinstance(payload, dict) else []
        if len(uploaded_items) == 1 and isinstance(uploaded_items[0], dict):
            returned_name = uploaded_items[0].get("name") or uploaded_items[0].get("filename")
            returned_pair = find_url_pair(uploaded_items[0], str(returned_name or ""))
            if returned_pair and returned_pair[0] and returned_pair[1]:
                if returned_name and returned_name != path.name:
                    LOGGER.warning(
                        "TTG renamed %s to %s despite overwrite=overwrite; using returned URLs",
                        path.name, returned_name,
                    )
                return returned_pair
        raise RuntimeError(f"Could not find original/thumbnail URLs after uploading {path.name}: {payload}")
    return pair


def upload_screenshots(files: list[Path], folder: str, token: str,
                       attempts: int = 3, retry_delay: float = 10,
                       event_callback: Any = None, verbose: bool = True) -> tuple[
                           dict[Path, tuple[str, str]], dict[Path, str]
                       ]:
    uploaded: dict[Path, tuple[str, str]] = {}
    failed: dict[Path, str] = {}
    for index, path in enumerate(files, 1):
        for attempt in range(1, attempts + 1):
            if event_callback:
                event_callback("start", path, attempt, attempts, None)
            elif verbose:
                print(
                    f"Uploading {folder}/{path.name} ({index}/{len(files)}), "
                    f"attempt {attempt}/{attempts}...",
                    file=sys.stderr,
                )
            try:
                uploaded[path] = upload_image(path, folder, token)
                if event_callback:
                    event_callback("success", path, attempt, attempts, None)
                break
            except (OSError, RuntimeError) as exc:
                failed[path] = str(exc)
                LOGGER.warning(
                    "Screenshot upload failed: %s/%s (attempt %d/%d): %s",
                    folder, path.name, attempt, attempts, exc,
                )
                if attempt < attempts:
                    if event_callback:
                        event_callback("retry", path, attempt, attempts, str(exc))
                    elif verbose:
                        print(
                            f"warning: upload failed for {path.name}: {exc}; "
                            f"retrying in {retry_delay:g} seconds",
                            file=sys.stderr,
                        )
                    time.sleep(retry_delay)
                elif event_callback:
                    event_callback("failed", path, attempt, attempts, str(exc))
        if path in uploaded:
            failed.pop(path, None)
    return uploaded, failed


def cached_screenshot_urls(config: dict[str, Any], screenshot_root: Path) -> dict[
    str, dict[Path, tuple[str, str]]
]:
    """Resolve persisted screenshot URLs for files which still exist locally."""
    result: dict[str, dict[Path, tuple[str, str]]] = {}
    cache = config.get("screenshot_uploads", {})
    if not isinstance(cache, dict):
        return result
    for section in ("Comparison", "More"):
        section_cache = cache.get(section, {})
        if not isinstance(section_cache, dict):
            continue
        resolved: dict[Path, tuple[str, str]] = {}
        for path in image_files(screenshot_root / section, missing_ok=True):
            item = section_cache.get(path.name, {})
            if isinstance(item, dict) and item.get("url") and item.get("thumbnail_url"):
                resolved[path] = (str(item["url"]), str(item["thumbnail_url"]))
        if resolved:
            result[section] = resolved
    return result


def upload_screenshots_cached(
    config: dict[str, Any], config_path: Path, screenshot_root: Path,
    root_folder: str, token: str, document: dict[str, Any] | None = None,
    event_callback: Any = None, verbose: bool = True,
) -> tuple[dict[str, dict[Path, tuple[str, str]]], dict[str, dict[str, str]]]:
    """Upload missing/previously failed images and persist progress in config."""
    cache = config.setdefault("screenshot_uploads", {})
    if not isinstance(cache, dict):
        cache = config["screenshot_uploads"] = {}
    old_failures = config.get("failed_screenshot_uploads", {})
    if not isinstance(old_failures, dict):
        old_failures = {}
    retry_names = {
        section: set(items) for section, items in old_failures.items()
        if isinstance(items, dict)
    }
    retry_only = any(retry_names.values())
    failures: dict[str, dict[str, str]] = {
        section: dict(items) for section, items in old_failures.items()
        if isinstance(items, dict)
    }

    def persist() -> None:
        config["failed_screenshot_uploads"] = {
            section: items for section, items in failures.items() if items
        }
        config_path.write_text(
            json.dumps(document if document is not None else config,
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    for section in ("Comparison", "More"):
        files = image_files(screenshot_root / section, missing_ok=True)
        section_cache = cache.setdefault(section, {})
        if not isinstance(section_cache, dict):
            section_cache = cache[section] = {}
        if retry_only:
            targets = [path for path in files if path.name in retry_names.get(section, set())]
        else:
            targets = [path for path in files if path.name not in section_cache]
        for path in targets:
            succeeded, failed = upload_screenshots(
                [path], f"{root_folder}/{section}", token,
                event_callback=event_callback, verbose=verbose,
            )
            if path in succeeded:
                full, thumb = succeeded[path]
                section_cache[path.name] = {"url": full, "thumbnail_url": thumb}
                failures.setdefault(section, {}).pop(path.name, None)
            else:
                failures.setdefault(section, {})[path.name] = failed.get(
                    path, "Unknown upload error"
                )
            persist()
    persist()
    return cached_screenshot_urls(config, screenshot_root), {
        section: items for section, items in failures.items() if items
    }


def report_failed_uploads(failures: dict[str, dict[str, str]]) -> None:
    if not failures:
        return
    print("\n*** FAILED SCREENSHOT UPLOADS (will retry next run) ***", file=sys.stderr)
    for section, items in failures.items():
        for filename, error in items.items():
            print(f"  [{section}] {filename}: {error}", file=sys.stderr)
    print("****************************************************", file=sys.stderr)


def screenshot_block(files: list[Path], base_url: str, thumb_suffix: str,
                     columns: int = 2,
                     uploaded: dict[Path, tuple[str, str]] | None = None) -> str:
    items = []
    for path in files:
        if uploaded is not None and path not in uploaded:
            continue
        full, thumb = uploaded[path] if uploaded is not None else image_url(path, base_url, thumb_suffix)
        items.append(f"[URL={full}][IMG]{thumb}[/IMG][/URL]")
    return "\n".join(" ".join(items[i:i + columns]) for i in range(0, len(items), columns))


def info_line(label: str, value: Any) -> str:
    dotted_label = f"{label:.<26}:"
    return f"[font=courier new]{dotted_label}[/font] {value}"


def release_heading(english_name: str) -> str:
    """Apply the release-title colors used by the WiKi BBCode template."""
    match = re.match(r"^(.*?)(\s+\d{4}\b.*)$", english_name.strip())
    if match:
        movie_title, release_details = match.groups()
    else:
        movie_title, release_details = english_name.strip(), ""
    release_details = re.sub(
        r"\bBluRay\b", "[color=#054ed3]BluRay[/color]", release_details,
        flags=re.IGNORECASE,
    )
    return (
        f"[b][size=3][color=#ff0000]{movie_title}[/color]"
        f"{release_details} [/size][/b]"
    )


def encoder_info_heading(video_codec: Any) -> str:
    match = re.search(
        r"(?<![A-Za-z0-9])(x26[45])(?=$|[^A-Za-z0-9])",
        str(video_codec), flags=re.IGNORECASE,
    )
    return f".{match.group(1).lower()}.Info" if match else ".Encoder.Info"


def render(config: dict[str, Any], config_dir: Path,
           uploaded: dict[str, dict[Path, tuple[str, str]]] | None = None) -> str:
    movie_file = (config_dir / required(config, "file_path")).resolve()
    screenshot_root = (config_dir / required(config, "screenshots_dir")).resolve()
    if not movie_file.is_file():
        raise FileNotFoundError(f"Movie file not found: {movie_file}")

    imdb = imdb_metadata(str(required(config, "imdb_id")))
    imdb.update(config.get("imdb_overrides", {}))
    if config.get("movie_name"):
        imdb["name"] = str(config["movie_name"])
    media = media_metadata(movie_file)
    media.update(config.get("media_overrides", {}))

    english = str(required(config, "english_name"))
    chinese = str(required(config, "chinese_name")).strip()
    if not (chinese.startswith("[") and chinese.endswith("]")):
        chinese = f"[{chinese}]"
    extra = str(config.get("extra_description", "")).strip()
    encoder = str(required(config, "encoder"))
    encoder_info = text_value(config, "encoder_info", config_dir)
    description = text_value(config, "movie_description", config_dir)
    if not encoder_info:
        raise ValueError("Set encoder_info or encoder_info_file")
    if not description:
        raise ValueError("Set movie_description or movie_description_file")

    thumb_suffix = str(config.get("thumbnail_suffix", "_thumb"))
    if not screenshot_root.is_dir():
        raise FileNotFoundError(f"Screenshot root directory not found: {screenshot_root}")
    comparisons = image_files(screenshot_root / "Comparison", missing_ok=True)
    more = image_files(screenshot_root / "More", missing_ok=True)
    url_base = ""
    if (comparisons or more) and uploaded is None:
        url_base = screenshot_url_base(config)
    title = " ".join(x for x in (english, chinese, extra) if x)
    heading_name = str(config.get("release_heading") or english)
    poster = str(config.get("poster_url") or imdb["poster_url"])
    source = str(config.get("source", "Unknown"))
    release_date = str(config.get("release_date", date.today().isoformat()))
    plot = str(config.get("plot") or imdb["plot"] or "No plot available.")

    audio = "\n".join(
        info_line(f"AUDiO CODEC {i}" if len(media["audios"]) > 1 else "AUDiO CODEC", value)
        for i, value in enumerate(media["audios"], 1)
    )
    encoder_heading = str(
        config.get("encoder_heading") or encoder_info_heading(media["video_codec"])
    )
    comparison_section = ""
    if comparisons:
        comparison_images = screenshot_block(
            comparisons, url_base + "/Comparison", thumb_suffix,
            uploaded=uploaded.get("Comparison") if uploaded else None,
        )
        comparison_section = f"""[color=red][u][b].Comparisons[/b][/u][/color]
[b][color=#054ED3]Source[/color][/b]                                                                       [b][color=#054ED3]{encoder}[/color][/b]
{comparison_images}

"""
    more_section = ""
    if more:
        more_images = screenshot_block(
            more, url_base + "/More", thumb_suffix,
            uploaded=uploaded.get("More") if uploaded else None,
        )
        more_section = f"""[color=red][u][b].More.Screens[/b][/u][/color]
{more_images}

"""
    return f"""{title}

[img]{poster}[/img]

{comparison_section}{more_section}
{release_heading(heading_name)}

[quote][u][b].Plot[/b][/u]

{plot}

{info_line('TAGLiNE', f'{imdb["name"]} ({imdb["year"]})')}
{info_line('GENRE', imdb['genre'])}
{info_line('iMDb RATiNG', imdb['rating'])}
{info_line('iMDb LiNK', imdb['imdb_url'])}

[u][b].Release.Info[/b][/u]
{info_line('ENCODER', encoder)}
{info_line('RELEASE DATE', release_date)}
{info_line('RELEASE SiZE', media['file_size'])}
{info_line('SOURCE', source)}

[u][b].Media.Info[/b][/u]
{info_line('RUNTiME', media['duration'])}
{info_line('ViDEO CODEC', media['video_codec'])}
{info_line('RESOLUTiON', media['resolution'])}
{info_line('DiSPLAY ASPECT RATiO', media['aspect_ratio'])}
{info_line('FRAME RATE', media['frame_rate'])}
{audio}
{info_line('SUBTiTLES', media['subtitles'])}
{info_line('CHAPTERS', media['chapters'])}

[b][u]{encoder_heading}[/u][/b]
{encoder_info}
[/quote]

{description}
"""


def example_config() -> dict[str, Any]:
    return {
        "english_name": "Golden Boy 2025 1080p BluRay x265 10bit-WiKi",
        "chinese_name": "[金童]",
        "extra_description": "国粤双语 内封中字",
        "file_path": "path/to/Golden.Boy.2025.1080p.BluRay.x265.10bit-WiKi.mkv",
        "imdb_id": "tt35498436",
        "encoder": "WiKi",
        "source": "1080p Blu-ray AVC TrueHD 5.1",
        "screenshots_dir": "path/to/Artifacts/Screenshots",
        "encoder_info_file": "encoder_info.txt",
        "movie_description_file": "movie_description.txt",
        "screenshot_url_prefix_env": "TTG_SCREENSHOT_URL_PREFIX",
        "thumbnail_suffix": "_thumb",
        "output": "Golden.Boy.2025.1080p.BluRay.x265.10bit-WiKi.bbcode.txt"
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="bbcode_config.json",
                        help="UTF-8 JSON config (default: bbcode_config.json)")
    parser.add_argument("-o", "--output", help="override the output path")
    parser.add_argument("--print-example", action="store_true", help="print example JSON and exit")
    parser.add_argument("--upload", action="store_true",
                        help="upload screenshots to TTG and embed returned URLs")
    parser.add_argument("--env-file", default=".env",
                        help="environment file containing TU_TTG_TOKEN (default: .env)")
    args = parser.parse_args()
    if args.print_example:
        print(json.dumps(example_config(), ensure_ascii=False, indent=2))
        return 0

    config_path = Path(args.config).resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        load_env(Path(args.env_file).resolve())
        screenshot_root = (config_path.parent / required(config, "screenshots_dir")).resolve()
        uploaded_cache = cached_screenshot_urls(config, screenshot_root)
        uploaded = uploaded_cache or None
        failures: dict[str, dict[str, str]] = {}
        if args.upload:
            token = os.environ.get("TU_TTG_TOKEN", "").strip()
            if not token:
                raise ValueError("TU_TTG_TOKEN is not set in the environment or .env")
            movie = (config_path.parent / required(config, "file_path")).resolve()
            default_folder = remote_release_folder(movie)
            root_folder = str(config.get("upload_folder") or default_folder).strip("/\\")
            uploaded_cache, failures = upload_screenshots_cached(
                config, config_path, screenshot_root, root_folder, token
            )
            uploaded = uploaded_cache or {}
        output_value = args.output or config.get("output")
        if output_value:
            output = (config_path.parent / output_value).resolve()
        else:
            movie = (config_path.parent / required(config, "file_path")).resolve()
            output = Path.cwd() / f"{movie.stem}.bbcode.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render(config, config_path.parent, uploaded), encoding="utf-8", newline="\n")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {output}")
    report_failed_uploads(failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
