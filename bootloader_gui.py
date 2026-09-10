import os
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import serial
import serial.tools.list_ports

from proteus32_protocol import (
    BLOCK_SIZE,
    CMD_IAP_ERASE,
    CMD_IAP_PROM,
    CMD_IAP_VERIFY,
    FLASH_BASE,
    MAX_FLASH_SIZE,
    PROTOCOL_REV,
    ST_DICT,
    ST_ERR_VERIFY,
    TIMEOUT_ERASE,
    make_packet,
    open_port,
    probe_protocol_v2,
    send_end_best_effort,
    send_packet,
)

VERSION = "2.1"

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
BOOTLOADER_BAUD = 460800  # the only rate the fixed BRR (0x34 @ HSI 24 MHz) supports


def describe_status(status):
    """Human-readable description of an ST status code (or missing response)."""
    if status is None:
        return "no response from bootloader (check IAP mode: power on with button held)"
    return ST_DICT.get(status, "unknown status 0x%02X" % status)


class FlasherApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"CH32V003 Flasher v{VERSION} (protocol rev {PROTOCOL_REV})")
        self.root.geometry("600x520")
        self.root.resizable(False, False)

        self.firmware_path = tk.StringVar()
        self.com_port = tk.StringVar()
        self.baud_rate = tk.IntVar(value=BOOTLOADER_BAUD)
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
        self.baud_combo.bind("<<ComboboxSelected>>", self.on_baud_selected)

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

    def on_baud_selected(self, _event=None):
        baud = self.baud_rate.get()
        if baud != BOOTLOADER_BAUD:
            messagebox.showwarning(
                "Unsupported baud rate",
                "The bootloader firmware uses a fixed USART divider "
                "(BRR = 0x34 at HSI 24 MHz), so only %d baud is supported.\n"
                "Any other value cannot communicate with the device.\n"
                "Reverting to %d." % (BOOTLOADER_BAUD, BOOTLOADER_BAUD),
            )
            self.baud_rate.set(BOOTLOADER_BAUD)

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

            ser = open_port(port, baudrate)

            total_start = time.time()

            ser.reset_input_buffer()
            ser.reset_output_buffer()

            self.log("Checking bootloader revision (READ probe)...")
            probe_data, probe_err = probe_protocol_v2(ser)
            if probe_data is None:
                self.log("ERROR: bootloader rev 2 not confirmed: %s" % probe_err)
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
            ok, _status = send_end_best_effort(ser)
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
