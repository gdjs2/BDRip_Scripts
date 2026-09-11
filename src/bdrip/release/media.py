"""Movie metadata and shared MediaInfo formatting for release documents."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import imdbinfo
from pymediainfo import MediaInfo


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
    encoder = (
        getattr(track, "encoded_library_name", None)
        or getattr(track, "commercial_name", None)
        or getattr(track, "format", None)
        or getattr(track, "codec_id", None)
        or "Unknown"
    )
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


def audio_channel_text(track: Any, default: str = "Unknown") -> str:
    """Convert MediaInfo's discrete count/positions to a layout such as 7.1."""
    positions = getattr(track, "other_channel_positions", None)
    if positions:
        try:
            count = sum(float(part) for part in str(positions[0]).split("/"))
            return f"{count:.1f}"
        except ValueError:
            pass
    count = float(getattr(track, "channel_s", 0) or 0)
    if count:
        # MediaInfo's raw count includes the LFE channel; use the conventional
        # surround-layout notation when no position string is available.
        if count in {6.0, 8.0}:
            return f"{count - 1:.0f}.1"
        return f"{count:.1f}"
    return default


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
    language = first(
        getattr(track, "other_language", None), track.language or "Unknown"
    )
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
        language = first(
            getattr(track, "other_language", None), track.language or "Unknown"
        )
        languages.append(language)
        audio_lines.append(audio_text(track))

    subtitle_groups: dict[str, set[str]] = {}
    for track in texts:
        codec = track.codec_id or track.format or "Text"
        subtitle_groups.setdefault(codec, set()).add(track.language or "und")
    subtitles = (
        " | ".join(
            f"{codec} ({'&'.join(sorted(langs))})"
            for codec, langs in subtitle_groups.items()
        )
        or "None"
    )

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
