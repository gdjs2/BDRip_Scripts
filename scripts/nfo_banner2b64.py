import sys
import base64
import argparse
import re

from loguru import logger


def split_template(content: bytes) -> tuple[bytes, bytes]:
    """Extract the bytes surrounding the variable release-information block."""
    newline = b"\r\n" if b"\r\n" in content else b"\n"
    lines = content.splitlines(keepends=True)

    # The release name is the first non-empty indented ASCII line immediately
    # before NAME.................:. Keep its indentation in the top banner so
    # nfo_generator.py can append the configured filename directly to it.
    name_index = next(
        (index for index, line in enumerate(lines)
         if re.search(br"\bNAME\.{5,}:", line)),
        None,
    )
    if name_index is None:
        raise ValueError("NAME metadata line not found")
    title_index = name_index - 1
    while title_index >= 0 and not lines[title_index].strip():
        title_index -= 1
    if title_index < 0:
        raise ValueError("Release-name line not found before NAME")
    title_line = lines[title_index]
    title_text = title_line.rstrip(b"\r\n")
    indentation = title_text[:len(title_text) - len(title_text.lstrip(b" "))]
    top_offset = sum(len(line) for line in lines[:title_index])
    top_banner = content[:top_offset] + indentation

    source_index = next(
        (index for index in range(name_index, len(lines))
         if re.search(br"\bSOURCE\.{5,}:", lines[index])),
        None,
    )
    if source_index is None:
        raise ValueError("SOURCE metadata line not found")
    bottom_offset = sum(len(line) for line in lines[:source_index + 1])
    bottom_banner = content[bottom_offset:]
    if not title_line.endswith((b"\n", b"\r")) or newline not in content:
        raise ValueError("Unsupported template line endings")
    return top_banner, bottom_banner

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Convert NFO banner to base64')
    parser.add_argument('input_file', help='Path to the input NFO file')
    args = parser.parse_args()

    input_file = args.input_file
    try:
        with open(input_file, 'rb') as f:
            content = f.read()
        top_banner, bottom_banner = split_template(content)

        top_banner_b64 = base64.b64encode(top_banner).decode('ascii')
        bottom_banner_b64 = base64.b64encode(bottom_banner).decode('ascii')
        print("Top Banner Base64:", top_banner_b64)
        print("Bottom Banner Base64:", bottom_banner_b64)

    except Exception as e:
        logger.error(f"An error occurred: {e}")
        sys.exit(1)
