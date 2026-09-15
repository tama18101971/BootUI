"""CH32V003 Bootloader Flasher GUI.

Modern Windows 11 Fluent interface for flashing CH32V003 microcontrollers
over UART via Proteus32 IAP bootloader protocol (rev 2).
"""

import os
import sys
import time
import queue
import ctypes
import threading
import tkinter as tk
from tkinter import ttk, filedialog
import serial
import serial.tools.list_ports

try:
    import sv_ttk
    HAS_SV_TTK = True
except ImportError:
    HAS_SV_TTK = False

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

VERSION = "2.2"
BOOTLOADER_BAUD = 460800  # The only rate the fixed BRR (0x34 @ HSI 24 MHz) supports
MAX_LOG_LINES = 2000      # log widget cap; oldest lines are dropped beyond it


# ---------------------------------------------------------------------------
# Windows System Integrations (High-DPI & Dark Titlebar)
# ---------------------------------------------------------------------------

def enable_high_dpi():
    """Enable Per-Monitor DPI awareness on Windows."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def is_windows_dark_theme():
    """Detect whether Windows system apps use Dark Theme."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return val == 0
    except Exception:
        return False


def set_window_dark_titlebar(window, dark: bool):
    """Sync native Windows titlebar with dark/light mode via DwmSetWindowAttribute.

    NOTE: must not call update_idletasks() here — running pending ttk idle
    tasks between sv_ttk.set_theme() and widget creation leaves the base
    '.' style unconfigured, and all controls then render with light colors.

    The caption is drawn by the toplevel wrapper HWND, which Tk creates only
    when the window is first mapped. Before that, GetParent() is 0 and any
    DWM attribute set on winfo_id() would target the inner child (useless,
    and it gets re-parented away on map). Returns True when the attribute
    was applied to a real wrapper; False means "window not mapped yet" and
    the caller should re-apply on <Map>.
    """
    if sys.platform != "win32":
        return True
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
            return False
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (Windows 11 / 10 20H1+), 19 on older builds
        applied = False
        for attr in (20, 19):
            val = ctypes.c_int(1 if dark else 0)
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)
            )
            if res == 0:
                applied = True
                break
        if applied:
            # Force immediate non-client area recalculation so the caption
            # repaints right away instead of waiting for the next DWM frame.
            swp = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, swp)
        return applied
    except Exception:
        return True


def describe_status(status):
    """Human-readable description of an ST status code (or missing response)."""
    if status is None:
        return "нет ответа от загрузчика (проверьте режим IAP: зажмите кнопку при включении питания)"
    return ST_DICT.get(status, f"неизвестный статус 0x{status:02X}")


# ---------------------------------------------------------------------------
# Background Worker Threads (Non-blocking I/O)
# ---------------------------------------------------------------------------

class FlasherWorker(threading.Thread):
    """Worker thread executing erase, write, verify and application launch."""

    def __init__(self, filepath, port, baudrate, msg_queue, abort_event):
        super().__init__(daemon=True)
        self.filepath = filepath
        self.port = port
        self.baudrate = baudrate
        self.queue = msg_queue
        self.abort_event = abort_event

    def run(self):
        ser = None
        success = False
        try:
            with open(self.filepath, "rb") as f:
                data = f.read()

            if not data:
                self.queue.put(("log", "ERROR", "Файл прошивки пуст, нечего прошивать."))
                return

            if len(data) > MAX_FLASH_SIZE:
                self.queue.put((
                    "log", "ERROR",
                    f"Размер прошивки ({len(data)} Б) превышает лимит ({MAX_FLASH_SIZE} Б). "
                    "Прошивка заблокирована для защиты настроек WL_FLASH."
                ))
                return

            if len(data) % BLOCK_SIZE:
                data += b"\xFF" * (BLOCK_SIZE - len(data) % BLOCK_SIZE)
            blocks = len(data) // BLOCK_SIZE

            self.queue.put(("log", "HEADER", "=" * 54))
            self.queue.put(("log", "INFO", f"Запуск прошивки: {os.path.basename(self.filepath)}"))
            self.queue.put(("log", "INFO", f"Порт: {self.port} | Скорость: {self.baudrate} бод"))
            self.queue.put(("log", "INFO", f"Размер: {len(data) / 1024:.2f} КиБ ({blocks} блоков по {BLOCK_SIZE} Б)"))
            self.queue.put(("log", "HEADER", "=" * 54))

            if self.abort_event.is_set():
                self.queue.put(("log", "WARN", "Операция отменена пользователем."))
                return

            # Phase 1: Probe connection
            self.queue.put(("phase", "Проверка загрузчика (READ probe)...", 1, 4))
            self.queue.put(("progress", 0, "Подключение к загрузчику..."))
            ser = open_port(self.port, self.baudrate)
            total_start = time.time()

            ser.reset_input_buffer()
            ser.reset_output_buffer()

            probe_data, probe_err = probe_protocol_v2(ser)
            if probe_data is None:
                self.queue.put(("log", "ERROR", f"Загрузчик rev 2 не ответил: {probe_err}"))
                self.queue.put(("log", "WARN", "Если на плате старый загрузчик rev 1, обновите его через WCH-Link:"))
                self.queue.put(("log", "WARN", "Команды ERASE/END повреждают WL_FLASH на rev 1."))
                return

            self.queue.put(("log", "OK", "Bootloader rev 2 подтвержден, связь стабильна."))

            if self.abort_event.is_set():
                self.queue.put(("log", "WARN", "Операция отменена пользователем."))
                return

            # Phase 2: Erase
            self.queue.put(("phase", "Очистка памяти чипа...", 2, 4))
            self.queue.put(("progress", 10, "Стирание Flash..."))
            self.queue.put(("log", "INFO", "Стирание области приложения (WL_FLASH защищена)..."))

            erase_start = time.time()
            ok, status = send_packet(ser, make_packet(CMD_IAP_ERASE), timeout=TIMEOUT_ERASE)
            if not ok:
                self.queue.put(("log", "ERROR", f"Ошибка стирания: {describe_status(status)}"))
                return
            self.queue.put(("log", "OK", f"Память успешно очищена ({time.time() - erase_start:.2f} с)"))

            if self.abort_event.is_set():
                self.queue.put(("log", "WARN", "Операция отменена пользователем."))
                return

            # Phase 3: Programming
            self.queue.put(("phase", f"Запись блоков памяти (0/{blocks})...", 3, 4))
            self.queue.put(("log", "INFO", f"Запись {blocks} блоков..."))
            prog_start = time.time()

            for idx in range(blocks):
                if self.abort_event.is_set():
                    self.queue.put(("log", "WARN", "Запись прервана пользователем!"))
                    return
                offset = idx * BLOCK_SIZE
                ok, status = send_packet(
                    ser,
                    make_packet(CMD_IAP_PROM, data=data[offset:offset + BLOCK_SIZE]),
                )
                if not ok:
                    self.queue.put((
                        "log", "ERROR",
                        f"Ошибка записи блока {idx} (0x{offset:08X}): {describe_status(status)}"
                    ))
                    return

                pct = 15 + int(((idx + 1) / blocks) * 45)  # 15%..60%
                elapsed = time.time() - prog_start
                speed = ((idx + 1) * BLOCK_SIZE) / elapsed / 1024 if elapsed > 0 else 0
                detail = f"Запись: блок {idx + 1}/{blocks} ({speed:.1f} КиБ/с)"
                self.queue.put(("progress", pct, detail))
                self.queue.put(("phase", f"Запись блоков: {idx + 1}/{blocks}", 3, 4))

            prog_time = time.time() - prog_start
            self.queue.put(("log", "OK", f"Запись завершена за {prog_time:.2f} с"))

            # Phase 4: Verify
            self.queue.put(("phase", f"Верификация данных (0/{blocks})...", 4, 4))
            self.queue.put(("log", "INFO", f"Проверка целостности {blocks} блоков..."))
            verify_start = time.time()

            for idx in range(blocks):
                if self.abort_event.is_set():
                    self.queue.put(("log", "WARN", "Верификация прервана пользователем!"))
                    return
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
                        self.queue.put((
                            "log", "ERROR",
                            f"Несовпадение данных в блоке {idx} (смещение 0x{offset:08X})! Flash отличается от файла."
                        ))
                    else:
                        self.queue.put((
                            "log", "ERROR",
                            f"Ошибка верификации (0x{offset:08X}): {describe_status(status)}"
                        ))
                    return

                pct = 60 + int(((idx + 1) / blocks) * 35)  # 60%..95%
                detail = f"Проверка: блок {idx + 1}/{blocks}"
                self.queue.put(("progress", pct, detail))
                self.queue.put(("phase", f"Проверка блоков: {idx + 1}/{blocks}", 4, 4))

            verify_time = time.time() - verify_start
            self.queue.put(("log", "OK", f"Верификация пройдена успешно ({verify_time:.2f} с)"))

            # Phase 5: Start user application (END command)
            self.queue.put(("progress", 97, "Запуск пользовательской программы..."))
            self.queue.put(("log", "INFO", "Отправка команды запуска приложения..."))
            ok, _status = send_end_best_effort(ser)
            if ok:
                self.queue.put(("log", "OK", "Команда перехода в приложение подтверждена."))
            else:
                self.queue.put(("log", "INFO", "Устройство сразу перешло к исполнению программы (штатно)."))

            time.sleep(0.3)
            total_time = time.time() - total_start
            speed = len(data) / total_time / 1024 if total_time > 0 else 0

            self.queue.put(("log", "HEADER", "=" * 54))
            self.queue.put(("log", "OK", "Прошивка завершена успешно!"))
            self.queue.put(("log", "INFO", f"Время работы: {total_time:.2f} с | Средняя скорость: {speed:.1f} КиБ/с"))
            self.queue.put(("log", "HEADER", "=" * 54))
            self.queue.put(("progress", 100, f"Готово! ({total_time:.1f} с)"))
            self.queue.put(("phase", "Прошивка завершена успешно", 4, 4))
            success = True

        except serial.SerialException as e:
            self.queue.put(("log", "ERROR", f"Ошибка COM-порта: {e}"))
        except Exception as e:
            self.queue.put(("log", "ERROR", f"Сбой операции: {e}"))
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            self.queue.put(("flash_done", success, "Прошивка успешно завершена!" if success else "Ошибка прошивки"))


# ---------------------------------------------------------------------------
# Themed message dialog (replaces native tkinter.messagebox)
# ---------------------------------------------------------------------------

class ThemedMessageBox(tk.Toplevel):
    """Modal sv-ttk themed dialog replacing native Win32 MessageBox.

    Native messageboxes always follow the SYSTEM app theme and cannot be
    rendered dark when the application itself is switched to dark mode.
    """

    ICONS = {"info": ("\u24D8", "#569CD6"), "success": ("\u2713", "#4EC9B0"),
             "warning": ("\u26A0", "#CE9178"), "error": ("\u2716", "#F44747")}
    ICONS_LIGHT = {"info": ("\u24D8", "#005FB8"), "success": ("\u2713", "#107C41"),
                   "warning": ("\u26A0", "#B25900"), "error": ("\u2716", "#C42B1C")}

    def __init__(self, parent, title, message, kind="info", dark=True):
        super().__init__(parent)
        self.dark = dark
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.withdraw()

        icon_char, icon_color = (self.ICONS if dark else self.ICONS_LIGHT).get(kind, self.ICONS["info"])

        body = ttk.Frame(self, padding=(22, 18))
        body.pack(fill="both", expand=True)

        top = ttk.Frame(body)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text=icon_char, foreground=icon_color,
                  font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="nw", padx=(0, 14))
        ttk.Label(top, text=message, wraplength=340, justify="left",
                  font=("Segoe UI", 10)).grid(row=0, column=1, sticky="nw")

        btn_row = ttk.Frame(body)
        btn_row.pack(fill="x", pady=(18, 0))
        ok_btn = ttk.Button(btn_row, text="OK", style="Accent.TButton",
                            width=10, command=self.destroy)
        ok_btn.pack(side="right")

        self.bind("<Return>", lambda e: self.destroy())
        self.bind("<Escape>", lambda e: self.destroy())
        ok_btn.focus_set()

        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{max(px + (pw - w) // 2, 0)}+{max(py + (ph - h) // 3, 0)}")
        self.deiconify()
        self.grab_set()

        if kind in ("warning", "error"):
            self.bell()

        if not set_window_dark_titlebar(self, dark):
            self.bind("<Map>", lambda e: set_window_dark_titlebar(self, dark), add="+")


# ---------------------------------------------------------------------------
# Main Application GUI
# ---------------------------------------------------------------------------

class FlasherApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"CH32V003 Bootloader Flasher v{VERSION}")
        self.root.geometry("720x650")
        self.root.minsize(640, 570)

        # Variables
        self.firmware_path = tk.StringVar()
        self.com_port_display = tk.StringVar()
        self.baud_rate = tk.IntVar(value=BOOTLOADER_BAUD)
        self.port_map = {}  # display_label -> device_name
        self.is_running = False
        self.abort_event = threading.Event()
        self.msg_queue = queue.Queue()
        self.probe_state = None  # None / True / False - last probe outcome
        self.probe_active = False  # probe thread in flight
        self.dark_mode = is_windows_dark_theme()

        # Theme is applied AFTER widgets are created: applying sv_ttk theme
        # before any widget exists (or running idle tasks in between) leaves
        # the base '.' ttk style unconfigured and controls render light.
        self.create_widgets()
        self.apply_theme(self.dark_mode)
        self.refresh_ports()

        # Start periodic queue polling
        self.root.after(40, self.process_queue)

    def create_widgets(self):
        # Configure fonts
        ui_font = ("Segoe UI", 9)
        title_font = ("Segoe UI", 12, "bold")
        sub_font = ("Segoe UI", 8)

        # Main Root Grid
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        container = ttk.Frame(self.root, padding=(14, 10, 14, 10))
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        # Row 3 (Log) expands vertically
        container.rowconfigure(3, weight=1)

        # -------------------------------------------------------------------
        # Header Bar
        # -------------------------------------------------------------------
        header_frame = ttk.Frame(container)
        header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header_frame.columnconfigure(0, weight=1)

        title_box = ttk.Frame(header_frame)
        title_box.grid(row=0, column=0, sticky="w")

        title_lbl = ttk.Label(title_box, text="⚡ CH32V003 Flasher", font=title_font)
        title_lbl.pack(side=tk.LEFT)

        proto_badge = ttk.Label(
            title_box,
            text=f"v{VERSION} · rev {PROTOCOL_REV}",
            font=sub_font,
            foreground="#888888" if self.dark_mode else "#666666",
        )
        proto_badge.pack(side=tk.LEFT, padx=(10, 0), pady=(3, 0))
        self.proto_badge = proto_badge

        self.theme_btn = ttk.Button(
            header_frame,
            text="☀ Светлая тема" if self.dark_mode else "🌙 Тёмная тема",
            command=self.toggle_theme,
        )
        self.theme_btn.grid(row=0, column=1, sticky="e")

        # -------------------------------------------------------------------
        # Card 1: Connection Settings
        # -------------------------------------------------------------------
        conn_frame = ttk.LabelFrame(container, text=" 🔌 Подключение к устройству ", padding=10)
        conn_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        conn_frame.columnconfigure(1, weight=1)

        # Port row
        ttk.Label(conn_frame, text="COM-порт:", font=ui_font).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)

        self.port_combo = ttk.Combobox(
            conn_frame,
            textvariable=self.com_port_display,
            state="readonly",
            font=ui_font,
        )
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=(0, 6), pady=4)

        self.refresh_btn = ttk.Button(conn_frame, text="Обновить", command=self.refresh_ports, width=10)
        self.refresh_btn.grid(row=0, column=2, sticky="e", padx=(0, 6), pady=4)

        self.probe_btn = ttk.Button(conn_frame, text="Проверить", command=self.check_device, width=11)
        self.probe_btn.grid(row=0, column=3, sticky="e", pady=4)

        # Baud rate & Status row
        ttk.Label(conn_frame, text="Скорость:", font=ui_font).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(4, 0))

        baud_info_box = ttk.Frame(conn_frame)
        baud_info_box.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(4, 0))

        baud_val_lbl = ttk.Label(
            baud_info_box,
            text="460800 бод (фиксировано загрузчиком BRR=0x34)",
            font=("Segoe UI", 9, "italic"),
        )
        baud_val_lbl.pack(side=tk.LEFT)

        self.conn_status_badge = ttk.Label(
            baud_info_box,
            text="○ Связь не проверялась",
            font=ui_font,
            foreground="#888888" if self.dark_mode else "#666666",
        )
        self.conn_status_badge.pack(side=tk.RIGHT)

        # -------------------------------------------------------------------
        # Card 2: Firmware File
        # -------------------------------------------------------------------
        file_frame = ttk.LabelFrame(container, text=" 💾 Файл прошивки ", padding=10)
        file_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        file_frame.columnconfigure(0, weight=1)

        file_top = ttk.Frame(file_frame)
        file_top.grid(row=0, column=0, sticky="ew")
        file_top.columnconfigure(0, weight=1)

        self.file_entry = ttk.Entry(file_top, textvariable=self.firmware_path, font=ui_font)
        self.file_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.file_entry.bind("<FocusOut>", lambda e: self.validate_file())
        self.file_entry.bind("<Return>", lambda e: self.validate_file())

        self.browse_btn = ttk.Button(file_top, text="📁 Обзор...", command=self.browse_file, width=12)
        self.browse_btn.grid(row=0, column=1, sticky="e")

        # Memory usage stats & visual gauge
        file_bottom = ttk.Frame(file_frame)
        file_bottom.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        file_bottom.columnconfigure(0, weight=1)

        self.file_info_lbl = ttk.Label(
            file_bottom,
            text="Файл не выбран. Максимальный размер прошивки: 15.75 КиБ (16128 Б).",
            font=ui_font,
        )
        self.file_info_lbl.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.mem_bar = ttk.Progressbar(file_bottom, mode="determinate", maximum=100)
        self.mem_bar.grid(row=1, column=0, sticky="ew")

        # -------------------------------------------------------------------
        # Card 3: Execution, Progress & Log
        # -------------------------------------------------------------------
        work_frame = ttk.LabelFrame(container, text=" ⚡ Процесс и журнал операций ", padding=10)
        work_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 6))
        work_frame.columnconfigure(0, weight=1)
        # The log box row (4) is weighted below when it is created; only it
        # should expand vertically.

        # Status & phase indicators
        progress_info = ttk.Frame(work_frame)
        progress_info.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        progress_info.columnconfigure(0, weight=1)

        self.phase_lbl = ttk.Label(progress_info, text="Готов к работе", font=("Segoe UI", 9, "bold"))
        self.phase_lbl.grid(row=0, column=0, sticky="w")

        self.pct_lbl = ttk.Label(progress_info, text="0%", font=("Segoe UI", 9, "bold"))
        self.pct_lbl.grid(row=0, column=1, sticky="e")

        self.progress = ttk.Progressbar(work_frame, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        # Control Buttons
        btn_box = ttk.Frame(work_frame)
        btn_box.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        btn_box.columnconfigure(0, weight=1)

        self.detail_lbl = ttk.Label(btn_box, text="", font=sub_font, foreground="#888888")
        self.detail_lbl.grid(row=0, column=0, sticky="w")

        self.abort_btn = ttk.Button(btn_box, text="⏹️ Отмена", command=self.abort_operation, state=tk.DISABLED, width=12)
        self.abort_btn.grid(row=0, column=1, sticky="e", padx=(0, 8))

        self.flash_btn = ttk.Button(
            btn_box,
            text="⚡ Прошить",
            style="Accent.TButton" if HAS_SV_TTK else "TButton",
            command=self.start_flash,
            width=16,
        )
        self.flash_btn.grid(row=0, column=2, sticky="e")

        # Log Toolbar
        log_header = ttk.Frame(work_frame)
        log_header.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        log_header.columnconfigure(0, weight=1)

        ttk.Label(log_header, text="Лог взаимодействия:", font=sub_font).grid(row=0, column=0, sticky="w")

        log_actions = ttk.Frame(log_header)
        log_actions.grid(row=0, column=1, sticky="e")

        copy_btn = ttk.Button(log_actions, text="📋 Копировать", command=self.copy_log, width=14)
        copy_btn.pack(side=tk.LEFT, padx=(0, 4))

        save_btn = ttk.Button(log_actions, text="💾 Сохранить", command=self.save_log, width=13)
        save_btn.pack(side=tk.LEFT, padx=(0, 4))

        clear_btn = ttk.Button(log_actions, text="🗑️ Очистить", command=self.clear_log, width=12)
        clear_btn.pack(side=tk.LEFT)

        # Log Text Box with custom scrollbar
        log_box = ttk.Frame(work_frame)
        log_box.grid(row=4, column=0, sticky="nsew")
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        work_frame.rowconfigure(4, weight=1)

        mono_font = ("Cascadia Mono", 9) if sys.platform == "win32" else ("Courier New", 9)
        self.log_text = tk.Text(
            log_box,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=mono_font,
            bd=0,
            padx=8,
            pady=8,
            relief=tk.FLAT,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(log_box, orient=tk.VERTICAL, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        # -------------------------------------------------------------------
        # Bottom Status Bar
        # -------------------------------------------------------------------
        status_bar = ttk.Frame(container)
        status_bar.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        status_bar.columnconfigure(0, weight=1)

        self.status_left = ttk.Label(status_bar, text="Готов к работе", font=sub_font)
        self.status_left.grid(row=0, column=0, sticky="w")

        self.status_right = ttk.Label(status_bar, text="Портов: 0", font=sub_font)
        self.status_right.grid(row=0, column=1, sticky="e")

    # -----------------------------------------------------------------------
    # Theme & Appearance Management
    # -----------------------------------------------------------------------

    def set_probe_badge(self):
        """Render connection badge text and color for the current probe state."""
        colors = {
            None: ("○ Связь не проверялась", "#888888" if self.dark_mode else "#666666"),
            True: ("● Загрузчик v2 на связи", "#4EC9B0" if self.dark_mode else "#107C41"),
            False: ("● Нет ответа загрузчика", "#F44747" if self.dark_mode else "#C42B1C"),
            "busy": ("● Опрос загрузчика...", "#CE9178" if self.dark_mode else "#B25900"),
        }
        text, color = colors[self.probe_state]
        self.conn_status_badge.config(text=text, foreground=color)

    def apply_theme(self, dark):
        """Apply sv-ttk theme, native titlebar and all theme-dependent colors."""
        self.dark_mode = dark
        if HAS_SV_TTK:
            sv_ttk.set_theme("dark" if dark else "light")
        # At first startup the window is still unmapped and its toplevel
        # wrapper HWND does not exist, so the DWM call cannot be applied yet;
        # schedule a one-shot re-apply when the window is actually shown.
        if not set_window_dark_titlebar(self.root, dark):
            self.root.bind("<Map>", self._apply_titlebar_on_map, add="+")
        self.theme_btn.config(text="☀ Светлая тема" if dark else "🌙 Тёмная тема")
        self.proto_badge.config(foreground="#888888" if dark else "#666666")
        self.set_probe_badge()
        self.update_log_colors()

    def _apply_titlebar_on_map(self, event=None):
        if event is not None and str(event.widget) != str(self.root):
            return
        set_window_dark_titlebar(self.root, self.dark_mode)
        self.root.unbind("<Map>")

    def toggle_theme(self):
        self.apply_theme(not self.dark_mode)

    def update_log_colors(self):
        """Set syntax highlighting palette for the Tk Text widget."""
        if self.dark_mode:
            bg_col = "#1E1E1E"
            fg_col = "#D4D4D4"
            ts_col = "#858585"
            ok_col = "#4EC9B0"
            err_col = "#F44747"
            warn_col = "#CE9178"
            head_col = "#569CD6"
            sel_bg = "#264F78"
        else:
            bg_col = "#FAFAFA"
            fg_col = "#202020"
            ts_col = "#6E6E6E"
            ok_col = "#107C41"
            err_col = "#C42B1C"
            warn_col = "#B25900"
            head_col = "#005FB8"
            sel_bg = "#B4D8FD"

        self.log_text.config(
            bg=bg_col,
            fg=fg_col,
            insertbackground=fg_col,
            selectbackground=sel_bg,
            selectforeground=fg_col,
        )

        mono_family = "Cascadia Mono" if sys.platform == "win32" else "Courier New"
        self.log_text.tag_configure("ts", foreground=ts_col)
        self.log_text.tag_configure("INFO", foreground=fg_col)
        self.log_text.tag_configure("OK", foreground=ok_col, font=(mono_family, 9, "bold"))
        self.log_text.tag_configure("WARN", foreground=warn_col, font=(mono_family, 9, "bold"))
        self.log_text.tag_configure("ERROR", foreground=err_col, font=(mono_family, 9, "bold"))
        self.log_text.tag_configure("HEADER", foreground=head_col, font=(mono_family, 9, "bold"))

    # -----------------------------------------------------------------------
    # File & Port Handling
    # -----------------------------------------------------------------------

    def browse_file(self):
        filepath = filedialog.askopenfilename(
            title="Выберите файл прошивки",
            filetypes=[("Файлы прошивки (*.bin)", "*.bin"), ("Все файлы (*.*)", "*.*")],
        )
        if filepath:
            self.firmware_path.set(filepath)
            self.validate_file()

    def validate_file(self):
        path = self.firmware_path.get().strip()
        if not path or not os.path.exists(path):
            self.file_info_lbl.config(
                text="Файл не выбран. Максимальный размер прошивки: 15.75 КиБ (16128 Б).",
                foreground="",
            )
            self.mem_bar["value"] = 0
            return False

        try:
            size = os.path.getsize(path)
            pct = min(100.0, (size / MAX_FLASH_SIZE) * 100)
            self.mem_bar["value"] = pct

            name = os.path.basename(path)
            if size == 0:
                self.file_info_lbl.config(
                    text=f"⚠️ {name}: файл пуст!",
                    foreground="#C42B1C",
                )
                return False
            elif size > MAX_FLASH_SIZE:
                self.file_info_lbl.config(
                    text=f"❌ {name}: {size} Б ({size/1024:.2f} КиБ) превышает лимит {MAX_FLASH_SIZE} Б!",
                    foreground="#C42B1C",
                )
                return False
            else:
                free = MAX_FLASH_SIZE - size
                self.file_info_lbl.config(
                    text=f"✓ {name}: {size} Б ({size/1024:.2f} КиБ) из {MAX_FLASH_SIZE/1024:.2f} КиБ ({pct:.1f}% памяти занято, {free} Б свободно)",
                    foreground="#107C41" if not self.dark_mode else "#4EC9B0",
                )
                return True
        except Exception as e:
            self.file_info_lbl.config(text=f"Ошибка чтения файла: {e}", foreground="#C42B1C")
            return False

    def refresh_ports(self):
        ports = serial.tools.list_ports.comports()
        self.port_map.clear()
        display_list = []

        for p in sorted(ports, key=lambda x: x.device):
            desc = p.description.strip() if p.description else ""
            if desc and desc != p.device and not desc.startswith("n/a"):
                label = f"{p.device} ({desc})"
            else:
                label = p.device
            self.port_map[label] = p.device
            display_list.append(label)

        self.port_combo["values"] = display_list
        if display_list:
            if not self.com_port_display.get() or self.com_port_display.get() not in self.port_map:
                self.port_combo.current(0)
            self.status_right.config(text=f"Портов обнаружено: {len(display_list)}")
            self.log("INFO", f"Найдено доступных COM-портов: {len(display_list)}")
        else:
            self.com_port_display.set("")
            self.status_right.config(text="Порты не обнаружены")
            self.log("WARN", "COM-порты не обнаружены. Подключите USB-UART адаптер и нажмите 'Обновить'.")

    def get_selected_port(self):
        disp = self.com_port_display.get()
        return self.port_map.get(disp, disp)

    # -----------------------------------------------------------------------
    # Logging Operations
    # -----------------------------------------------------------------------

    def log(self, level, message):
        """Append a message with timestamp and level tags to the log widget."""
        ts = f"[{time.strftime('%H:%M:%S')}] "
        tag = level if level in ("INFO", "OK", "WARN", "ERROR", "HEADER") else "INFO"

        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, ts, "ts")
        self.log_text.insert(tk.END, f"{message}\n", tag)
        # Cap log length: drop oldest lines beyond MAX_LOG_LINES so long
        # sessions with many flash/probe runs cannot grow the widget forever.
        line_count = int(self.log_text.index("end-1c").split(".", 1)[0])
        if line_count > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def copy_log(self):
        content = self.log_text.get("1.0", tk.END).strip()
        if content:
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.status_left.config(text="Журнал скопирован в буфер обмена")
            self.root.after(2500, lambda: self.status_left.config(text="Готов к работе"))

    def save_log(self):
        content = self.log_text.get("1.0", tk.END).strip()
        if not content:
            self.show_dialog("info", "Сохранение журнала", "Журнал пуст.")
            return
        filename = filedialog.asksaveasfilename(
            title="Сохранить журнал операций",
            defaultextension=".txt",
            filetypes=[("Текстовые файлы (*.txt)", "*.txt"), ("Все файлы (*.*)", "*.*")],
        )
        if filename:
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(content)
                self.status_left.config(text=f"Журнал сохранен в {os.path.basename(filename)}")
            except Exception as e:
                self.show_dialog("error", "Ошибка сохранения", f"Не удалось сохранить файл:\n{e}")

    # -----------------------------------------------------------------------
    # Probe Device (Non-destructive Check)
    # -----------------------------------------------------------------------

    def check_device(self):
        if self.is_running or self.probe_active:
            return

        port = self.get_selected_port()
        if not port:
            self.show_dialog("warning", "Проверка связи", "Выберите COM-порт для проверки.")
            return

        self.probe_active = True
        self.probe_state = "busy"
        self.set_probe_badge()
        self._set_ui_busy("probe")
        self.status_left.config(text=f"Проверка связи на {port}...")

        threading.Thread(
            target=self._probe_thread,
            args=(port, self.baud_rate.get()),
            daemon=True,
        ).start()

    def _probe_thread(self, port, baudrate):
        ser = None
        try:
            self.msg_queue.put(("log", "INFO", f"Проверка загрузчика на порту {port} ({baudrate} бод)..."))
            ser = open_port(port, baudrate)
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            probe_data, probe_err = probe_protocol_v2(ser)
            if probe_data is not None:
                self.msg_queue.put(("probe_result", True, None))
                self.msg_queue.put(("log", "OK", "Bootloader rev 2 успешно подтвержден (ответ 64 Б получен)."))
            else:
                self.msg_queue.put(("probe_result", False, None))
                self.msg_queue.put(("log", "WARN", f"Устройство не отвечает: {probe_err}"))
                self.msg_queue.put(("log", "INFO", "Совет: зажмите кнопку на плате перед подачей питания для входа в IAP."))
        except Exception as e:
            self.msg_queue.put(("probe_result", False, None))
            self.msg_queue.put(("log", "ERROR", f"Не удалось открыть порт {port}: {e}"))
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            self.msg_queue.put(("probe_done", None, None))

    # -----------------------------------------------------------------------
    # Flash Process Management
    # -----------------------------------------------------------------------

    def show_dialog(self, kind, title, message):
        """Modal themed dialog matching the current app theme."""
        self.root.wait_window(ThemedMessageBox(self.root, title, message, kind, self.dark_mode))

    def _set_ui_busy(self, mode):
        """Single source of truth for control states: 'idle' | 'flash' | 'probe'."""
        if mode == "flash":
            self.flash_btn.config(state=tk.DISABLED)
            self.probe_btn.config(state=tk.DISABLED)
            self.refresh_btn.config(state=tk.DISABLED)
            self.browse_btn.config(state=tk.DISABLED)
            self.port_combo.config(state=tk.DISABLED)
            self.file_entry.config(state=tk.DISABLED)
            self.abort_btn.config(state=tk.NORMAL)
        elif mode == "probe":
            self.flash_btn.config(state=tk.DISABLED)
            self.probe_btn.config(state=tk.DISABLED)
            self.refresh_btn.config(state=tk.DISABLED)
            self.abort_btn.config(state=tk.DISABLED)
        else:  # idle
            self.flash_btn.config(state=tk.NORMAL)
            self.probe_btn.config(state=tk.NORMAL)
            self.refresh_btn.config(state=tk.NORMAL)
            self.browse_btn.config(state=tk.NORMAL)
            self.port_combo.config(state="readonly")
            self.file_entry.config(state=tk.NORMAL)
            self.abort_btn.config(state=tk.DISABLED)

    def start_flash(self):
        if self.is_running or self.probe_active:
            return

        if not self.validate_file():
            self.show_dialog("warning", "Предупреждение", "Пожалуйста, выберите корректный файл прошивки (.bin).")
            return

        port = self.get_selected_port()
        if not port:
            self.show_dialog("warning", "Предупреждение", "Пожалуйста, выберите COM-порт устройства.")
            return

        self.is_running = True
        self.abort_event.clear()
        self._set_ui_busy("flash")

        self.progress["value"] = 0
        self.pct_lbl.config(text="0%")
        self.status_left.config(text="Выполняется прошивка...")

        worker = FlasherWorker(
            filepath=self.firmware_path.get().strip(),
            port=port,
            baudrate=self.baud_rate.get(),
            msg_queue=self.msg_queue,
            abort_event=self.abort_event,
        )
        worker.start()

    def abort_operation(self):
        if self.is_running:
            self.abort_btn.config(state=tk.DISABLED)
            self.abort_event.set()
            self.log("WARN", "Запрошена отмена операции, ожидание завершения текущего блока...")
            self.status_left.config(text="Отмена операции...")

    # -----------------------------------------------------------------------
    # Queue Dispatcher
    # -----------------------------------------------------------------------

    def process_queue(self):
        """Handle background thread events safely on the Tk main thread."""
        # Themed dialogs below run a nested event loop (wait_window), which
        # re-enters this callback via the pending after() tick. Guard so the
        # polling chain stays exactly one — the outer tick reschedules when
        # its dialog closes; nested ticks just return.
        if getattr(self, "_pq_running", False):
            return
        self._pq_running = True
        pending_phase = None
        pending_progress = None
        try:
            try:
                if not self.root.winfo_exists():
                    return
            except tk.TclError:
                return

            while True:
                item = self.msg_queue.get_nowait()
                msg_type = item[0]

                if msg_type == "log":
                    _, level, message = item
                    self.log(level, message)

                elif msg_type == "phase":
                    pending_phase = item

                elif msg_type == "progress":
                    pending_progress = item

                elif msg_type == "probe_result":
                    self.probe_state = item[1]
                    self.set_probe_badge()

                elif msg_type == "probe_done":
                    self.probe_active = False
                    if not self.is_running:
                        self._set_ui_busy("idle")
                        self.status_left.config(text="Готов к работе")

                elif msg_type == "flash_done":
                    _, success, msg = item
                    self.is_running = False
                    self._set_ui_busy("idle")

                    if success:
                        self.progress["value"] = 100
                        self.pct_lbl.config(text="100%")
                        self.phase_lbl.config(text="✓ Прошивка успешно завершена!")
                        self.status_left.config(text="Прошивка завершена успешно")
                        self.show_dialog("success", "Успешно", "Прошивка микроконтроллера успешно завершена!")
                    else:
                        self.phase_lbl.config(text="❌ Ошибка прошивки")
                        self.status_left.config(text="Ошибка во время прошивки")
                        self.show_dialog("error", "Ошибка", f"{msg}. Подробности см. в журнале операций.")

        except queue.Empty:
            pass
        finally:
            self._pq_running = False
            try:
                alive = self.root.winfo_exists()
            except tk.TclError:
                alive = False
            if alive:
                # Apply coalesced updates after the drain so widgets are
                # touched once per tick, then keep the single poll chain alive.
                if pending_phase is not None:
                    _, phase_text, step, total = pending_phase
                    self.phase_lbl.config(text=f"Этап [{step}/{total}]: {phase_text}")

                if pending_progress is not None:
                    _, pct, detail = pending_progress
                    self.progress["value"] = pct
                    self.pct_lbl.config(text=f"{pct}%")
                    if detail:
                        self.detail_lbl.config(text=detail)

                self.root.after(40, self.process_queue)


def main():
    enable_high_dpi()
    root = tk.Tk()
    app = FlasherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
