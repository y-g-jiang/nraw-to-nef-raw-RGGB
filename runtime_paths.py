from pathlib import Path
import sys


def resources():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def bundled_decoder():
    return resources() / 'tools' / 'nraw' / 'REDR3D-x64.dll'


def bundled_exiftool():
    return resources() / 'tools' / 'exiftool' / 'exiftool.exe'


def runtime_files():
    decoder, exiftool = bundled_decoder(), bundled_exiftool()
    missing = [path.name for path in (decoder, exiftool) if not path.is_file()]
    if missing:
        raise FileNotFoundError('程序文件不完整，请重新解压完整便携包：' + '、'.join(missing))
    return decoder, exiftool
