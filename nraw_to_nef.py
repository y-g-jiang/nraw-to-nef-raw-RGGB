import argparse
import struct
from fractions import Fraction
from pathlib import Path

from nev import read_sample
from raw_win import Decoder


class TIFF:
    def __init__(self):
        self.b = bytearray(b"II*\x00\x08\x00\x00\x00")

    def data(self, value):
        self.b += b"\0" * (len(self.b) % 2)
        offset = len(self.b)
        self.b += value
        return offset

    def ifd(self, entries):
        offset = self.data(b"\0" * (6 + 12 * len(entries)))
        struct.pack_into("<H", self.b, offset, len(entries))
        for index, (number, kind, count, data) in enumerate(sorted(entries)):
            value = data.ljust(4, b"\0") if len(data) <= 4 else struct.pack("<I", self.data(data))
            position = offset + 2 + 12 * index
            struct.pack_into("<HHI", self.b, position, number, kind, count)
            self.b[position + 8:position + 12] = value
        return offset

    def patch(self, offset, number, value):
        for index in range(struct.unpack_from("<H", self.b, offset)[0]):
            position = offset + 2 + 12 * index
            if struct.unpack_from("<H", self.b, position)[0] == number:
                struct.pack_into("<I", self.b, position + 8, value)
                return
        raise KeyError(number)


def tag(number, kind, value):
    if kind == 2:
        data = value.encode("ascii") + b"\0"
        count = len(data)
    elif kind in (1, 7):
        data = bytes(value)
        count = len(data)
    else:
        values = (value,) if isinstance(value, (float, int)) else value
        code = {3: "H", 4: "I", 5: "I", 9: "i", 10: "i", 11: "f", 12: "d"}[kind]
        data = struct.pack("<" + code * len(values), *values)
        count = len(values) // (2 if kind in (5, 10) else 1)
    return number, kind, count, data


def parse_metadata(header):
    result = {}
    position = 0
    while position < len(header):
        index = header.find(b"NRMT", position)
        if index < 0:
            break
        if index < 4:
            raise ValueError("Invalid NRMT offset")
        size = struct.unpack_from(">I", header, index - 4)[0]
        end = index - 4 + size
        if size < 13 or end > len(header):
            raise ValueError("Invalid NRMT size")
        data = header[index + 4:end]
        result[(int.from_bytes(data[:2], "big"), int.from_bytes(data[2:4], "big"))] = data[5:]
        position = end
    return result


def parse_nikon(data):
    if data[:2] != b"II":
        raise ValueError("Unsupported Nikon byte order")
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
    entries = {}
    position = 2
    while position < len(data):
        number, kind, count = struct.unpack_from("<HHI", data, position)
        position += 8
        size = sizes[kind] * count
        value = data[position:position + size]
        if len(value) != size:
            raise ValueError("Truncated Nikon metadata")
        entries[number] = number, kind, count, value
        position += size
    return entries


def maker(entries):
    tiff = TIFF()
    tiff.ifd(entries)
    return b"Nikon\0\x02\x11\0\0" + tiff.b


def rational(value):
    fraction = Fraction(value).limit_denominator(1000000)
    return fraction.numerator, fraction.denominator


def convert(source, output, sdk, frame=0, model="NIKON Z6_3", threads=2):
    sample = read_sample(source, frame)
    metadata = parse_metadata(sample[:1024])
    nikon = parse_nikon(metadata[(0x200, 1)])
    crop = struct.unpack("<4H", nikon[0x45][3])
    iso = int.from_bytes(metadata[(0x110, 34866)], "big")
    exposure = struct.unpack(">f", metadata[(0x110, 33434)])[0]
    aperture = struct.unpack(">f", metadata[(0x110, 33437)])[0]
    raw, depth = Decoder(sdk, threads).decode(sample)
    if depth != 14 or raw.shape != (3410, 6060) or model != "NIKON Z6_3":
        raise ValueError((model, depth, raw.shape))
    entries = [nikon[number] for number in (5, 12, 61)]
    entries += [tag(1, 7, b"0211"), tag(2, 3, (0, iso)), tag(0x45, 3, crop), tag(0x93, 3, 2)]
    tiff = TIFF()
    root = tiff.ifd([
        tag(254, 4, 1), tag(256, 4, 160), tag(257, 4, 90),
        tag(258, 3, (8, 8, 8)), tag(259, 3, 1), tag(262, 3, 2),
        tag(271, 2, "NIKON CORPORATION"), tag(272, 2, model),
        tag(273, 4, 0), tag(274, 3, 1), tag(277, 3, 3),
        tag(278, 4, 90), tag(279, 4, 160 * 90 * 3),
        tag(330, 4, 0), tag(34665, 4, 0), tag(37398, 1, b"\1\0\0\0"),
    ])
    exif = tiff.ifd([
        tag(33434, 5, rational(exposure)), tag(33437, 5, rational(aperture)),
        tag(34855, 3, iso), tag(37500, 7, maker(entries)),
        tag(40962, 4, crop[2]), tag(40963, 4, crop[3]),
        tag(41730, 7, b"\2\0\2\0\0\1\1\2"),
    ])
    raw_ifd = tiff.ifd([
        tag(254, 4, 0), tag(256, 4, raw.shape[1]), tag(257, 4, raw.shape[0]),
        tag(258, 3, depth), tag(259, 3, 1), tag(262, 3, 32803),
        tag(273, 4, 0), tag(277, 3, 1), tag(278, 4, raw.shape[0]),
        tag(279, 4, raw.nbytes), tag(284, 3, 1),
        tag(33421, 3, (2, 2)), tag(33422, 1, b"\0\1\1\2"), tag(37399, 3, 2),
    ])
    preview_offset = tiff.data(bytes(160 * 90 * 3))
    raw_offset = tiff.data(raw.astype("<u2").tobytes())
    tiff.patch(root, 273, preview_offset)
    tiff.patch(root, 330, raw_ifd)
    tiff.patch(root, 34665, exif)
    tiff.patch(raw_ifd, 273, raw_offset)
    with Path(output).open("xb") as stream:
        stream.write(tiff.b)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--frame", type=int, default=0)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    convert(args.source, args.output, args.sdk, args.frame, threads=args.threads)


if __name__ == "__main__":
    main()
