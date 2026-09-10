#!/usr/bin/env python3
"""
PROTEUS32 Firmware Programmer — protocol v2.1 (BOOTLOADER_TZ.md v1.0).

CLI for the CH32V003 UART bootloader (revision 2, WL_FLASH-safe):

    python proteus32_3.py <port> <firmware.bin> [-b 460800] [--no-verify]   full cycle
    python proteus32_3.py <port> --erase-only                               erase app area
    python proteus32_3.py <port> --dump <file> --addr 0x08000000 --len 256  READ (0x84)
    python proteus32_3.py <port> --go                                       run app (END)
    python proteus32_3.py --version

The protocol layer (frames, statuses, timeouts, transport) lives in the
shared module proteus32_protocol.py and is common with bootloader_gui.py.

PROTOCOL_REV = 2: works ONLY with bootloader revision 2 (iap.c/h v2.0.0).
Incompatible with the old bootloader (v1): VERIFY-with-data and READ are not
supported there, and its END writes the magic word 0x5AA55AA5 (WL damage).
Keep script and bootloader in lockstep.

Python 3.8+, deps: pyserial, tqdm (optional). Exit codes: 0 = success, 1 = error.
"""

import argparse
import os
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

from proteus32_protocol import (
    BLOCK_SIZE,
    CMD_IAP_END,
    CMD_IAP_ERASE,
    CMD_IAP_PROM,
    CMD_IAP_READ,
    CMD_IAP_VERIFY,
    FLASH_BASE,
    FLASH_USER_END,
    MAX_FLASH_SIZE,
    PROTOCOL_REV,
    ST_DICT,
    ST_ERR_RANGE,
    ST_ERR_VERIFY,
    TIMEOUT_ERASE,
    make_packet,
    open_port,
    probe_protocol_v2,
    send_end_best_effort,
    send_packet,
    send_read_packet,
)

VERSION = "2.1"


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


def fail_no_v2(err):
    print("FAILED")
    print(
        "Загрузчик не подтвердил протокол rev 2 (READ 0x84): %s\n"
        "Проверьте: устройство в IAP-режиме (питание при зажатой кнопке),\n"
        "проводку PD5/PD6 и скорость 460800.\n"
        "Если на плате загрузчик rev 1 — обновите его через WCH-Link:\n"
        "ERASE/END этого скрипта на rev 1 повреждают настройки WL_FLASH."
        % (err or "нет ответа")
    )
    return 1


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

        # ---- 0. Protocol probe (READ exists only in rev 2) ----
        print("▶ Checking bootloader revision...".ljust(40), end="", flush=True)
        probe_data, probe_err = probe_protocol_v2(ser)
        if probe_data is None:
            return fail_no_v2(probe_err)
        print("OK (rev 2)")

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

        # ---- 4. END (best-effort ACK: the device may already run the app) ----
        print("▶ Starting application...".ljust(40), end="", flush=True)
        send_end_best_effort(ser)
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
        print("▶ Checking bootloader revision...".ljust(40), end="", flush=True)
        probe_data, probe_err = probe_protocol_v2(ser)
        if probe_data is None:
            return fail_no_v2(probe_err)
        print("OK (rev 2)")
        print("▶ Erasing app area (WL_FLASH untouched)...".ljust(40), end="", flush=True)
        ok, status = send_packet(ser, make_packet(CMD_IAP_ERASE), timeout=TIMEOUT_ERASE)
        if not ok:
            return fail_with_hint(status)
        print("OK")
        send_end_best_effort(ser)
        time.sleep(0.3)
        return 0
    finally:
        ser.close()


def cmd_dump(port, baudrate, filepath, addr, length):
    if length <= 0:
        print("ERROR: --len must be a positive byte count.")
        return 1
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
            block, err = send_read_packet(ser, packet)
            if block is None:
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
        print("▶ Checking bootloader revision...".ljust(40), end="", flush=True)
        probe_data, probe_err = probe_protocol_v2(ser)
        if probe_data is None:
            return fail_no_v2(probe_err)
        print("OK (rev 2)")
        ok, _status = send_end_best_effort(ser)
        if ok:
            print("✓ END acknowledged, device reset into the application")
        else:
            print("✓ END sent (no ACK - device already runs the application)")
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
