import argparse
import os
import serial
import struct
import sys
import time
from tqdm import tqdm

VERSION = "2.0"

SYNC_HEAD1 = 0xAA
SYNC_HEAD2 = 0x55

CMD_IAP_PROM   = 0x80
CMD_IAP_ERASE  = 0x81
CMD_IAP_VERIFY = 0x82
CMD_IAP_END    = 0x83

FLASH_BASE = 0x08000000
BLOCK_SIZE = 64
MAX_RETRIES = 3  # Количество попыток при ошибке передачи


def make_packet(cmd, addr=None, data=None):
    """Формирует пакет для загрузчика."""
    payload = bytearray([cmd])

    if cmd == CMD_IAP_ERASE:
        payload.append(0)
        payload.extend(struct.pack("<I", addr if addr else FLASH_BASE))
    elif cmd == CMD_IAP_PROM:
        payload.append(len(data))
        payload.extend(data)
    elif cmd == CMD_IAP_VERIFY:
        payload.append(0)
        payload.extend(struct.pack("<I", addr if addr else FLASH_BASE))
    elif cmd == CMD_IAP_END:
        payload.append(0)

    checksum = sum(payload)

    return bytes([
        SYNC_HEAD1,
        SYNC_HEAD2,
        *payload,
        checksum & 0xFF,
        (checksum >> 8) & 0xFF,
        SYNC_HEAD2,
        SYNC_HEAD1,
    ])


def wait_response(ser, timeout=2.0):
    """Оптимизированное чтение ответа (чтением массивом, а не по 1 байту)."""
    expected_len = 6
    response = bytearray()
    start = time.time()

    while time.time() - start < timeout:
        if ser.in_waiting:
            # Читаем сразу все доступные байты до нужного количества
            to_read = min(ser.in_waiting, expected_len - len(response))
            response.extend(ser.read(to_read))
            if len(response) >= expected_len:
                break
        time.sleep(0.001)

    return (
        len(response) >= expected_len and
        response[0] == SYNC_HEAD1 and
        response[1] == SYNC_HEAD2 and
        response[3] == 0
    )


def send_packet(ser, packet, expect_response=True):
    """Отправка пакета с механизмом повторных попыток."""
    for attempt in range(MAX_RETRIES):
        ser.write(packet)
        ser.flush()
        
        if not expect_response:
            return True
            
        if wait_response(ser):
            return True
            
        # Если ответ не получен, очищаем буфер от мусора перед следующей попыткой
        ser.reset_input_buffer()

    return False


def flash_firmware(port, filepath, baudrate):
    with open(filepath, "rb") as f:
        data = f.read()

    # Выводим красивый заголовок ДО попытки открыть порт
    print("=" * 56)
    print(f" PROTEUS32 Programmer v{VERSION}")
    print("=" * 56)
    print(f"{'Port':<10}: {port}")
    print(f"{'Baudrate':<10}: {baudrate}")
    print(f"{'Firmware':<10}: {os.path.basename(filepath)}")
    print(f"{'Size':<10}: {len(data)/1024:.1f} KiB")
    print()

    ser = serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=2,
    )

    total_start = time.time()

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # --- ЭТАП 1: Стирание ---
        stage_start = time.time()
        # ljust(40) делает ровные отступы, чтобы "OK" было на одном уровне
        print(f"▶ Erasing flash...".ljust(40), end="", flush=True)

        if not send_packet(ser, make_packet(CMD_IAP_ERASE, addr=FLASH_BASE)):
            print("FAILED")
            return False

        erase_time = time.time() - stage_start
        print(f"OK ({erase_time*1000:.0f} ms)")
        print()

        time.sleep(0.1)

        # --- ЭТАП 2: Прошивка ---
        print("▶ Programming\n")
        prog_start = time.time()

        # Стандартный tqdm выглядит лучше всего и защищен от глюков консоли.
        # Он сам форматирует скорость и байты.
        with tqdm(
            total=len(data),
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc="Flashing",
            ncols=56,
            leave=True,
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}"
        ) as pbar:
            
            for offset in range(0, len(data), BLOCK_SIZE):
                # Берем реальный кусок данных
                chunk = data[offset:offset+BLOCK_SIZE]
                actual_chunk_size = len(chunk)

                # Выравниваем последний блок нулями (0xFF - состояние стертой флешки)
                if actual_chunk_size < BLOCK_SIZE:
                    chunk = chunk.ljust(BLOCK_SIZE, b'\xFF')

                if not send_packet(
                    ser,
                    make_packet(CMD_IAP_PROM, data=chunk),
                ):
                    pbar.close() # Закрываем бар, чтобы не сломать вывод ошибки
                    print(f"\n✗ Programming failed at offset 0x{offset:08X} after {MAX_RETRIES} retries")
                    return False

                # Обновляем прогресс на размер реальных данных (без паддинга)
                pbar.update(actual_chunk_size)

        # tqdm автоматически переносит курсор на новую строку после завершения
        print()

        # --- ЭТАП 3: Финализация ---
        print(f"▶ Finalizing...".ljust(40), end="", flush=True)

        if not send_packet(
            ser,
            make_packet(CMD_IAP_VERIFY, addr=FLASH_BASE),
        ):
            print("FAILED")
            return False

        send_packet(
            ser,
            make_packet(CMD_IAP_END),
            expect_response=False,
        )

        time.sleep(0.5)
        print("OK")
        print()

        # --- ИТОГИ ---
        total_time = time.time() - total_start
        speed = len(data) / total_time / 1024 if total_time > 0 else 0

        print("=" * 56)
        print("✓ Flash completed successfully")
        print()
        print(f"Elapsed : {total_time:.2f} s")
        print(f"Speed   : {speed:.1f} KiB/s")
        print("=" * 56)
        print()

        return True

    finally:
        ser.close()


def main():
    parser = argparse.ArgumentParser(
        description="PROTEUS32 Firmware Programmer"
    )

    parser.add_argument("port", help="Serial port (e.g. COM5)")
    parser.add_argument("firmware", help="Firmware binary (*.bin)")
    parser.add_argument(
        "-b", "--baudrate",
        type=int,
        default=460800,
        help="Baudrate (default: 460800)"
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}"
    )

    args = parser.parse_args()

    if not os.path.isfile(args.firmware):
        print(f"ERROR: File not found: {args.firmware}")
        return 1

    try:
        return 0 if flash_firmware(
            args.port,
            args.firmware,
            args.baudrate,
        ) else 1

    except serial.SerialException as e:
        print(f"\n✗ Serial error: {e}")
        return 1

    except KeyboardInterrupt:
        print("\n✗ Cancelled by user.")
        return 1

    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())