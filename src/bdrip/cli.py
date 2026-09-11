"""One command dispatcher; feature dependencies are loaded only when selected."""

from __future__ import annotations

import argparse
import sys
from importlib import import_module

COMMANDS = {
    "crf": (
        "bdrip.crf.calibration",
        "Calibrate B-frame QP and bitrate at CRFs 13 and 20",
    ),
    "gui": ("bdrip.crf.gui.app", "Open the video task queue and interactive plots"),
    "release": ("bdrip.release.pipeline", "Prepare or build Blu-ray release artifacts"),
    "bbcode": ("bdrip.release.bbcode", "Generate a BBCode release post"),
    "nfo": ("bdrip.release.nfo", "Generate an NFO release document"),
    "nfo-banner": ("bdrip.release.banner", "Extract the artwork from an NFO template"),
    "torrent-check": (
        "bdrip.release.torrent",
        "Verify torrent pieces against local files",
    ),
    "thumbnails": ("bdrip.images.thumbnails", "Generate screenshot thumbnails"),
    "subtitles": (
        "bdrip.subtitles.cli",
        "Convert PGS/SUP subtitles to SRT with PaddleOCR",
    ),
    "rip": ("bdrip.video.two_pass", "Run a full-file two-pass PyAV encode"),
    "crop": ("bdrip.video.cropdetect", "Inspect exact video crop bounds with PyAV"),
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="bdrip",
        description="Video calibration and Blu-ray release tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Commands:\n"
        + "\n".join(
            f"  {name:15} {description}" for name, (_, description) in COMMANDS.items()
        )
        + "\n\nRun bdrip COMMAND --help for that command's options.",
    )
    if not args or args[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    if args[0] not in COMMANDS:
        parser.error(f"Unknown command: {args[0]}")
    module_name, _ = COMMANDS[args[0]]
    return import_module(module_name).main(args[1:]) or 0
