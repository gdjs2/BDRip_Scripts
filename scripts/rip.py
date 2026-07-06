import os
import re
import sys
import glob
import ffmpeg
import argparse

from tqdm import tqdm
from loguru import logger
from pathlib import Path

def get_video_duration(file_path: Path) -> float:
    try:
        probe = ffmpeg.probe(str(file_path))
        duration = float(probe['format']['duration'])
        return duration
    except ffmpeg.Error as e:
        logger.error(f"Error probing video file '{file_path}': {e.stderr.decode('utf-8', errors='ignore')}")
        sys.exit(1)
    except KeyError:
        logger.error(f"Could not retrieve duration for video file '{file_path}'.")
        sys.exit(1)

def parse_time_to_seconds(time_str: str) -> float:
    h, m, s = time_str.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)

def run_ffmpeg(stream, description: str, total_duration: float, emit_output: bool = False):
    logger.info(f"[ffmpeg] {description}")

    process = stream.run_async(capture_stdout=True, capture_stderr=True)

    time_pattern = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})")

    last_time = 0
    last_lines = []

    with tqdm(total=total_duration, unit="s", desc=description.split()[0], bar_format="{l_bar}{bar}| {n_fmt:.1f}s/{total_fmt:.1f}s [{elapsed}<{remaining}]") as pbar:
        for line in process.strerr:
            line_str = line.decode('utf-8', errors='ignore').strip()
            last_lines.append(line_str)
            if len(last_lines) > 10:
                last_lines.pop(0)
            match = time_pattern.search(line_str)
            if match:
                current_time = parse_time_to_seconds(match.group(1))
                increment = current_time - last_time
                if increment > 0:
                    pbar.update(increment)
                    last_time = current_time
    
    process.wait()
    if process.returncode != 0:
        logger.error(f"[ffmpeg] Process exited with code {process.returncode} for '{description}'.")
        logger.error("[ffmpeg] Last 10 lines of stderr:")
        for line in last_lines:
            logger.error(f"\t{line}")
        sys.exit(1)
    else:
        if emit_output:
            logger.info(f"[ffmpeg] Captured output for: {description}")
            for line in last_lines:
                logger.info(f"\t{line.strip()}")
        logger.success(f"[ffmpeg] Completed: {description}")


def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="2Pass ripper for video files.")
    parser.add_argument("input_file", type=str, help="Path to the input video file.")
    parser.add_argument("dest_dir", type=str, help="Destination directory for the output files.")
    parser.add_argument("bitrate", type=int, help="Target bitrate for the output video.")
    parser.add_argument("crop_filter", type=str, help="ffmpeg crop filter to apply (e.g., 'crop=1280:720:0:0').")
    args = parser.parse_args()

    if args.bitrate <= 0:
        logger.error("Bitrate must be a positive integer.")
        sys.exit(1)

    input_file_path = Path(args.input_file)
    dest_dir_path = Path(args.dest_dir)
    video_bitrate = f"{args.bitrate}k" 
    crop_filter = args.crop_filter

    # Validations
    if not input_file_path.is_file():
        logger.error(f"Input file '{input_file_path}' does not exist.")
        sys.exit(1)
    if dest_dir_path.exists():
        if not dest_dir_path.is_dir():
            logger.error(f"Destination must be a directory, but a file exists at '{dest_dir_path}'.")
            sys.exit(1)
    else:
        try:
            dest_dir_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create destination directory '{dest_dir_path}': {e}")
            sys.exit(1)
    logs_dir_path = dest_dir_path / "logs"
    if not logs_dir_path.exists():
        try:
            logs_dir_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create logs directory '{logs_dir_path}': {e}")
            sys.exit(1)
    
    absolute_input_path = input_file_path.resolve()
    absolute_dest_dir = dest_dir_path.resolve()
    absolute_logs_dir = logs_dir_path.resolve()

    encoder_config = [
        {"name": "x264", "library": "libx264"}, 
        {"name": "x265", "library": "libx265"},
    ]

    logger.success("Validated rip arguments:")
    logger.info(f"\tSource: {absolute_input_path}")
    logger.info(f"\tDestination: {absolute_dest_dir}")
    logger.info(f"\tBitrate: {video_bitrate}")
    logger.info(f"\tCrop Filter: {crop_filter}")

    # 2-pass encode
    total_duration = get_video_duration(absolute_input_path)

    source_basename = absolute_input_path.stem
    encoded_output_paths = []
    null_dev = "NUL" if os.name == "nt" else "/dev/null"

    for config in encoder_config:
        selected_encoder = config["name"]
        encoder_lib = config["library"]

        encoded_output_path = absolute_dest_dir / f"{source_basename}.{selected_encoder}.{video_bitrate}.mkv"
        pass_log_prefix = absolute_logs_dir / f"{source_basename}.{selected_encoder}.{video_bitrate}.2pass"
        pass_log_prefix_str = pass_log_prefix.as_posix()

        try:
            pass1_kwargs = {
                "map": "0:v:0",
                "c:v": encoder_lib,
                "vf": crop_filter,
                "preset": "veryslow",
                "b:v": video_bitrate,
                "pass": 1,
                "passlogfile": pass_log_prefix_str,
                "an": None,
                "sn": None,
                "dn": None,
                "format": "null"
            }

            pass1_stream = (
                ffmpeg
                .input(str(absolute_input_path))
                .output(null_dev, **pass1_kwargs)
                .overwrite_output()
                .global_args("-hide_banner")
            )

            run_ffmpeg(pass1_stream, f"2-pass encode pass 1 ({encoder_lib}, {video_bitrate})", total_duration)

            pass2_kwargs = {
                "map": "0:v:0",
                "c:v": encoder_lib,
                "vf": crop_filter,
                "preset": "veryslow",
                "b:v": video_bitrate,
                "pass": 2,
                "passlogfile": pass_log_prefix_str,
                "an": None,
                "sn": None,
                "dn": None,
            }

            pass2_stream = (
                ffmpeg
                .input(str(absolute_input_path))
                .output(str(encoded_output_path), **pass2_kwargs)
                .overwrite_output()
                .global_args("-hide_banner")
            )

            run_ffmpeg(pass2_stream, f"2-pass encode pass 2 ({encoder_lib}, {video_bitrate})", emit_output=True)

            encoded_output_paths.append(encoded_output_path)

        finally:
            # Cleanup pass log files
            for log_file in glob.glob(f"{pass_log_prefix_str}*"):
                try:
                    os.remove(log_file)
                except OSError:
                    pass

    for output_path in encoded_output_paths:
        logger.success(f"Encoded output: {output_path}")
    
if __name__ == '__main__':
    main()