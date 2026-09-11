# PyAV video utilities

`bdrip rip` and `bdrip crop` use PyAV for inspection, decoding, filtering,
encoding, and muxing. No FFmpeg/FFprobe executable is needed.

- `video/probe.py`: stream inspection and timing.
- `video/crop.py`: shared luminance-bound detection and exact crop filtering.
- `video/cropdetect.py`: crop inspection command.
- `video/two_pass.py`: full-file workflow, progress, logs, and command arguments.
- `video/transcode.py`: isolated Python worker for a native encoder pass.

## Commands

Inspect the first minute, or choose another interval:

```sh
uv run bdrip crop movie.mkv
uv run bdrip crop movie.mkv --start 00:10:00 --end 00:11:00
```

Detection inspects the selected interval with the same PyAV `bbox` detector
used for CRF calibration. Short videos clamp the interval to their end.
Uncertain or absent content bounds retain the full frame. Dimensions and
offsets are exact; incompatible odd dimensions produce an encoding error
rather than being rounded.

Run two-pass encoding with bitrates in kbps:

```sh
uv run bdrip rip movie.mkv output --x264_bitrate 8000 --x265_bitrate 5000
uv run bdrip rip movie.mkv output --x264_bitrate 8000 --crop_filter crop=1920:804:0:137
```

Both passes use the ripper's existing `veryslow` preset and include video only.
x264 finishes both passes before x265 starts. These settings are separate from
the CRF calibration configuration. The source pixel format is preserved when
supported by the encoder; otherwise it uses `yuv420p`. Presentation timestamps,
sample aspect ratio, and color properties are carried through the encode.

Native x264/x265 rate control writes statistics in pass 1 and reads them in
pass 2. The second pass requires a complete statistics file. Temporary stats
are isolated per encode and cleaned up afterward; Python worker processes
keep native diagnostics and failures isolated from the caller. Console progress
updates during each pass and waits for buffered frames to finish.

Output filenames contain the source stem, codec, and bitrate, such as
`movie.x264.8000k.mkv`. Native logs and job settings are saved in
`output/logs/movie.x264.8000k.encode.log`, appended on later runs. Outputs are
written to temporary sibling files and replace the final file only after both
passes complete. Failure or cancellation retains earlier complete movies and
logs, stops the active worker, and removes its temporary files.

## Reusing the functions

```python
from pathlib import Path

from bdrip.video.cropdetect import detect_crop
from bdrip.video.two_pass import encode_two_pass

source = Path("movie.mkv")
crop = detect_crop(source, start=600, end=660)
outputs = encode_two_pass(
    source, Path("output"), x264_bitrate=8000, crop_filter=crop,
)
```

Functions return crop text or output paths and raise exceptions for errors.
The command entry points turn failures into a message and a nonzero exit
status. Ctrl+C returns exit code 130 after worker cleanup.

See [command migration](commands.md) for replacements for the removed scripts.
