import binascii
import struct
from io import BytesIO

import numpy as np
from PIL import Image

from nikon_lossless import encode


GEOMETRY = {
    'NIKON Z 8': ((5520, 8280), (12, 8, 8256, 5504)),
    'NIKON Z6_3': ((4040, 6064), (8, 4, 6048, 4032)),
}


def geometry(model):
    if model not in GEOMETRY:
        raise ValueError('无模板模式暂支持 Z8、Z6III；此机型可指定 NEF 模板')
    return GEOMETRY[model]


class TiffBuffer:
    def __init__(self):
        self.data = bytearray(b'II*\0\x08\0\0\0')

    def append(self, value):
        self.data.extend(b'\0' * (len(self.data) % 2))
        offset = len(self.data)
        self.data.extend(value)
        return offset

    def ifd(self, entries):
        entries = sorted(entries)
        if len({row[0] for row in entries}) != len(entries):
            raise ValueError('Duplicate TIFF tag')
        offset = self.append(bytes(6 + 12 * len(entries)))
        struct.pack_into('<H', self.data, offset, len(entries))
        for index, (number, kind, count, value) in enumerate(entries):
            at = offset + 2 + index * 12
            payload = value.ljust(4, b'\0') if len(value) <= 4 else struct.pack('<I', self.append(value))
            struct.pack_into('<HHI', self.data, at, number, kind, count)
            self.data[at + 8:at + 12] = payload
        return offset

    def pointer(self, offset, number, value):
        count, = struct.unpack_from('<H', self.data, offset)
        for index in range(count):
            at = offset + 2 + index * 12
            if struct.unpack_from('<H', self.data, at)[0] == number:
                struct.pack_into('<I', self.data, at + 8, value)
                return
        raise KeyError(number)


def tag(number, kind, value):
    if kind == 2:
        payload = value.encode('utf-8') + b'\0'
        count = len(payload)
    elif kind in (1, 7):
        payload = bytes(value)
        count = len(payload)
    else:
        values = (value,) if isinstance(value, int) else tuple(value)
        payload = struct.pack('<' + {3: 'H', 4: 'I', 5: 'I', 9: 'i', 10: 'i'}[kind] * len(values), *values)
        count = len(values) // (2 if kind in (5, 10) else 1)
    return number, kind, count, payload


def nikon_block(version, entries):
    tiff = TiffBuffer()
    tiff.ifd(entries)
    return b'Nikon\0' + version + b'\0\0' + tiff.data


def crc_block(value):
    return value + struct.pack('<H', binascii.crc_hqx(value, 0xffff))


def preview_image(raw, meta):
    step = max(2, ((max(raw.shape) // 1280 + 1) // 2) * 2)
    planes = [raw[y::step, x::step].astype(np.float32) for y, x in ((0, 0), (0, 1), (1, 0), (1, 1))]
    rows = min(p.shape[0] for p in planes)
    cols = min(p.shape[1] for p in planes)
    planes = [p[:rows, :cols] for p in planes]
    proxy = np.stack((planes[0], (planes[1] + planes[2]) * 0.5, planes[3]), axis=-1)
    black = np.array([meta['black'][0], (meta['black'][1] + meta['black'][2]) * 0.5, meta['black'][3]], np.float32)
    gains = np.array([meta['wb'][0] / meta['wb'][1], 1, meta['wb'][2] / meta['wb'][3]], np.float32)
    proxy = np.maximum(proxy - black, 0) / (16383 - black) * gains
    proxy = np.where(proxy <= 0.0031308, 12.92 * proxy, 1.055 * np.power(np.maximum(proxy, 0), 1 / 2.4) - 0.055)
    return Image.fromarray(np.rint(np.clip(proxy, 0, 1) * 255).astype(np.uint8))


def write_container(raw, meta, info, output):
    if raw.dtype != np.uint16 or raw.ndim != 2 or min(raw.shape) < 2 or any(n % 2 for n in raw.shape):
        raise ValueError('Expected an even-sized uint16 RGGB frame')
    x, y, width, height = meta['crop']
    if min(x, y) < 0 or min(width, height) <= 0 or x + width > raw.shape[1] or y + height > raw.shape[0]:
        raise ValueError('Source active crop is out of bounds')
    if len(meta['black']) != 4 or any(not 0 <= v < 16383 for v in meta['black']):
        raise ValueError('Invalid source black levels')
    if len(meta['wb']) != 8 or any(v <= 0 for v in meta['wb']):
        raise ValueError('Invalid source white balance')
    encoded, compression = encode(raw)
    compression = compression[:10] + struct.pack('<H', 34) + bytes(34)
    maker = {number: (number, kind, count, value) for number, (kind, count, value) in meta['maker'].items()
             if number in (5, 7, 0x0c, 0x25, 0x3f, 0x4f)}
    maker.update({entry[0]: entry for entry in (
        tag(1, 7, b'0211'),
        tag(0x0c, 5, meta['wb']), tag(0x3d, 3, meta['black']), tag(0x45, 3, meta['crop']),
        tag(0x51, 7, b'01020501' + struct.pack('<8H', 0, 3, 0, 0, 6, 0, 0, 0)),
        tag(0x93, 3, 3), tag(0x96, 7, compression))})
    for number, name, kind in ((5, 'WhiteBalance', 2), (7, 'FocusMode', 2),
                               (0x1d, 'SerialNumber', 2), (0x22, 'ActiveD-Lighting', 3),
                               (0xa7, 'ShutterCount', 4), (0xb1, 'HighISONoiseReduction', 3)):
        if number not in maker and info.get(name) is not None:
            value = str(info[name]) if kind == 2 else int(info[name])
            maker[number] = tag(number, kind, value)
    if 0x3f not in maker and info.get('WhiteBalanceFineTune') is not None:
        from fractions import Fraction
        values = info['WhiteBalanceFineTune']
        values = values.split() if isinstance(values, str) else values
        rationals = [Fraction(str(value)).limit_denominator(1000000) for value in values]
        if len(rationals) != 2:
            raise ValueError('Invalid source white balance fine tune')
        maker[0x3f] = tag(0x3f, 10, [part for value in rationals for part in (value.numerator, value.denominator)])
    if 0x25 not in maker:
        import math
        iso = max(0, min(255, round((math.log2(meta['iso'] / 100) + 5) * 12)))
        maker[0x25] = tag(0x25, 7, bytes((iso, 1, 12, 0, 0, 0, iso, 1, 12, 0, 0, 0, 0, 0)))
    for number, key in ((0x23, '_picture_control_hex'), (0x24, '_worldtime_little_hex')):
        if info.get(key):
            maker[number] = tag(number, 7, bytes.fromhex(info[key]))
    mn = nikon_block(b'\x02\x11', list(maker.values()))
    values = {9: struct.pack('<8I', *meta['wb']) + bytes(2),
              10: b'0100' + struct.pack('<4H', *meta['black']),
              11: b'0100' + struct.pack('<4H', *meta['crop']),
              12: b'0100' + bytes(4), 13: b'0100' + struct.pack('<H', 3),
              14: b'0100' + compression, 15: b'0100' + bytes(8)}
    nefinfo = nikon_block(b'\x01\x03', [tag(n, 7, crc_block(v)) for n, v in values.items()])
    image = preview_image(raw, meta)
    preview = BytesIO()
    image.save(preview, format='JPEG', quality=90, optimize=True)
    jpeg = preview.getvalue()
    tiff = TiffBuffer()
    root = tiff.ifd([
        tag(254, 4, 1), tag(259, 3, 6), tag(271, 2, info.get('Make', 'NIKON CORPORATION')),
        tag(272, 2, info['Model']), tag(274, 3, meta.get('orientation', 1)),
        tag(282, 5, (300, 1)), tag(283, 5, (300, 1)), tag(296, 3, 2),
        tag(305, 2, 'NRAW to NEF 1.4'), tag(330, 4, (0, 0)), tag(513, 4, 0),
        tag(514, 4, len(jpeg)), tag(531, 3, 2), tag(34665, 4, 0), tag(37398, 1, b'\1\0\0\0')])
    exif = tiff.ifd([tag(36864, 7, b'0231'), tag(37500, 7, mn), tag(40960, 7, b'0100'),
                     tag(40961, 3, 1), tag(40962, 4, width), tag(40963, 4, height),
                     tag(41495, 3, 2), tag(41728, 7, b'\3'), tag(41729, 7, b'\1'),
                     tag(41730, 7, b'\2\0\2\0\0\1\1\2')])
    preview_ifd = tiff.ifd([tag(254, 4, 1), tag(259, 3, 6),
                           tag(513, 4, 0), tag(514, 4, len(jpeg))])
    raw_ifd = tiff.ifd([
        tag(254, 4, 0), tag(256, 4, raw.shape[1]), tag(257, 4, raw.shape[0]),
        tag(258, 3, 14), tag(259, 3, 34713), tag(262, 3, 32803), tag(273, 4, 0),
        tag(277, 3, 1), tag(278, 4, raw.shape[0]), tag(279, 4, len(encoded)), tag(284, 3, 1),
        tag(33421, 3, (2, 2)), tag(33422, 1, b'\0\1\1\2'), tag(37399, 3, 2),
        tag(51157, 7, nefinfo), tag(51158, 1, b'\1')])
    entries = struct.unpack_from('<H', tiff.data, root)[0]
    for index in range(entries):
        at = root + 2 + index * 12
        if struct.unpack_from('<H', tiff.data, at)[0] == 330:
            array_at, = struct.unpack_from('<I', tiff.data, at + 8)
            struct.pack_into('<II', tiff.data, array_at, preview_ifd, raw_ifd)
    tiff.pointer(root, 34665, exif)
    preview_at = tiff.append(jpeg)
    tiff.pointer(root, 513, preview_at)
    tiff.pointer(preview_ifd, 513, preview_at)
    raw_at = len(tiff.data) + len(tiff.data) % 2
    tiff.data.extend(b'\0' * (raw_at - len(tiff.data)))
    tiff.pointer(raw_ifd, 273, raw_at)
    with output.open('wb') as stream:
        stream.write(tiff.data)
        stream.write(encoded)
    return dict(encoded_bytes=len(encoded), previews=[dict(width=image.width, height=image.height, bytes=len(jpeg))])


def write_nef(raw, meta, info, output, source_name, frame, elapsed=None):
    import nraw_to_nef as engine
    shape, native_crop = geometry(info['Model'])
    if any(s > d for s, d in zip(raw.shape, shape)):
        raise ValueError('Decoded frame does not fit the same-camera still container')
    x, y, width, height = meta['crop']
    if min(x, y) < 0 or min(width, height) <= 0 or x + width > raw.shape[1] or y + height > raw.shape[0]:
        raise ValueError('Source active crop is out of bounds')
    y0, x0 = ((shape[0] - raw.shape[0]) // 4 * 2, (shape[1] - raw.shape[1]) // 4 * 2)
    canvas = np.empty(shape, np.uint16)
    for (row, col), black in zip(((0, 0), (0, 1), (1, 0), (1, 1)), meta['black']):
        canvas[row::2, col::2] = black
    canvas[y0:y0 + raw.shape[0], x0:x0 + raw.shape[1]] = raw
    crop = (x0 + x, y0 + y, width, height)
    nx, ny, nw, nh = native_crop
    crs_crop = dict(left=(crop[0] - nx) / nw, top=(crop[1] - ny) / nh,
                    right=(crop[0] + width - nx) / nw, bottom=(crop[1] + height - ny) / nh)
    if not all(0 <= value <= 1 for value in crs_crop.values()):
        raise ValueError('Source crop falls outside the native photographic active area')
    container_meta = {**meta, 'crop': native_crop}
    result = write_container(canvas, container_meta, info, output)
    inheritance = engine.inherit_exif(output, meta, info, source_name, frame, elapsed)
    inheritance['limitations'] = ['Only current frame and clip metadata are written; no photographic donor blocks']
    arguments = [*engine.exiftool_command(), '-overwrite_original', '-XMP-crs:Version=18.7',
                 '-XMP-crs:ProcessVersion=15.4', '-XMP-crs:HasSettings=True', '-XMP-crs:HasCrop=True',
                 '-XMP-crs:CropAngle=0', '-XMP-crs:CropConstrainToWarp=0']
    arguments += [f'-XMP-crs:Crop{key.title()}={value:.12f}' for key, value in crs_crop.items()]
    update = engine.run([*arguments, output])
    if update.returncode:
        raise RuntimeError('NEF crop metadata update failed: ' + update.stderr.decode(errors='replace'))
    check = engine.run([*engine.exiftool_command(), '-j', '-n', '-XMP-crs:HasCrop',
                       *[f'-XMP-crs:Crop{key.title()}' for key in crs_crop], output])
    import json
    saved = json.loads(check.stdout)[0] if check.returncode == 0 else {}
    if not saved.get('HasCrop') or any(abs(float(saved.get('Crop' + key.title(), -1)) - value) > 1e-10
                                      for key, value in crs_crop.items()):
        raise RuntimeError('NEF crop metadata readback mismatch')
    with engine.rawpy.imread(str(output)) as decoded:
        exact = np.array_equal(canvas, decoded.raw_image)
        if not exact:
            raise RuntimeError('NEF round trip changed RAW pixels')
        if decoded.raw_pattern.tolist() != [[0, 1], [3, 2]] or decoded.black_level_per_channel != [meta['black'][i] for i in (0, 1, 3, 2)]:
            raise RuntimeError('NEF CFA or black level mismatch')
        expected_wb = [meta['wb'][i] / meta['wb'][i + 1] for i in (0, 4, 2, 6)]
        if not np.allclose(decoded.camera_whitebalance, expected_wb, rtol=1e-7, atol=0):
            raise RuntimeError('NEF white balance mismatch')
        readback = dict(black_rgbg=decoded.black_level_per_channel, white_level=decoded.white_level,
                        wb_rgbg=decoded.camera_whitebalance, cfa=decoded.raw_pattern.tolist(),
                        rawpy_version=engine.rawpy.__version__, libraw_version=engine.rawpy.libraw_version)
    return dict(result, native_canvas_shape=list(shape), source_offset_xy=[x0, y0], source_shape=list(raw.shape),
                source_active_crop_xywh=list(meta['crop']), native_active_crop_xywh=list(native_crop),
                output_active_crop_xywh=list(crop), adobe_develop_crop=crs_crop,
                lens_crop_guard=dict(status='No borrowed lens correction blocks'), template=None,
                source_mosaic_sha256=engine.sha(raw), canvas_sha256=engine.sha(canvas), roundtrip_exact=exact,
                libraw=readback, exif_inheritance=inheritance,
                limitations=['No photographic template or borrowed camera/lens private blocks',
                             'Built-in lens distortion/vignette correction is not embedded',
                             'Native canvas compatibility geometry; source pixels are neither scaled nor interpolated'])
