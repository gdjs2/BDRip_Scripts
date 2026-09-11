"""Discover visible images in natural filename order."""

from pathlib import Path

from bdrip.common.files import is_visible_file, natural_key

SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
IMAGE_EXTENSIONS = SCREENSHOT_EXTENSIONS | {".tif", ".tiff"}


def image_files(
    directory: Path, *, missing_ok: bool = False, extensions: set[str] | None = None
) -> list[Path]:
    if not directory.is_dir():
        if missing_ok:
            return []
        raise FileNotFoundError(f"Screenshot directory not found: {directory}")
    supported = IMAGE_EXTENSIONS if extensions is None else extensions
    return sorted(
        (
            path
            for path in directory.iterdir()
            if is_visible_file(path) and path.suffix.casefold() in supported
        ),
        key=natural_key,
    )


def screenshot_files(directory: Path, *, missing_ok: bool = False) -> list[Path]:
    return image_files(
        directory, missing_ok=missing_ok, extensions=SCREENSHOT_EXTENSIONS
    )
