import struct
from pathlib import Path


def atoms(stream, start, end):
    position = start
    while position < end:
        stream.seek(position)
        header = stream.read(8)
        if len(header) != 8:
            raise ValueError("Truncated atom")
        size, kind = struct.unpack(">I4s", header)
        header_size = 8
        if size == 1:
            size = struct.unpack(">Q", stream.read(8))[0]
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            raise ValueError("Invalid atom size")
        yield kind, position + header_size, position + size
        position += size


def child(stream, atom, kind):
    return next(item for item in atoms(stream, atom[1], atom[2]) if item[0] == kind)


def body(stream, atom):
    stream.seek(atom[1])
    return stream.read(atom[2] - atom[1])


def read_sample(source, frame):
    source = Path(source)
    if frame < 0:
        raise IndexError(frame)
    with source.open("rb") as stream:
        movie = next(item for item in atoms(stream, 0, source.stat().st_size) if item[0] == b"moov")
        for track in atoms(stream, movie[1], movie[2]):
            if track[0] != b"trak":
                continue
            media = child(stream, track, b"mdia")
            table = child(stream, child(stream, media, b"minf"), b"stbl")
            description = child(stream, table, b"stsd")
            if not any(item[0] == b"NRAW" for item in atoms(stream, description[1] + 8, description[2])):
                continue
            sizes_body = body(stream, child(stream, table, b"stsz"))
            fixed, count = struct.unpack_from(">II", sizes_body, 4)
            if frame >= count:
                raise IndexError(frame)
            sizes = [fixed] * count if fixed else struct.unpack_from(">" + "I" * count, sizes_body, 12)
            chunks_body = body(stream, child(stream, table, b"stsc"))
            row_count = struct.unpack_from(">I", chunks_body, 4)[0]
            rows = [struct.unpack_from(">III", chunks_body, 8 + 12 * i) for i in range(row_count)]
            offset_atom = next(item for item in atoms(stream, table[1], table[2]) if item[0] in (b"stco", b"co64"))
            offset_body = body(stream, offset_atom)
            chunk_count = struct.unpack_from(">I", offset_body, 4)[0]
            code = "Q" if offset_atom[0] == b"co64" else "I"
            offsets = struct.unpack_from(">" + code * chunk_count, offset_body, 8)
            sample_index = row_index = 0
            for chunk_index, offset in enumerate(offsets, 1):
                while row_index + 1 < len(rows) and rows[row_index + 1][0] <= chunk_index:
                    row_index += 1
                for _ in range(rows[row_index][1]):
                    if sample_index >= count:
                        raise ValueError("Sample count mismatch")
                    size = sizes[sample_index]
                    if sample_index == frame:
                        stream.seek(offset)
                        sample = stream.read(size)
                        if len(sample) != size:
                            raise ValueError("Truncated sample")
                        return sample
                    offset += size
                    sample_index += 1
    raise ValueError("NRAW sample not found")
