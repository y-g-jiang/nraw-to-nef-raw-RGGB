import struct
import numpy as np

def encode(raw, seeds=None):
    if raw.dtype != np.uint16 or raw.ndim != 2 or min(raw.shape) < 2:
        raise ValueError('Expected a uint16 Bayer plane, at least 2 x 2')
    if raw.shape[1] % 2 or int(raw.max()) >= 16384:
        raise ValueError('Nikon lossless mode requires even width and 14-bit values')
    seeds = raw[:2, :2].ravel().tolist() if seeds is None else list(seeds)
    if len(seeds) != 4 or not all(0 <= int(v) < 16384 for v in seeds):
        raise ValueError('Expected four 14-bit predictor seeds')
    counts = (0,1,4,2,2,3,1,2)
    categories = (7,6,8,5,9,4,10,3,11,12,2,0,1,13,14)
    code = at = 0
    huff = {}
    for length, count in enumerate(counts, 1):
        for _ in range(count):
            huff[categories[at]] = (code, length)
            at += 1
            code += 1
        code <<= 1

    lut_code = np.empty(32767, dtype=np.uint32)
    lut_len = np.empty(32767, dtype=np.uint8)
    for delta in range(-16383, 16384):
        category = abs(delta).bit_length()
        prefix, length = huff[category]
        amplitude = delta if delta >= 0 else delta + (1 << category) - 1
        lut_code[delta + 16383] = (prefix << category) | amplitude
        lut_len[delta + 16383] = length + category
    output = bytearray()
    carry = np.empty(0, dtype=np.uint8)
    positions = np.arange(22, dtype=np.uint32)[None, :]
    for top in range(0, raw.shape[0], 32):
        bottom = min(top + 32, raw.shape[0])
        block = raw[top:bottom].astype(np.int32)
        diff = np.empty_like(block)
        diff[:, 2:] = block[:, 2:] - block[:, :-2]
        for local, row in enumerate(range(top, bottom)):
            prev = raw[row-2, :2].astype(np.int32) if row >= 2 else np.array(seeds[2*row:2*row+2], dtype=np.int32)
            diff[local, :2] = block[local, :2] - prev
        indices = diff.ravel() + 16383
        lengths = lut_len[indices].astype(np.uint32)[:, None]
        codes = lut_code[indices][:, None]
        keep = positions < lengths
        shifts = np.maximum(lengths.astype(np.int32) - 1 - positions.astype(np.int32), 0).astype(np.uint32)
        bits = ((codes >> shifts) & 1).astype(np.uint8)[keep]
        if carry.size:
            bits = np.concatenate((carry, bits))
        complete = (bits.size // 8) * 8
        output += np.packbits(bits[:complete]).tobytes()
        carry = bits[complete:].copy()
    if carry.size:
        output += np.packbits(carry).tobytes()

    output += b'\0' * 4
    metadata = b'F0' + struct.pack('<4HH', *seeds, 0)
    return bytes(output), metadata
