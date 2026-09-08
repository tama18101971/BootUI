import os
import struct
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import serial
import serial.tools.list_ports

VERSION = "2.1"
PROTOCOL_REV = 2  # bootloader revision 2 (WL_FLASH-safe); incompatible with v1

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
    ST_ERR:        "command execution error",
    ST_ERR_RANGE:  "address out of app area (WL_FLASH protection)",
    ST_ERR_VERIFY: "VERIFY: data mismatch",
}

FLASH_BASE = 0x08000000
FLASH_APP_END = 0x08003F00   # end of app area, start of WL_FLASH settings
MAX_FLASH_SIZE = FLASH_APP_END - FLASH_BASE  # 16128
BLOCK_SIZE = 64
MAX_RETRIES = 3

TIMEOUT_ERASE = 10.0  # 252 x 64 B pages, ~1 s of real erase
TIMEOUT_CMD = 3.0

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]


def make_packet(cmd, addr=FLASH_BASE, data=None):
    """Build a request frame: AA 55 CMD LEN PAYLOAD CKL CKH 55 AA.

    Protocol rev 2 (BOOTLOADER_TZ.md 3.5): the checksum is the 16-bit LE sum
    of CMD + LEN + all payload bytes. ERASE carries a 4-byte addr stub
    (ignored by the bootloader but included in the checksum); END carries
    nothing; VERIFY carries a 4-byte addr stub plus 64 expected bytes; PROM
    carries 64 data bytes; READ carries a 4-byte addr (rev-2 probe only).
    """
    if cmd == CMD_IAP_ERASE:
        payload = bytearray([cmd, 0]) + struct.pack("<I", addr)
    elif cmd == CMD_IAP_END:
        payload = bytearray([cmd, 0])
    elif cmd == CMD_IAP_PROM:
        payload = bytearray([cmd, len(data)])
        payload += data
    elif cmd == CMD_IAP_VERIFY:
        payload = bytearray([cmd, BLOCK_SIZE])
        payload += struct.pack("<I", addr)
        payload += data
    elif cmd == CMD_IAP_READ:
        payload = bytearray([cmd, 4])
        payload += struct.pack("<I", addr)
    else:
        raise ValueError("unknown command 0x%02X" % cmd)

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
    """Parse the 6-byte base response AA 55 00 ST 55 AA.

    Returns (ok, status): ok is True only for ST_OK; status carries the ST
    code, or None when the frame is missing or garbled.
    """
    if raw is None or len(raw) < 6:
        return False, None
    if raw[0] != SYNC_HEAD1 or raw[1] != SYNC_HEAD2 or raw[2] != 0x00:
        return False, None
    if raw[4] != SYNC_HEAD2 or raw[5] != SYNC_HEAD1:
        return False, None
    status = raw[3]
    return status == ST_OK, status


def describe_status(status):
    """Human-readable description of an ST status code (or missing response)."""
    if status is None:
        return "no response from bootloader (check IAP mode: power on with button held)"
    return ST_DICT.get(status, "unknown status 0x%02X" % status)


def wait_response(ser, timeout=TIMEOUT_CMD):
    """Read one base response frame (6 bytes). Returns raw bytes (may be short)."""
    buf = bytearray()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting:
            buf += ser.read(min(ser.in_waiting, 6 - len(buf)))
            if len(buf) >= 6:
                break
        time.sleep(0.001)
    return bytes(buf)


def send_packet(ser, packet, timeout=TIMEOUT_CMD):
    """Send a frame with retries (3x; input buffer reset before each attempt).

    Returns (ok, status): ok=True on ACK; on failure status is the ST error
    code reported by the bootloader, or None when it never answered. An
    explicit error status is not retried: resending cannot fix a guard or
    verify failure.
    """
    for _ in range(MAX_RETRIES):
        ser.reset_input_buffer()
        ser.write(packet)
        ser.flush()

        ok, status = parse_base_response(wait_response(ser, timeout))
        if ok:
            return True, status
        if status is not None:
            return False, status

    return False, None


def probe_protocol_v2(ser, timeout=TIMEOUT_CMD):
    """Confirm the device runs bootloader rev 2 before any destructive command.

    READ (0x84) exists only in rev 2: rev 2 answers with a 72-byte data
    frame, rev 1 answers with an error frame or stays silent — and on rev 1
    the identical ERASE/END frames damage WL_FLASH settings. Light check:
    header, CMD/LEN and tail of the response are enough to tell the
    revisions apart. Returns (ok, error_message).
    """
    ser.reset_input_buffer()
    ser.write(make_packet(CMD_IAP_READ, addr=FLASH_BASE))
    ser.flush()
    buf = bytearray()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting:
            buf += ser.read(min(ser.in_waiting, 72 - len(buf)))
            if len(buf) >= 72:
                break
        time.sleep(0.001)
    if len(buf) >= 72:
        if (buf[0] == SYNC_HEAD1 and buf[1] == SYNC_HEAD2
                and buf[2] == CMD_IAP_READ and buf[3] == 0x00
                and buf[-2:] == bytes((SYNC_HEAD2, SYNC_HEAD1))):
            return True, None
        return False, "unexpected READ response from bootloader"
    return False, "no READ response (device in IAP mode? bootloader rev 1?)"


class FlasherApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"CH32V003 Flasher v{VERSION} (protocol rev {PROTOCOL_REV})")
        self.root.geometry("600x520")
        self.root.resizable(False, False)

        self.firmware_path = tk.StringVar()
        self.com_port = tk.StringVar()
        self.baud_rate = tk.IntVar(value=460800)
        self.is_flashing = False

        self.create_widgets()
        self.refresh_ports()

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        file_frame = ttk.LabelFrame(main_frame, text="Firmware File", padding=10)
        file_frame.pack(fill=tk.X, pady=(0, 10))

        file_entry = ttk.Entry(file_frame, textvariable=self.firmware_path, state="readonly")
        file_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        browse_btn = ttk.Button(file_frame, text="Browse...", command=self.browse_file)
        browse_btn.pack(side=tk.RIGHT)

        settings_frame = ttk.LabelFrame(main_frame, text="Connection Settings", padding=10)
        settings_frame.pack(fill=tk.X, pady=(0, 10))

        port_frame = ttk.Frame(settings_frame)
        port_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(port_frame, text="COM Port:").pack(side=tk.LEFT)

        self.port_combo = ttk.Combobox(port_frame, textvariable=self.com_port, state="readonly", width=15)
        self.port_combo.pack(side=tk.LEFT, padx=(10, 10))

        refresh_btn = ttk.Button(port_frame, text="Refresh", command=self.refresh_ports)
        refresh_btn.pack(side=tk.LEFT)

        baud_frame = ttk.Frame(settings_frame)
        baud_frame.pack(fill=tk.X)

        ttk.Label(baud_frame, text="Baud Rate:").pack(side=tk.LEFT)

        self.baud_combo = ttk.Combobox(
            baud_frame,
            textvariable=self.baud_rate,
            values=BAUD_RATES,
            state="readonly",
            width=15
        )
        self.baud_combo.pack(side=tk.LEFT, padx=(10, 10))

        log_frame = ttk.LabelFrame(main_frame, text="Log", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.log_text = tk.Text(log_frame, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(self.log_text, orient=tk.VERTICAL, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.progress = ttk.Progressbar(main_frame, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(0, 10))

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X)

        self.flash_btn = ttk.Button(btn_frame, text="Flash Firmware", command=self.start_flash)
        self.flash_btn.pack(side=tk.RIGHT)

        self.file_info_label = ttk.Label(btn_frame, text="No file selected")
        self.file_info_label.pack(side=tk.LEFT)

    def browse_file(self):
        filepath = filedialog.askopenfilename(
            title="Select Firmware File",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")]
        )
        if filepath:
            self.firmware_path.set(filepath)
            size = os.path.getsize(filepath)
            self.file_info_label.config(
                text=f"{os.path.basename(filepath)} ({size / 1024:.1f} / {MAX_FLASH_SIZE / 1024:.1f} KiB)"
            )
            self.log(f"Selected: {filepath}")

    def refresh_ports(self):
        ports = serial.tools.list_ports.comports()
        port_list = [f"{p.device}" for p in sorted(ports)]
        self.port_combo["values"] = port_list
        if port_list:
            self.port_combo.current(0)
        self.log(f"Found {len(port_list)} COM port(s)")

    def log(self, message):
        # tk.Text is not thread-safe; flash_thread() logs from a worker
        # thread, so all widget access is marshalled to the main loop.
        line = f"[{time.strftime('%H:%M:%S')}] {message}\n"
        self.root.after(0, self._append_log, line)

    def _append_log(self, line):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def start_flash(self):
        if self.is_flashing:
            return

        if not self.firmware_path.get():
            messagebox.showwarning("Warning", "Please select a firmware file")
            return

        if not self.com_port.get():
            messagebox.showwarning("Warning", "Please select a COM port")
            return

        if os.path.getsize(self.firmware_path.get()) > MAX_FLASH_SIZE:
            messagebox.showerror(
                "Error",
                "Firmware exceeds the app-area limit of %d B (0x3F00).\n"
                "Flashing is blocked to protect WL_FLASH settings at 0x08003F00..0x08004000."
                % MAX_FLASH_SIZE,
            )
            return

        self.is_flashing = True
        self.flash_btn.config(state=tk.DISABLED)
        self.progress["value"] = 0

        thread = threading.Thread(target=self.flash_thread, daemon=True)
        thread.start()

    def flash_thread(self):
        success = False
        ser = None
        try:
            filepath = self.firmware_path.get()
            port = self.com_port.get()
            baudrate = self.baud_rate.get()

            with open(filepath, "rb") as f:
                data = f.read()

            if not data:
                self.log("ERROR: firmware file is empty, nothing to flash")
                return

            if len(data) > MAX_FLASH_SIZE:
                self.log(
                    "ERROR: firmware is %d B, app-area limit is %d B (0x3F00). "
                    "Aborting to protect WL_FLASH settings at 0x08003F00..0x08004000."
                    % (len(data), MAX_FLASH_SIZE)
                )
                return

            if len(data) % BLOCK_SIZE:
                data += b"\xFF" * (BLOCK_SIZE - len(data) % BLOCK_SIZE)
            blocks = len(data) // BLOCK_SIZE

            self.log("=" * 50)
            self.log(f"Starting flash: {os.path.basename(filepath)}")
            self.log(f"Port: {port}, Baud: {baudrate}")
            self.log(f"Size: {len(data) / 1024:.1f} KiB ({blocks} x {BLOCK_SIZE} B blocks)")
            self.log("=" * 50)

            ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=2,
            )

            total_start = time.time()

            ser.reset_input_buffer()
            ser.reset_output_buffer()

            self.log("Checking bootloader revision (READ probe)...")
            probe_ok, probe_err = probe_protocol_v2(ser)
            if not probe_ok:
                self.log("ERROR: %s" % probe_err)
                self.log("If the board runs bootloader rev 1, update it via WCH-Link first:")
                self.log("ERASE/END of this tool would damage WL_FLASH settings on rev 1.")
                return

            self.log("Erasing app area (WL_FLASH untouched)...")
            erase_start = time.time()
            ok, status = send_packet(
                ser,
                make_packet(CMD_IAP_ERASE),
                timeout=TIMEOUT_ERASE,
            )
            if not ok:
                self.log("ERROR: Erase failed: %s" % describe_status(status))
                return
            self.log(f"Erase complete ({time.time() - erase_start:.2f}s)")

            self.log("Programming...")
            prog_start = time.time()

            for idx in range(blocks):
                offset = idx * BLOCK_SIZE
                ok, status = send_packet(
                    ser,
                    make_packet(CMD_IAP_PROM, data=data[offset:offset + BLOCK_SIZE]),
                )
                if not ok:
                    self.log("ERROR: Programming failed at offset 0x%08X: %s" % (offset, describe_status(status)))
                    return
                self.root.after(0, self.update_progress, (idx + 1) / blocks * 100)

            prog_time = time.time() - prog_start
            self.log(f"Programming complete ({prog_time:.2f}s)")

            self.log("Verifying...")
            verify_start = time.time()

            for idx in range(blocks):
                offset = idx * BLOCK_SIZE
                ok, status = send_packet(
                    ser,
                    make_packet(
                        CMD_IAP_VERIFY,
                        addr=FLASH_BASE + offset,
                        data=data[offset:offset + BLOCK_SIZE],
                    ),
                )
                if not ok:
                    if status == ST_ERR_VERIFY:
                        self.log("ERROR: VERIFY mismatch at block %d (offset 0x%08X)" % (idx, offset))
                        self.log("Flash on the device differs from the firmware image")
                    else:
                        self.log("ERROR: VERIFY failed at offset 0x%08X: %s" % (offset, describe_status(status)))
                    return

            self.log(f"Verify complete ({time.time() - verify_start:.2f}s)")

            self.log("Starting application...")
            # END is sent once without retries; the ACK may be lost when the
            # bootloader jumps into the application, which is not an error.
            ser.reset_input_buffer()
            ser.write(make_packet(CMD_IAP_END))
            ser.flush()
            ok, _status = parse_base_response(wait_response(ser, timeout=2.0))
            if ok:
                self.log("Application start acknowledged")
            else:
                self.log("END ACK not received - device already switched to the application (not an error)")
            time.sleep(0.5)

            total_time = time.time() - total_start
            speed = len(data) / total_time / 1024 if total_time > 0 else 0

            self.log("=" * 50)
            self.log("Flash completed successfully!")
            self.log(f"Elapsed: {total_time:.2f}s, Speed: {speed:.1f} KiB/s")
            self.log("=" * 50)
            success = True

        except serial.SerialException as e:
            self.log(f"Serial error: {e}")

        except Exception as e:
            self.log(f"Error: {e}")

        finally:
            if ser is not None:
                ser.close()
            self.root.after(0, self.flash_complete, success)

    def update_progress(self, value):
        self.progress["value"] = value

    def flash_complete(self, success):
        self.is_flashing = False
        self.flash_btn.config(state=tk.NORMAL)
        self.progress["value"] = 100 if success else 0

        if success:
            messagebox.showinfo("Success", "Firmware flashed successfully!")
        else:
            messagebox.showerror("Error", "Flashing failed. Check the log for details.")


def main():
    root = tk.Tk()
    app = FlasherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
