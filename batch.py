import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import time
import uuid

VERSION = '1.2.0'
SDK_SHA256 = 'a3c3039647716ebfb254898568dbb0954925e35dcb76a48b24cc58af36ca4b31'


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.03)
    finally:
        temporary.unlink(missing_ok=True)


def frame_count(source):
    from nev import atoms, body, child
    source = Path(source)
    with source.open('rb') as stream:
        movie = next(a for a in atoms(stream, 0, source.stat().st_size) if a[0] == b'moov')
        for track in atoms(stream, movie[1], movie[2]):
            if track[0] != b'trak':
                continue
            media = child(stream, track, b'mdia')
            table = child(stream, child(stream, media, b'minf'), b'stbl')
            description = child(stream, table, b'stsd')
            if any(a[0] == b'NRAW' for a in atoms(stream, description[1] + 8, description[2])):
                stream.seek(child(stream, table, b'stsz')[1] + 8)
                count, = struct.unpack('>I', stream.read(4))
                if count > 0:
                    return count
    raise ValueError('没有可读取的 NRAW 视频轨')


def selection(count, first=1, last=None, step=1):
    if first < 1 or step < 1 or (last is not None and last < first):
        raise ValueError('帧范围无效')
    return range(first - 1, min(last if last is not None else count, count), step)


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def output_folders(sources, destination):
    destination = Path(destination).resolve()
    reserved = set()
    result = []
    for source in sources:
        source = Path(source).resolve()
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', source.stem).strip(' .')[:100] or 'NEV'
        if stem.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
            stem = '_' + stem
        token = hashlib.sha256(canonical(source).encode('utf-8')).hexdigest()[:8]
        candidate = destination / stem
        prior = destination / f'{stem}_{token}'
        try:
            if json.loads((prior / '.nraw-to-nef' / 'source.json').read_text(encoding='utf-8'))['source'] == canonical(source):
                candidate = prior
        except (OSError, ValueError, KeyError):
            pass
        sequence = 0
        while True:
            owner = candidate / '.nraw-to-nef' / 'source.json'
            collision = canonical(candidate) in reserved
            if candidate.exists():
                try:
                    collision = collision or json.loads(owner.read_text(encoding='utf-8'))['source'] != canonical(source)
                except (OSError, ValueError, KeyError):
                    collision = True
            if not collision:
                break
            sequence += 1
            candidate = destination / f'{stem}_{token}' if sequence == 1 else destination / f'{stem}_{token}_{sequence}'
        reserved.add(canonical(candidate))
        result.append(candidate)
    return result


@contextlib.contextmanager
def folder_lock(directory):
    import msvcrt
    path = Path(directory) / 'write.lock'
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('输出文件夹正在被使用') from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def worker(job_path):
    job_path = Path(job_path).resolve()
    job = json.loads(job_path.read_text(encoding='utf-8'))
    status_path = job_path.parent / 'status.json'
    cancel = job_path.parent / 'cancel'
    temporary = None
    done = written = skipped = 0
    started = time.monotonic()

    def emit(state, **values):
        atomic_json(status_path, dict(state=state, done=done, written=written, skipped=skipped,
                                     seconds=round(time.monotonic() - started, 3), stamp=time.time_ns(), **values))

    try:
        emit('loading')
        for key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
            os.environ[key] = '1'
        import nraw_to_nef as engine
        from raw_win import Decoder
        source = Path(job['source']).resolve()
        template = Path(job['template']).resolve()
        output = Path(job['output']).resolve()
        engine.EXIFTOOL = job['exiftool']
        engine.PERL = job.get('perl', 'perl')
        if source == template or source.suffix.lower() != '.nev' or template.suffix.lower() != '.nef':
            raise ValueError('请选择 NEV 视频和 NEF 模板')
        if digest(job['sdk']) != SDK_SHA256:
            raise ValueError('解码器版本不匹配')
        source_stat = source.stat()
        source_identity = dict(source=canonical(source), size=source_stat.st_size, mtime_ns=source_stat.st_mtime_ns)
        template_stat = template.stat()
        template_sha = digest(template)
        samples, timescale, timing = engine.index_nev(source)
        frames = selection(len(samples), job['first'], job['last'], job['step'])
        if not frames:
            emit('complete', total=0)
            return 0
        info = engine.clip_metadata(source)
        with engine.tifffile.TiffFile(template) as tiff:
            if tiff.byteorder != '<' or tiff.pages[0].tags[272].value != info['Model']:
                raise ValueError('NEF 模板与视频机型不一致')
            page = next(p for p in tiff.pages[0].pages if p.tags.get(262) and p.tags[262].value == 32803)
            worst = int(page.shape[0]) * int(page.shape[1]) * 22 // 8 + page.tags[273].value[0] + 1024 * 1024
        output.mkdir(parents=True, exist_ok=True)
        metadata = output / '.nraw-to-nef'
        metadata.mkdir(exist_ok=True)
        with folder_lock(metadata):
            source_record = metadata / 'source.json'
            if source_record.exists():
                previous = json.loads(source_record.read_text(encoding='utf-8'))
                if previous != source_identity:
                    raise ValueError('源视频已变化，请选择新的输出目录')
            else:
                if any(output.glob('*.[Nn][Ee][Ff]')):
                    raise ValueError('输出目录含有其他 NEF')
                atomic_json(source_record, source_identity)
            for abandoned in output.iterdir():
                if re.fullmatch(r'\.frame_\d{6,}_[0-9a-f]{32}\.partial\.NEF(?:_original)?', abandoned.name):
                    abandoned.unlink()
            receipts = metadata / 'frames'
            receipts.mkdir(exist_ok=True)
            decoder = None
            with source.open('rb') as stream:
                for frame in frames:
                    if cancel.exists():
                        emit('cancelled', total=len(frames))
                        return 0
                    current_source = source.stat()
                    current_template = template.stat()
                    if (current_source.st_size, current_source.st_mtime_ns) != (source_stat.st_size, source_stat.st_mtime_ns):
                        raise ValueError('转换时源视频发生变化')
                    if (current_template.st_size, current_template.st_mtime_ns) != (template_stat.st_size, template_stat.st_mtime_ns):
                        raise ValueError('转换时 NEF 模板发生变化')
                    target = output / f'frame_{frame + 1:06d}.NEF'
                    receipt_path = receipts / f'{frame + 1:06d}.json'
                    offset, length = samples[frame]
                    stream.seek(offset)
                    sample = stream.read(length)
                    if len(sample) != length:
                        raise ValueError('视频帧不完整')
                    signature = dict(source=source_identity, sample_sha256=hashlib.sha256(sample).hexdigest(),
                                     template_sha256=template_sha, sdk_sha256=SDK_SHA256, version=VERSION, frame=frame)
                    if target.exists():
                        try:
                            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
                            valid = receipt['input'] == signature and receipt['output_sha256'] == digest(target)
                        except (OSError, ValueError, KeyError):
                            valid = False
                        if not valid:
                            raise ValueError(f'{target.name} 已存在且校验记录不匹配')
                        skipped += 1
                    else:
                        if shutil.disk_usage(output).free < worst + 256 * 1024 ** 2:
                            raise OSError('输出磁盘空间不足')
                        emit('decoding', total=len(frames), frame=frame + 1)
                        if decoder is None:
                            decoder = Decoder(job['sdk'], max(1, min(int(job.get('threads', 1)), 8)))
                        raw, depth = decoder.decode(sample)
                        if depth != 14:
                            raise ValueError(f'不支持的解码位深：{depth}')
                        meta = engine.frame_metadata(sample[:1024])
                        temporary = output / f'.frame_{frame + 1:06d}_{uuid.uuid4().hex}.partial.NEF'
                        emit('writing', total=len(frames), frame=frame + 1)
                        result = engine.write_nef(raw, meta, info, template, temporary, source.name, frame,
                                                  elapsed=engine.frame_elapsed(frame, timescale, timing))
                        receipt = dict(input=signature, output_sha256=digest(temporary),
                                       canvas_sha256=result['canvas_sha256'], source_mosaic_sha256=result['source_mosaic_sha256'],
                                       roundtrip_exact=result['roundtrip_exact'], bytes=temporary.stat().st_size,
                                       iso=meta['iso'], fnumber=meta['fnumber'], exposure_seconds=meta['shutter'],
                                       exif_verified=result['exif_inheritance']['verified'])
                        atomic_json(receipt_path, receipt)
                        temporary.rename(target)
                        temporary = None
                        written += 1
                    done += 1
                    emit('progress', total=len(frames), frame=frame + 1)
            emit('complete', total=len(frames))
        return 0
    except Exception as exc:
        translations = {
            'Template must use Nikon 14-bit lossless F0 compression': '模板需为 14 位无损压缩 NEF',
            'Template lens differs from source; lens correction metadata cannot be borrowed': 'NEF 模板与视频镜头不一致',
            'Different sampling scale/FOV requires an explicit lens-coordinate transform; centre padding alone is not valid': '视频采样模式与 NEF 模板不兼容',
            'Decoded frame does not fit the same-camera still container': '视频尺寸与 NEF 模板不兼容',
        }
        emit('failed', error=translations.get(str(exc), str(exc))[:4000])
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + '_original').unlink(missing_ok=True)
