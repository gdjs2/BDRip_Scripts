import os
import sys
import glob
import ffmpeg
import argparse

from loguru import logger
from pathlib import Path

def run_ffmpeg(stream, description: str, emit_output: bool = False):
    logger.info(f"[ffmpeg] {description}")
    try:
        _, err = stream.run(capture_stdout=True, capture_stderr=True)
        if emit_output and err:
            logger.info(f"[ffmpeg] Captured output for: {description}")
            lines = err.decode('utf-8', errors="ignore").splitlines()
            for line in lines[-10:]:
                logger.info(f"\t{line.strip()}")
            logger.success(f"[ffmpeg] Completed: {description}")
    except ffmpeg.Error as e:
        logger.error(f"[ffmpeg] Error occurred while running '{description}'.")
        if e.stderr:
            logger.error(f"[ffmpeg] stderr:\n{e.stderr.decode('utf-8', errors='ignore')}")
        sys.exit(1)


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

            run_ffmpeg(pass1_stream, f"2-pass encode pass 1 ({encoder_lib}, {video_bitrate})")

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