import os
import struct
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import serial
import serial.tools.list_ports

VERSION = "2.0"

SYNC_HEAD1 = 0xAA
SYNC_HEAD2 = 0x55

CMD_IAP_PROM   = 0x80
CMD_IAP_ERASE  = 0x81
CMD_IAP_VERIFY = 0x82
CMD_IAP_END    = 0x83

FLASH_BASE = 0x08000000
BLOCK_SIZE = 64
MAX_RETRIES = 3

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]


def make_packet(cmd, addr=None, data=None):
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
    expected_len = 6
    response = bytearray()
    start = time.time()

    while time.time() - start < timeout:
        if ser.in_waiting:
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
    for attempt in range(MAX_RETRIES):
        ser.write(packet)
        ser.flush()

        if not expect_response:
            return True

        if wait_response(ser):
            return True

        ser.reset_input_buffer()

    return False


class FlasherApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"CH32V003 Flasher v{VERSION}")
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
            self.file_info_label.config(text=f"{os.path.basename(filepath)} ({size/1024:.1f} KiB)")
            self.log(f"Selected: {filepath}")

    def refresh_ports(self):
        ports = serial.tools.list_ports.comports()
        port_list = [f"{p.device}" for p in sorted(ports)]
        self.port_combo["values"] = port_list
        if port_list:
            self.port_combo.current(0)
        self.log(f"Found {len(port_list)} COM port(s)")

    def log(self, message):
        self.log_text.config(state=tk.NORMAL)
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
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

        self.is_flashing = True
        self.flash_btn.config(state=tk.DISABLED)
        self.progress["value"] = 0

        thread = threading.Thread(target=self.flash_thread, daemon=True)
        thread.start()

    def flash_thread(self):
        try:
            filepath = self.firmware_path.get()
            port = self.com_port.get()
            baudrate = self.baud_rate.get()

            with open(filepath, "rb") as f:
                data = f.read()

            self.log("=" * 50)
            self.log(f"Starting flash: {os.path.basename(filepath)}")
            self.log(f"Port: {port}, Baud: {baudrate}")
            self.log(f"Size: {len(data)/1024:.1f} KiB")
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

            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()

                self.log("Erasing flash...")
                if not send_packet(ser, make_packet(CMD_IAP_ERASE, addr=FLASH_BASE)):
                    self.log("ERROR: Erase failed")
                    return

                self.log("Erase complete")
                time.sleep(0.1)

                self.log("Programming...")
                prog_start = time.time()

                total_blocks = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE

                for offset in range(0, len(data), BLOCK_SIZE):
                    chunk = data[offset:offset+BLOCK_SIZE]
                    actual_chunk_size = len(chunk)

                    if actual_chunk_size < BLOCK_SIZE:
                        chunk = chunk.ljust(BLOCK_SIZE, b'\xFF')

                    if not send_packet(
                        ser,
                        make_packet(CMD_IAP_PROM, data=chunk),
                    ):
                        self.log(f"ERROR: Programming failed at offset 0x{offset:08X}")
                        return

                    progress = (offset + actual_chunk_size) / len(data) * 100
                    self.root.after(0, self.update_progress, progress)

                prog_time = time.time() - prog_start
                self.log(f"Programming complete ({prog_time:.2f}s)")

                self.log("Finalizing...")
                if not send_packet(
                    ser,
                    make_packet(CMD_IAP_VERIFY, addr=FLASH_BASE),
                ):
                    self.log("ERROR: Verify failed")
                    return

                send_packet(
                    ser,
                    make_packet(CMD_IAP_END),
                    expect_response=False,
                )

                time.sleep(0.5)
                self.log("Finalize complete")

                total_time = time.time() - total_start
                speed = len(data) / total_time / 1024 if total_time > 0 else 0

                self.log("=" * 50)
                self.log(f"Flash completed successfully!")
                self.log(f"Elapsed: {total_time:.2f}s, Speed: {speed:.1f} KiB/s")
                self.log("=" * 50)

                self.root.after(0, self.flash_complete, True)

            finally:
                ser.close()

        except serial.SerialException as e:
            self.log(f"Serial error: {e}")
            self.root.after(0, self.flash_complete, False)

        except Exception as e:
            self.log(f"Error: {e}")
            self.root.after(0, self.flash_complete, False)

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
