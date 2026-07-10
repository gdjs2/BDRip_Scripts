import sys
import base64
import argparse

from loguru import logger

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Convert NFO banner to base64')
    parser.add_argument('input_file', help='Path to the input NFO file')
    args = parser.parse_args()

    input_file = args.input_file
    try:
        with open(input_file, 'rb') as f:
            content = f.read()
        top_boundary = b"Zootopia 2\r\n"
        bottom_boundary = b"SOURCE...............: NULL\r\n"
        top_split = content.split(top_boundary)
        if len(top_split) > 1:
            top_banner = top_split[0]
        else:
            logger.error("Top boundary not found in the NFO file.")
            sys.exit(1)
        bottom_split = content.split(bottom_boundary)
        if len(bottom_split) > 1:
            bottom_banner = bottom_split[1]
        else:
            logger.error("Bottom boundary not found in the NFO file.")
            sys.exit(1)

        top_banner_b64 = base64.b64encode(top_banner).decode('ascii')
        bottom_banner_b64 = base64.b64encode(bottom_banner).decode('ascii')
        print("Top Banner Base64:", top_banner_b64)
        print("Bottom Banner Base64:", bottom_banner_b64)

    except Exception as e:
        logger.error(f"An error occurred: {e}")
        sys.exit(1)