import struct
import unittest
from io import BytesIO

import numpy as np
from PIL import Image

from nraw_to_nef import replace_previews


class PreviewTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(20261107)
        self.image = Image.fromarray(rng.integers(0, 256, (192, 256, 3), dtype=np.uint8))
        stream = BytesIO()
        Image.new('RGB', self.image.size, 'black').save(stream, format='JPEG', quality=90, optimize=True)
        self.old_jpeg = stream.getvalue()

    def fixture(self, reserved, base=0):
        start, count_at, offset_at = 128, 8, 12
        data = bytearray(start+reserved)
        data[start:start+len(self.old_jpeg)] = self.old_jpeg
        struct.pack_into('<II', data, count_at, reserved, start-base)
        return data, (start, reserved, count_at, offset_at, base)

    def check_preview(self, blob, slot, receipt):
        start, reserved, count_at, offset_at, base = slot
        count, = struct.unpack_from('<I', blob, count_at)
        offset, = struct.unpack_from('<I', blob, offset_at)
        target = offset+base
        self.assertEqual(count, receipt['bytes'])
        with Image.open(BytesIO(blob[target:target+count])) as image:
            self.assertEqual(image.size, self.image.size)
            image.load()
            self.assertGreater(np.asarray(image).mean(), 80)
        return target

    def test_fits_absolute_and_relative(self):
        for base in (0, 48):
            with self.subTest(base=base):
                data, slot = self.fixture(200000, base)
                raw = b'RAW payload sentinel'
                receipt, tail = replace_previews(data, [slot], self.image, len(data)+len(raw))
                self.assertEqual(tail, b'')
                self.assertFalse(receipt[0]['relocated'])
                self.assertEqual(receipt[0]['quality'], 90)
                self.assertEqual(self.check_preview(data, slot, receipt[0]), slot[0])
                self.assertFalse(any(data[slot[0]+receipt[0]['bytes']:]))

    def test_overflow_absolute_and_relative(self):
        for base in (0, 48):
            with self.subTest(base=base):
                data, slot = self.fixture(len(self.old_jpeg), base)
                prefix_length = len(data)
                raw = b'RAW payload sentinel'
                end = prefix_length+len(raw)
                if end%2 == 0:
                    raw += b'!'
                    end += 1
                receipt, tail = replace_previews(data, [slot], self.image, end)
                blob = data+raw+tail
                target = self.check_preview(blob, slot, receipt[0])
                self.assertTrue(receipt[0]['relocated'])
                self.assertGreaterEqual(target, end)
                self.assertEqual(target%2, 0)
                self.assertEqual(blob[prefix_length:end], raw)
                self.assertFalse(any(data[slot[0]:]))

    def test_shared_old_preview_has_valid_independent_pointers(self):
        data, first = self.fixture(len(self.old_jpeg))
        start, reserved, _, _, _ = first
        second = (start, reserved, 24, 28, 48)
        struct.pack_into('<II', data, 24, reserved, start-48)
        raw = b'unchanged RAW bytes'
        end = len(data)+len(raw)
        receipt, tail = replace_previews(data, [first, second], self.image, end)
        blob = data+raw+tail
        targets = [self.check_preview(blob, slot, item) for slot, item in zip((first, second), receipt)]
        self.assertTrue(all(target%2 == 0 for target in targets))
        self.assertGreaterEqual(targets[1], targets[0]+receipt[0]['bytes'])

    def test_invalid_preview_bounds(self):
        data, slot = self.fixture(len(self.old_jpeg))
        before = bytes(data)
        for start, reserved in [(-1, 1), (len(data), 1), (128, 0), (128, len(data))]:
            with self.subTest(start=start, reserved=reserved):
                with self.assertRaisesRegex(ValueError, 'preview bounds'):
                    replace_previews(data, [(start, reserved, 8, 12, 0)], self.image, len(data))
        self.assertEqual(data, before)

    def test_invalid_tag_bounds(self):
        data, slot = self.fixture(len(self.old_jpeg))
        for count_at, offset_at in [(-1, 12), (8, len(data)-2)]:
            with self.subTest(count_at=count_at, offset_at=offset_at):
                with self.assertRaisesRegex(ValueError, 'tag bounds'):
                    replace_previews(data, [(slot[0], slot[1], count_at, offset_at, 0)], self.image, len(data))

    def test_rejects_overflow_inside_prefix(self):
        data, slot = self.fixture(len(self.old_jpeg))
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            replace_previews(data, [slot], self.image, len(data)-1)

    def test_rejects_unrepresentable_tiff_offsets(self):
        for end in (2**32, 2**32-2):
            with self.subTest(end=end):
                data, slot = self.fixture(len(self.old_jpeg))
                with self.assertRaisesRegex(ValueError, 'TIFF offset limits'):
                    replace_previews(data, [slot], self.image, end)


if __name__ == '__main__':
    unittest.main()
