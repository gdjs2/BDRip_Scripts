"""Small video/audio fixtures generated locally with PyAV."""

from fractions import Fraction

import av


def make_media(path, *, audio=True, video=True, borders=False, seconds=1):
    """Write moving image and optional silent audio at 12 frames per second."""
    width, height = (128, 96) if borders else (96, 64)
    with av.open(str(path), mode="w") as output:
        video_stream = None
        audio_stream = None
        if video:
            video_stream = output.add_stream("ffv1", rate=12)
            video_stream.width = width
            video_stream.height = height
            video_stream.pix_fmt = "yuv420p"
            video_stream.time_base = Fraction(1, 12)
        if audio:
            audio_stream = output.add_stream("pcm_s16le", rate=12000)
            audio_stream.layout = "mono"
        for index in range(12 * seconds):
            if video_stream is not None:
                frame = av.VideoFrame(width, height, "yuv420p")
                for plane_index, plane in enumerate(frame.planes):
                    if borders:
                        values = (
                            bytearray([16 if plane_index == 0 else 128])
                            * plane.buffer_size
                        )
                        if plane_index == 0:
                            for y in range(16, height - 16):
                                for x in range(16, width - 16):
                                    values[y * plane.line_size + x] = (
                                        64 + (index * 17 + x * 3 + y * 5) % 150
                                    )
                        plane.update(values)
                    else:
                        plane.update(
                            bytes(
                                (index * 17 + offset // 11 + plane_index * 81) % 256
                                for offset in range(plane.buffer_size)
                            )
                        )
                frame.pts = index
                frame.time_base = Fraction(1, 12)
                for packet in video_stream.encode(frame):
                    output.mux(packet)
            if audio_stream is not None:
                sound = av.AudioFrame(format="s16", layout="mono", samples=1000)
                sound.sample_rate = 12000
                sound.pts = index * 1000
                sound.time_base = Fraction(1, 12000)
                sound.planes[0].update(bytes(sound.planes[0].buffer_size))
                for packet in audio_stream.encode(sound):
                    output.mux(packet)
        for stream in (video_stream, audio_stream):
            if stream is not None:
                for packet in stream.encode(None):
                    output.mux(packet)
