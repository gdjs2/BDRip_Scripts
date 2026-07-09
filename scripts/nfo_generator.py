import sys
import time
import base64
import imdbinfo
import argparse

from pathlib import Path
from loguru import logger
from pymediainfo import MediaInfo

from constants import BANNER_B64, BOTTOM_B64

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
    }

def get_file_metadata(file_path: Path) -> dict:
    media_info = MediaInfo.parse(file_path)
    metadata = {}
    
    for track in media_info.tracks:
        if track.track_type == "General":
            metadata["file_size"] = f"{track.file_size / (1 << 30):.2f} GB"
            metadata["duration"] = f"{time.strftime('%Hh:%Mm:%Ss', time.gmtime(track.duration / 1000))}"
        elif track.track_type == "Video":
            metadata["video_codec"] = track.codec
            metadata["resolution"] = f"{track.width}x{track.height}"
            metadata["frame_rate"] = f"{track.frame_rate} fps"
            
            hdr_format_string = track.hdr_format_string
            if hdr_format_string:
                metadata["hdr_format"] = hdr_format_string.split(" / ")
            
        elif track.track_type == "Audio":
            if "audio" not in metadata:
                metadata["audio"] = []
            metadata["audio"].append(f"{track.language} {track.codec} {track.channel_s}ch @ {track.bit_rate / 1000:.0f} kbps")

    return metadata

def get_metadata(imdb_id: str, file_path: Path) -> dict:
    imdb_metadata = get_imdb_metadata(imdb_id)
    file_metadata = get_file_metadata(file_path)
    return imdb_metadata | file_metadata

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Genereate NFO file')
    parser.add_argument("imdb_id", help="IMDB ID of the movie")
    parser.add_argument("input_file", help="Path to the movie file")
    args = parser.parse_args()

    imdb_id = args.imdb_id
    input_file_path = Path(args.input_file)

    base_name = input_file_path.stem
    metadata = get_metadata(imdb_id, input_file_path)

    print(f"Generating NFO for {base_name} with metadata: {metadata}")
