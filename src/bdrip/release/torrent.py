"""Private torrent generation, verification, and release checksums."""

from __future__ import annotations

import argparse
import hashlib
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from rich.status import Status

from .progress import CONSOLE, status_text, update_status

LOGGER = logging.getLogger(__name__)


def verify_torrent(torrent_path: Path, data_root: Path) -> bool:
    """Use torf's piece verifier for both single-file and multi-file torrents."""
    from torf import Torrent

    torrent = Torrent.read(torrent_path, validate=True)
    data_root = data_root.expanduser().resolve()
    if torrent.mode == "singlefile" and data_root.is_dir():
        data_root = data_root / torrent.name
    return torrent.verify(data_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify torrent pieces against local files"
    )
    parser.add_argument("torrent", type=Path)
    parser.add_argument(
        "data_root",
        type=Path,
        help="Torrent content directory, or the file for a single-file torrent",
    )
    args = parser.parse_args(argv)
    from torf import TorfError

    try:
        if verify_torrent(args.torrent, args.data_root):
            CONSOLE.print("ALL PIECES MATCH.")
            return 0
        CONSOLE.print("Torrent verification failed: file sizes or piece hashes differ.")
    except (OSError, ValueError, TorfError) as exc:
        CONSOLE.print(f"Torrent verification failed: {exc}", markup=False)
    return 1


HIDDEN_FILE_REGEX = r"(^|[\\/])\.[^\\/]+$"


def md5_file(path: Path, chunk_size: int = 8 << 20) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def torrent_trackers(config: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    settings = config.get("torrent")
    if not isinstance(settings, dict):
        raise ValueError("Set common.torrent in release_pipeline.json")
    raw = settings.get("trackers") or settings.get("tracker")
    if isinstance(raw, str):
        trackers = [raw.strip()] if raw.strip() else []
    elif isinstance(raw, list):
        trackers = [str(value).strip() for value in raw if str(value).strip()]
    else:
        trackers = []
    if not trackers:
        raise ValueError("Set common.torrent.tracker in release_pipeline.json")
    for tracker in trackers:
        parsed = urlsplit(tracker)
        if (
            parsed.scheme.casefold() not in {"http", "https", "udp"}
            or not parsed.netloc
        ):
            raise ValueError(
                "Every torrent tracker must be an absolute HTTP(S) or UDP URL"
            )
    return trackers, settings


class TorrentProgress:
    def __init__(
        self, status: Status, status_label: str, detail: str, step: int = 7
    ) -> None:
        self.status = status
        self.status_label = status_label
        self.detail = detail
        self.step = step

    def __call__(self, *args: Any) -> None:
        completed, total = int(args[2]), int(args[3])
        update_status(
            self.status,
            self.status_label,
            self.detail,
            completed,
            total,
            self.step,
        )
        error = args[6] if len(args) > 6 else None
        if error:
            LOGGER.error("%s: %s", self.detail, error)

    def close(self) -> None:
        pass


def create_private_torrent(
    config: dict[str, Any],
    bt_dir: Path,
    output: Path,
    status: Status | None = None,
    status_label: str = "Creating torrent",
) -> str:
    """Create a private v1 torrent and independently recheck every piece."""
    from torf import Torrent

    trackers, settings = torrent_trackers(config)
    bt_dir = bt_dir.resolve()
    output = output.resolve()
    if output.is_relative_to(bt_dir):
        raise ValueError("torrent_output must be outside the BT distribution directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    piece_size = settings.get("piece_size")
    if piece_size is not None:
        try:
            piece_size = int(piece_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("common.torrent.piece_size must be bytes or null") from exc
    source = str(settings.get("source") or "").strip() or None
    torrent = Torrent(
        path=bt_dir,
        exclude_regexs=(HIDDEN_FILE_REGEX,),
        trackers=trackers,
        private=True,
        source=source,
        piece_size=piece_size,
        created_by="BDRip Scripts / torf",
    )
    owned_status = (
        CONSOLE.status(status_text(status_label, "starting"), spinner="dots")
        if status is None
        else None
    )
    active_status = status or owned_status
    if owned_status is not None:
        owned_status.start()
    try:
        LOGGER.info("Creating private torrent from %s", bt_dir)
        assert active_status is not None
        update_status(active_status, status_label, "hashing torrent", step=7)
        hashing_progress = TorrentProgress(
            active_status, status_label, "hashing torrent"
        )
        try:
            if not torrent.generate(callback=hashing_progress, interval=1):
                raise RuntimeError(f"Torrent hashing was aborted for {bt_dir}")
        finally:
            hashing_progress.close()
        torrent.write(temporary, overwrite=True)
        loaded = Torrent.read(temporary, validate=True)
        if not loaded.private:
            raise RuntimeError("Generated torrent is missing the private flag")
        loaded_trackers = {str(tracker) for tier in loaded.trackers for tracker in tier}
        if not set(trackers).issubset(loaded_trackers):
            raise RuntimeError(
                "Generated torrent does not contain every configured tracker"
            )
        if loaded.infohash != torrent.infohash:
            raise RuntimeError(
                "Torrent info hash changed after writing and reading the file"
            )
        if bool(settings.get("verify", True)):
            update_status(active_status, status_label, "verifying torrent", step=7)
            verify_progress = TorrentProgress(
                active_status, status_label, "verifying torrent"
            )
            try:
                if not loaded.verify(bt_dir, callback=verify_progress, interval=1):
                    raise RuntimeError(
                        f"Torrent piece verification failed for {bt_dir}"
                    )
            finally:
                verify_progress.close()
        temporary.replace(output)
        LOGGER.info("Torrent verified: %s (info hash %s)", output, loaded.infohash)
        return loaded.infohash
    finally:
        if temporary.exists():
            temporary.unlink()
        if owned_status is not None:
            owned_status.stop()


if __name__ == "__main__":
    raise SystemExit(main())
