import sys
import time
import base64
import pprint
import imdbinfo
import argparse

from pathlib import Path
from loguru import logger
from pymediainfo import MediaInfo

from constants import MYBANNER_B64 as BANNER_B64, MYBOTTOM_B64 as BOTTOM_B64

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
    
    for track in media_info.tracks:
        if track.track_type == "General":
            metadata["file_size"] = f"{track.file_size / (1 << 30):.2f} GB"
            metadata["duration"] = f"{track.other_duration[0]}"
        elif track.track_type == "Video":
            metadata["video_codec"] = f"{track.codec_id} @ {track.other_bit_rate[0]}"
            metadata["resolution"] = f"{track.width}x{track.height}"
            metadata["framerate"] = f"{track.frame_rate} fps"
            
            metadata["hdr_format"] = track.hdr_format
            
        elif track.track_type == "Audio":
            if "audios" not in metadata:
                metadata["audios"] = []
            channel = sum(map(float, track.other_channel_positions[0].split("/")))
            metadata["audios"].append(f"{track.other_language[0]} {track.commercial_name} {channel:.1f}ch @ {track.other_bit_rate[0]}")
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
        metadata["language"] = " | ".join(metadata["language"])
    
    if "subtitles" in metadata:
        for codec, languages in metadata["subtitles"].items(): # type: ignore
            metadata["subtitles"][codec] = "&".join(list(languages))
        codec_list = metadata["subtitles"].keys() # type: ignore
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
         ENCODED BY...........: NULL\r
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
         SOURCE...............: NULL\r
"""
    return info_block

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Genereate NFO file')
    parser.add_argument("imdb_id", help="IMDB ID of the movie")
    parser.add_argument("input_file", help="Path to the movie file")
    args = parser.parse_args()

    imdb_id = args.imdb_id
    input_file_path = Path(args.input_file)

    base_name = input_file_path.stem
    metadata = get_metadata(imdb_id, input_file_path)
    metadata["file_name"] = base_name

    all_block = base64.b64decode(BANNER_B64) + get_info_block(metadata).encode() + base64.b64decode(BOTTOM_B64)
    
    with open(f"{base_name}.nfo", "wb") as nfo_file:
        nfo_file.write(all_block)

    print(f"Generating NFO for {base_name} with metadata: {metadata}")
