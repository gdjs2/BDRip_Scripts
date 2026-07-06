import os
import re
import sys
import ffmpeg
import argparse

from pathlib import Path
from loguru import logger
from collections import Counter

def parse_ffmpeg_time(time_str: str) -> float:
    try:
        if ':' in time_str:
            parts = list(map(int, time_str.split(':')))
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                return parts[0] * 60 + parts[1]
        return float(time_str)
    except ValueError:
        logger.error(f"Invalid time format: {time_str}")
        sys.exit(1)

def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Detect ffmpeg crop filter for video files.")
    parser.add_argument("input_file", help="Path to the input video file.")

    parser.add_argument("--start", "-s", type=str,
                        help="Start time for crop detection (default: 00:00:00). Format: HH:MM:SS or seconds.")
    parser.add_argument("--end", "-e", type=str,
                        help="End time for crop detection (default: 00:01:00). Format: HH:MM:SS or seconds.")
    args = parser.parse_args()

    # Check arguments
    if (args.start is None) != (args.end is None):
        parser.error("--start and --end must be provided together.")
    start_time = args.start if args.start else "00:00:00"
    end_time = args.end if args.end else "00:01:00"

    start_sec = parse_ffmpeg_time(start_time)
    end_sec = parse_ffmpeg_time(end_time)

    if start_sec >= end_sec:
        parser.error("Start time must be less than end time.")

    input_file_path = Path(args.input_file)

    # Check if the input file exists
    if not input_file_path.is_file():
        print(f"Error: The file '{input_file_path}' does not exist.")
        return
    
    # Resolve the absolute path of the input file
    absolute_input_path = input_file_path.resolve()

    logger.success("Validated input stream:")
    logger.info(f"\tSource: {absolute_input_path}")

    # Detect crop filter using ffmpeg
    logger.info(f"[detect cropfilter] Running cropdetect from {start_time} to {end_time}...")

    null_dev = "NUL" if os.name == "nt" else "/dev/null"

    try:
        stream = ffmpeg.input(str(absolute_input_path), ss=start_time, to=end_time) \
            .video.filter("cropdetect") \
            .output(null_dev, format="null", an=None, sn=None, dn=None) \
            .global_args("-hide_banner")
        _, err = stream.run(capture_stdout=True, capture_stderr=True)
    except ffmpeg.Error as e:
        logger.error(f"ffmpeg stderr:\n{e.stderr.decode('utf-8', errors="ignore")}")
        sys.exit(1)

    ffmpeg_output_text = err.decode("utf-8", errors="ignore")
    ffmpeg_output_lines = ffmpeg_output_text.splitlines()

    crop_line_pattern = re.compile(r'x1:(?P<x1>-?\d+)\s+x2:(?P<x2>-?\d+)\s+y1:(?P<y1>-?\d+)\s+y2:(?P<y2>-?\d+)')
    bounds_freq = Counter()

    for line in ffmpeg_output_lines:
        if "Parsed_cropdetect" in line or "time=" in line:
            logger.debug(f"[ffmpeg] {line.strip()}")
        
        match = crop_line_pattern.search(line)
        if not match:
            continue
            
        x1, x2, y1, y2 = (
            int(match.group("x1")),
            int(match.group("x2")),
            int(match.group("y1")),
            int(match.group("y2"))
        )

        bounds_key = (x1, x2, y1, y2)
        bounds_freq[bounds_key] += 1

    logger.info(f"[ffmpeg] Output lines captured: {len(ffmpeg_output_lines)}")

    if not bounds_freq:
        logger.error("No cropdeyect bounds were parsed from ffmpeg output.")
        sys.exit(1)

    selected_bounds, count = bounds_freq.most_common(1)[0]
    x1, x2, y1, y2 = selected_bounds

    exact_width = x2 - x1 + 1
    exact_height = y2 - y1 + 1

    if exact_width <= 0 or exact_height <= 0:
        logger.error(f"Invalid crop bounds detected: x1={x1}, x2={x2}, y1={y1}, y2={y2}")
        sys.exit(1)
    
    final_crop_filter = f"crop={exact_width}:{exact_height}:{x1}:{y1}"

    logger.success(f"[detect cropfilter] Selected bounds count: {count}")
    logger.success(f"[detect cropfilter] Exact bounds: x1={x1}, x2={x2}, y1={y1}, y2={y2}")
    logger.success(f"[detect cropfilter] Final crop filter: {final_crop_filter}")

if __name__ == "__main__":
    main()