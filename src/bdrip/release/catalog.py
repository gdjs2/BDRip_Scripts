"""IMDb/TMDB title lookup and release title selection."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import unicodedata
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import imdbinfo

from .progress import CONSOLE

LOGGER = logging.getLogger(__name__)

TMDB_API_DEFAULT = "https://api.themoviedb.org/3"


CHINESE_COUNTRY_PRIORITY = {
    "CN": 0,
    "SG": 1,
    "HK": 2,
    "MO": 3,
    "TW": 4,
}


CHINESE_LANGUAGE_CODES = {"zh", "cmn", "yue", "wuu", "nan", "hak"}


HAN_CHARACTER_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]"
)


def movie_for(imdb_id: str) -> Any:
    movie = imdbinfo.get_movie(imdb_id)
    if movie is None:
        raise RuntimeError(f"IMDb returned no metadata for {imdb_id}")
    return movie


def release_title(value: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^A-Za-z0-9]+", ".", ascii_name).strip(".")
    if not cleaned:
        raise ValueError(f"IMDb title cannot form a release name: {value!r}")
    return cleaned


def movie_title_candidates(movie: Any) -> list[str]:
    """Return unique IMDb-provided titles in stable preference order."""
    values = [
        getattr(movie, "title", None),
        getattr(movie, "title_localized", None),
        *(getattr(movie, "title_akas", None) or []),
    ]
    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        title = str(value or "").strip()
        key = title.casefold()
        if title and key not in seen:
            candidates.append(title)
            seen.add(key)
    if not candidates:
        raise ValueError("IMDb returned no usable movie title")
    return candidates


def normalized_chinese_title(value: Any) -> str:
    """Normalize a discovered title without changing simplified/traditional text."""
    title = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", title).strip()


def is_chinese_language(code: Any) -> bool:
    language = str(code or "").strip().casefold().replace("_", "-")
    return language.split("-", 1)[0] in CHINESE_LANGUAGE_CODES


def chinese_title_sort_key(record: tuple[str, str, str, str]) -> tuple[Any, ...]:
    title, country, language, provider = record
    return (
        CHINESE_COUNTRY_PRIORITY.get(country.upper(), len(CHINESE_COUNTRY_PRIORITY)),
        0 if provider == "imdb" else 1,
        language.casefold(),
        title.casefold(),
        title,
    )


def format_chinese_titles(records: Iterable[tuple[str, str, str, str]]) -> str:
    """Return deterministic, de-duplicated titles as ``[title/title]``."""
    titles: list[str] = []
    seen: set[str] = set()
    for raw_title, _country, _language, _provider in sorted(
        records, key=chinese_title_sort_key
    ):
        title = normalized_chinese_title(raw_title)
        key = title.casefold()
        if title and HAN_CHARACTER_RE.search(title) and key not in seen:
            titles.append(title)
            seen.add(key)
    return f"[{'/'.join(titles)}]" if titles else ""


def imdb_chinese_title_records(
    imdb_id: str, movie: Any
) -> list[tuple[str, str, str, str]]:
    """Collect country/language-qualified Chinese AKA titles from IMDb."""
    records: list[tuple[str, str, str, str]] = []
    try:
        result = imdbinfo.get_akas(imdb_id)
        akas = getattr(result, "akas", result if isinstance(result, list) else [])
        for aka in akas:
            if isinstance(aka, dict):
                title = aka.get("title", "")
                country = str(aka.get("country_code", "") or "").upper()
                language = str(aka.get("language_code", "") or "")
            else:
                title = getattr(aka, "title", "")
                country = str(getattr(aka, "country_code", "") or "").upper()
                language = str(getattr(aka, "language_code", "") or "")
            normalized = normalized_chinese_title(title)
            if (
                normalized
                and HAN_CHARACTER_RE.search(normalized)
                and (
                    country in CHINESE_COUNTRY_PRIORITY or is_chinese_language(language)
                )
            ):
                records.append((normalized, country, language, "imdb"))
    except imdbinfo.ImdbinfoError as exc:
        LOGGER.warning("IMDb Chinese AKA lookup failed: %s", exc)

    # IMDb's localized title has no locale metadata in MovieDetail. Retain it
    # only when it is visibly Chinese and let qualified AKA records sort first.
    localized = normalized_chinese_title(getattr(movie, "title_localized", ""))
    if localized and HAN_CHARACTER_RE.search(localized):
        records.append((localized, "", "zh", "imdb"))
    return records


def tmdb_json(
    base: str,
    path: str,
    token: str = "",
    api_key: str = "",
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    query = dict(params or {})
    if api_key:
        query["api_key"] = api_key
    endpoint = f"{base.rstrip('/')}/{path.lstrip('/')}"
    if query:
        endpoint = f"{endpoint}?{urlencode(query)}"
    headers = {"Accept": "application/json", "User-Agent": "BDRip-Scripts/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(endpoint, headers=headers), timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
    except HTTPError as exc:
        raise RuntimeError(f"TMDB request failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"TMDB request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("TMDB response was not a JSON object")
    return payload


def tmdb_chinese_title_records(
    imdb_id: str,
    base: str,
    token: str = "",
    api_key: str = "",
) -> list[tuple[str, str, str, str]]:
    """Collect every Chinese translation and alternative title from TMDB."""
    if not token and not api_key:
        LOGGER.info("TMDB title lookup skipped: no API token or key configured")
        return []

    found = tmdb_json(
        base,
        f"find/{quote(imdb_id, safe='')}",
        token,
        api_key,
        {"external_source": "imdb_id"},
    )
    media: list[tuple[str, int]] = []
    for media_type, result_key in (("movie", "movie_results"), ("tv", "tv_results")):
        for item in found.get(result_key, []) or []:
            if isinstance(item, dict) and item.get("id") is not None:
                identity = (media_type, int(item["id"]))
                if identity not in media:
                    media.append(identity)
    if not media:
        LOGGER.warning("TMDB found no movie or TV entry for IMDb ID %s", imdb_id)
        return []

    records: list[tuple[str, str, str, str]] = []
    for media_type, tmdb_id in media:
        try:
            translations = tmdb_json(
                base, f"{media_type}/{tmdb_id}/translations", token, api_key
            )
        except RuntimeError as exc:
            LOGGER.warning(
                "%s for TMDB %s %s; trying alternative titles",
                exc,
                media_type,
                tmdb_id,
            )
        else:
            for item in translations.get("translations", []) or []:
                if not isinstance(item, dict) or not is_chinese_language(
                    item.get("iso_639_1")
                ):
                    continue
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                title = data.get("title") or data.get("name")
                country = str(item.get("iso_3166_1", "") or "").upper()
                normalized = normalized_chinese_title(title)
                if normalized and HAN_CHARACTER_RE.search(normalized):
                    records.append((normalized, country, "zh", "tmdb"))

        try:
            alternatives = tmdb_json(
                base, f"{media_type}/{tmdb_id}/alternative_titles", token, api_key
            )
        except RuntimeError as exc:
            LOGGER.warning(
                "%s for TMDB %s %s; keeping available translations",
                exc,
                media_type,
                tmdb_id,
            )
        else:
            alternative_items = (
                alternatives.get("titles", [])
                if media_type == "movie"
                else alternatives.get("results", [])
            )
            for item in alternative_items or []:
                if not isinstance(item, dict):
                    continue
                country = str(item.get("iso_3166_1", "") or "").upper()
                title = normalized_chinese_title(item.get("title"))
                if (
                    country in CHINESE_COUNTRY_PRIORITY
                    and title
                    and HAN_CHARACTER_RE.search(title)
                ):
                    records.append((title, country, "zh", "tmdb"))
    return records


def discover_chinese_name(imdb_id: str, movie: Any, config: dict[str, Any]) -> str:
    """Discover all IMDb/TMDB Chinese names and return the BBCode heading form."""
    records = imdb_chinese_title_records(imdb_id, movie)
    token_env = str(config.get("tmdb_api_token_env") or "TMDB_API_TOKEN")
    key_env = str(config.get("tmdb_api_key_env") or "TMDB_API_KEY")
    try:
        records.extend(
            tmdb_chinese_title_records(
                imdb_id,
                str(config.get("tmdb_api_base") or TMDB_API_DEFAULT),
                os.environ.get(token_env, "").strip(),
                os.environ.get(key_env, "").strip(),
            )
        )
    except RuntimeError as exc:
        LOGGER.warning("%s; keeping IMDb Chinese title candidates", exc)
    return format_chinese_titles(records)


def choose_movie_title(movie: Any, requested: str = "", input_fn=input) -> str:
    """Choose an IMDb title interactively when more than one exists."""
    if requested.strip():
        return requested.strip()
    candidates = movie_title_candidates(movie)
    if len(candidates) == 1:
        return candidates[0]
    if not sys.stdin.isatty() and input_fn is input:
        choices = ", ".join(repr(title) for title in candidates)
        raise ValueError(
            f"Multiple IMDb titles found in non-interactive mode: {choices}. "
            "Pass --title to select one."
        )
    CONSOLE.print("Multiple IMDb movie names found:")
    for index, title in enumerate(candidates, 1):
        CONSOLE.print(f"  {index}.", title)
    while True:
        answer = input_fn(
            f"Select movie name [1-{len(candidates)}] (default 1): "
        ).strip()
        if not answer:
            return candidates[0]
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        CONSOLE.print("Invalid selection. Enter one of the displayed numbers.")
