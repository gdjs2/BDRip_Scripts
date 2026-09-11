"""Create screenshot thumbnails while preserving the originals."""

import argparse
from pathlib import Path

from PIL import Image

from .files import image_files

THUMB_WIDTH = 300


def make_thumbnail(image_path: Path, width: int = THUMB_WIDTH) -> Path | None:
    with Image.open(image_path) as image:
        if image.width <= width:
            print(f"Skip (too small): {image_path.name}")
            return None
        height = max(1, round(image.height * width / image.width))
        thumbnail = image.resize((width, height), Image.Resampling.LANCZOS)
        output = image_path.with_name(image_path.stem + "_thumb.png")
        thumbnail.save(output, optimize=True)
    print(f"{image_path.name} -> {output.name} ({width}x{height})")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folder", nargs="?", type=Path, help="Screenshot folder; prompt if omitted"
    )
    args = parser.parse_args(argv)
    folder = (args.folder or Path(input("Folder: ").strip())).expanduser()
    if not folder.is_dir():
        parser.error(f"Folder does not exist: {folder}")
    for image in image_files(folder):
        if not image.stem.endswith("_thumb"):
            make_thumbnail(image)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
