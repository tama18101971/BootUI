import importlib.util
import struct
import sys
from pathlib import Path

import proteus32_protocol as proto

BOOTUI_ROOT = Path(__file__).resolve().parents[1]
BOOTLOADER_CLI = BOOTUI_ROOT.parent / "Proteus32-BootLoader" / "proteus32_3.py"

if str(BOOTUI_ROOT) not in sys.path:
    sys.path.insert(0, str(BOOTUI_ROOT))


def read_frame(data):
    """Build a TZ 3.5.4 READ answer: AA 55 84 00 <64B data> CK 55 AA."""
    ck = (proto.CMD_IAP_READ + 0x00 + sum(data)) & 0xFFFF
    return bytes([0xAA, 0x55, proto.CMD_IAP_READ, 0x00]) + bytes(data) + bytes(
        [ck & 0xFF, (ck >> 8) & 0xFF, 0x55, 0xAA]
    )


def read_frame_echo(data, addr):
    """READ answer variant with a 4-byte address echo before the data."""
    frame = read_frame(data)
    return frame[:4] + struct.pack("<I", addr) + frame[4:]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeSerial:
    """Minimal pyserial stand-in: delivers canned responses on write().

    responses items are consumed per write() call; a callable item receives
    the written packet and returns the bytes to append to the RX buffer.
    read() is capped by chunk_size to emulate split USB-serial delivery.
    """

    def __init__(self, responses=None, rx=None, chunk_size=None):
        self.rx = bytearray(rx or b"")
        self.writes = []
        self.responses = list(responses or [])
        self.chunk_size = chunk_size

    @property
    def in_waiting(self):
        return len(self.rx)

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if self.responses:
            item = self.responses.pop(0)
            if callable(item):
                item = item(data)
            self.rx.extend(item)

    def read(self, n=1):
        n = min(n, len(self.rx))
        if self.chunk_size is not None:
            n = min(n, self.chunk_size)
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def reset_input_buffer(self):
        self.rx.clear()

    def reset_output_buffer(self):
        pass

    def flush(self):
        pass

    def close(self):
        pass
