"""Generate a WiKi-style BBCode release post from JSON configuration."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from bdrip.common.environment import load_env
from bdrip.images.files import screenshot_files as image_files

from .media import imdb_metadata, media_metadata
from .screenshots import (
    cached_screenshot_urls,
    remote_release_folder,
    upload_screenshots_cached,
)

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())


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


def image_url(
    path: Path, base_url: str, thumb_suffix: str = "_thumb"
) -> tuple[str, str]:
    stem = quote(path.stem, safe="")
    suffix = quote(path.suffix, safe=".")
    base = base_url.rstrip("/")
    return f"{base}/{stem}{suffix}", f"{base}/{stem}{thumb_suffix}{suffix}"


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
        prefix = (
            prefix
            or str(
                config.get("screenshot_url_base")
                or config.get("temporary_url_base")
                or ""
            ).strip()
        )
        complete_base = bool(prefix)
    value = prefix
    if not value:
        raise ValueError(
            "Set the environment variable named by screenshot_url_prefix_env "
            "when not using --upload"
        )
    if any(character in value for character in "\r\n[]"):
        raise ValueError(
            "screenshot_url_base contains unsafe BBCode/control characters"
        )
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ValueError("screenshot_url_base must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("screenshot_url_base must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "screenshot URL prefix must not contain a query string or fragment"
        )
    base = value.rstrip("/")
    if complete_base:
        return base
    movie_stem = remote_release_folder(str(required(config, "file_path")))
    return f"{base}/{quote(movie_stem, safe='-_')}"


def report_failed_uploads(failures: dict[str, dict[str, str]]) -> None:
    if not failures:
        return
    print("\n*** FAILED SCREENSHOT UPLOADS (will retry next run) ***", file=sys.stderr)
    for section, items in failures.items():
        for filename, error in items.items():
            print(f"  [{section}] {filename}: {error}", file=sys.stderr)
    print("****************************************************", file=sys.stderr)


def screenshot_block(
    files: list[Path],
    base_url: str,
    thumb_suffix: str,
    columns: int = 2,
    uploaded: dict[Path, tuple[str, str]] | None = None,
) -> str:
    items = []
    for path in files:
        if uploaded is not None and path not in uploaded:
            continue
        full, thumb = (
            uploaded[path]
            if uploaded is not None
            else image_url(path, base_url, thumb_suffix)
        )
        items.append(f"[URL={full}][IMG]{thumb}[/IMG][/URL]")
    return "\n".join(
        " ".join(items[i : i + columns]) for i in range(0, len(items), columns)
    )


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
        r"\bBluRay\b",
        "[color=#054ed3]BluRay[/color]",
        release_details,
        flags=re.IGNORECASE,
    )
    return (
        f"[b][size=3][color=#ff0000]{movie_title}[/color]{release_details} [/size][/b]"
    )


def encoder_info_heading(video_codec: Any) -> str:
    match = re.search(
        r"(?<![A-Za-z0-9])(x26[45])(?=$|[^A-Za-z0-9])",
        str(video_codec),
        flags=re.IGNORECASE,
    )
    return f".{match.group(1).lower()}.Info" if match else ".Encoder.Info"


def render(
    config: dict[str, Any],
    config_dir: Path,
    uploaded: dict[str, dict[Path, tuple[str, str]]] | None = None,
) -> str:
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
        raise FileNotFoundError(
            f"Screenshot root directory not found: {screenshot_root}"
        )
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
        info_line(
            f"AUDiO CODEC {i}" if len(media["audios"]) > 1 else "AUDiO CODEC", value
        )
        for i, value in enumerate(media["audios"], 1)
    )
    encoder_heading = str(
        config.get("encoder_heading") or encoder_info_heading(media["video_codec"])
    )
    comparison_section = ""
    if comparisons:
        comparison_images = screenshot_block(
            comparisons,
            url_base + "/Comparison",
            thumb_suffix,
            uploaded=uploaded.get("Comparison") if uploaded else None,
        )
        comparison_section = f"""[color=red][u][b].Comparisons[/b][/u][/color]
[b][color=#054ED3]Source[/color][/b]                                                                       [b][color=#054ED3]{encoder}[/color][/b]
{comparison_images}

"""
    more_section = ""
    if more:
        more_images = screenshot_block(
            more,
            url_base + "/More",
            thumb_suffix,
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

{info_line("TAGLiNE", f"{imdb['name']} ({imdb['year']})")}
{info_line("GENRE", imdb["genre"])}
{info_line("iMDb RATiNG", imdb["rating"])}
{info_line("iMDb LiNK", imdb["imdb_url"])}

[u][b].Release.Info[/b][/u]
{info_line("ENCODER", encoder)}
{info_line("RELEASE DATE", release_date)}
{info_line("RELEASE SiZE", media["file_size"])}
{info_line("SOURCE", source)}

[u][b].Media.Info[/b][/u]
{info_line("RUNTiME", media["duration"])}
{info_line("ViDEO CODEC", media["video_codec"])}
{info_line("RESOLUTiON", media["resolution"])}
{info_line("DiSPLAY ASPECT RATiO", media["aspect_ratio"])}
{info_line("FRAME RATE", media["frame_rate"])}
{audio}
{info_line("SUBTiTLES", media["subtitles"])}
{info_line("CHAPTERS", media["chapters"])}

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
        "output": "Golden.Boy.2025.1080p.BluRay.x265.10bit-WiKi.bbcode.txt",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        default="bbcode_config.json",
        help="UTF-8 JSON config (default: bbcode_config.json)",
    )
    parser.add_argument("-o", "--output", help="override the output path")
    parser.add_argument(
        "--print-example", action="store_true", help="print example JSON and exit"
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="upload screenshots to TTG and embed returned URLs",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="environment file containing TU_TTG_TOKEN (default: .env)",
    )
    args = parser.parse_args(argv)
    if args.print_example:
        print(json.dumps(example_config(), ensure_ascii=False, indent=2))
        return 0

    config_path = Path(args.config).resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        load_env(Path(args.env_file).resolve())
        screenshot_root = (
            config_path.parent / required(config, "screenshots_dir")
        ).resolve()
        uploaded_cache = cached_screenshot_urls(config, screenshot_root)
        uploaded = uploaded_cache or None
        failures: dict[str, dict[str, str]] = {}
        if args.upload:
            token = os.environ.get("TU_TTG_TOKEN", "").strip()
            if not token:
                raise ValueError("TU_TTG_TOKEN is not set in the environment or .env")
            movie = (config_path.parent / required(config, "file_path")).resolve()
            default_folder = remote_release_folder(movie)
            root_folder = str(config.get("upload_folder") or default_folder).strip(
                "/\\"
            )
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
        output.write_text(
            render(config, config_path.parent, uploaded), encoding="utf-8", newline="\n"
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {output}")
    report_failed_uploads(failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
