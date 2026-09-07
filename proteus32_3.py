#!/usr/bin/env python3
"""
PROTEUS32 Firmware Programmer — protocol v2.1 (BOOTLOADER_TZ.md v1.0).

CLI for the CH32V003 UART bootloader (revision 2, WL_FLASH-safe):

    python proteus32_3.py <port> <firmware.bin> [-b 460800] [--no-verify]   full cycle
    python proteus32_3.py <port> --erase-only                               erase app area
    python proteus32_3.py <port> --dump <file> --addr 0x08000000 --len 256  READ (0x84)
    python proteus32_3.py <port> --go                                       run app (END)
    python proteus32_3.py --version

PROTOCOL_REV = 2: works ONLY with bootloader revision 2 (iap.c/h v2.0.0).
Incompatible with the old bootloader (v1): VERIFY-with-data and READ are not
supported there, and its END writes the magic word 0x5AA55AA5 (WL damage).
Keep script and bootloader in lockstep.

Python 3.8+, deps: pyserial, tqdm (optional). Exit codes: 0 = success, 1 = error.
"""

import argparse
import os
import struct
import sys
import time

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable=None, **_kwargs):
        """tqdm fallback: plain iterator wrapper with minmal output."""
        if iterable is None:
            class _NullCtx:
                def __enter__(self):
                    return self
                def __exit__(self, *exc):
                    return False
                def update(self, _n=1):
                    pass
                def close(self):
                    pass
            return _NullCtx()
        return iterable

import serial

VERSION = "2.1"
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


def parse_base_response(raw, timeout=None):
    """Parse a 6-byte base response. Returns (ok, status, raw).

    ok = True only for AA 55 00 00 55 AA (ST_OK). For other statuses ok=False,
    status carries the code so the caller can spell out the error.
    """
    if raw is None:
        return False, None, b""
    if len(raw) < 6:
        return False, None, raw
    if raw[0] != SYNC_HEAD1 or raw[1] != SYNC_HEAD2 or raw[2] != 0x00:
        return False, None, raw
    status = raw[3]
    if raw[4] != SYNC_HEAD2 or raw[5] != SYNC_HEAD1:
        return False, None, raw
    return status == ST_OK, status, raw


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
    """Read a READ frame: AA 55 84 00 <64B> CKL CKH 55 AA (76 bytes).

    Validates the frame and the checksum. Returns (data, None) on success,
    (None, error_message) otherwise.
    """
    expected_len = 6 + BLOCK_SIZE + 4
    buf = bytearray()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting:
            buf += ser.read(min(ser.in_waiting, expected_len - len(buf)))
            if len(buf) >= expected_len:
                break
        time.sleep(0.001)
    if len(buf) < expected_len:
        return None, "неполный кадр READ (получено %d из %d байт)" % (len(buf), expected_len)
    if buf[0] != SYNC_HEAD1 or buf[1] != SYNC_HEAD2 or buf[2] != CMD_IAP_READ or buf[3] != 0x00:
        return None, "неверный заголовок кадра READ"
    ck = (CMD_IAP_READ + 0x00 + sum(buf[4:4 + BLOCK_SIZE])) & 0xFFFF
    if buf[70] != (ck & 0xFF) or buf[71] != ((ck >> 8) & 0xFF):
        return None, "контрольная сумма READ не совпадает"
    if buf[72] != SYNC_HEAD2 or buf[73] != SYNC_HEAD1:
        return None, "неверный хвост кадра READ"
    return bytes(buf[4:4 + BLOCK_SIZE]), None


def send_packet(ser, packet, timeout=TIMEOUT_CMD, expect_response=True):
    """Send a packet with retries (3x, reset_input_buffer between attempts).

    Returns (ok, status_or_error):
      ok=True        -> ACK (ST_OK)
      ok=False, code -> ST code (0x01/0x02/0x03) or None if no response.
    """
    for _ in range(MAX_RETRIES):
        ser.reset_input_buffer()
        ser.write(packet)
        ser.flush()

        if not expect_response:
            return True, ST_OK

        raw = wait_response(ser, timeout)
        ok, status, raw = parse_base_response(raw)
        if ok:
            return True, status
        if status is not None:
            # Bootloader answered with an error code — no point retrying the
            # whole frame (guards/verify are idempotent, resend won't help).
            return False, status

    return False, None


def check_size(size):
    """Strict firmware size check: cannot exceed the app area (no --force)."""
    if size > MAX_FLASH_SIZE:
        print(
            "ERROR: Firmware size (%d B) exceeds the app-area limit of %d B (0x3F00).\n"
            "Aborting to protect WL_FLASH storage at 0x08003F00..0x08004000.\n"
            "No --force exists anymore: the limit is structural (bootloader guards)."
            % (size, MAX_FLASH_SIZE)
        )
        return False
    return True


def open_port(port, baudrate):
    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=2,
    )


def fail_with_hint(last_status):
    print("FAILED")
    if last_status in ST_DICT:
        print("Ответ загрузчика: ST=0x%02X — %s" % (last_status, ST_DICT[last_status]))
    else:
        print(
            "Ответа от загрузчика нет.\n"
            "Проверьте: устройство в IAP-режиме (питание при зажатой кнопке),\n"
            "проводку PD5/PD6 и скорость 460800."
        )
    return 1


def cmd_write(port, filepath, baudrate, do_verify):
    with open(filepath, "rb") as f:
        data = f.read()

    if not data:
        print("ERROR: Firmware file is empty; nothing to flash.")
        return 1

    if not check_size(len(data)):
        return 1

    print("=" * 56)
    print(" PROTEUS32 Programmer v%s (protocol rev %d)" % (VERSION, PROTOCOL_REV))
    print("=" * 56)
    print("{:<10}: {}".format("Port", port))
    print("{:<10}: {}".format("Baudrate", baudrate))
    print("{:<10}: {}".format("Firmware", os.path.basename(filepath)))
    print("{:<10}: {:.1f} KiB".format("Size", len(data) / 1024))
    print()

    # Firmware 0xFF-padded to a multiple of 64 B (same image for PROM and VERIFY)
    if len(data) % BLOCK_SIZE:
        data += b"\xFF" * (BLOCK_SIZE - len(data) % BLOCK_SIZE)
    blocks = len(data) // BLOCK_SIZE

    ser = open_port(port, baudrate)
    total_start = time.time()

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # ---- 1. ERASE (timeout 10 s, ~1 s of real erase) ----
        stage_start = time.time()
        print("▶ Erasing app area...".ljust(40), end="", flush=True)
        ok, status = send_packet(ser, make_packet(CMD_IAP_ERASE), timeout=TIMEOUT_ERASE)
        if not ok:
            return fail_with_hint(status)
        print("OK (%.0f ms)" % ((time.time() - stage_start) * 1000))
        print()

        # ---- 2. PROGRAM (64-B blocks, padded) ----
        print("▶ Programming\n")
        try:
            pbar = tqdm(
                total=len(data),
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc="Flashing",
                ncols=56,
                leave=True,
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}",
            )
        except Exception:
            pbar = None

        ret = 1
        for offset in range(0, len(data), BLOCK_SIZE):
            chunk = data[offset:offset + BLOCK_SIZE]
            packet = make_packet(CMD_IAP_PROM, data=chunk)
            ok, status = send_packet(ser, packet)
            if not ok:
                if pbar is not None:
                    pbar.close()
                print("\n✗ Programming failed at offset 0x%08X" % offset)
                return fail_with_hint(status)
            if pbar is not None:
                pbar.update(BLOCK_SIZE)

        if pbar is not None:
            pbar.close()
        print()

        # ---- 3. VERIFY (real transfer of the same padded image) ----
        if do_verify:
            print("▶ Verifying...".ljust(40), end="", flush=True)
            vstart = time.time()
            for idx in range(blocks):
                offset = idx * BLOCK_SIZE
                addr_stub = FLASH_BASE + offset
                packet = make_packet(CMD_IAP_VERIFY, addr=addr_stub, data=data[offset:offset + BLOCK_SIZE])
                ok, status = send_packet(ser, packet)
                if not ok:
                    if status == ST_ERR_VERIFY:
                        print(
                            "\n✗ VERIFY mismatch at block %d (offset 0x%08X).\n"
                            "Flash on the device differs from the firmware image."
                            % (idx, offset)
                        )
                    elif status == ST_ERR_RANGE:
                        print("\n✗ VERIFY out of range at offset 0x%08X" % offset)
                    else:
                        print("\n✗ VERIFY failed at offset 0x%08X" % offset)
                    return 1
            print("OK (%.0f ms)" % ((time.time() - vstart) * 1000))
        else:
            print("▶ Verify".ljust(40), end="", flush=True)
            print("SKIPPED (--no-verify)")

        # ---- 4. END (ACK may be lost in the reset; not an error) ----
        print("▶ Starting application...".ljust(40), end="", flush=True)
        send_packet(ser, make_packet(CMD_IAP_END), expect_response=False)
        time.sleep(0.5)
        print("OK")
        print()

        # ---- summary ----
        total_time = time.time() - total_start
        speed = len(data) / total_time / 1024 if total_time > 0 else 0
        print("=" * 56)
        print("✓ Flash completed successfully")
        print()
        print("Elapsed : %.2f s" % total_time)
        print("Speed   : %.1f KiB/s" % speed)
        print("=" * 56)
        print()
        ret = 0

    finally:
        ser.close()
    return ret


def cmd_erase_only(port, baudrate):
    print("=" * 56)
    print(" PROTEUS32 Programmer v%s — erase app area only" % VERSION)
    print("=" * 56)
    ser = open_port(port, baudrate)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        print("▶ Erasing app area (WL_FLASH untouched)...".ljust(40), end="", flush=True)
        ok, status = send_packet(ser, make_packet(CMD_IAP_ERASE), timeout=TIMEOUT_ERASE)
        if not ok:
            return fail_with_hint(status)
        print("OK")
        send_packet(ser, make_packet(CMD_IAP_END), expect_response=False)
        time.sleep(0.3)
        return 0
    finally:
        ser.close()


def cmd_dump(port, baudrate, filepath, addr, length):
    if addr % BLOCK_SIZE:
        print("ERROR: --addr must be 64-byte aligned (page granularity).")
        return 1
    if addr < FLASH_BASE or addr + length > FLASH_USER_END:
        print("ERROR: address range 0x%08X..0x%08X is outside user flash"
              " 0x%08X..0x%08X" % (addr, addr + length, FLASH_BASE, FLASH_USER_END))
        return 1

    print("=" * 56)
    print(" PROTEUS32 Programmer v%s — READ (0x84)" % VERSION)
    print("=" * 56)
    print("{:<10}: {}".format("Port", port))
    print("{:<10}: {}".format("Addr", hex(addr)))
    print("{:<10}: {}".format("Length", length))
    print()

    ser = open_port(port, baudrate)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        out = bytearray()
        for a in range(addr, addr + length, BLOCK_SIZE):
            packet = make_packet(CMD_IAP_READ, addr=a)
            ok = False
            for _ in range(MAX_RETRIES):
                ser.reset_input_buffer()
                ser.write(packet)
                ser.flush()
                block, err = wait_read_response(ser)
                if block is not None:
                    ok = True
                    break
            if not ok:
                print("✗ READ failed at 0x%08X: %s" % (a, err or "нет ответа"))
                return 1
            out += block

        with open(filepath, "wb") as f:
            f.write(out[:length])
        print("✓ Dumped %d bytes to %s" % (length, filepath))
        return 0
    finally:
        ser.close()


def cmd_go(port, baudrate):
    print("=" * 56)
    print(" PROTEUS32 Programmer v%s — start application (END)" % VERSION)
    print("=" * 56)
    ser = open_port(port, baudrate)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        send_packet(ser, make_packet(CMD_IAP_END), expect_response=False)
        time.sleep(0.5)
        print("✓ END sent, device reset into the application")
        return 0
    finally:
        ser.close()


def main():
    parser = argparse.ArgumentParser(
        description="PROTEUS32 Firmware Programmer (protocol rev %d, WL_FLASH-safe)" % PROTOCOL_REV
    )
    parser.add_argument("port", nargs="?", help="Serial port (e.g. COM5)")
    parser.add_argument("firmware", nargs="?", help="Firmware binary (*.bin)")
    parser.add_argument(
        "-b", "--baudrate",
        type=int,
        default=460800,
        help="Baudrate (default: 460800)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--erase-only",
        action="store_true",
        help="Erase the app area only (WL_FLASH untouched), then END",
    )
    mode.add_argument(
        "--dump",
        metavar="FILE",
        help="Read flash (READ 0x84) and save to FILE",
    )
    mode.add_argument(
        "--go",
        action="store_true",
        help="Send END only (start the application)",
    )

    parser.add_argument(
        "--addr",
        type=lambda s: int(s, 0),
        default=FLASH_BASE,
        help="Start address for --dump (default: 0x08000000)",
    )
    parser.add_argument(
        "--len",
        type=lambda s: int(s, 0),
        default=None,
        help="Byte count for --dump (default: 256)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the VERIFY stage of the full cycle",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 2.1 (protocol rev 2, WL_FLASH-safe)",
    )

    args = parser.parse_args()
    args.len = args.len if args.len is not None else 256

    if args.dump:
        if not args.port:
            print("ERROR: --dump requires <port>")
            return 1
        run = lambda: cmd_dump(args.port, args.baudrate, args.dump, args.addr, args.len)
    elif args.erase_only:
        if not args.port:
            print("ERROR: --erase-only requires <port>")
            return 1
        run = lambda: cmd_erase_only(args.port, args.baudrate)
    elif args.go:
        if not args.port:
            print("ERROR: --go requires <port>")
            return 1
        run = lambda: cmd_go(args.port, args.baudrate)
    else:
        # Default: full write cycle
        if not args.port or not args.firmware:
            parser.print_help()
            return 1

        if not os.path.isfile(args.firmware):
            print("ERROR: File not found: %s" % args.firmware)
            return 1
        run = lambda: cmd_write(args.port, args.firmware, args.baudrate, not args.no_verify)

    try:
        return run()
    except serial.SerialException as e:
        print(
            "\n✗ Serial error: %s\n"
            "Проверьте: порт существует/не занят, кабель USB-UART подключён." % e
        )
        return 1
    except KeyboardInterrupt:
        print("\n✗ Cancelled by user.")
        return 1
    except Exception as e:
        print("\n✗ Unexpected error: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())