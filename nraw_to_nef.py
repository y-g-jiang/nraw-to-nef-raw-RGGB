import argparse
import binascii
import datetime as dt
import hashlib
import shutil
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from fractions import Fraction
from io import BytesIO

ROOT = Path(__file__).resolve().parent
import numpy as np
import rawpy
import tifffile
from PIL import Image
from nikon_lossless import encode
from raw_win import Decoder, SDK_SHA256
from lens_crop import bounded_crop
from exif_inheritance import (CLIP_TAGS,source_info,frame_elapsed,metadata_plan,
                              write_arguments,verify_readback)

PERL = os.environ.get('PERL', 'perl')
EXIFTOOL = os.environ.get('EXIFTOOL', 'exiftool')
ADOBE = os.environ.get('ADOBE_DNG_CONVERTER', 'C:/Program Files/Adobe/Adobe DNG Converter/Adobe DNG Converter.exe')

def exiftool_command():
    path = Path(shutil.which(str(EXIFTOOL)) or EXIFTOOL)
    if path.is_file():
        with path.open('rb') as stream:
            if stream.read(2) == b'#!':
                return [PERL, path]
    return [path]

def sha(data):
    return hashlib.sha256(data).hexdigest()

def run(args, timeout=120):
    return subprocess.run([str(a) for a in args], capture_output=True,
                          creationflags=0x08000000, timeout=timeout)

def allowed_write(path):
    path = Path(path).resolve()
    return path

def clip_metadata(source):
    p = run([*exiftool_command(), '-j', '-n', '-G1', *['-'+name for name in CLIP_TAGS], source])
    if p.returncode:
        raise RuntimeError(p.stderr.decode(errors='replace'))
    result=source_info(json.loads(p.stdout)[0],source)

    with source.open('rb') as f:
        def search(start,end):
            for kind,a,b in boxes(f,start,end):
                if kind in (b'moov',b'udta',b'NCDT'):
                    found=search(a,b)
                    if found is not None:return found
                elif kind==b'NCTG':
                    f.seek(a);return f.read(b-a)
        data=search(0,source.stat().st_size)
    if data:
        units={1:1,2:1,3:2,4:4,5:8,6:1,7:1,8:2,9:4,10:8,11:4,12:8};pos=0
        while pos+8<=len(data):
            tag,typ,count=struct.unpack_from('>IHH',data,pos);n=units[typ]*count
            value=data[pos+8:pos+8+n]
            if len(value)!=n:raise ValueError('Truncated source NCTG')
            if tag==0x19:result['_timezone_offset']=value.rstrip(b'\0').decode('ascii')
            if tag==0x2000024 and n==4:

                result['_worldtime_little_hex']=struct.pack('<hBB',*struct.unpack('>hBB',value)).hex()
            if tag==0x2000023:result['_picture_control_hex']=value.hex()
            pos+=8+n
    return result

def boxes(f, start, end):
    while start < end:
        if end-start < 8:
            raise ValueError('Truncated BMFF box')
        f.seek(start)
        size, kind = struct.unpack('>I4s', f.read(8))
        head = 8
        if size == 1:
            size, = struct.unpack('>Q', f.read(8))
            head = 16
        if size == 0:
            size = end-start
        if size < head or start+size > end:
            raise ValueError('Invalid BMFF box bounds')
        yield (kind, start+head, start+size)
        start += size

def index_nev(source):
    with source.open('rb') as f:
        top = list(boxes(f, 0, source.stat().st_size))
        movie = next(x for x in top if x[0] == b'moov')
        def children(box):
            return list(boxes(f, box[1], box[2]))
        def one(box, kind):
            return next(x for x in children(box) if x[0] == kind)
        def body(box):
            f.seek(box[1])
            return f.read(box[2]-box[1])
        for track in [x for x in children(movie) if x[0] == b'trak']:
            media = one(track, b'mdia')
            table = one(one(media, b'minf'), b'stbl')
            desc = one(table, b'stsd')
            if not any(x[0] == b'NRAW' for x in boxes(f, desc[1]+8, desc[2])):
                continue
            sz = body(one(table, b'stsz'))
            default, count = struct.unpack_from('>II', sz, 4)
            sizes = [default]*count if default else struct.unpack_from('>'+str(count)+'I', sz, 12)
            sc = body(one(table, b'stsc'))
            n, = struct.unpack_from('>I', sc, 4)
            rows = [struct.unpack_from('>III', sc, 8+12*i) for i in range(n)]
            cb = next(x for x in children(table) if x[0] in (b'stco', b'co64'))
            offsets = body(cb)
            n, = struct.unpack_from('>I', offsets, 4)
            offsets = struct.unpack_from('>'+str(n)+('Q' if cb[0] == b'co64' else 'I'), offsets, 8)
            samples, i, ri = [], 0, 0
            mdats = [x for x in top if x[0] == b'mdat']
            for chunk, offset in enumerate(offsets, 1):
                while ri+1 < len(rows) and rows[ri+1][0] <= chunk:
                    ri += 1
                for _ in range(rows[ri][1]):
                    if i >= count or not any(x[1] <= offset and offset+sizes[i] <= x[2] for x in mdats):
                        raise ValueError('Invalid sample bounds')
                    samples.append((offset, sizes[i]))
                    offset += sizes[i]
                    i += 1
            if i != count:
                raise ValueError('NRAW sample-count mismatch')
            header = body(one(media, b'mdhd'))
            timescale, = struct.unpack_from('>I', header, 20 if header[0] == 1 else 12)
            timing = body(one(table, b'stts'))
            n, = struct.unpack_from('>I', timing, 4)
            timing = [struct.unpack_from('>II', timing, 8+8*i) for i in range(n)]
            return samples, timescale, timing
    raise ValueError('No NRAW video track')

def frame_metadata(header):
    records, pos = {}, 0
    while True:
        i = header.find(b'NRMT', pos)
        if i < 0:
            break
        size, = struct.unpack_from('>I', header, i-4)
        if size < 13 or i-4+size > len(header):
            raise ValueError('Invalid NRAW metadata record')
        data = header[i+4:i-4+size]
        records[struct.unpack_from('>HH', data)] = data[5:]
        pos = i-4+size
    stream = records[(0x200, 1)]
    if stream[:2] != b'II':
        raise ValueError('Unsupported NRAW Nikon metadata byte order')
    p, maker = 2, {}
    units = {1:1, 2:1, 3:2, 4:4, 5:8, 7:1, 9:4, 10:8}
    while p < len(stream):
        tag, typ, count = struct.unpack_from('<HHI', stream, p)
        p += 8
        length = units[typ]*count
        value = stream[p:p+length]
        if len(value) != length:
            raise ValueError('Truncated Nikon frame metadata')
        maker[tag] = (typ, count, value)
        p += length
    cfa = records[(0x110, 41730)]
    if cfa != b'\0\2\0\2\0\1\1\2':
        raise ValueError('This writer requires a frame explicitly labelled RGGB')
    black = struct.unpack('<4H', maker[0x3d][2])
    crop = struct.unpack('<4H', maker[0x45][2])
    wb = struct.unpack('<8I', maker[0x0c][2])
    return dict(maker=maker, black=black, crop=crop, wb=wb,
                iso=struct.unpack('>I', records[(0x110, 34866)])[0],
                shutter=struct.unpack('>f', records[(0x110, 33434)])[0],
                fnumber=struct.unpack('>f', records[(0x110, 33437)])[0],
                focal_length=struct.unpack('>f', records[(0x110, 37386)])[0],
                exposure_compensation=struct.unpack('>f',records[(0x110,37380)])[0]
                    if (0x110,37380) in records else None,
                orientation=struct.unpack('>H',records[(0x110,274)])[0]
                    if (0x110,274) in records else 1)

def inherit_exif(output,meta,info,source_name,frame,elapsed=None):
    plan=metadata_plan(meta,info,source_name,frame,elapsed)
    args=[*exiftool_command(),'-overwrite_original',*write_arguments(plan),output]
    p=run(args)
    if p.returncode:raise RuntimeError('EXIF metadata update failed: '+p.stderr.decode(errors='replace'))
    wanted=list(plan['tags'])+['ExifIFD:SubSecTimeOriginal','ExifIFD:SubSecTimeDigitized','ExifIFD:SubSecTime']
    q=run([*exiftool_command(),'-j','-n','-G1',*['-'+tag for tag in wanted],output])
    if q.returncode:raise RuntimeError('EXIF metadata readback failed')
    checks=verify_readback(plan,json.loads(q.stdout)[0])
    return dict(verified=True,tags_written=len(plan['tags']),checks=checks,timestamp=plan['timestamp'],
                source_software=info.get('Software'),source=str(info.get('_source','')),
                stderr=p.stderr.decode(errors='replace'),
                limitations=['Uninterpreted camera private blocks are retained from the same-camera container',
                             'Lens correction migration is deferred; existing lens blocks are not rewritten'])

def ifd_entries(data, offset):
    count, = struct.unpack_from('<H', data, offset)
    if count > 1024 or offset+6+count*12 > len(data):
        raise ValueError('Invalid TIFF IFD')
    return {struct.unpack_from('<H', data, offset+2+12*i)[0]:offset+2+12*i for i in range(count)}

def tag_payload(data, entry, base=0):
    _, typ, count, value = struct.unpack_from('<HHII', data, entry)
    length = {1:1,2:1,3:2,4:4,5:8,7:1,9:4,10:8}[typ]*count
    start = entry+8 if length <= 4 else base+value
    if start < 0 or start+length > len(data):
        raise ValueError('Invalid TIFF tag payload')
    return start, length

def replace_value(data, entries, tag, value, base=0):
    if tag not in entries:
        return False
    start, length = tag_payload(data, entries[tag], base)
    if len(value) != length:
        raise ValueError(f'Tag {tag:x} size changed')
    data[start:start+length] = value
    return True

def preview_slots(data, template):
    slots = []
    with tifffile.TiffFile(template) as t:
        def page(p):
            if 513 in p.tags:
                start = p.tags[513].value[0]
                length = p.tags[514].value[0]
                slots.append((start, length, p.tags[514].offset+8, p.tags[513].offset+8, 0))
            if p.pages:
                for sub in p.pages:
                    page(sub)
        page(t.pages[0])
        exif = struct.unpack_from('<I', data, t.pages[0].tags[34665].offset+8)[0]
    entries = ifd_entries(data, exif)
    start, _ = tag_payload(data, entries[37500])
    base = start+10
    first = base+struct.unpack_from('<I', data, base+4)[0]
    mn = ifd_entries(data, first)
    if 0x11 in mn:
        off, = struct.unpack_from('<I', data, mn[0x11]+8)
        pi = ifd_entries(data, base+off)
        if 513 in pi:
            off, = struct.unpack_from('<I', data, pi[513]+8)
            length, = struct.unpack_from('<I', data, pi[514]+8)
            slots.append((base+off, length, pi[514]+8, pi[513]+8, base))
    return slots

def replace_previews(data, slots, image, overflow_start):
    receipt = []
    overflow = bytearray()
    dimensions = []
    for start, reserved, count_at, offset_at, base in slots:
        if start < 0 or reserved <= 0 or start+reserved > len(data):
            raise ValueError('Invalid template preview bounds')
        if any(at < 0 or at+4 > len(data) for at in (count_at, offset_at)):
            raise ValueError('Invalid template preview tag bounds')
        with Image.open(BytesIO(data[start:start+reserved])) as old:
            dimensions.append(old.size)
    for (start, reserved, count_at, offset_at, base), size in zip(slots, dimensions):
        new = image.resize(size, Image.Resampling.LANCZOS)
        out = BytesIO()
        new.save(out, format='JPEG', quality=90, optimize=True)
        jpeg = out.getvalue()
        relocated = len(jpeg) > reserved
        if relocated:
            if overflow_start < len(data):
                raise ValueError('Preview overflow overlaps TIFF data')
            overflow.extend(b'\0'*((-(overflow_start+len(overflow)))%2))
            target = overflow_start+len(overflow)
            if not 0 <= target-base <= 0xffffffff or target+len(jpeg) > 0x100000000:
                raise ValueError('Preview exceeds classic TIFF offset limits')
            struct.pack_into('<I', data, offset_at, target-base)
            overflow.extend(jpeg)
            data[start:start+reserved] = b'\0'*reserved
        else:
            target = start
            data[start:start+reserved] = jpeg+b'\0'*(reserved-len(jpeg))
        struct.pack_into('<I', data, count_at, len(jpeg))
        receipt.append(dict(width=size[0], height=size[1], bytes=len(jpeg),
                            relocated=relocated, quality=90))
    return receipt, overflow

def write_nef(raw, meta, source_info, template, output, source_name, frame, lens_reference=None, elapsed=None):
    with tifffile.TiffFile(template) as t:
        if t.byteorder != '<' or t.pages[0].tags[272].value != source_info['Model']:
            raise ValueError('A little-endian NEF template from the same camera model is required')
        sub = [p for p in t.pages[0].pages if 262 in p.tags and p.tags[262].value == 32803]
        if len(sub) != 1:
            raise ValueError('Template must contain one Bayer RAW image')
        pg = sub[0]
        shape = pg.shape
        raw_offset = pg.tags[273].value[0]
        count_at = pg.tags[279].offset+8
        template_exif = t.pages[0].tags[34665].value
        template_lens = template_exif.get('LensModel')
        if template_lens and source_info.get('LensModel') and template_lens.strip()!=source_info['LensModel'].strip():
            raise ValueError('Template lens differs from source; lens correction metadata cannot be borrowed')
        with template.open('rb') as stream:
            stream.seek(t.pages[0].tags[34665].offset+8)
            exif_offset, = struct.unpack('<I', stream.read(4))
        info_at, info_size = pg.tags[51157].valueoffset, pg.tags[51157].count
        xmp_tag = t.pages[0].tags.get(700)
        xmp_at = xmp_tag.valueoffset if xmp_tag else None
        xmp_size = xmp_tag.count if xmp_tag else 0
    if any(s > d for s, d in zip(raw.shape, shape)):
        raise ValueError('Decoded frame does not fit the same-camera still container')
    with template.open('rb') as stream:
        data = bytearray(stream.read(raw_offset))
    slots = preview_slots(data, template)
    exif = ifd_entries(data, exif_offset)
    mn_start, _ = tag_payload(data, exif[37500])
    base = mn_start+10
    mn = ifd_entries(data, base+struct.unpack_from('<I', data, base+4)[0])
    at, length = tag_payload(data, mn[0x96], base)
    compression_meta = bytes(data[at:at+length])
    if compression_meta[:2] != b'F0':
        raise ValueError('Template must use Nikon 14-bit lossless F0 compression')
    seeds = struct.unpack_from('<4H', compression_meta, 2)

    y0, x0 = ((shape[0]-raw.shape[0])//4*2, (shape[1]-raw.shape[1])//4*2)
    canvas = np.empty(shape, np.uint16)
    for (y, x), value in zip(((0,0),(0,1),(1,0),(1,1)), meta['black']):
        canvas[y::2, x::2] = value
    canvas[y0:y0+raw.shape[0], x0:x0+raw.shape[1]] = raw
    x, y, w, h = meta['crop']
    crop = (x0+x, y0+y, w, h)
    if x+w > raw.shape[1] or y+h > raw.shape[0]:
        raise ValueError('Source active crop is out of bounds')

    old_crop_at, _ = tag_payload(data, mn[0x45], base)
    native_crop = struct.unpack_from('<4H', data, old_crop_at)
    if meta['crop'][2] != native_crop[2]:
        raise ValueError('Different sampling scale/FOV requires an explicit lens-coordinate transform; centre padding alone is not valid')
    for n, value in [(0x0c, meta['maker'][0x0c][2]),
                     (0x3d, struct.pack('<4H', *meta['black'])),
                     (0x45, struct.pack('<4H', *crop))]:
        replace_value(data, mn, n, value, base)
    if 0x4f in meta['maker']:
        replace_value(data, mn, 0x4f, meta['maker'][0x4f][2], base)
    if 0x25 in meta['maker']:
        replace_value(data,mn,0x25,meta['maker'][0x25][2],base)
    worldtime_copied=False
    if source_info.get('_worldtime_little_hex'):
        worldtime_copied=replace_value(data,mn,0x24,bytes.fromhex(source_info['_worldtime_little_hex']),base)
    picture_control_copied=False
    if source_info.get('_picture_control_hex') and 0x23 in mn:
        block=bytes.fromhex(source_info['_picture_control_hex'])
        _,capacity=tag_payload(data,mn[0x23],base)
        if len(block)==capacity:
            picture_control_copied=replace_value(data,mn,0x23,block,base)

    info_base = info_at+10
    ni = ifd_entries(data, info_base+struct.unpack_from('<I', data, info_base+4)[0])
    for n, value in [(9, meta['maker'][0x0c][2]),
                     (10, b'0100'+struct.pack('<4H', *meta['black'])),
                     (11, b'0100'+struct.pack('<4H', *crop))]:
        start, length = tag_payload(data, ni[n], info_base)
        if len(value) > length-2:
            raise ValueError('Source metadata does not fit Nikon private field')

        block = bytearray(data[start:start+length])
        block[:len(value)] = value
        struct.pack_into('<H', block, length-2, binascii.crc_hqx(block[:-2], 0xffff))
        replace_value(data, ni, n, block, info_base)

    encoded, _ = encode(canvas, seeds)
    struct.pack_into('<I', data, count_at, len(encoded))
    nx, ny, nw, nh = native_crop
    display_crop=crop
    lens_guard={'status':'No audited warp reference supplied; source crop only'}
    if lens_reference:
        display_crop,lens_guard=bounded_crop(lens_reference,template,shape,raw.shape,
                                             (x0,y0),crop,meta,source_info)
    crs_crop = dict(left=(display_crop[0]-nx)/nw, top=(display_crop[1]-ny)/nh,
                    right=(display_crop[0]+display_crop[2]-nx)/nw, bottom=(display_crop[1]+display_crop[3]-ny)/nh)
    if not all(0 <= v <= 1 for v in crs_crop.values()):
        raise ValueError('Source crop falls outside the template photographic active area')

    xmp = (f'<?xpacket begin="\ufeff"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
           f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           f'<rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" '
           f'crs:Version="18.7" crs:ProcessVersion="15.4" crs:HasSettings="True" '
           f'crs:HasCrop="True" crs:CropAngle="0" crs:CropConstrainToWarp="0" '
           f'crs:CropLeft="{crs_crop["left"]:.12f}" crs:CropTop="{crs_crop["top"]:.12f}" '
           f'crs:CropRight="{crs_crop["right"]:.12f}" crs:CropBottom="{crs_crop["bottom"]:.12f}"/>'
           f'</rdf:RDF></x:xmpmeta><?xpacket end="w"?>').encode('utf-8')
    if xmp_at is None or len(xmp) > xmp_size:
        raise ValueError('Template lacks sufficient XMP reservation')
    data[xmp_at:xmp_at+xmp_size] = xmp+b' '*(xmp_size-len(xmp))

    step = max(2, ((max(shape)//1280+1)//2)*2)
    red = canvas[0::step,0::step].astype(np.float32)
    green = (canvas[0::step,1::step].astype(np.float32)+canvas[1::step,0::step].astype(np.float32))*0.5
    blue = canvas[1::step,1::step].astype(np.float32)
    rows=min(red.shape[0],green.shape[0],blue.shape[0]);cols=min(red.shape[1],green.shape[1],blue.shape[1])
    proxy=np.stack((red[:rows,:cols],green[:rows,:cols],blue[:rows,:cols]),axis=-1)
    blacks=np.array([meta['black'][0],(meta['black'][1]+meta['black'][2])*0.5,meta['black'][3]],np.float32)
    gains=np.array([meta['wb'][0]/meta['wb'][1],1,meta['wb'][2]/meta['wb'][3]],np.float32)
    proxy=np.maximum(proxy-blacks,0)/(16383-blacks)*gains
    proxy=np.where(proxy<=0.0031308,12.92*proxy,1.055*np.power(np.maximum(proxy,0),1/2.4)-0.055)
    preview=Image.fromarray(np.rint(np.clip(proxy,0,1)*255).astype(np.uint8))
    previews, preview_tail = replace_previews(data, slots, preview, len(data)+len(encoded))

    with tifffile.TiffFile(template) as t:
        for p in t.pages[0].pages:
            if 262 in p.tags and p.tags[262].value == 1 and p.tags.get(258) and p.tags[258].value == 8 and 273 in p.tags:
                start, size = p.tags[273].value[0], p.tags[279].value[0]
                if size and start+size <= len(data):
                    thumb = preview.convert('L').resize((p.shape[1],p.shape[0]), Image.Resampling.LANCZOS).tobytes()
                    if len(thumb) == size:
                        data[start:start+size] = thumb
    with output.open('wb') as stream:
        stream.write(data)
        stream.write(encoded)
        stream.write(preview_tail)
    inheritance=inherit_exif(output,meta,source_info,source_name,frame,elapsed)
    inheritance['picture_control_block_copied']=picture_control_copied
    inheritance['worldtime_block_copied']=worldtime_copied
    if picture_control_copied:inheritance['picture_control_sha256']=sha(bytes.fromhex(source_info['_picture_control_hex']))

    with rawpy.imread(str(output)) as r:
        reread = r.raw_image
        exact = np.array_equal(canvas, reread)
        if not exact or not np.array_equal(raw, reread[y0:y0+raw.shape[0],x0:x0+raw.shape[1]]):
            raise RuntimeError('NEF round trip changed RAW pixels')
        expected_wb = [meta['wb'][0]/meta['wb'][1], meta['wb'][4]/meta['wb'][5],
                       meta['wb'][2]/meta['wb'][3], meta['wb'][6]/meta['wb'][7]]
        if r.raw_pattern.tolist() != [[0,1],[3,2]] or r.black_level_per_channel != [meta['black'][0],meta['black'][1],meta['black'][3],meta['black'][2]]:
            raise RuntimeError('NEF CFA or black level mismatch')
        if not np.allclose(r.camera_whitebalance, expected_wb, rtol=1e-7, atol=0):
            raise RuntimeError('NEF white balance mismatch')
        readback = dict(rawpy_version=rawpy.__version__, libraw_version=rawpy.libraw_version,
                        black_rgbg=r.black_level_per_channel, white_level=r.white_level,
                        wb_rgbg=r.camera_whitebalance, cfa=r.raw_pattern.tolist())
    return dict(native_canvas_shape=list(shape), source_offset_xy=[x0,y0],
                source_shape=list(raw.shape), source_active_crop_xywh=list(meta['crop']),
                native_active_crop_xywh=list(native_crop), output_active_crop_xywh=list(crop),
                adobe_develop_crop=crs_crop, lens_crop_guard=lens_guard, source_mosaic_sha256=sha(raw),
                canvas_sha256=sha(canvas), roundtrip_exact=exact,
                lens_coordinates='Unscaled full still-sensor canvas; source active width equals the native photo active width',
                template_lens=template_lens,
                encoded_bytes=len(encoded), previews=previews,
                preview_method='Bounded CFA proxy with recorded WB and sRGB OETF; approximate camera-RGB colour, no lens warp', libraw=readback,
                exif_inheritance=inheritance,
                metadata_update_stderr=inheritance['stderr'])

def adobe_check(nef, scratch, expected_canvas_sha, expected_opcode_sha=None):
    scratch = allowed_write(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    dng = scratch/nef.with_suffix('.dng').name
    if dng.exists():
        raise FileExistsError(dng)
    p = run([ADOBE, '-u', '-p0', '-d', scratch, nef])
    if not dng.exists() or b'Failed to convert' in p.stderr:
        raise RuntimeError('Adobe rejected NEF: '+p.stderr.decode(errors='replace'))
    with rawpy.imread(str(dng)) as r:
        same = sha(r.raw_image) == expected_canvas_sha
        result = dict(success=True, engine_version='18.7', dng_bytes=dng.stat().st_size,
                      adobe_decoded_canvas_exact=same, adobe_black_rgbg=r.black_level_per_channel,
                      adobe_white_level=r.white_level)
    with tifffile.TiffFile(dng) as t:
        def visit(p):
            yield p
            if p.pages:
                for sub in p.pages:
                    yield from visit(sub)
        pg=next(p for top in t.pages for p in visit(top)
                if p.tags.get(262) and p.tags[262].value == 32803)
        opcode=pg.tags.get(51022)
        result['opcode_list3_sha256']=sha(opcode.value) if opcode else None
    if expected_opcode_sha and result['opcode_list3_sha256'] != expected_opcode_sha:
        raise RuntimeError('Adobe lens opcodes differ from the audited crop reference')
    xmp = run([*exiftool_command(), '-j', '-XMP-crs:HasCrop', '-XMP-crs:CropTop',
               '-XMP-crs:CropBottom', '-XMP-crs:CropLeft', '-XMP-crs:CropRight', dng])
    result['preserved_develop_crop'] = json.loads(xmp.stdout)[0]
    result['stderr'] = p.stderr.decode(errors='replace')

    dng.unlink()
    if not same:
        raise RuntimeError('Adobe conversion changed the decoded Bayer canvas')
    return result

def frame_list(text, count):
    chosen = []
    for part in text.split(','):
        if ':' in part:
            values = [int(x) if x else None for x in part.split(':')]
            if len(values) not in (2,3):
                raise ValueError('Use start:stop[:step] frame ranges')
            values += [None]*(3-len(values))
            chosen.extend(range(values[0] or 0, values[1] if values[1] is not None else count, values[2] or 1))
        else:
            chosen.append(int(part))
    chosen = list(dict.fromkeys(chosen))
    if not chosen or min(chosen) < 0 or max(chosen) >= count:
        raise ValueError('Frame selection out of bounds')
    return chosen

def main():
    global EXIFTOOL, PERL, ADOBE
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--frames', default='0')
    parser.add_argument('--out-dir', type=Path, default=ROOT/'outputs')
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--sdk', type=Path, required=True)
    parser.add_argument('--exiftool', default=EXIFTOOL)
    parser.add_argument('--perl', default=PERL)
    parser.add_argument('--adobe-converter', default=ADOBE)
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--adobe-check', action='store_true')
    parser.add_argument('--lens-reference', type=Path)
    parser.add_argument('--max-output-mib', type=int, default=512)
    args = parser.parse_args()
    EXIFTOOL, PERL, ADOBE = args.exiftool, args.perl, args.adobe_converter
    source = args.source.resolve()
    output = allowed_write(args.out_dir)
    output.mkdir(parents=True, exist_ok=True)
    samples, timescale, timing = index_nev(source)
    frames = frame_list(args.frames, len(samples))

    with tifffile.TiffFile(args.template) as t:
        pg = next(p for p in t.pages[0].pages if p.tags.get(262) and p.tags[262].value == 32803)
        worst = int(np.prod(pg.shape))*22//8+pg.tags[273].value[0]+1024*1024
    if worst*len(frames) > args.max_output_mib*1024**2:
        raise ValueError('Selected frames exceed --max-output-mib worst-case bound')
    if args.adobe_check:

        verified=[]
        for frame in frames:
            command=[sys.executable,Path(__file__).resolve(),source,'--frames',str(frame),
                     '--out-dir',output,'--template',args.template,'--sdk',args.sdk,
                     '--exiftool',EXIFTOOL,'--perl',PERL,'--adobe-converter',ADOBE,
                     '--threads',str(args.threads),'--max-output-mib',str(args.max_output_mib)]
            if args.lens_reference:
                command += ['--lens-reference',args.lens_reference]
            worker=run(command)
            if worker.returncode:
                raise RuntimeError('Conversion worker failed: '+worker.stderr.decode(errors='replace'))
            nef=output/f'{source.stem}_frame{frame:06d}.NEF'
            receipt=nef.with_suffix('.json')
            result=json.loads(receipt.read_text(encoding='utf-8'))
            result['adobe']=adobe_check(nef,ROOT/'work/adobe_validation',result['canvas_sha256'],
                                       result['lens_crop_guard'].get('opcode_list3_sha256'))
            receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            verified.append(result)
            print(json.dumps(dict(frame=frame,nef=str(nef),bytes=nef.stat().st_size,
                                  roundtrip_exact=True,adobe=True),ensure_ascii=False),flush=True)
        verification=output/f'{source.stem}_adobe_verified_{frames[0]}_{frames[-1]}.json'
        if verification.exists():
            raise FileExistsError(verification)
        verification.write_text(json.dumps({'results':verified},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return
    dec = Decoder(args.sdk, args.threads)
    info = clip_metadata(source)
    report = dict(format='Nikon lossless NEF, no DNGVersion tag', source=str(source),
                  source_bytes=source.stat().st_size, source_model=info['Model'],
                  frames_in_clip=len(samples), timescale=timescale, timing=timing,
                  template=str(args.template.resolve()), template_sha256=sha(args.template.read_bytes()),
                  sdk_sha256=SDK_SHA256,
                  limits=['Same-camera template and fitting RGGB NRAW modes only',
                          'Same-camera private still metadata is retained where source video semantics are unverified',
                          'Adobe uses its photo camera profile and photo white level; this does not reproduce Nikon/RED NRAW rendering',
                          'Adobe engine CLI tested; Lightroom/Camera Raw application UI is not installed here'],
                  results=[])
    for frame in frames:
        nef = output/f'{source.stem}_frame{frame:06d}.NEF'
        receipt = nef.with_suffix('.json')
        if nef.exists() or receipt.exists():
            raise FileExistsError(f'Refusing overwrite: {nef}')
        offset, length = samples[frame]
        with source.open('rb') as f:
            f.seek(offset)
            sample = f.read(length)
        raw, depth = dec.decode(sample)
        if depth != 14:
            raise ValueError(f'Expected decoded 14-bit values, got {depth}')
        meta = frame_metadata(sample[:1024])
        result = write_nef(raw, meta, info, args.template, nef, source.name, frame, args.lens_reference,
                           frame_elapsed(frame,timescale,timing))
        result.update(frame=frame, source_sample_offset=offset, source_sample_bytes=length,
                      source_sample_sha256=sha(sample), output=str(nef), output_bytes=nef.stat().st_size,
                      output_sha256=sha(nef.read_bytes()), source_iso=meta['iso'],
                      source_exposure_seconds=meta['shutter'], source_black_rggb=meta['black'],
                      source_wb_rb_rationals=list(meta['wb']))
        if args.adobe_check:
            result['adobe'] = adobe_check(nef, ROOT/'work/adobe_validation', result['canvas_sha256'])
        receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        report['results'].append(result)
        print(json.dumps(dict(frame=frame, nef=str(nef), bytes=nef.stat().st_size,
                              roundtrip_exact=result['roundtrip_exact'],
                              adobe=result.get('adobe',{}).get('success')),ensure_ascii=False),flush=True)
    report_path = output/f'{source.stem}_conversion_report.json'
    if report_path.exists():
        report_path = output/f'{source.stem}_conversion_report_{frames[0]}_{frames[-1]}.json'
    if report_path.exists():
        raise FileExistsError(report_path)
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

if __name__ == '__main__':
    main()
