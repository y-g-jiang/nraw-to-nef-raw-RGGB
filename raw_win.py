import ctypes as C
import hashlib
import os
from pathlib import Path

import numpy as np


SDK_SHA256 = "a3c3039647716ebfb254898568dbb0954925e35dcb76a48b24cc58af36ca4b31"


class Decoder:
    def __init__(self, sdk, threads=8):
        path = Path(sdk).resolve()
        if hashlib.sha256(path.read_bytes()).hexdigest() != SDK_SHA256:
            raise ValueError("Unsupported SDK build")
        self.search = os.add_dll_directory(str(path.parent))
        self.dll = C.CDLL(str(path))
        self.base = self.dll._handle
        self.fn = lambda r, ret, *args: C.CFUNCTYPE(ret, *args)(self.base + r)
        P, I, Z = C.c_void_p, C.c_int, C.c_size_t
        if self.fn(0x3158F0, C.c_ubyte, C.c_uint)(threads) != 1:
            raise RuntimeError("SDK initialization failed")
        state = self.fn(0x315860, P)()
        self.format = C.c_void_p.from_address(state + 8).value
        self.decoder = C.c_void_p.from_address(state + 16).value
        self.inp = self.fn(0x1150, P)()
        self.out = self.fn(0x1150, P)()
        self.opt = self.fn(0x1670, P)()
        self.parse = self.fn(0x31B0, I, P, Z, P, P)
        self.configure = self.fn(0x26C0, I, P, P, P, P, P)
        self.width = self.fn(0x11B0, I, P)
        self.height = self.fn(0x11C0, I, P)
        self.depth = self.fn(0x1080, I, P)
        self.decode_fn = self.fn(0x3210, I, P, P, P, Z, P, Z, P, P, P)
        self.raw = None

    def decode(self, sample):
        if sample[4:8] != b"NRAW" or sample[1024:1026] != b"\xff\x10":
            raise ValueError("Unexpected NRAW sample")
        payload = C.create_string_buffer(sample[1024:])
        size = len(sample) - 1024
        result = self.parse(self.decoder, size, payload, self.inp)
        if result != 0:
            raise RuntimeError(("parse", result))
        result = self.configure(self.decoder, self.format, self.opt, self.inp, self.out)
        if result != 0:
            raise RuntimeError(("configure", result))
        width, height, depth = self.width(self.out), self.height(self.out), self.depth(self.inp)
        if not (0 < width < 20000 and 0 < height < 12000):
            raise ValueError((width, height))
        if self.raw is None or self.raw.shape != (height, width):
            self.storage = np.empty(width * height * 2 + 128, dtype=np.uint8)
            skip = (-self.storage.ctypes.data) % 64
            self.raw = self.storage[skip:skip + width * height * 2].view("<u2").reshape(height, width)
        result = self.decode_fn(self.decoder, self.opt, self.format, size, payload, self.raw.nbytes, self.raw.ctypes.data, None, None)
        if result != 0:
            raise RuntimeError(("decode", result))
        return self.raw, depth
