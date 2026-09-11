"""Screenshot uploads and the resumable per-release upload cache."""

from __future__ import annotations

import json
import logging
import mimetypes
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bdrip.images.files import screenshot_files as image_files

TTG_API_URL = "https://tu.totheglory.im/api.php"
HTTP_HEADERS = {"User-Agent": "curl/8.12.1", "Accept": "application/json"}
LOGGER = logging.getLogger(__name__)


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
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    safe_name = file_path.name.replace('"', "")
    chunks.extend(
        (
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode(
                "utf-8"
            ),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return b"".join(chunks), boundary


def find_url_pair(payload: Any, filename: str) -> tuple[str, str] | None:
    """Find original/thumbnail URLs in upload or list responses."""
    if isinstance(payload, dict):
        name = str(payload.get("filename") or payload.get("name") or "")
        original = next(
            (
                str(payload[k])
                for k in ("url", "file_url", "original_url", "full_url")
                if payload.get(k)
            ),
            "",
        )
        thumbnail = next(
            (
                str(payload[k])
                for k in ("thumbnail_url", "thumb_url", "thumbnail", "thumb")
                if payload.get(k)
            ),
            "",
        )
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
        f"{TTG_API_URL}?{query}",
        data=body,
        method="POST",
        headers={
            **HTTP_HEADERS,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    payload = api_json(request)
    pair = find_url_pair(payload, path.name)
    if not pair or not pair[0] or not pair[1]:
        uploaded_items = (
            payload.get("uploaded", []) if isinstance(payload, dict) else []
        )
        if len(uploaded_items) == 1 and isinstance(uploaded_items[0], dict):
            returned_name = uploaded_items[0].get("name") or uploaded_items[0].get(
                "filename"
            )
            returned_pair = find_url_pair(uploaded_items[0], str(returned_name or ""))
            if returned_pair and returned_pair[0] and returned_pair[1]:
                if returned_name and returned_name != path.name:
                    LOGGER.warning(
                        "TTG renamed %s to %s despite overwrite=overwrite; using returned URLs",
                        path.name,
                        returned_name,
                    )
                return returned_pair
        raise RuntimeError(
            f"Could not find original/thumbnail URLs after uploading {path.name}: {payload}"
        )
    return pair


def upload_screenshots(
    files: list[Path],
    folder: str,
    token: str,
    attempts: int = 3,
    retry_delay: float = 10,
    event_callback: Any = None,
    verbose: bool = True,
) -> tuple[dict[Path, tuple[str, str]], dict[Path, str]]:
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
                    folder,
                    path.name,
                    attempt,
                    attempts,
                    exc,
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


def cached_screenshot_urls(
    config: dict[str, Any], screenshot_root: Path
) -> dict[str, dict[Path, tuple[str, str]]]:
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
    config: dict[str, Any],
    config_path: Path,
    screenshot_root: Path,
    root_folder: str,
    token: str,
    document: dict[str, Any] | None = None,
    event_callback: Any = None,
    verbose: bool = True,
) -> tuple[dict[str, dict[Path, tuple[str, str]]], dict[str, dict[str, str]]]:
    """Upload missing/previously failed images and persist progress in config."""
    cache = config.setdefault("screenshot_uploads", {})
    if not isinstance(cache, dict):
        cache = config["screenshot_uploads"] = {}
    old_failures = config.get("failed_screenshot_uploads", {})
    if not isinstance(old_failures, dict):
        old_failures = {}
    retry_names = {
        section: set(items)
        for section, items in old_failures.items()
        if isinstance(items, dict)
    }
    retry_only = any(retry_names.values())
    failures: dict[str, dict[str, str]] = {
        section: dict(items)
        for section, items in old_failures.items()
        if isinstance(items, dict)
    }

    def persist() -> None:
        config["failed_screenshot_uploads"] = {
            section: items for section, items in failures.items() if items
        }
        config_path.write_text(
            json.dumps(
                document if document is not None else config,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    for section in ("Comparison", "More"):
        files = image_files(screenshot_root / section, missing_ok=True)
        section_cache = cache.setdefault(section, {})
        if not isinstance(section_cache, dict):
            section_cache = cache[section] = {}
        if retry_only:
            targets = [
                path for path in files if path.name in retry_names.get(section, set())
            ]
        else:
            targets = [path for path in files if path.name not in section_cache]
        for path in targets:
            succeeded, failed = upload_screenshots(
                [path],
                f"{root_folder}/{section}",
                token,
                event_callback=event_callback,
                verbose=verbose,
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


def remote_release_folder(file_path: str | Path) -> str:
    """Convert a media filename to its TTG folder; preserve hyphens."""
    return Path(file_path).stem.replace(".", "_")
