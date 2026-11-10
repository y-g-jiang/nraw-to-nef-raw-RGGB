import binascii
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np
import rawpy
import tifffile

from nraw_to_nef import ifd_entries, tag_payload
from standalone_nef import geometry, write_container


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.meta = dict(black=[500, 600, 700, 800], wb=[900, 512, 700, 512, 512, 512, 512, 512],
                         crop=[6, 4, 500, 500], iso=800, maker={})
        self.raw = np.random.default_rng(5).integers(0, 16384, size=(512, 512), dtype=np.uint16)
        self.raw[:2, :2] = [[0, 16383], [16383, 0]]

    def test_pixels_and_private_blocks_without_donor(self):
        before = self.raw.copy()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'standalone.NEF'
            write_container(self.raw, self.meta, dict(Model='NIKON Z 8'), output)
            with rawpy.imread(str(output)) as decoded:
                np.testing.assert_array_equal(decoded.raw_image, self.raw)
                self.assertEqual(decoded.black_level_per_channel, [500, 600, 800, 700])
                np.testing.assert_allclose(decoded.camera_whitebalance, [900/512, 1, 700/512, 1])
            with tifffile.TiffFile(output) as tiff:
                self.assertNotIn(50706, tiff.pages[0].tags)
                raw = tiff.pages[0].pages[1]
                self.assertEqual(raw.tags[259].value, 34713)
                private = raw.tags[51157].value
                entries = ifd_entries(private, 18)
                self.assertEqual(set(entries), set(range(9, 16)))
                for entry in entries.values():
                    at, size = tag_payload(private, entry, 10)
                    data = private[at:at + size]
                    self.assertEqual(binascii.crc_hqx(data[:-2], 0xffff), struct.unpack('<H', data[-2:])[0])
                at, size = tag_payload(private, entries[9], 10)
                self.assertEqual(size, 36)
                self.assertEqual(private[at + 32:at + 34], bytes(2))
                at, size = tag_payload(private, entries[14], 10)
                self.assertEqual(size, 52)
                self.assertEqual(private[at + 4:at + 6], b'F0')
                self.assertEqual(struct.unpack_from('<H', private, at + 14)[0], 34)
        np.testing.assert_array_equal(self.raw, before)

    def test_invalid_source_rejected_before_file_write(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'invalid.NEF'
            for raw, meta in ((self.raw[:, :-1], self.meta),
                              (self.raw, {**self.meta, 'crop': [6, 4, 512, 512]}),
                              (self.raw, {**self.meta, 'wb': [0] * 8}),
                              (self.raw, {**self.meta, 'black': [20000] * 4})):
                with self.assertRaises(ValueError):
                    write_container(raw, meta, dict(Model='NIKON Z 8'), output)
                self.assertFalse(output.exists())

    def test_unknown_geometry_does_not_borrow_other_camera(self):
        with self.assertRaises(ValueError):
            geometry('NIKON UNKNOWN')


if __name__ == '__main__':
    unittest.main()
