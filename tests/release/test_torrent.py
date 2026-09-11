"""Verify generated torrents locally without contacting trackers."""

import tempfile
import unittest
from pathlib import Path

from torf import Torrent

from bdrip.release.torrent import create_private_torrent, main, md5_file, verify_torrent


class TorrentTests(unittest.TestCase):
    def test_release_torrent_excludes_hidden_files_and_detects_changed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = root / "Movie"
            content.mkdir()
            (content / "movie.mkv").write_bytes(b"video" * 10_000)
            (content / "release.nfo").write_bytes(b"release info")
            (content / ".DS_Store").write_bytes(b"ignored")
            output = root / "release.torrent"
            infohash = create_private_torrent(
                {
                    "torrent": {
                        "tracker": "https://tracker.example/announce",
                        "piece_size": 16384,
                    }
                },
                content,
                output,
            )
            torrent = Torrent.read(output)
            self.assertTrue(torrent.private)
            self.assertEqual(torrent.infohash, infohash)
            self.assertEqual(
                {p.name for p in torrent.files}, {"movie.mkv", "release.nfo"}
            )
            self.assertTrue(verify_torrent(output, content))
            self.assertEqual(main([str(output), str(content)]), 0)
            (content / "movie.mkv").write_bytes(b"other" * 10_000)
            self.assertEqual(main([str(output), str(content)]), 1)
            self.assertFalse(output.with_suffix(".torrent.tmp").exists())

    def test_single_file_torrent_accepts_file_or_parent_and_rejects_missing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.mkv"
            source.write_bytes(b"data" * 10_000)
            output = root / "single.torrent"
            torrent = Torrent(path=source, piece_size=16384)
            self.assertTrue(torrent.generate())
            torrent.write(output)
            self.assertTrue(verify_torrent(output, source))
            self.assertTrue(verify_torrent(output, root))
            self.assertEqual(main([str(output), str(root / "missing")]), 1)

    def test_torrent_output_cannot_be_inside_its_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "outside"):
                create_private_torrent(
                    {"torrent": {"tracker": "https://tracker.example/announce"}},
                    root,
                    root / "recursive.torrent",
                )

    def test_checksum_matches_known_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file"
            path.write_bytes(b"abc")
            self.assertEqual(
                md5_file(path, chunk_size=2), "900150983cd24fb0d6963f7d28e17f72"
            )
