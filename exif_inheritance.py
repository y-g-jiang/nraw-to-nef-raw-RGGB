import datetime as dt
import math
import struct
from fractions import Fraction

CLIP_TAGS = (
    'Make','Model','Software','DateTimeOriginal','CreateDate','LensModel','LensMake',
    'LensSerialNumber','SerialNumber','FocusDistance','LensInfo','FocalLength',
    'ExposureProgram','ExposureMode','MeteringMode','ExposureCompensation',
    'FocalLengthIn35mmFormat','WhiteBalance','FocusMode','WhiteBalanceFineTune',
    'ShutterCount','HighISONoiseReduction','LongExposureNoiseReduction',
    'ActiveD-Lighting','ISOExpansion','ISOExpansion2','TimeZone','DaylightSavings',
    'DateDisplayFormat','Artist','Copyright','OwnerName','ImageDescription',
    'UserComment','Rating','PictureControlName',
    'GPSLatitude','GPSLatitudeRef','GPSLongitude','GPSLongitudeRef',
    'GPSAltitude','GPSAltitudeRef','GPSDateStamp','GPSTimeStamp','GPSMapDatum',
    'GPSHPositioningError','GPSDOP','GPSStatus','GPSMeasureMode',
)

def source_info(grouped, source):

    values={}; origins={}
    for name in CLIP_TAGS:
        for group in ('Nikon','ExifIFD','IFD0','GPS','XMP-exif','XMP-tiff','XMP-dc','XMP-xmp'):
            key=f'{group}:{name}'
            if key in grouped:
                value=grouped[key]
                if isinstance(value,str):value=value.strip(' \0')
                values[name]=value;origins[name]=key;break
    values['_origins']=origins
    values['_source']=str(source)
    return values

def frame_elapsed(frame, timescale, timing):
    remaining=frame;ticks=0
    if timescale<=0:raise ValueError('Invalid NRAW time scale')
    for count,delta in timing:
        used=min(remaining,count);ticks+=used*delta;remaining-=used
        if not remaining:return Fraction(ticks,timescale)
    raise ValueError('Frame timestamp is outside stts')

def _number(value):
    if isinstance(value,Fraction):return f'{value.numerator}/{value.denominator}'
    if isinstance(value,(list,tuple)):return ' '.join(_number(x) for x in value)
    if isinstance(value,float):
        if not math.isfinite(value):raise ValueError('Non-finite EXIF value')
        return format(value,'.12g')
    return str(value)

def metadata_plan(meta, info, source_name, frame, elapsed=None):

    tags={};origins={}
    def put(tag,value,origin):
        if value is not None:
            tags[tag]=value;origins[tag]=origin
    def clip(dest,name):put(dest,info.get(name),info.get('_origins',{}).get(name,'clip:'+name))
    clip('IFD0:Make','Make');clip('IFD0:Model','Model')
    put('IFD0:Software','NRAW to NEF 1.1','conversion software')
    for dest,name in [
        ('ExifIFD:ExposureProgram','ExposureProgram'),('ExifIFD:ExposureMode','ExposureMode'),
        ('ExifIFD:MeteringMode','MeteringMode'),('ExifIFD:LensModel','LensModel'),
        ('ExifIFD:LensMake','LensMake'),('ExifIFD:LensSerialNumber','LensSerialNumber'),
        ('ExifIFD:LensInfo','LensInfo'),('ExifIFD:SerialNumber','SerialNumber'),
        ('Nikon:SerialNumber','SerialNumber'),('ExifIFD:SubjectDistance','FocusDistance'),
        ('IFD0:Artist','Artist'),('IFD0:Copyright','Copyright'),('ExifIFD:OwnerName','OwnerName'),
        ('ExifIFD:UserComment','UserComment'),('XMP-xmp:Rating','Rating'),
        ('Nikon:WhiteBalance','WhiteBalance'),('Nikon:FocusMode','FocusMode'),
        ('Nikon:WhiteBalanceFineTune','WhiteBalanceFineTune'),('Nikon:ShutterCount','ShutterCount'),
        ('Nikon:HighISONoiseReduction','HighISONoiseReduction'),
        ('Nikon:LongExposureNoiseReduction','LongExposureNoiseReduction'),
        ('Nikon:ActiveD-Lighting','ActiveD-Lighting'),('Nikon:ISOExpansion','ISOExpansion'),
        ('Nikon:ISOExpansion2','ISOExpansion2'),
    ]:clip(dest,name)

    exposure=float(Fraction(meta['shutter']).limit_denominator(1_000_000))
    put('ExifIFD:ISO',meta['iso'],'current frame nominal ISO')
    iso_block=meta['maker'].get(0x25)
    for tag,offset in [('Nikon:ISO',0),('Nikon:ISO2',6)]:
        if iso_block and len(iso_block[2])>offset:

            private_iso=100*2**(iso_block[2][offset]/12-5)
            put(tag,private_iso,'current frame Nikon ISOInfo actual encoded readout')
        else:
            encoded=round((math.log2(meta['iso']/100)+5)*12)
            put(tag,100*2**(encoded/12-5),'current frame ISO converted to Nikon ISOInfo 1/12 EV representation')
    for tag in ['ExifIFD:ExposureTime','ExifIFD:ShutterSpeedValue']:put(tag,exposure,'current frame exposure time')
    for tag in ['ExifIFD:FNumber','ExifIFD:ApertureValue']:put(tag,meta['fnumber'],'current frame aperture')
    put('ExifIFD:FocalLength',meta['focal_length'],'current frame focal length')
    put('ExifIFD:ExifImageWidth',meta['crop'][2],'current frame active width; TIFF sensor canvas unchanged')
    put('ExifIFD:ExifImageHeight',meta['crop'][3],'current frame active height; TIFF sensor canvas unchanged')
    frame_bias=meta.get('exposure_compensation')
    put('ExifIFD:ExposureCompensation',frame_bias if frame_bias is not None else info.get('ExposureCompensation'),
        'current frame exposure compensation' if frame_bias is not None else 'clip exposure compensation')
    if 'orientation' in meta:put('IFD0:Orientation',meta['orientation'],'current frame orientation')

    for tag,maker in [('Nikon:WhiteBalance',5),('Nikon:FocusMode',7)]:
        if maker in meta['maker']:
            put(tag,meta['maker'][maker][2].decode('ascii').strip(' \0'),'current frame MakerNote')
    if 0x3f in meta['maker']:
        values=struct.unpack('<4i',meta['maker'][0x3f][2])
        put('Nikon:WhiteBalanceFineTune',[values[0]/values[1],values[2]/values[3]],'current frame MakerNote')
    if 'Nikon:WhiteBalance' in tags:
        put('ExifIFD:WhiteBalance',0 if tags['Nikon:WhiteBalance'].upper().startswith('AUTO') else 1,'current frame WB mode')

    timestamp={'source_start':info.get('DateTimeOriginal'),'elapsed_seconds':float(elapsed) if elapsed is not None else None,
               'source_precision':'whole second unless a finer source timestamp is supplied',
               'method':'clip camera start plus sample PTS; no invented EXIF subseconds'}
    date=info.get('DateTimeOriginal')
    if date:
        if elapsed is not None:
            start=dt.datetime.strptime(date,'%Y:%m:%d %H:%M:%S')
            date=(start+dt.timedelta(seconds=elapsed.numerator//elapsed.denominator)).strftime('%Y:%m:%d %H:%M:%S')
        for tag in ['ExifIFD:DateTimeOriginal','ExifIFD:CreateDate','IFD0:ModifyDate']:
            put(tag,date,'clip camera time + whole-second frame PTS' if elapsed is not None else 'clip camera time')
    timestamp['written_time']=date
    zone=info.get('_timezone_offset')
    if zone:
        for tag in ['ExifIFD:OffsetTimeOriginal','ExifIFD:OffsetTimeDigitized','ExifIFD:OffsetTime']:
            put(tag,zone,'clip NCTG explicit UTC offset')
    for name in CLIP_TAGS:
        if name.startswith('GPS') and info.get(name) is not None:clip('GPS:'+name,name)
    clip('IFD0:ImageDescription','ImageDescription')
    put('XMP-dc:Source',f'{source_name}#{frame}','source clip/frame identity')
    put('XMP-xmp:CreatorTool','NRAW to NEF 1.1','conversion software')
    clears=set(tags)

    clears.update([
        'IFD0:Artist','IFD0:Copyright','IFD0:ImageDescription','ExifIFD:OwnerName','ExifIFD:UserComment',
        'ExifIFD:DateTimeOriginal','ExifIFD:CreateDate','IFD0:ModifyDate',
        'ExifIFD:SubSecTime','ExifIFD:SubSecTimeOriginal','ExifIFD:SubSecTimeDigitized',
        'ExifIFD:OffsetTime','ExifIFD:OffsetTimeOriginal','ExifIFD:OffsetTimeDigitized',
        'ExifIFD:LensModel','ExifIFD:LensMake','ExifIFD:LensSerialNumber','ExifIFD:LensInfo',
        'ExifIFD:SerialNumber','ExifIFD:SubjectDistance','ExifIFD:FocalLengthIn35mmFormat',
        'ExifIFD:ExposureProgram','ExifIFD:ExposureMode','ExifIFD:MeteringMode',
        'ExifIFD:LightSource','ExifIFD:ImageUniqueID',
        'XMP-exif:ISO','XMP-exif:ExposureTime','XMP-exif:FNumber',
        'XMP-exif:DateTimeOriginal','XMP-exifEX:PhotographicSensitivity','GPS:all',
    ])

    if info.get('FocalLengthIn35mmFormat') is not None and info.get('FocalLength')==meta['focal_length']:
        clip('ExifIFD:FocalLengthIn35mmFormat','FocalLengthIn35mmFormat')
    return dict(tags=tags,origins=origins,clear_tags=sorted(clears-set(tags)),timestamp=timestamp)

def write_arguments(plan):
    return [f'-{tag}=' for tag in plan['clear_tags']]+[
        f'-{tag}#={_number(value)}' for tag,value in plan['tags'].items()]

def verify_readback(plan, grouped):
    checks=[]
    for tag,expected in plan['tags'].items():
        actual=grouped.get(tag)
        if isinstance(expected,Fraction):expected=float(expected)
        if isinstance(expected,(int,float)):
            try:good=math.isclose(float(actual),float(expected),rel_tol=2e-6,abs_tol=2e-7)
            except (ValueError,TypeError):good=False
        elif isinstance(expected,(list,tuple)):
            try:good=all(math.isclose(float(a),float(b),rel_tol=2e-6,abs_tol=2e-7) for a,b in zip(str(actual).split(),expected)) and len(str(actual).split())==len(expected)
            except (ValueError,TypeError):good=False
        else:good=str(actual).strip(' \0')==str(expected).strip(' \0')
        checks.append(dict(tag=tag,expected=expected,actual=actual,matched=good,source=plan['origins'][tag]))
    for tag in ['ExifIFD:SubSecTimeOriginal','ExifIFD:SubSecTimeDigitized','ExifIFD:SubSecTime']:
        checks.append(dict(tag=tag,expected=None,actual=grouped.get(tag),matched=tag not in grouped,
                           source='donor subsecond removed; source recording start has whole-second precision'))
    failed=[x for x in checks if not x['matched']]
    if failed:raise RuntimeError('EXIF inheritance readback mismatch: '+repr(failed))
    return checks
