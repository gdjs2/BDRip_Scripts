from pathlib import Path
from PIL import Image

# 支持的图片格式
IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
}

# 缩略图宽度
THUMB_WIDTH = 300


def make_thumbnail(image_path: Path):
    with Image.open(image_path) as img:
        width, height = img.size

        if width <= THUMB_WIDTH:
            print(f"Skip (too small): {image_path.name}")
            return

        new_height = round(height * THUMB_WIDTH / width)

        thumb = img.resize(
            (THUMB_WIDTH, new_height),
            Image.Resampling.LANCZOS,
        )

        output = image_path.with_name(
            image_path.stem + "_thumb.png"
        )

        thumb.save(output, optimize=True)

        print(f"{image_path.name} -> {output.name} ({THUMB_WIDTH}x{new_height})")


def main():
    folder = Path(input("Folder: ").strip()).expanduser()

    if not folder.is_dir():
        print("Folder does not exist.")
        return

    for file in sorted(folder.iterdir()):
        if file.suffix.lower() in IMAGE_EXTS:
            if file.stem.endswith("_thumb"):
                continue
            make_thumbnail(file)


if __name__ == "__main__":
    main()