from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime_paths import runtime_files


class RuntimePathsTests(unittest.TestCase):
    def test_relocated_bundle_needs_no_external_configuration(self):
        with tempfile.TemporaryDirectory(prefix='portable_') as directory:
            root = Path(directory) / '全新用户 空格' / '_internal'
            decoder = root / 'tools/nraw/REDR3D-x64.dll'
            exiftool = root / 'tools/exiftool/exiftool.exe'
            for path in (decoder, exiftool):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'test fixture')
            with patch.object(sys, '_MEIPASS', str(root), create=True):
                with patch.dict('os.environ', {'PATH': '', 'NRAW_SDK': 'missing.dll', 'EXIFTOOL': 'missing.exe'}):
                    self.assertEqual(runtime_files(), (decoder, exiftool))

    def test_incomplete_bundle_is_reported_without_external_search(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, '_MEIPASS', directory, create=True):
                with self.assertRaisesRegex(FileNotFoundError, '重新解压完整便携包'):
                    runtime_files()


if __name__ == '__main__':
    unittest.main()
