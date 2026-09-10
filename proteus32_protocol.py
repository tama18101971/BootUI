"""Shared PROTEUS32 protocol layer (revision 2, BOOTLOADER_TZ.md v1.0).

Single source of truth for the frame format, statuses, timeouts and
transport rules used by both clients in this repository:
bootloader_gui.py (GUI) and proteus32_3.py (CLI).

Keeps the copies from drifting: the READ-frame parsing fix originally
landed in only one of the scripts, and the END-ACK timeout diverged from
TIMEOUT_CMD in the other. Protocol changes must be made here and here only.

PROTOCOL_REV = 2: works ONLY with bootloader revision 2 (iap.c/h v2.0.0).
Incompatible with the old bootloader (v1): VERIFY-with-data and READ are
not supported there, and its END writes the magic word 0x5AA55AA5
(WL damage). Keep clients and bootloader in lockstep.
"""

import struct
import time

import serial

PROTOCOL_REV = 2

SYNC_HEAD1 = 0xAA
SYNC_HEAD2 = 0x55

CMD_IAP_PROM   = 0x80
CMD_IAP_ERASE  = 0x81
CMD_IAP_VERIFY = 0x82
CMD_IAP_END    = 0x83
CMD_IAP_READ   = 0x84

ST_OK         = 0x00
ST_ERR        = 0x01
ST_ERR_RANGE  = 0x02
ST_ERR_VERIFY = 0x03
ST_DICT = {
    ST_OK:         "OK",
    ST_ERR:        "ошибка выполнения команды",
    ST_ERR_RANGE:  "нарушение границ app-области (защита WL)",
    ST_ERR_VERIFY: "VERIFY: несовпадение данных",
}

FLASH_BASE = 0x08000000
FLASH_APP_END = 0x08003F00   # 16128 Б — конец app-области, граница WL_FLASH
FLASH_USER_END = 0x08004000  # конец всего user-flash (для READ)
MAX_FLASH_SIZE = FLASH_APP_END - FLASH_BASE  # 16128
BLOCK_SIZE = 64
MAX_RETRIES = 3

TIMEOUT_ERASE = 10.0
TIMEOUT_CMD = 3.0


def make_packet(cmd, addr=FLASH_BASE, data=None):
    """Build a request frame per TZ 3.5.1: AA 55 CMD LEN PAYLOAD CKL CKH 55 AA.

    ERASE/END: LEN=0, ERASE carries 4 addr bytes (ignored, part of the CK);
               END carries nothing (per spec vector AA 55 83 00 83 00 55 AA).
    VERIFY:    LEN=0x40, payload = 4 addr-stub bytes + 64 expected bytes.
    READ:      LEN=4, payload = addr, u32 LE.
    PROM:      LEN=0x40, payload = 64 data bytes.
    """
    if cmd == CMD_IAP_ERASE:
        payload = bytearray([cmd, 0]) + struct.pack("<I", addr)
        data = None
    elif cmd == CMD_IAP_END:
        payload = bytearray([cmd, 0])
        data = None
    elif cmd == CMD_IAP_PROM:
        payload = bytearray([cmd, len(data)])
    elif cmd == CMD_IAP_VERIFY:
        payload = bytearray([cmd, 0x40])
        payload += struct.pack("<I", addr)
    elif cmd == CMD_IAP_READ:
        payload = bytearray([cmd, 4])
        payload += struct.pack("<I", addr)
    else:
        raise ValueError("unknown command 0x%02X" % cmd)

    if data is not None:
        payload += data

    checksum = sum(payload) & 0xFFFF
    return bytes([
        SYNC_HEAD1,
        SYNC_HEAD2,
        *payload,
        checksum & 0xFF,
        (checksum >> 8) & 0xFF,
        SYNC_HEAD2,
        SYNC_HEAD1,
    ])


def parse_base_response(raw):
    """Parse a 6-byte base response. Returns (ok, status).

    ok = True only for AA 55 00 00 55 AA (ST_OK). For other statuses ok=False,
    status carries the code so the caller can spell out the error.
    """
    if raw is None:
        return False, None
    if len(raw) < 6:
        return False, None
    if raw[0] != SYNC_HEAD1 or raw[1] != SYNC_HEAD2 or raw[2] != 0x00:
        return False, None
    status = raw[3]
    if raw[4] != SYNC_HEAD2 or raw[5] != SYNC_HEAD1:
        return False, None
    return status == ST_OK, status


def wait_response(ser, timeout=TIMEOUT_CMD):
    """Read one base response frame (6 bytes). Returns raw bytes or None."""
    buf = bytearray()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting:
            buf += ser.read(min(ser.in_waiting, 6 - len(buf)))
            if len(buf) >= 6:
                break
        time.sleep(0.001)
    return bytes(buf)


def wait_read_response(ser, timeout=TIMEOUT_CMD):
    """Read a READ frame: AA 55 84 00 <64B> CKL CKH 55 AA (72 bytes).

    Tolerates the variant with a 4-byte address echo before the data
    (76 bytes total, as flashed on some benches): the layout is detected
    by checksum validation. Returns (data, None) on success,
    (None, error_message) otherwise.
    """
    min_len = 4 + BLOCK_SIZE + 4       # header + data + CK + tail = 72
    max_len = min_len + 4              # room for an eventual addr echo
    tail = bytes([SYNC_HEAD2, SYNC_HEAD1])
    buf = bytearray()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting:
            buf += ser.read(min(ser.in_waiting, max_len - len(buf)))
            if len(buf) >= max_len:
                break
            # Stop only on a complete frame candidate: breaking at min_len
            # could truncate the 76-byte echo variant on a split USB-serial
            # read, burning all retries on a mis-timed split.
            if len(buf) >= min_len and buf[-2:] == tail:
                break
        time.sleep(0.001)
    if len(buf) < min_len:
        return None, "неполный кадр READ (получено %d байт)" % len(buf)

    for skip in (0, 4):                # 0 = no echo, 4 = addr echo before data
        if len(buf) < 4 + skip + BLOCK_SIZE + 4:
            continue
        data = buf[4 + skip:4 + skip + BLOCK_SIZE]
        ck = (CMD_IAP_READ + 0x00 + sum(data)) & 0xFFFF
        ck_off = 4 + skip + BLOCK_SIZE
        tail = buf[ck_off + 2:ck_off + 4]
        if (buf[ck_off] == (ck & 0xFF) and buf[ck_off + 1] == ((ck >> 8) & 0xFF)
                and tail == bytes([SYNC_HEAD2, SYNC_HEAD1])):
            return bytes(data), None
        if buf[0] != SYNC_HEAD1 or buf[1] != SYNC_HEAD2 or buf[2] != CMD_IAP_READ or buf[3] != 0x00:
            return None, "неверный заголовок кадра READ"
    return None, "контрольная сумма READ не совпадает"


def send_packet(ser, packet, timeout=TIMEOUT_CMD):
    """Send a packet with retries (3x, reset_input_buffer between attempts).

    Returns (ok, status_or_error):
      ok=True        -> ACK (ST_OK)
      ok=False, code -> ST code (0x01/0x02/0x03) or None if no response.
    """
    for _ in range(MAX_RETRIES):
        ser.reset_input_buffer()
        ser.write(packet)
        ser.flush()

        ok, status = parse_base_response(wait_response(ser, timeout))
        if ok:
            return True, status
        if status is not None:
            # Bootloader answered with an error code — no point retrying the
            # whole frame (guards/verify are idempotent, resend won't help).
            return False, status

    return False, None


def send_read_packet(ser, packet, timeout=TIMEOUT_CMD):
    """Send a READ request with retries and collect the data frame answer.

    Same transport policy as send_packet (MAX_RETRIES attempts,
    reset_input_buffer between attempts), but the expected answer is the
    72/76-byte READ data frame, not the 6-byte base response. Returns
    (data, None) on success, (None, error_message) otherwise.
    """
    err = None
    for _ in range(MAX_RETRIES):
        ser.reset_input_buffer()
        ser.write(packet)
        ser.flush()
        block, err = wait_read_response(ser, timeout=timeout)
        if block is not None:
            return block, None
    return None, err


def send_end_best_effort(ser, timeout=TIMEOUT_CMD):
    """Send END once (no retries) and wait for its ACK best-effort.

    The bootloader jumps into the application right after END, so the ACK
    can be lost on the wire; per TZ 4.3 p.5 a missing ACK is not an error.
    The wait uses TIMEOUT_CMD (3 s) like every other command, not a shorter
    hardcode. Returns (ok, status): ok=True only on a confirmed ST_OK ACK.
    """
    ser.reset_input_buffer()
    ser.write(make_packet(CMD_IAP_END))
    ser.flush()
    ok, status = parse_base_response(wait_response(ser, timeout=timeout))
    return ok, status


def probe_protocol_v2(ser, timeout=TIMEOUT_CMD):
    """Confirm the device runs bootloader rev 2 before any destructive command.

    READ (0x84) exists only in rev 2: a rev-2 device answers with a 72-byte
    data frame, rev 1 answers with an error frame or stays silent — and on
    rev 1 the identical ERASE/END frames damage WL_FLASH settings. Returns
    (data, None) on success, (None, error_message) otherwise.
    """
    ser.reset_input_buffer()
    ser.write(make_packet(CMD_IAP_READ, addr=FLASH_BASE))
    ser.flush()
    return wait_read_response(ser, timeout=timeout)


def open_port(port, baudrate):
    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=2,
    )
