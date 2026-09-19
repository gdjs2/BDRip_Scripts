"""Prepare and build WiKi x264/x265 Blu-ray release artifacts."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path
from threading import Lock
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pymediainfo import MediaInfo
from rich.progress import Progress, SpinnerColumn, TextColumn

from bdrip.common.environment import load_env
from bdrip.common.files import is_visible_file
from bdrip.images.files import screenshot_files as image_files

from . import bbcode, nfo, screenshots
from .catalog import (
    TMDB_API_DEFAULT,
    choose_movie_title,
    discover_chinese_name,
    movie_for,
    release_title,
)
from .media import audio_channel_text
from .progress import CONSOLE, ProgressStatus, status_text, update_status
from .torrent import create_private_torrent, md5_file

VARIANTS = {
    "x264": {"artifact": "Artifacts", "codec": "x264", "bit_depth": ""},
    "x265": {"artifact": "Artifacts5", "codec": "x265", "bit_depth": ".10bit"},
}
CONFIG_SUFFIX = ".bbcode.json"
PIPELINE_CONFIG_NAME = "release_pipeline.json"
DESCRIPTION_API_DEFAULT = "https://ptgen.rhilip.info"
LOG_FILE_NAME = "release_pipeline.log"
LOGGER = logging.getLogger("bdrip.release")

COMMON_CONFIG_KEYS = {
    "movie_name",
    "chinese_name",
    "extra_description",
    "imdb_id",
    "douban_id",
    "encoder",
    "source",
    "movie_description_file",
    "description_api_base",
    "description_api_key_env",
    "screenshot_url_prefix_env",
    "thumbnail_suffix",
    "nfo_encoding",
    "imdb_overrides",
    "media_overrides",
    "poster_url",
    "plot",
    "release_date",
    "tmdb_api_base",
    "tmdb_api_token_env",
    "tmdb_api_key_env",
}


def variants(value: str) -> list[str]:
    return list(VARIANTS) if value == "all" else [value]


def setup_logging(root: Path) -> Path:
    log_root = root if root.is_dir() else Path.cwd()
    log_path = log_root / LOG_FILE_NAME
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.info("========== release pipeline started ==========")
    return log_path


def audio_release_token(media_file: Path) -> str:
    tracks = [t for t in MediaInfo.parse(media_file).tracks if t.track_type == "Audio"]
    if not tracks:
        return ""

    def track_name(track: Any) -> str:
        return " ".join(
            str(getattr(track, key, "") or "")
            for key in ("commercial_name", "format", "codec_id", "title")
        ).casefold()

    def is_core(track: Any) -> bool:
        name = track_name(track)
        if any(codec in name for codec in ("ac-3", "e-ac-3", "aac", "pcm")):
            return True
        return "dts" in name and not any(
            codec in name for codec in ("dts-hd", "dts:x", "dts-x", "xll")
        )

    def bit_rate(track: Any) -> int:
        value = getattr(track, "bit_rate", 0)
        return int(value or 0)

    def core_score(track: Any) -> tuple[int, int]:
        return int(getattr(track, "channel_s", 0) or 0), bit_rate(track)

    core_tracks = [track for track in tracks if is_core(track)]
    if core_tracks:
        track = max(core_tracks, key=core_score)
        name = track_name(track)
        if "pcm" in name:
            codec = "LPCM"
        elif "e-ac-3" in name or "digital plus" in name:
            codec = "DDP"
        elif "ac-3" in name or "dolby digital" in name:
            codec = "DD"
        elif "aac" in name:
            codec = "AAC"
        else:
            codec = "DTS"
        # Core and LPCM codecs do not carry a channel suffix in release names.
        # Plain AC-3/Dolby Digital is the default and gets no audio token.
        return "" if codec == "DD" else codec

    def lossless_score(track: Any) -> tuple[int, int]:
        name = " ".join(
            str(getattr(track, key, "") or "")
            for key in ("commercial_name", "format", "title")
        ).casefold()
        priority = (
            5
            if "atmos" in name
            else 4
            if "truehd" in name
            else 3
            if "dts-hd" in name
            else 1
        )
        return priority, int(getattr(track, "channel_s", 0) or 0)

    track = max(tracks, key=lossless_score)
    name = " ".join(
        str(getattr(track, key, "") or "")
        for key in ("commercial_name", "format", "title")
    ).casefold()
    layout = audio_channel_text(track, default="")
    if "atmos" in name and "truehd" in name:
        codec = "Atmos.TrueHD"
    elif "atmos" in name and ("e-ac-3" in name or "digital plus" in name):
        codec = "Atmos.DDP"
    elif "dts-hd master" in name:
        return f"DTS.MA{layout}"
    elif "dts:x" in name or "dts-x" in name:
        codec = "DTS-X.DTS-HD.MA"
    elif "truehd" in name:
        codec = "TrueHD"
    elif "dts-hd" in name:
        codec = "DTS-HD"
    elif "e-ac-3" in name or "digital plus" in name:
        codec = "DDP"
    elif "ac-3" in name or "dolby digital" in name:
        codec = "DD"
    else:
        codec = re.sub(
            r"[^A-Za-z0-9-]+", ".", str(track.format or track.codec_id)
        ).strip(".")
    return ".".join(x for x in (codec, layout) if x)


def source_mkv(root: Path) -> Path:
    streams = root / "Streams"
    files = sorted(path for path in streams.glob("*.mkv") if is_visible_file(path))
    if len(files) != 1:
        raise ValueError(
            f"Expected exactly one MKV directly in {streams}; found {len(files)}"
        )
    return files[0]


def source_description(root: Path) -> str:
    match = re.search(r"\.\d{4}\.(.+)$", root.name)
    value = match.group(1) if match else root.name
    value = value.replace("@", "-").replace(".", " ")
    value = re.sub(r"\b([257]) ([01])\b", r"\1.\2", value)
    return re.sub(r"\s+", " ", value).strip()


def make_release_name(
    movie: Any, variant: str, audio_token: str, selected_title: str = ""
) -> str:
    spec = VARIANTS[variant]
    parts = [
        release_title(selected_title or movie.title),
        str(movie.year),
        "1080p",
        "BluRay",
        f"{spec['codec']}{spec['bit_depth']}",
        audio_token,
    ]
    return ".".join(part for part in parts if part) + "-WiKi"


def display_release_name(release_name: str) -> str:
    value = release_name.replace(".", " ")
    value = re.sub(
        r"\b(DTS MA|DDP|DD|AAC|DTS)([257]) ([01])\b",
        lambda match: f"{match.group(1)}{match.group(2)}.{match.group(3)}",
        value,
    )
    value = re.sub(r"\b([257]) ([01])\b", r"\1.\2", value)
    value = re.sub(r"\bDTS MA(?=\d+\.\d+\b)", "DTS.MA", value)
    value = re.sub(r"\b(TrueHD|DTS-HD|DDP|DD) (?=\d+\.\d+\b)", r"\1.", value)
    return value


def pipeline_config_path(root: Path) -> Path:
    return root / PIPELINE_CONFIG_NAME


def split_legacy_config(
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    common = {key: value for key, value in config.items() if key in COMMON_CONFIG_KEYS}
    variant = {
        key: value
        for key, value in config.items()
        if key not in COMMON_CONFIG_KEYS and key not in {"pipeline_version", "variant"}
    }
    return common, variant


def absolutize_config_paths(
    config: dict[str, Any], base: Path, keys: Iterable[str]
) -> None:
    """Keep relative legacy paths valid after moving their config to the BD root."""
    for key in keys:
        value = str(config.get(key, "")).strip()
        if value and not Path(value).is_absolute():
            config[key] = str((base / value).resolve())


def read_or_migrate_pipeline_config(root: Path) -> tuple[Path, dict[str, Any], bool]:
    path = pipeline_config_path(root)
    if path.is_file():
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(document.get("common"), dict) or not isinstance(
            document.get("variants"), dict
        ):
            raise ValueError(f"Invalid unified pipeline config: {path}")
        return path, document, False

    document: dict[str, Any] = {"pipeline_version": 2, "common": {}, "variants": {}}
    migrated = False
    for variant, spec in VARIANTS.items():
        configs = sorted(
            path
            for path in (root / spec["artifact"]).glob(f"*{CONFIG_SUFFIX}")
            if is_visible_file(path)
        )
        if len(configs) > 1:
            raise ValueError(
                f"Cannot migrate {variant}: expected at most one *{CONFIG_SUFFIX} "
                f"in {root / spec['artifact']}; found {len(configs)}"
            )
        if not configs:
            continue
        legacy = json.loads(configs[0].read_text(encoding="utf-8-sig"))
        common, variant_config = split_legacy_config(legacy)
        absolutize_config_paths(common, configs[0].parent, ("movie_description_file",))
        absolutize_config_paths(
            variant_config,
            configs[0].parent,
            (
                "file_path",
                "screenshots_dir",
                "encoder_info_file",
                "output",
                "torrent_output",
            ),
        )
        for key, value in common.items():
            document["common"].setdefault(key, value)
        document["variants"][variant] = variant_config
        migrated = True
    return path, document, migrated


def prepare(
    root: Path,
    imdb_id: str,
    douban_id: str,
    selected: list[str],
    force: bool,
    requested_title: str = "",
) -> None:
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Input directory not found: {root}")
    source = source_mkv(root)
    movie = movie_for(imdb_id)
    selected_title = choose_movie_title(movie, requested_title)
    LOGGER.info("Selected IMDb movie name: %s", selected_title)
    audio_token = audio_release_token(source)
    for name in ("Ripped", "Logs"):
        (root / name).mkdir(exist_ok=True)

    path, document, migrated = read_or_migrate_pipeline_config(root)
    common = document["common"]
    load_env(Path.cwd() / ".env")
    load_env(root / ".env")
    with CONSOLE.status("Discovering Chinese movie names", spinner="dots"):
        discovered_chinese_name = discover_chinese_name(imdb_id, movie, common)
    if discovered_chinese_name:
        LOGGER.info("Discovered Chinese movie names: %s", discovered_chinese_name)
    else:
        LOGGER.warning("No Chinese movie names found through IMDb or TMDB")

    common_defaults = {
        "movie_name": selected_title,
        "chinese_name": discovered_chinese_name,
        "extra_description": "",
        "imdb_id": imdb_id,
        "douban_id": str(douban_id or ""),
        "encoder": "WiKi",
        "source": source_description(root),
        "movie_description_file": str(root / "movie_description.txt"),
        "description_api_base": DESCRIPTION_API_DEFAULT,
        "description_api_key_env": "PT_GEN_API_KEY",
        "tmdb_api_base": TMDB_API_DEFAULT,
        "tmdb_api_token_env": "TMDB_API_TOKEN",
        "tmdb_api_key_env": "TMDB_API_KEY",
        "screenshot_url_prefix_env": "TTG_SCREENSHOT_URL_PREFIX",
        "thumbnail_suffix": "_thumb",
        "nfo_encoding": "cp437",
        "torrent": {
            "tracker": "",
            "source": "",
            "piece_size": None,
            "verify": True,
        },
    }
    for key, value in common_defaults.items():
        if (force and key != "torrent") or key not in common:
            common[key] = value
    if discovered_chinese_name and not str(common.get("chinese_name", "")).strip():
        common["chinese_name"] = discovered_chinese_name
    # The explicitly selected IMDb name belongs to this preparation run.
    common["movie_name"] = selected_title
    document["pipeline_version"] = 2

    with CONSOLE.status(
        status_text("Preparing releases", "starting"), spinner="dots"
    ) as status:
        for index, variant in enumerate(selected, 1):
            update_status(
                status,
                "Preparing releases",
                "creating directories",
                index,
                len(selected),
                total_steps=len(selected),
            )
            artifact = root / VARIANTS[variant]["artifact"]
            screenshots = artifact / "Screenshots"
            for directory in (screenshots / "Comparison", screenshots / "More"):
                directory.mkdir(parents=True, exist_ok=True)
            release_name = make_release_name(
                movie, variant, audio_token, selected_title
            )
            bt_dir = artifact / release_name
            bt_dir.mkdir(exist_ok=True)
            variant_config = {
                "release_name": release_name,
                "english_name": display_release_name(release_name),
                "file_path": str(bt_dir / f"{release_name}.mkv"),
                "screenshots_dir": str(screenshots),
                "handbrake_log": "",
                "encoder_info_file": str(artifact / "encoder_info.txt"),
                "output": str(artifact / f"{release_name}.bbcode.txt"),
                "torrent_output": str(artifact / f"{release_name}.torrent"),
            }
            if variant in document["variants"] and not force:
                document["variants"][variant].setdefault(
                    "torrent_output", variant_config["torrent_output"]
                )
                LOGGER.info("Keeping existing %s settings in %s", variant, path)
            else:
                document["variants"][variant] = variant_config
            LOGGER.info("Prepared %s directory: %s", variant, bt_dir)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    action = "Migrated legacy configs to" if migrated else "Wrote"
    LOGGER.info("%s %s", action, path)
    CONSOLE.print("Configuration ready:", path)


def load_variant_config(
    root: Path, variant: str
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    path = pipeline_config_path(root)
    if path.is_file():
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        common = document.get("common")
        variants_config = document.get("variants")
        if not isinstance(common, dict) or not isinstance(variants_config, dict):
            raise ValueError(f"Invalid unified pipeline config: {path}")
        variant_config = variants_config.get(variant)
        if not isinstance(variant_config, dict):
            raise ValueError(
                f"No {variant} object in {path}; run prepare --variant {variant}"
            )
        return path, common | variant_config, document, variant_config

    # Compatibility for projects which have not run prepare since schema v2.
    artifact = root / VARIANTS[variant]["artifact"]
    configs = sorted(
        path for path in artifact.glob(f"*{CONFIG_SUFFIX}") if is_visible_file(path)
    )
    if len(configs) != 1:
        raise ValueError(
            f"No {PIPELINE_CONFIG_NAME}; expected exactly one legacy *{CONFIG_SUFFIX} "
            f"in {artifact}, found {len(configs)}. Run prepare to migrate."
        )
    legacy = json.loads(configs[0].read_text(encoding="utf-8-sig"))
    return configs[0], legacy, legacy, legacy


def last_log_for(root: Path, variant: str, configured: str) -> Path:
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = root / path
        if not is_visible_file(path):
            raise FileNotFoundError(f"HandBrake log not found: {path}")
        return path
    candidates = [
        path
        for path in (root / "Logs").glob(f"*{variant}*_encode_*.txt")
        if is_visible_file(path)
    ]
    if not candidates:
        candidates = [
            path
            for path in (root / "Logs").glob(f"*{variant}*.txt")
            if is_visible_file(path)
        ]
    if not candidates:
        raise FileNotFoundError(f"No {variant} HandBrake log found in {root / 'Logs'}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def resolved_path(value: Any, base: Path) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base / path).resolve()


def last_matching(lines: Iterable[str], pattern: str) -> str | None:
    regex = re.compile(pattern, flags=re.IGNORECASE)
    matches = [line.strip() for line in lines if regex.search(line)]
    return matches[-1] if matches else None


def clean_encoder_line(line: str) -> str:
    return re.sub(r"^.*?(?=(?:x26[45]|\[libx264))", "", line).strip()


def extract_encoder_info(log_path: Path, variant: str) -> str:
    lines = log_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    if variant == "x265":
        patterns = [
            r"x265 \[info\]: Main .* profile, Level-",
            r"x265 \[info\]: frame I:",
            r"x265 \[info\]: frame P:",
            r"x265 \[info\]: frame B:",
            r"x265 \[info\]: Weighted P-Frames:",
            r"x265 \[info\]: Weighted B-Frames:",
            r"x265 \[info\]: consecutive B-frames:",
        ]
    else:
        patterns = [
            r"(?:x264 \[info\]:|\[libx264[^]]*\])\s*profile ",
            r"(?:x264 \[info\]:|\[libx264[^]]*\])\s*frame I:",
            r"(?:x264 \[info\]:|\[libx264[^]]*\])\s*frame P:",
            r"(?:x264 \[info\]:|\[libx264[^]]*\])\s*frame B:",
            r"(?:x264 \[info\]:|\[libx264[^]]*\])\s*consecutive B-frames:",
        ]
    found = [last_matching(lines, pattern) for pattern in patterns]
    result = [clean_encoder_line(line) for line in found if line]
    required = 6 if variant == "x265" else 5
    if len(result) < required:
        raise RuntimeError(
            f"Only found {len(result)} encoder summary lines in {log_path}"
        )
    return "\n".join(result)


def find_description(payload: Any) -> str | None:
    if isinstance(payload, str) and "◎" in payload:
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("format", "description", "data", "result", "content"):
            if key in payload:
                found = find_description(payload[key])
                if found:
                    return found
        for value in payload.values():
            found = find_description(value)
            if found:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = find_description(value)
            if found:
                return found
    return None


def ptgen_description(douban_id: str, base: str, api_key: str = "") -> str:
    movie_url = f"https://movie.douban.com/subject/{douban_id}/"
    endpoint = f"{base.rstrip('/')}?url={quote(movie_url, safe='')}"
    headers = {"Accept": "application/json", "User-Agent": "BDRip-Scripts/1.0"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        with urlopen(Request(endpoint, headers=headers), timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"PT-Gen description request failed: {exc}") from exc
    description = find_description(payload)
    if not description:
        raise RuntimeError(
            "PT-Gen response did not contain a formatted movie description"
        )
    return description


def imdb_description(imdb_id: str, douban_id: str) -> str:
    movie = movie_for(imdb_id)
    people = lambda values: " / ".join(str(person) for person in values)
    plot = movie.plot or (movie.summaries[0] if movie.summaries else "暂无简介")
    rows = [
        f"◎片　　名　{movie.title}",
        f"◎年　　代　{movie.year or ''}",
        f"◎产　　地　{' / '.join(movie.countries)}",
        f"◎类　　别　{' / '.join(movie.genres)}",
        f"◎语　　言　{' / '.join(movie.languages_text or movie.languages)}",
        f"◎上映日期　{movie.release_date or ''}",
        f"◎IMDb评分  {movie.rating or 'N/A'}/10 ({movie.votes or 0:,} votes)",
        f"◎IMDb链接  {movie.url}",
    ]
    if douban_id:
        rows.append(f"◎豆瓣链接　https://movie.douban.com/subject/{douban_id}/")
    if movie.duration:
        rows.append(f"◎片　　长　{movie.duration}分钟")
    if movie.directors:
        rows.append(f"◎导　　演　{people(movie.directors)}")
    if movie.stars:
        rows.append(f"◎主　　演　{people(movie.stars)}")
    rows.extend(("", "◎简　　介", "", f"　　{plot}"))
    return "\n".join(rows)


def generate_description(config: dict[str, Any]) -> str:
    douban_id = str(config.get("douban_id", "")).strip()
    if douban_id:
        try:
            key = os.environ.get(
                str(config.get("description_api_key_env", "PT_GEN_API_KEY")), ""
            )
            return ptgen_description(
                douban_id,
                str(config.get("description_api_base") or DESCRIPTION_API_DEFAULT),
                key,
            )
        except RuntimeError as exc:
            existing_path = Path(str(config.get("movie_description_file", "")))
            if existing_path.is_file():
                existing = existing_path.read_text(encoding="utf-8-sig").strip()
                if existing:
                    LOGGER.warning("%s; keeping existing description", exc)
                    return existing
            LOGGER.warning("%s; falling back to IMDb description", exc)
    return imdb_description(str(config["imdb_id"]), douban_id)


def write_nfo(config: dict[str, Any], movie_file: Path, bt_dir: Path) -> Path:
    metadata = nfo.get_metadata(str(config["imdb_id"]), movie_file)
    metadata.update(
        {
            "file_name": movie_file.stem,
            "name": str(config.get("movie_name") or metadata["name"]),
            "encoded_by": str(config.get("encoder", "WiKi")),
            "source": str(config["source"]),
        }
    )
    path = bt_dir / f"{movie_file.stem}.nfo"
    path.write_bytes(nfo.render_nfo(metadata, str(config.get("nfo_encoding", "cp437"))))
    return path


def pending_upload_count(config: dict[str, Any], screenshot_root: Path) -> int:
    cache = config.get("screenshot_uploads", {})
    cache = cache if isinstance(cache, dict) else {}
    failures = config.get("failed_screenshot_uploads", {})
    failures = failures if isinstance(failures, dict) else {}
    retry_only = any(isinstance(items, dict) and items for items in failures.values())
    total = 0
    for section in ("Comparison", "More"):
        files = image_files(screenshot_root / section, missing_ok=True)
        section_cache = cache.get(section, {})
        section_cache = section_cache if isinstance(section_cache, dict) else {}
        section_failures = failures.get(section, {})
        section_failures = (
            section_failures if isinstance(section_failures, dict) else {}
        )
        if retry_only:
            total += sum(path.name in section_failures for path in files)
        else:
            total += sum(path.name not in section_cache for path in files)
    return total


def build_variant(
    root: Path,
    variant: str,
    upload: bool,
    shared_description: str | None,
    context: tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
    status_reporter: ProgressStatus | None = None,
    config_lock: Lock | None = None,
) -> str:
    config_file, config, document, variant_config = context or load_variant_config(
        root, variant
    )
    config_base = config_file.parent
    movie_file = resolved_path(config["file_path"], config_base)
    if not movie_file.is_file():
        raise FileNotFoundError(f"Finished MKV not found: {movie_file}")
    bt_dir = movie_file.parent
    LOGGER.info("Building %s from %s", variant, movie_file)
    failures: dict[str, dict[str, str]] = {}
    status_label = f"Building {variant}"
    status_context = (
        nullcontext(status_reporter)
        if status_reporter is not None
        else CONSOLE.status(status_text(status_label, "starting"), spinner="dots")
    )
    with status_context as status:
        assert status is not None
        current_step = 1

        def next_stage(label: str) -> None:
            nonlocal current_step
            current_step = min(7, current_step + 1)
            update_status(status, status_label, label, step=current_step)

        update_status(status, status_label, "reading encoder info", step=current_step)
        encode_log = last_log_for(root, variant, str(config.get("handbrake_log", "")))
        encoder_info = extract_encoder_info(encode_log, variant)
        encoder_path = resolved_path(config["encoder_info_file"], config_base)
        encoder_path.write_text(encoder_info + "\n", encoding="utf-8")
        LOGGER.info("Encoder information: %s", encoder_path)
        next_stage("generating description")

        description_path = resolved_path(config["movie_description_file"], config_base)
        config["movie_description_file"] = str(description_path)
        if shared_description is None:
            description = generate_description(config)
            description_path.write_text(description.rstrip() + "\n", encoding="utf-8")
        else:
            description = shared_description
        LOGGER.info("Movie description: %s", description_path)
        next_stage("processing screenshots")

        load_env(Path.cwd() / ".env")
        load_env(root / ".env")
        screenshot_root = resolved_path(config["screenshots_dir"], config_base)
        uploaded_cache = screenshots.cached_screenshot_urls(config, screenshot_root)
        uploaded = uploaded_cache or None
        if upload:
            token = os.environ.get("TU_TTG_TOKEN", "").strip()
            if not token:
                raise ValueError("TU_TTG_TOKEN is required for screenshot uploads")
            remote_root = screenshots.remote_release_folder(movie_file)
            upload_total = pending_upload_count(variant_config, screenshot_root)
            upload_done = 0

            def upload_event(
                event: str, path: Path, attempt: int, attempts: int, error: str | None
            ) -> None:
                nonlocal upload_done
                detail = f"uploading {path.name}, attempt {attempt}/{attempts}"
                if event in {"success", "failed"}:
                    upload_done += 1
                update_status(
                    status,
                    status_label,
                    detail,
                    upload_done,
                    upload_total,
                    current_step,
                )
                if event == "success":
                    LOGGER.info("Screenshot uploaded: %s", path)
                elif event == "failed":
                    LOGGER.error(
                        "Screenshot skipped after retries: %s: %s", path, error
                    )

            upload_args = (
                variant_config,
                config_file,
                screenshot_root,
                remote_root,
                token,
            )
            if config_lock is None:
                uploaded_cache, failures = screenshots.upload_screenshots_cached(
                    *upload_args,
                    document=document,
                    event_callback=upload_event,
                    verbose=False,
                )
            else:
                # The network work is serialized because both variants persist
                # into one JSON document. The remaining build stages stay parallel.
                with config_lock:
                    uploaded_cache, failures = screenshots.upload_screenshots_cached(
                        *upload_args,
                        document=document,
                        event_callback=upload_event,
                        verbose=False,
                    )
            uploaded = uploaded_cache or {}
        next_stage("rendering BBCode")

        bbcode_output = resolved_path(config["output"], config_base)
        bbcode_output.write_text(
            bbcode.render(config, config_file.parent, uploaded),
            encoding="utf-8",
            newline="\n",
        )
        LOGGER.info("BBCode: %s", bbcode_output)
        next_stage("rendering NFO")

        nfo_path = write_nfo(config, movie_file, bt_dir)
        LOGGER.info("NFO: %s", nfo_path)
        next_stage("calculating MD5")

        md5_path = bt_dir / f"{movie_file.stem}.md5"
        md5_path.write_text(
            f"{md5_file(movie_file)} {movie_file.name}\n", encoding="ascii"
        )
        LOGGER.info("MD5: %s", md5_path)
        next_stage("creating torrent")

        torrent_output = resolved_path(
            config.get("torrent_output")
            or bt_dir.parent / f"{movie_file.stem}.torrent",
            config_base,
        )
        info_hash = create_private_torrent(
            config,
            bt_dir,
            torrent_output,
            status=status,
            status_label=status_label,
        )
        update_status(status, status_label, "complete", step=7)

    LOGGER.info("Completed %s (info hash %s)", variant, info_hash)
    CONSOLE.print(f"[bold green]{variant} complete[/] - info hash: {info_hash}")
    if failures:
        failed_names = [
            f"{section}/{filename}"
            for section, items in failures.items()
            for filename in items
        ]
        LOGGER.error(
            "Failed screenshot uploads for %s: %s", variant, ", ".join(failed_names)
        )
        CONSOLE.print(
            f"[bold yellow]{variant}: {len(failed_names)} screenshot upload(s) "
            f"skipped; see {LOG_FILE_NAME}.[/]"
        )
        for name in failed_names:
            CONSOLE.print("  [bold red]FAILED SCREENSHOT:[/]", name)
    return description


def load_build_contexts(
    root: Path, selected: list[str]
) -> dict[str, tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]]:
    path = pipeline_config_path(root)
    if not path.is_file():
        return {variant: load_variant_config(root, variant) for variant in selected}
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    common = document.get("common")
    variants_config = document.get("variants")
    if not isinstance(common, dict) or not isinstance(variants_config, dict):
        raise ValueError(f"Invalid unified pipeline config: {path}")
    contexts = {}
    for variant in selected:
        variant_config = variants_config.get(variant)
        if not isinstance(variant_config, dict):
            raise ValueError(
                f"No {variant} object in {path}; run prepare --variant {variant}"
            )
        contexts[variant] = (path, common | variant_config, document, variant_config)
    return contexts


def build(root: Path, selected: list[str], upload: bool) -> None:
    root = root.resolve()
    contexts = load_build_contexts(root, selected)
    first_config_file, first_config, _, _ = contexts[selected[0]]
    load_env(Path.cwd() / ".env")
    load_env(root / ".env")
    description_path = resolved_path(
        first_config["movie_description_file"], first_config_file.parent
    )
    first_config["movie_description_file"] = str(description_path)
    with CONSOLE.status(
        status_text("Preparing shared metadata", "generating description"),
        spinner="dots",
    ):
        description = generate_description(first_config)
        description_paths: set[Path] = set()
        for config_file, config, _, _ in contexts.values():
            path = resolved_path(config["movie_description_file"], config_file.parent)
            config["movie_description_file"] = str(path)
            description_paths.add(path)
        for path in description_paths:
            path.write_text(description.rstrip() + "\n", encoding="utf-8")
    LOGGER.info("Shared movie description: %s", description_path)

    config_lock = Lock()
    progress = Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("{task.description}"),
        console=CONSOLE,
        transient=True,
    )
    with progress:
        reporters = {
            variant: ProgressStatus(
                progress,
                progress.add_task(f"Building {variant} - queued", total=None),
            )
            for variant in selected
        }
        with ThreadPoolExecutor(
            max_workers=len(selected), thread_name_prefix="release"
        ) as executor:
            futures = {
                executor.submit(
                    build_variant,
                    root,
                    variant,
                    upload,
                    description,
                    contexts[variant],
                    reporters[variant],
                    config_lock,
                ): variant
                for variant in selected
            }
            errors: list[tuple[str, Exception]] = []
            for future in as_completed(futures):
                variant = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append((variant, exc))
                    LOGGER.exception("%s build failed", variant)
            if errors:
                summary = "; ".join(f"{variant}: {error}" for variant, error in errors)
                raise RuntimeError(f"Parallel build failed: {summary}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser(
        "prepare", help="create release directory structures/configs"
    )
    prepare_parser.add_argument("root", type=Path, help="Blu-ray source directory")
    prepare_parser.add_argument("--imdb-id", required=True)
    prepare_parser.add_argument("--douban-id", default="")
    prepare_parser.add_argument(
        "--title",
        default="",
        help="select an IMDb movie name without an interactive prompt",
    )
    prepare_parser.add_argument(
        "--variant", choices=("all", "x264", "x265"), default="all"
    )
    prepare_parser.add_argument(
        "--force", action="store_true", help="overwrite existing generated configs"
    )

    build_parser = subparsers.add_parser(
        "build", help="generate final release artifacts"
    )
    build_parser.add_argument("root", type=Path, help="Blu-ray source directory")
    build_parser.add_argument(
        "--variant", choices=("all", "x264", "x265"), default="all"
    )
    build_parser.add_argument(
        "--upload", action="store_true", help="upload screenshots to TTG"
    )
    args = parser.parse_args(argv)
    log_path = setup_logging(args.root.resolve())
    LOGGER.info("Command: %s; variants: %s", args.command, args.variant)
    try:
        if args.command == "prepare":
            prepare(
                args.root,
                args.imdb_id,
                args.douban_id,
                variants(args.variant),
                args.force,
                args.title,
            )
        else:
            build(args.root, variants(args.variant), args.upload)
    except (OSError, ValueError, RuntimeError) as exc:
        LOGGER.exception("Pipeline failed")
        CONSOLE.print("[bold red]Error:[/]", str(exc))
        CONSOLE.print("Details:", log_path)
        return 1
    LOGGER.info("Pipeline completed successfully")
    CONSOLE.print("Log:", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
