import hashlib
import sys
from pathlib import Path


def decode(data, pos=0):
    c = data[pos:pos + 1]

    if c == b'i':
        end = data.index(b'e', pos)
        return int(data[pos + 1:end]), end + 1

    if c == b'l':
        result = []
        pos += 1
        while data[pos:pos + 1] != b'e':
            value, pos = decode(data, pos)
            result.append(value)
        return result, pos + 1

    if c == b'd':
        result = {}
        pos += 1
        while data[pos:pos + 1] != b'e':
            key, pos = decode(data, pos)
            value, pos = decode(data, pos)
            result[key] = value
        return result, pos + 1

    if c.isdigit():
        colon = data.index(b':', pos)
        length = int(data[pos:colon])
        start = colon + 1
        return data[start:start + length], start + length

    raise ValueError(f"Invalid bencode at {pos}")


torrent_path = Path(sys.argv[1])
data_root = Path(sys.argv[2])

torrent, _ = decode(torrent_path.read_bytes())
info = torrent[b'info']

piece_length = info[b'piece length']
piece_hashes_raw = info[b'pieces']

expected_hashes = [
    piece_hashes_raw[i:i + 20]
    for i in range(0, len(piece_hashes_raw), 20)
]

files = []

for entry in info[b'files']:
    rel = Path(*[
        x.decode('utf-8')
        for x in entry[b'path']
    ])

    path = data_root / rel
    files.append((path, entry[b'length']))


print(f"Piece size : {piece_length:,} bytes")
print(f"Pieces     : {len(expected_hashes)}")
print()

for path, size in files:
    print(f"{path.name}")
    print(f"  expected size: {size:,}")
    print(f"  actual size  : {path.stat().st_size:,}")
    print()


# Torrent pieces treat all files as one continuous byte stream.
piece_buffer = bytearray()
piece_index = 0
global_offset = 0

bad_pieces = []


def check_piece(buf, index, offset):
    actual = hashlib.sha1(buf).digest()
    expected = expected_hashes[index]

    if actual != expected:
        print(
            f"[BAD] piece {index:4d}  "
            f"global offset {offset:,} - "
            f"{offset + len(buf) - 1:,}"
        )

        print(f"      expected: {expected.hex()}")
        print(f"      actual  : {actual.hex()}")

        bad_pieces.append(index)


for path, expected_size in files:
    if path.stat().st_size != expected_size:
        print(
            f"WARNING: size mismatch: {path}\n"
            f" expected={expected_size}, actual={path.stat().st_size}"
        )

    with open(path, 'rb') as f:
        while True:
            need = piece_length - len(piece_buffer)
            chunk = f.read(need)

            if not chunk:
                break

            piece_buffer.extend(chunk)

            if len(piece_buffer) == piece_length:
                check_piece(
                    piece_buffer,
                    piece_index,
                    global_offset,
                )

                global_offset += len(piece_buffer)
                piece_index += 1
                piece_buffer.clear()


if piece_buffer:
    check_piece(
        piece_buffer,
        piece_index,
        global_offset,
    )


print()
if not bad_pieces:
    print("ALL PIECES MATCH.")
else:
    print(f"{len(bad_pieces)} mismatching pieces:")
    print(", ".join(map(str, bad_pieces)))