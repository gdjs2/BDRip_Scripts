import sys
import time
import base64
import pprint
import imdbinfo
import argparse
import math

from pathlib import Path
from loguru import logger
from pymediainfo import MediaInfo

from constants import BANNER_B64, BOTTOM_B64


def get_video_codec(track) -> str:
    encoder = (
        track.encoded_library_name
        or track.commercial_name
        or track.format
        or track.codec_id
        or "Unknown"
    )
    profile = track.format_profile or ""
    codec = f"{encoder}_{profile}" if profile else str(encoder)
    bit_rate = track.other_bit_rate[0] if track.other_bit_rate else "Unknown bitrate"
    return f"{codec} @ {bit_rate}"


def get_aspect_ratio(track) -> str:
    value = track.display_aspect_ratio
    if not value and track.other_display_aspect_ratio:
        value = track.other_display_aspect_ratio[0]
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    try:
        if ":" in text:
            width, height = text.split(":", 1)
            ratio = float(width) / float(height)
        else:
            ratio = float(text)
    except (ValueError, ZeroDivisionError):
        return text if text.endswith(":1") else f"{text}:1"
    truncated = math.floor(ratio * 100) / 100
    return f"{truncated:.2f}:1"


def compact_audio_codec(name: str) -> str:
    replacements = {
        "Dolby TrueHD with Dolby Atmos": "Atmos/ TrueHD",
        "Dolby Digital Plus with Dolby Atmos": "Atmos/ E-AC-3",
        "DTS-HD Master Audio": "DTS-HD MA",
        "DTS-HD High Resolution Audio": "DTS-HD HRA",
        "Dolby Digital Plus": "E-AC-3",
        "Dolby Digital": "AC-3",
    }
    return replacements.get(name, name)


def get_audio_text(track, channel: float, max_length: int = 48) -> str:
    language = track.other_language[0] if track.other_language else track.language or "Unknown"
    codec = track.commercial_name or track.format or track.codec_id or "Unknown"
    bit_rate = track.other_bit_rate[0] if track.other_bit_rate else "Unknown bitrate"

    def render(codec_name: str) -> str:
        return f"{language} {codec_name} {channel:.1f}ch @ {bit_rate}"

    audio_text = render(codec)
    if len(audio_text) > max_length:
        audio_text = render(compact_audio_codec(codec))
    return audio_text

def get_imdb_metadata(imdb_id: str) -> dict:
    movie = imdbinfo.get_movie(imdb_id)
    if movie is None:
        logger.error(f"Failed to fetch metadata for IMDB ID: {imdb_id}")
        sys.exit(1)
        
    return {
        "name": movie.title_localized,
        "genre": " | ".join(movie.genres),
        "rating": f"{movie.rating}/10 ({movie.votes:,} votes)",
        "release_date": movie.release_date,
        "imdb": f"{movie.url}",
    }

def get_file_metadata(file_path: Path) -> dict:
    media_info = MediaInfo.parse(file_path)
    metadata = {}

    # print(media_info.to_json())
    
    for track in media_info.tracks:
        if track.track_type == "General":
            metadata["file_size"] = f"{track.file_size / (1 << 30):.2f} GB"
            metadata["duration"] = f"{track.other_duration[0]}"
        elif track.track_type == "Video":
            metadata["video_codec"] = get_video_codec(track)
            metadata["resolution"] = f"{track.width}x{track.height} ({get_aspect_ratio(track)})"
            metadata["framerate"] = f"{track.frame_rate} fps"
            
            metadata["hdr_format"] = track.hdr_format

        elif track.track_type == "Audio":
            if "audios" not in metadata:
                metadata["audios"] = []

            if track.other_channel_positions:
                channel = sum(map(float, track.other_channel_positions[0].split("/")))
            else:
                channel = float(track.channel_s)

            metadata["audios"].append(get_audio_text(track, channel))
            if track.other_language:
                if "language" not in metadata:
                    metadata["language"] = set()
                metadata["language"].add(track.other_language[0])

        elif track.track_type == "Text":
            if "subtitles" not in metadata:
                metadata["subtitles"] = {}
            codec = track.codec_id
            if codec not in metadata["subtitles"]:
                metadata["subtitles"][codec] = set()
            metadata["subtitles"][codec].add(track.language)
    
    if "language" in metadata:
        metadata["language"] = " | ".join(sorted(metadata["language"]))
    
    if "subtitles" in metadata:
        for codec, languages in metadata["subtitles"].items(): # type: ignore
            metadata["subtitles"][codec] = "&".join(sorted(languages))
        codec_list = sorted(metadata["subtitles"].keys()) # type: ignore
        metadata["subtitles"] = " | ".join([f"{codec} ({metadata['subtitles'][codec]})" for codec in codec_list]) # type: ignore

    return metadata

def get_metadata(imdb_id: str, file_path: Path) -> dict:
    imdb_metadata = get_imdb_metadata(imdb_id)
    file_metadata = get_file_metadata(file_path)
    return imdb_metadata | file_metadata

def get_info_block(metadata: dict) -> str:
    line_breaker = "\r\n" + ' '*9
    info_block = f"""{metadata["file_name"]}\r
\r
\r
         NAME.................: {metadata["name"]}\r
         GENRE................: {metadata["genre"]}\r
         RATiNG...............: {metadata["rating"]}\r
         iMDB.................: {metadata["imdb"]}\r
         RELEASE DATE.........: {metadata["release_date"]}\r
         ENCODED BY...........: {metadata.get("encoded_by", "WiKi")}\r
         RUNTiME..............: {metadata["duration"]}\r
         FiLE SiZE............: {metadata["file_size"]}\r
         ViDEO CODEC..........: {metadata["video_codec"]}"""
    
    if metadata["hdr_format"]:
        hdr_info = """HDR FORMAT...........: """
        hdr_line_breaker = '\r\n' + ' '*32
        hdr_line = hdr_line_breaker.join([metadata["hdr_format"]])
        hdr_info += hdr_line
        info_block = line_breaker.join([info_block, hdr_info])

    audio_info = """AUDiO CODEC..........: """
    audio_line_breaker = '\r\n' + ' '*32
    audio_line = audio_line_breaker.join(metadata["audios"])
    audio_info += audio_line
    info_block = line_breaker.join([info_block, audio_info])

    info_block += f"""{line_breaker}FRAMERATE............: {metadata["framerate"]}\r
         RESOLUTiON...........: {metadata["resolution"]}\r
         LANGUAGE.............: {metadata["language"]}\r
         SUBTiTLES............: {metadata["subtitles"]}\r
         SOURCE...............: {metadata["source"]}\r
"""
    return info_block


def render_nfo(metadata: dict, encoding: str = "cp437") -> bytes:
    """Render a consistently encoded NFO from the embedded CP437 artwork."""
    banner = base64.b64decode(BANNER_B64).decode("cp437")
    bottom = base64.b64decode(BOTTOM_B64).decode("cp437")
    document = banner + get_info_block(metadata) + bottom
    if encoding == "cp437":
        # Characters outside the legacy DOS set cannot be represented.
        return document.encode("cp437", errors="replace")
    # Include a BOM so remote NFO renderers can distinguish UTF-8 artwork from
    # legacy CP437 instead of interpreting each UTF-8 byte as a DOS character.
    return document.encode("utf-8-sig")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Genereate NFO file')
    parser.add_argument("imdb_id", help="IMDB ID of the movie")
    parser.add_argument("input_file", help="Path to the movie file")
    parser.add_argument("--encoder", default="WiKi", help="encoder name (default: WiKi)")
    parser.add_argument("--source", required=True, help="source release description")
    parser.add_argument(
        "--encoding", choices=("utf-8", "cp437"), default="cp437",
        help="output encoding (default: cp437)",
    )
    args = parser.parse_args()

    imdb_id = args.imdb_id
    input_file_path = Path(args.input_file)

    base_name = input_file_path.stem
    metadata = get_metadata(imdb_id, input_file_path)
    metadata["file_name"] = base_name
    metadata["encoded_by"] = args.encoder
    metadata["source"] = args.source

    all_block = render_nfo(metadata, args.encoding)
    
    with open(f"{base_name}.nfo", "wb") as nfo_file:
        nfo_file.write(all_block)

    print(f"Generating NFO for {base_name} with metadata: {metadata}")
