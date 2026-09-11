"""Release renderers keep their formats while sharing metadata helpers."""

import base64
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bdrip.release.artwork import BANNER_B64, BOTTOM_B64
from bdrip.release.banner import split_template
from bdrip.release.media import (
    aspect_ratio_text,
    audio_channel_text,
    compact_audio_codec,
    media_metadata,
)
from bdrip.release.nfo import get_file_metadata, get_info_block, render_nfo
from bdrip.release.pipeline import audio_release_token


class DocumentTests(unittest.TestCase):
    def test_shared_audio_layout_handles_positions_and_raw_counts(self):
        for positions, count, expected in (
            (["3/2/0.1"], 6, "5.1"),
            (None, 8, "7.1"),
            (["bad"], 2, "2.0"),
        ):
            track = SimpleNamespace(other_channel_positions=positions, channel_s=count)
            self.assertEqual(audio_channel_text(track), expected)
        track = SimpleNamespace(channel_s=None)
        self.assertEqual(audio_channel_text(track), "Unknown")
        self.assertEqual(audio_channel_text(track, default=""), "")

    def test_release_tokens_keep_layout_for_lossless_and_omit_it_for_core_audio(self):
        for codec, token in (
            ("Dolby TrueHD", "TrueHD.7.1"),
            ("DTS-HD Master Audio", "DTS.MA7.1"),
            ("E-AC-3", "DDP"),
            ("AC-3", ""),
        ):
            track = SimpleNamespace(
                track_type="Audio", commercial_name=codec, format=codec, channel_s=8
            )
            with (
                self.subTest(codec=codec),
                patch(
                    "bdrip.release.pipeline.MediaInfo.parse",
                    return_value=SimpleNamespace(tracks=[track]),
                ),
            ):
                self.assertEqual(audio_release_token(Path("movie.mkv")), token)

    def test_shared_metadata_keeps_document_specific_video_and_audio_formatting(self):
        general = SimpleNamespace(
            track_type="General", file_size=1 << 30, other_duration=["1 min"]
        )
        video = SimpleNamespace(
            track_type="Video",
            encoded_library_name="x264",
            commercial_name=None,
            format="AVC",
            codec_id="V_MPEG4/ISO/AVC",
            format_profile="High@L4.1",
            bit_rate=8_000_000,
            other_bit_rate=["8.0 Mb/s"],
            width=1920,
            height=804,
            display_aspect_ratio="2.388",
            other_display_aspect_ratio=[],
            frame_rate="23.976",
            hdr_format=None,
        )
        audio = SimpleNamespace(
            track_type="Audio",
            commercial_name="Dolby Digital Plus with Dolby Atmos",
            format="E-AC-3",
            codec_id="A_EAC3",
            other_bit_rate=["768 kb/s"],
            other_language=["English"],
            language="en",
            other_channel_positions=["3/2/0.1"],
            channel_s=6,
        )
        subtitle = SimpleNamespace(
            track_type="Text", codec_id="S_TEXT/UTF8", language="en"
        )
        tracks = SimpleNamespace(tracks=[general, video, audio, subtitle])
        with patch("bdrip.release.media.MediaInfo.parse", return_value=tracks):
            bbcode = media_metadata(Path("movie.mkv"))
            nfo = get_file_metadata(Path("movie.mkv"))
        self.assertEqual(bbcode["aspect_ratio"], "2.38:1")
        self.assertEqual(nfo["resolution"], "1920x804 (2.38:1)")
        self.assertEqual(bbcode["video_codec"], "x264_L4.1 @ 8.0 Mbps")
        self.assertEqual(nfo["video_codec"], "x264_High@L4.1 @ 8.0 Mb/s")
        self.assertEqual(bbcode["audios"], ["English Atmos/ E-AC-3 5.1 @ 768 kb/s"])
        self.assertEqual(nfo["audios"], ["English Atmos/ E-AC-3 5.1ch @ 768 kb/s"])

    def test_shared_formatters_keep_known_and_unknown_values(self):
        for value, expected in (
            ("16:9", "1.77:1"),
            (2.388, "2.38:1"),
            (None, "Unknown"),
        ):
            self.assertEqual(aspect_ratio_text(value), expected)
        self.assertEqual(compact_audio_codec("DTS-HD Master Audio"), "DTS-HD MA")
        self.assertEqual(compact_audio_codec("AAC"), "AAC")

    def test_nfo_artwork_round_trip_and_utf8_encoding(self):
        metadata = dict.fromkeys(
            [
                "file_name",
                "name",
                "genre",
                "rating",
                "imdb",
                "release_date",
                "duration",
                "file_size",
                "video_codec",
                "framerate",
                "resolution",
                "language",
                "subtitles",
                "source",
            ],
            "Example",
        )
        metadata.update(audios=["English AAC 2.0ch @ 128 kb/s"], hdr_format=None)
        document = render_nfo(metadata)
        top, bottom = split_template(document)
        self.assertEqual(top, base64.b64decode(BANNER_B64))
        self.assertEqual(bottom, base64.b64decode(BOTTOM_B64))
        self.assertEqual(
            document, top + get_info_block(metadata).encode("cp437") + bottom
        )
        metadata["name"] = "电影"
        utf8 = render_nfo(metadata, "utf-8")
        self.assertTrue(utf8.startswith(b"\xef\xbb\xbf"))
        self.assertIn("电影", utf8.decode("utf-8-sig"))
