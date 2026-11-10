import os
import hashlib
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs, copy_metadata

root = Path(SPECPATH)
exiftool = Path(os.environ.get('EXIFTOOL_PACKAGE', root / 'tools' / 'exiftool'))
licenses = Path(os.environ.get('APP_LICENSES', root / 'licenses'))
decoder = Path(os.environ.get('APP_DECODER_FILE', root / 'tools' / 'nraw' / 'REDR3D-x64.dll'))
if not decoder.is_file() or hashlib.sha256(decoder.read_bytes()).hexdigest() != 'a3c3039647716ebfb254898568dbb0954925e35dcb76a48b24cc58af36ca4b31':
    raise ValueError('The verified decoder runtime is required for a complete portable build')

datas = [(str(root / 'EULA.txt'), '.'), (str(exiftool), 'tools/exiftool'), (str(licenses), 'licenses'), (str(root / 'app.ico'), '.')]
for package in ('numpy', 'Pillow', 'rawpy', 'tifffile'):
    datas += copy_metadata(package)

analysis = Analysis(
    [str(root / 'gui.py')],
    pathex=[str(root)],
    binaries=collect_dynamic_libs('rawpy') + [(str(decoder), 'tools/nraw')],
    datas=datas,
    hiddenimports=['batch', 'nraw_to_nef', 'standalone_nef', 'runtime_paths', 'raw_win', 'nev', 'nikon_lossless', 'lens_crop', 'exif_inheritance'],
    excludes=['matplotlib', 'scipy', 'pandas', 'torch', 'IPython', 'pytest', 'numba', 'imagecodecs',
              'zarr', 'fsspec', 'dask', 'cupy', 'cv2', 'skimage', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'notebook',
              'lxml', 'PIL.AvifImagePlugin', 'PIL.WebPImagePlugin', 'PIL.Jpeg2KImagePlugin',
              'PIL.ImageCms', 'PIL.ImageFont', 'PIL.ImageQt', 'PIL.ImageTk', 'PIL._avif', 'PIL._webp'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name='NRAW-to-NEF',
    console=False,
    debug=False,
    strip=False,
    upx=False,
    icon=str(root / 'app.ico'),
)
collection = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='NRAW-to-NEF')
