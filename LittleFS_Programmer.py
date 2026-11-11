# -*- coding: utf-8 -*-
"""LittleFS Programmer - build, verify and flash a LittleFS image to an ESP32."""
import os
import sys

# ---- esptool passthrough: the exe re-launches itself to run esptool ----------
if len(sys.argv) > 1 and sys.argv[1] == "--esptool":
    import io
    try:
        sys.stdout = io.TextIOWrapper(open(1, "wb", closefd=False), encoding="utf-8",
                                      errors="replace", write_through=True)
    except Exception:
        pass
    sys.stderr = sys.stdout
    os.environ["NO_COLOR"] = "1"
    os.environ["COLUMNS"] = "200"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    import esptool
    sys.argv = ["esptool"] + sys.argv[2:]
    try:
        esptool._main()
    except SystemExit as e:
        sys.exit(e.code)
    except BaseException as e:  # never let a windowed exe pop up a traceback dialog
        print(f"A fatal error occurred: {e}")
        sys.exit(2)
    sys.exit(0)

import re
import json
import time
import queue
import struct
import shutil
import hashlib
import tempfile
import threading
import subprocess
import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont
import webbrowser
try:
    import winsound
except Exception:
    winsound = None

from PIL import Image, ImageTk

APP = "LittleFS Programmer"
VERSION = "1.0"
BLOCK, PAGE = 4096, 256
DEFAULT_OFFSET, DEFAULT_SIZE = 0x200000, 0x200000
PREF_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}   # CP210x, CH340, FTDI, Espressif
CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FROZEN = getattr(sys, "frozen", False)

# ------------------------------------------------------------------ paths ----
def app_dir():
    return os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))

def res(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)

def find_tool():
    for p in (os.path.join(app_dir(), "mklittlefs.exe"),
              os.path.join(app_dir(), "assets", "mklittlefs.exe"),
              res("mklittlefs.exe")):
        if os.path.isfile(p):
            return p
    return None

def output_dir():
    d = os.path.join(app_dir(), "output")
    try:
        os.makedirs(d, exist_ok=True)
        t = os.path.join(d, ".w")
        open(t, "w").close(); os.remove(t)
        return d
    except Exception:
        d = os.path.join(os.path.expanduser("~"), "Documents", "LittleFS_Programmer_Output")
        os.makedirs(d, exist_ok=True)
        return d

def settings_path():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LittleFS Programmer")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")

def esp_cmd(*a):
    if FROZEN:
        return [sys.executable, "--esptool", *a]
    return [sys.executable, os.path.abspath(__file__), "--esptool", *a]

# ---------------------------------------------------------------- helpers ----
def run_stream(cmd, on_line=None, timeout=None, cwd=None):
    """Run a command, stream output (split on CR/LF). Returns (code, text)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=CNW, cwd=cwd)
    timer = None
    timed_out = []
    if timeout:
        def kill():
            timed_out.append(1)
            try: p.kill()
            except Exception: pass
        timer = threading.Timer(timeout, kill)
        timer.start()
    buf, out = b"", []
    try:
        while True:
            chunk = p.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]+", buf)
            buf = parts.pop()
            for part in parts:
                line = part.decode("utf-8", "replace").rstrip()
                if line:
                    out.append(line)
                    if on_line:
                        on_line(line)
        if buf.strip():
            line = buf.decode("utf-8", "replace").rstrip()
            out.append(line)
            if on_line:
                on_line(line)
    finally:
        code = p.wait()
        if timer:
            timer.cancel()
    if timed_out:
        code = -999
    return code, "\n".join(out)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def tree(root):
    res_ = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            full = os.path.join(dp, f)
            res_[os.path.relpath(full, root).replace("\\", "/")] = full
    return res_

def fmt_size(n):
    if n >= 1 << 20: return f"{n / (1 << 20):.2f} MB"
    if n >= 1 << 10: return f"{n / (1 << 10):.1f} KB"
    return f"{n} B"

def parse_int(s):
    s = str(s).strip().lower().replace(",", "")
    if not s: raise ValueError
    if s.startswith("0x"): return int(s, 16)
    return int(s, 10)

def parse_ptable(b):
    parts = []
    for i in range(0, len(b) - 31, 32):
        e = b[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        t, st = e[2], e[3]
        off, sz = struct.unpack("<II", e[4:12])
        name = e[12:28].split(b"\0")[0].decode("ascii", "replace")
        parts.append(dict(type=t, sub=st, offset=off, size=sz, name=name))
    return parts

def pick_fs_partition(parts):
    for p in parts:
        if p["type"] == 1 and p["sub"] == 0x82:
            return p
    for p in parts:
        if p["type"] == 1 and re.search(r"littlefs|spiffs|ffat|storage", p["name"], re.I):
            return p
    return None

def chip_family(name):
    n = (name or "").upper()
    for key, val in (("ESP32-S3", "esp32s3"), ("ESP32-S2", "esp32s2"), ("ESP32-C3", "esp32c3"),
                     ("ESP32-C6", "esp32c6"), ("ESP32-H2", "esp32h2"), ("ESP32-C2", "esp32c2"),
                     ("ESP32-P4", "esp32p4"), ("ESP32", "esp32"), ("ESP8266", "esp8266")):
        if n.startswith(key):
            return val
    return "auto"

# ---------------------------------------------------------------- strings ----
S = {
"fa": dict(
 sub="ساخت، بررسی و برنامه‌ریزی فایل‌سیستم ESP32",
 s1="پوشه", s2="ساخت", s3="بررسی", s4="برنامه‌ریزی",
 h1="پوشه فایل‌ها", h2="دستگاه ESP32",
 choose="انتخاب پوشه data…", nofolder="هنوز پوشه‌ای انتخاب نشده است",
 m1="تعداد فایل", m2="حجم داده", m3="پارتیشن", usage="استفاده از پارتیشن",
 auto="خودکار", man="دستی", scan="اسکن مجدد", port="پورت", chip="چیپ", baud="سرعت", off="آدرس", size="حجم", refresh="تازه",
 c1="ابزار mklittlefs پیدا شد", c2="فایل bin ساخته شد و حجم آن درست است",
 c3="فایل‌های داخل bin با پوشه مبدأ یکسان‌اند", c4="آدرس و حجم با پارتیشن چیپ هم‌خوان است",
 c4w="هم‌خوانی با چیپ تأیید نشد",
 b_pick="انتخاب پوشه", b_prog="برنامه‌ریزی روی ESP32", b_busy="در حال انجام…", b_new="کار جدید",
 b_open="باز کردن پوشه خروجی", b_report="ذخیره گزارش", b_rebuild="ساخت دوباره",
 g0=("مرحله ۱: پوشه فایل‌ها را انتخاب کنید", "پوشه data (شامل index.html و بقیه فایل‌ها) را انتخاب کنید؛ بقیه کارها خودکار است."),
 g1=("در حال ساخت و بررسی…", "چند ثانیه صبر کنید."),
 g2=("همه‌چیز آماده است", "برد را وصل نگه دارید و روی دکمه‌ی برنامه‌ریزی بزنید."),
 g3=("در حال نوشتن روی ESP32…", "کابل USB را جدا نکنید و برد را خاموش نکنید."),
 g4=("برنامه‌ریزی با موفقیت تمام شد", "فایل‌ها روی چیپ نوشته و با hash تأیید شدند. می‌توانید برد را جدا کنید."),
 e_nodata=("پوشه خالی است", "در پوشه انتخابی هیچ فایلی پیدا نشد. پوشه درست data را انتخاب کنید."),
 e_tool=("ابزار mklittlefs.exe پیدا نشد", "فایل mklittlefs.exe را کنار برنامه (یا داخل پوشه assets) قرار دهید."),
 e_size=("حجم فایل‌ها از پارتیشن بیشتر است", "حجم داده {data} ولی پارتیشن فقط {part} است. فایل‌های اضافه را حذف کنید."),
 e_build=("ساخت فایل bin ناموفق بود", "جزئیات در لاگ پایین صفحه نوشته شده است."),
 e_verify=("بررسی فایل‌ها ناموفق بود", "فایل ساخته‌شده با پوشه مبدأ یکسان نیست؛ جزئیات در لاگ است."),
 e_dev=("دستگاه پیدا نشد", "کابل USB را بررسی کنید، سپس «اسکن مجدد» را بزنید یا از حالت «دستی» استفاده کنید."),
 e_port=("پورت انتخاب نشده است", "در حالت دستی یک پورت COM انتخاب کنید."),
 e_off=("آدرس یا حجم نامعتبر است", "آدرس باید مثل 0x200000 باشد و حجم مضربی از 4096."),
 e_fit=("فایل bin در حافظه‌ی چیپ جا نمی‌شود", "آدرس + حجم از اندازه‌ی فلش ({flash}) بیشتر است."),
 e_write=("نوشتن روی ESP32 ناموفق بود", "کابل و پورت را بررسی کنید؛ در صورت نیاز دکمه BOOT برد را نگه دارید و دوباره بزنید."),
 e_busy_port=("پورت در دسترس نیست", "برنامه‌ی دیگری (مثل Serial Monitor) پورت را باز نگه داشته است؛ آن را ببندید."),
 dev_scan="در حال جست‌وجوی دستگاه…", dev_none="دستگاهی شناسایی نشد", dev_ok="دستگاه شناسایی شد",
 dev_nopt="جدول پارتیشن پیدا نشد؛ از مقدارهای دستی استفاده می‌شود",
 dev_noport="هیچ پورت COM وصل نیست",
 dev_partial="پورت {port} پیدا شد ولی برد پاسخ نداد",
 dev_busy="پورت {port} در دسترس نیست",
 dev_busy_hint="برنامه‌ی دیگری (مثل Arduino IDE یا Serial Monitor) پورت را باز نگه داشته است. آن را ببندید و «اسکن مجدد» را بزنید.",
 dev_hint="برد در حالت دانلود نیست. دکمه BOOT را نگه دارید، یک بار EN (RESET) را بزنید، BOOT را رها کنید و «اسکن مجدد» را بزنید. می‌توانید مستقیم هم برنامه‌ریزی را امتحان کنید.",
 b_go="ادامه", b_cancel="انصراف", b_ok="باشه", b_try="امتحان مستقیم",
 boot_t="دکمه BOOT را فشار دهید",
 boot_msg="برد در حالت دانلود نیست.\n\n۱) دکمه BOOT را نگه دارید\n۲) هم‌زمان یک بار دکمه EN (RESET) را بزنید\n۳) BOOT را تا شروع نوشتن نگه دارید",
 boot_wait="در انتظار فشردن BOOT…", boot_seen="BOOT شناسایی شد ✔ در حال اتصال…",
 boot_normal="برد عادی بوت شد. BOOT را نگه دارید و دوباره EN را بزنید.",
 boot_busy="پورت در دسترس نیست. برنامه‌ی دیگری آن را باز نگه داشته است.",
 fin_t="برنامه‌ریزی تمام شد",
 fin_msg="فایل‌ها نوشته و از روی چیپ دوباره خوانده و تأیید شدند.\n\nدکمه BOOT را رها کنید و یک بار دکمه EN (RESET) را بزنید تا برنامه‌ی برد با فایل‌های جدید اجرا شود.",
 g5=("در حال خواندن از چیپ برای تأیید نهایی…", "فایل‌های نوشته‌شده از روی حافظه‌ی ESP32 دوباره خوانده و با فایل اصلی مقایسه می‌شوند."),
 g4v=("برنامه‌ریزی با موفقیت تمام شد", "فایل‌ها روی ESP32 نوشته شدند و از روی چیپ دوباره خوانده و با فایل اصلی یکسان بودند."),
 e_boot=("برد وارد حالت دانلود نشد", "دکمه BOOT را نگه دارید، یک بار دکمه EN (RESET) را بزنید، BOOT را رها کنید و دوباره روی برنامه‌ریزی بزنید."),
 confirm="فایل: {name} ({size})\nپورت: {port} ({chip})\nآدرس نوشتن: {off}\n\nداده‌ی قبلی این بخش از حافظه پاک می‌شود.\nادامه می‌دهید؟",
 confirm_warn="\n\nهشدار: آدرس واردشده با پارتیشن چیپ ({pt}) فرق دارد!",
 confirm_t="تأیید نوشتن", st0="آماده", st1="در حال ساخت…", st2="آماده برای برنامه‌ریزی", st3="در حال نوشتن…", st4="کامل شد",
 pick_title="پوشه data را انتخاب کنید",
 about="درباره ما", about_text="من پیمان غلامی هستم؛ متخصص تجهیزات IOT و سیستم‌های کنترلر تلمتری",
 contact="جهت ارتباط با من", copy="کپی", copied="کپی شد", close="بستن"),
"en": dict(
 sub="Build, verify and program the ESP32 filesystem",
 s1="Folder", s2="Build", s3="Verify", s4="Program",
 h1="Files folder", h2="ESP32 device",
 choose="Choose data folder…", nofolder="No folder selected yet",
 m1="Files", m2="Data size", m3="Partition", usage="Partition usage",
 auto="Auto", man="Manual", scan="Rescan", port="Port", chip="Chip", baud="Baud", off="Offset", size="Size", refresh="Refresh",
 c1="mklittlefs tool found", c2="bin file built and its size is correct",
 c3="Files inside the bin match the source folder", c4="Offset and size match the chip partition",
 c4w="Chip match not confirmed",
 b_pick="Choose folder", b_prog="Program ESP32", b_busy="Working…", b_new="New job",
 b_open="Open output folder", b_report="Save report", b_rebuild="Rebuild",
 g0=("Step 1: choose the files folder", "Pick the data folder (with index.html and the rest). Everything else is automatic."),
 g1=("Building and verifying…", "Wait a few seconds."),
 g2=("Everything is ready", "Keep the board connected and press Program ESP32."),
 g3=("Writing to ESP32…", "Do not unplug the USB cable or power off the board."),
 g4=("Programming finished successfully", "Files were written and verified by hash. You can unplug the board."),
 e_nodata=("The folder is empty", "No files were found in the chosen folder. Pick the correct data folder."),
 e_tool=("mklittlefs.exe not found", "Put mklittlefs.exe next to the program (or in the assets folder)."),
 e_size=("Files are larger than the partition", "Data is {data} but the partition is only {part}. Remove extra files."),
 e_build=("Building the bin file failed", "Details are in the log below."),
 e_verify=("File verification failed", "The built image differs from the source folder; see the log."),
 e_dev=("Device not found", "Check the USB cable, then press Rescan or use Manual mode."),
 e_port=("No port selected", "Choose a COM port in Manual mode."),
 e_off=("Invalid offset or size", "Offset must look like 0x200000 and size a multiple of 4096."),
 e_fit=("The bin does not fit in chip flash", "Offset + size exceeds the flash size ({flash})."),
 e_write=("Writing to the ESP32 failed", "Check the cable and port; if needed hold the BOOT button and try again."),
 e_busy_port=("The port is not available", "Another program (like a serial monitor) is holding the port; close it."),
 dev_scan="Searching for a device…", dev_none="No device detected", dev_ok="Device detected",
 dev_nopt="Partition table not found; using manual values",
 dev_noport="No COM port is connected",
 dev_partial="Port {port} found but the board did not answer",
 dev_busy="Port {port} is not available",
 dev_busy_hint="Another program (like Arduino IDE or a serial monitor) is holding the port. Close it and press Rescan.",
 dev_hint="The board is not in download mode. Hold BOOT, tap EN (RESET), release BOOT, then press Rescan. You can also try programming directly.",
 b_go="Continue", b_cancel="Cancel", b_ok="OK", b_try="Try anyway",
 boot_t="Press the BOOT button",
 boot_msg="The board is not in download mode.\n\n1) Press and hold the BOOT button\n2) Tap EN (RESET) once while holding it\n3) Keep holding BOOT until writing starts",
 boot_wait="Waiting for BOOT press…", boot_seen="BOOT detected ✔ connecting…",
 boot_normal="The board booted normally. Hold BOOT and tap EN again.",
 boot_busy="The port is not available. Another program is holding it.",
 fin_t="Programming finished",
 fin_msg="Files were written and read back from the chip for verification.\n\nRelease BOOT and tap EN (RESET) once so the board starts running with the new files.",
 g5=("Reading back from the chip for final check…", "The written data is read from ESP32 flash and compared with the original file."),
 g4v=("Programming finished successfully", "Files were written to ESP32 and read back from the chip: identical to the original."),
 e_boot=("The board did not enter download mode", "Hold the BOOT button, tap EN (RESET), release BOOT, then press Program again."),
 confirm="File: {name} ({size})\nPort: {port} ({chip})\nWrite address: {off}\n\nExisting data in that region of flash will be erased.\nContinue?",
 confirm_warn="\n\nWarning: the offset you entered differs from the chip partition ({pt})!",
 confirm_t="Confirm write", st0="Ready", st1="Building…", st2="Ready to program", st3="Writing…", st4="Complete",
 pick_title="Choose the data folder",
 about="About", about_text="I'm Peyman Gholami, a specialist in IoT equipment and telemetry controller systems.",
 contact="Contact me", copy="Copy", copied="Copied", close="Close"),
}

BG, CARD, LINE = "#0A0C0F", "#11151A", "#212830"
ACC, ACC_BG = "#58B8F2", "#10263A"
OK, OK_BG = "#52C97A", "#122A1B"
ER, ER_BG = "#FF7168", "#3A1614"
WA, WA_BG = "#F2B84B", "#3A2C0E"
TX, TX2 = "#EEF1F5", "#8F9BA8"
BTN, BTN_HI, FIELD, DIM = "#1D232B", "#2A323D", "#161B21", "#2F3945"
K = 1.0
ON_ACC = "#06121B"
KIND = {"info": (ACC_BG, ACC, "i"), "ok": (OK_BG, OK, "✔"), "err": (ER_BG, ER, "✖"),
        "warn": (WA_BG, WA, "!"), "busy": (ACC_BG, ACC, "…")}


def _cls(ch):
    o = ord(ch)
    if 0x0590 <= o <= 0x08FF or 0xFB1D <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF or o == 0x200F:
        return "R"
    if ch.isalnum():
        return "L"
    return "N"


def has_rtl(t):
    return any(_cls(c) == "R" for c in t)


def bidi_visual(text):
    """Tk draws each RTL/LTR run correctly but keeps runs in logical order.
    Reverse the run order (right-to-left paragraph) so mixed text reads correctly."""
    n = len(text)
    cl = [_cls(c) for c in text]
    res = cl[:]
    i = 0
    while i < n:
        if cl[i] == "N":
            j = i
            while j < n and cl[j] == "N":
                j += 1
            left = cl[i - 1] if i > 0 else "R"
            right = cl[j] if j < n else "R"
            d = left if left == right else "R"
            for k in range(i, j):
                res[k] = d
            i = j
        else:
            i += 1
    runs = []
    for c, d in zip(text, res):
        if runs and runs[-1][0] == d:
            runs[-1][1] += c
        else:
            runs.append([d, c])
    return "".join(r[1] for r in reversed(runs))


def shade(c, f):
    r, g, b = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    if f >= 1:
        r, g, b = [min(255, int(v + (255 - v) * (f - 1))) for v in (r, g, b)]
    else:
        r, g, b = [int(v * f) for v in (r, g, b)]
    return "#%02x%02x%02x" % (r, g, b)


def rrect(c, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


class RBtn(tk.Canvas):
    """Flat rounded button drawn on a canvas (crisp, dark-theme friendly)."""
    def __init__(self, parent, text="", command=None, bg=None, fg=None, font=None, padx=14, pady=6, width=0, **_):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0, cursor="hand2")
        self._t, self._bgc, self._fgc = text, bg or BTN, fg or TX
        self._font = font or tkfont.nametofont("TkDefaultFont")
        self._cmd, self._wc, self._state, self._hover = command, width, "normal", False
        self._padx, self._pady = int(padx * K), int(pady * K)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Configure>", lambda e: self._draw())
        self._fit()

    def _set_hover(self, v):
        self._hover = v and self._state == "normal"
        self._draw()

    def _click(self, e):
        if self._state == "normal" and self._cmd and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self._cmd()

    def _fit(self):
        f = self._font
        w = f.measure(self._t) + 2 * self._padx
        if self._wc:
            w = max(w, f.measure("0") * self._wc + self._padx)
        h = f.metrics("linespace") + 2 * self._pady
        tk.Canvas.configure(self, width=w, height=h)
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), int(float(tk.Canvas.cget(self, "width"))))
        h = max(self.winfo_height(), int(float(tk.Canvas.cget(self, "height"))))
        dis = self._state == "disabled"
        fill = DIM if dis else (shade(self._bgc, 1.14) if self._hover else self._bgc)
        rrect(self, 1, 1, w - 1, h - 1, 9 * K, fill=fill, outline="")
        self.create_text(w / 2, h / 2, text=self._t, font=self._font, fill=TX2 if dis else self._fgc)

    def config(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        for k in list(kw):
            if k == "text": self._t = kw.pop(k)
            elif k == "bg": self._bgc = kw.pop(k)
            elif k == "fg": self._fgc = kw.pop(k)
            elif k == "font": self._font = kw.pop(k)
            elif k == "state": self._state = kw.pop(k)
            elif k == "command": self._cmd = kw.pop(k)
        if kw:
            tk.Canvas.configure(self, **kw)
        self._fit()
    configure = config

    def cget(self, k):
        return {"text": self._t, "bg": self._bgc, "fg": self._fgc, "state": self._state}.get(k) or tk.Canvas.cget(self, k)


class Bar(tk.Canvas):
    """Flat progress bar that can fill from the right (RTL)."""
    def __init__(self, parent, color, rtl=False, height=12):
        super().__init__(parent, height=int(height * K), bg=parent.cget("bg"), highlightthickness=0)
        self.val, self.color, self.rtl = 0, color, rtl
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, v, color=None):
        self.val = max(0, min(100, v))
        if color:
            self.color = color
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        rrect(self, 0, 0, w, h, h / 2, fill=BTN, outline="")
        fw = int(w * self.val / 100)
        if fw > 0:
            x0, x1 = (w - fw, w) if self.rtl else (0, fw)
            rrect(self, x0, 0, x1, h, h / 2, fill=self.color, outline="")


class App(tk.Tk):
    def __init__(self):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("IoTHero.LittleFSProgrammer.1")
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
        super().__init__()
        global K
        K = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.title(f"{APP} {VERSION}")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(int(840 * K), sw - 40), min(int(660 * K), sh - 120)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")
        self.minsize(int(760 * K), int(520 * K))
        self.configure(bg=BG)
        try:
            self.iconbitmap(res("app.ico"))
        except Exception:
            pass
        try:
            self._icon = tk.PhotoImage(file=res("app_icon.png"))
            self.iconphoto(True, self._icon)
        except Exception:
            pass
        self.q = queue.Queue()
        self.cfg = self.load_cfg()
        self.lang = self.cfg.get("lang", "fa")
        self.mode = tk.StringVar(value=self.cfg.get("mode", "auto"))
        self.folder = None
        self.stage = 0
        self.busy = False
        self.dev = None
        self.dev_state = "scan"
        self.steps = ["on", "todo", "todo", "todo"]
        self.checks = ["todo"] * 4
        self.g = ("g0", "info", {})
        self.bin_path = None
        self.built_size = None
        self.metrics = None
        self.report = []
        self.reg = []
        self.known_ports = None
        self.detecting = False
        self.tool = find_tool()

        self.fam = tkfont.nametofont("TkDefaultFont").actual()["family"]
        self.f = tkfont.Font(family="Segoe UI", size=10)
        self.fb = tkfont.Font(family="Segoe UI Semibold", size=10)
        self.fh = tkfont.Font(family="Segoe UI Semibold", size=11)
        self.ft = tkfont.Font(family="Segoe UI Semibold", size=15)
        self.fm = tkfont.Font(family="Consolas", size=9)
        self.port = tk.StringVar()
        self.chip = tk.StringVar(value=self.cfg.get("chip", "esp32"))
        self.off = tk.StringVar(value=self.cfg.get("off", hex(DEFAULT_OFFSET)))
        self.size = tk.StringVar(value=self.cfg.get("size", hex(DEFAULT_SIZE)))
        self.baud = tk.StringVar(value=self.cfg.get("baud", "460800"))
        for v in (self.off, self.size):
            v.trace_add("write", lambda *a: self.on_manual_edit())
        self.S, self.E, self.A, self.J, self.rtl = "left", "right", "w", "left", False
        self.build_ui()
        self.apply_lang()
        self.render_sound()
        self.fit()
        self.after(80, self.pump)
        self.after(300, self.start_detect)
        self.after(2000, self.poll_ports)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def fit(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(int(840 * K), sw - 40)
        h = min(self.winfo_reqheight() + int(70 * K), sh - 110)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")

    # ------------------------------------------------------------ settings --
    def load_cfg(self):
        try:
            with open(settings_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_cfg(self):
        try:
            self.cfg.update(lang=self.lang, mode=self.mode.get(), baud=self.baud.get(),
                            off=self.off.get(), size=self.size.get(), chip=self.chip.get())
            if self.folder:
                self.cfg["folder"] = self.folder
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False)
        except Exception:
            pass

    def on_close(self):
        self.save_cfg()
        self.destroy()

    # ------------------------------------------------------------------ UI ---
    def rw(self, k, **kw):
        """Logical (unshaped) text: for native dialogs that do their own bidi."""
        v = S[self.lang][k]
        return v.format(**kw) if kw and isinstance(v, str) else v

    def shape(self, v, wrap=None, font=None):
        # Tk on Windows already orders mixed Persian/Latin text correctly; keep text as-is.
        return v
        if isinstance(v, tuple):
            return tuple(self.shape(x, wrap, font) for x in v)
        font = font or self.f
        out = []
        for line in v.split("\n"):
            if not has_rtl(line):
                out.append(line)
                continue
            if wrap:
                cur, lines = "", []
                for w in line.split(" "):
                    t = (cur + " " + w) if cur else w
                    if cur and font.measure(t) > wrap:
                        lines.append(cur)
                        cur = w
                    else:
                        cur = t
                lines.append(cur)
                out.extend(bidi_visual(x) for x in lines)
            else:
                out.append(bidi_visual(line))
        return "\n".join(out)

    def sym(self, symbol, text):
        return f"{symbol} {text}"

    def tr(self, k, **kw):
        return self.shape(self.rw(k, **kw))

    def label(self, parent, key=None, font=None, fg=TX, bg=CARD, **kw):
        l = tk.Label(parent, font=font or self.f, fg=fg, bg=bg, **kw)
        if key:
            self.reg.append((l, key))
        return l

    def card(self, parent, title_key):
        outer = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        head = tk.Frame(outer, bg=CARD)
        head.pack(fill="x", padx=14, pady=(8, 6))
        self.label(head, title_key, self.fh, fg=ACC, anchor=self.A).pack(side=self.S, fill="x", expand=True)
        return outer

    def build_ui(self):
        rtl = self.lang == "fa"
        S_, E_ = ("right", "left") if rtl else ("left", "right")
        A_, J_ = ("e", "right") if rtl else ("w", "left")
        self.S, self.E, self.A, self.J, self.rtl = S_, E_, A_, J_, rtl
        col = (lambda i, n: n - 1 - i) if rtl else (lambda i, n: i)

        # header
        hd = tk.Frame(self, bg=BG)
        hd.pack(fill="x")
        inner = tk.Frame(hd, bg=BG)
        inner.pack(fill="x", padx=16, pady=6)
        self.img1 = self.load_logo("logo-peyman-gholami.png", 34)
        self.img2 = self.load_logo("logo-iot-hero.png", 30)
        tk.Label(inner, image=self.img1, bg=BG).pack(side=S_)
        mid = tk.Frame(inner, bg=BG)
        mid.pack(side=S_, expand=True)
        tk.Label(mid, text="LittleFS  Programmer", font=tkfont.Font(family="Segoe UI Semibold", size=14), fg=TX, bg=BG).pack()
        self.label(mid, "sub", self.f, fg=TX2, bg=BG).pack()
        right = tk.Frame(inner, bg=BG)
        right.pack(side=E_)
        tk.Label(right, image=self.img2, bg=BG).pack(side=S_, padx=(0, 8) if not rtl else (8, 0))
        self.langbtn = RBtn(right, font=self.fb, width=4, command=self.toggle_lang, relief="flat",
                                 bg=ACC_BG, fg=ACC, activebackground=ACC_BG, cursor="hand2")
        self.langbtn.pack(side=S_)
        self.sndbtn = RBtn(right, font=self.fb, width=3, command=self.toggle_sound, relief="flat",
                                bg=ACC_BG, fg=ACC, activebackground=ACC_BG, cursor="hand2")
        self.sndbtn.pack(side=S_, padx=6)
        self.aboutbtn = RBtn(right, text="ⓘ", font=tkfont.Font(family="Segoe UI Symbol", size=12), width=3,
                                  command=self.show_about, relief="flat", bg=ACC_BG, fg=ACC,
                                  activebackground=ACC_BG, cursor="hand2")
        self.aboutbtn.pack(side=S_)

        tk.Frame(self, bg=LINE, height=1).pack(fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=6)

        # guide banner
        self.gbox = tk.Frame(body, bg=ACC_BG)
        self.gbox.pack(fill="x")
        self.gicon = tk.Label(self.gbox, font=tkfont.Font(family="Segoe UI Semibold", size=16), width=3, bg=ACC_BG, fg=ACC)
        self.gicon.pack(side=S_, padx=(6, 0) if not rtl else (0, 6), pady=6)
        gt = tk.Frame(self.gbox, bg=ACC_BG)
        gt.pack(side=S_, fill="x", expand=True, padx=6, pady=6)
        self.gtitle = tk.Label(gt, font=self.fh, bg=ACC_BG, fg=ACC, anchor=A_, justify=J_)
        self.gtitle.pack(fill="x")
        self.gsub = tk.Label(gt, font=self.f, bg=ACC_BG, fg=ACC, anchor=A_, justify=J_, wraplength=int(700 * K))
        self.gsub.pack(fill="x")

        # stepper
        self.stp = tk.Canvas(body, height=int(56 * K), bg=BG, highlightthickness=0)
        self.stp.pack(fill="x", pady=(6, 0))
        self.stp.bind("<Configure>", lambda e: self.draw_steps())

        # cards
        cols = tk.Frame(body, bg=BG)
        cols.pack(fill="x")
        cols.columnconfigure((0, 1), weight=1, uniform="c")
        self.cA = self.card(cols, "h1")
        self.cA.grid(row=0, column=col(0, 2), sticky="nsew", padx=(0, 6) if not rtl else (6, 0))
        self.cB = self.card(cols, "h2")
        self.cB.grid(row=0, column=col(1, 2), sticky="nsew", padx=(6, 0) if not rtl else (0, 6))
        self.build_folder_card()
        self.build_device_card()

        # checklist
        cl = tk.Frame(body, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        cl.pack(fill="x", pady=(8, 0))
        self.chk = []
        cl.columnconfigure((0, 1), weight=1, uniform="k")
        for i in range(4):
            r = tk.Frame(cl, bg=CARD)
            r.grid(row=i // 2, column=col(i % 2, 2), sticky=A_, padx=12, pady=3)
            ic = tk.Label(r, font=self.fb, bg=CARD, width=2)
            ic.pack(side=S_)
            tx = self.label(r, f"c{i + 1}", self.f, anchor=A_)
            tx.pack(side=S_)
            self.chk.append((ic, tx))

        # progress + buttons
        pr = tk.Frame(body, bg=BG)
        pr.pack(fill="x", pady=(8, 0))
        st = ttk.Style(self)
        try: st.theme_use("clam")
        except Exception: pass
        st.configure("TCombobox", fieldbackground=FIELD, background=BTN, foreground=TX, arrowcolor=TX,
                     bordercolor=LINE, lightcolor=FIELD, darkcolor=FIELD, selectbackground=FIELD, selectforeground=TX)
        st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TX)],
               selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TX)])
        st.configure("TEntry", fieldbackground=FIELD, foreground=TX, insertcolor=TX, bordercolor=LINE,
                     lightcolor=FIELD, darkcolor=FIELD)
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", TX)
        self.option_add("*TCombobox*Listbox.selectBackground", ACC)
        self.option_add("*TCombobox*Listbox.selectForeground", ON_ACC)
        self.pct = tk.Label(pr, text="0%", font=self.fb, bg=BG, fg=TX2, width=5)
        self.pct.pack(side=E_)
        self.pb = Bar(pr, ACC, rtl)
        self.pb.pack(side=S_, fill="x", expand=True)

        ab = tk.Frame(body, bg=BG)
        ab.pack(fill="x", pady=(8, 0))
        self.go = RBtn(ab, font=tkfont.Font(family="Segoe UI Semibold", size=12), relief="flat", bg=ACC, fg=ON_ACC,
                            activebackground="#3A94CE", activeforeground=ON_ACC, cursor="hand2", padx=20, pady=5,
                            command=self.on_go)
        self.go.pack(side=S_)
        self.bopen = RBtn(ab, font=self.f, relief="flat", bg=BTN, fg=TX, cursor="hand2", padx=10, pady=5,
                               command=self.open_output)
        self.bopen.pack(side=S_, padx=8)
        self.reg.append((self.bopen, "b_open"))
        self.brep = RBtn(ab, font=self.f, relief="flat", bg=BTN, fg=TX, cursor="hand2", padx=10, pady=5,
                              command=self.save_report)
        self.brep.pack(side=S_)
        self.reg.append((self.brep, "b_report"))

        self.log = tk.Text(body, height=3, font=self.fm, bg="#0A0D10", fg="#B9D4E6", relief="flat", state="disabled",
                           wrap="word", padx=8, pady=6)
        self.log.pack(fill="both", expand=True, pady=(8, 0))

        sb = tk.Frame(self, bg="#0A0D10")
        sb.pack(fill="x", side="bottom")
        self.status = tk.Label(sb, font=tkfont.Font(family="Segoe UI", size=9), bg="#0A0D10", fg=TX2)
        self.status.pack(side=S_, padx=12, pady=3)
        tk.Label(sb, text=f"v{VERSION}", font=tkfont.Font(family="Segoe UI", size=9), bg="#0A0D10", fg=TX2).pack(side=E_, padx=12)
        body.pack_forget()
        body.pack(fill="both", expand=True, padx=14, pady=6)

    def load_logo(self, name, h):
        im = Image.open(res(name)).convert("RGBA")
        r, g, b, a = im.split()
        from PIL import ImageEnhance
        rgb = ImageEnhance.Brightness(Image.merge("RGB", (r, g, b))).enhance(1.3)
        im = Image.merge("RGBA", (*rgb.split(), a))
        h = int(h * K)
        w = int(im.width * h / im.height)
        return ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))

    def build_folder_card(self):
        c, A_, J_, S_, E_ = self.cA, self.A, self.J, self.S, self.E
        col = (lambda i, n: n - 1 - i) if self.rtl else (lambda i, n: i)
        self.btn_choose = RBtn(c, font=self.fb, relief="flat", bg=ACC_BG, fg=ACC, cursor="hand2", pady=9,
                                    activebackground=ACC_BG, command=self.choose_folder)
        self.btn_choose.pack(fill="x", padx=14)
        self.reg.append((self.btn_choose, "choose"))
        self.folder_lbl = tk.Label(c, font=self.f, bg=CARD, fg=TX2, anchor=A_, wraplength=int(330 * K), justify=J_)
        self.folder_lbl.pack(fill="x", padx=14, pady=(4, 6))
        g = tk.Frame(c, bg=CARD)
        g.pack(fill="x", padx=14)
        g.columnconfigure((0, 1, 2), weight=1, uniform="m")
        self.mv = []
        for i in range(3):
            f = tk.Frame(g, bg=FIELD)
            f.grid(row=0, column=col(i, 3), sticky="nsew", padx=(0 if i == 0 else 4, 0 if i == 2 else 4))
            self.label(f, f"m{i + 1}", self.f, fg=TX2, bg=FIELD, anchor=A_).pack(fill="x", padx=8, pady=(6, 0))
            v = tk.Label(f, text="—", font=self.fh, bg=FIELD, fg=TX, anchor=A_)
            v.pack(fill="x", padx=8, pady=(0, 6))
            self.mv.append(v)
        u = tk.Frame(c, bg=CARD)
        u.pack(fill="x", padx=14, pady=(8, 10))
        top = tk.Frame(u, bg=CARD)
        top.pack(fill="x")
        self.label(top, "usage", self.f, fg=TX2).pack(side=S_)
        self.ulbl = tk.Label(top, text="—", font=self.f, bg=CARD, fg=TX2)
        self.ulbl.pack(side=E_)
        self.ubar = Bar(u, OK, self.rtl)
        self.ubar.pack(fill="x", pady=(4, 0))

    def build_device_card(self):
        c, A_, J_, S_, E_ = self.cB, self.A, self.J, self.S, self.E
        rtl = self.rtl
        seg = tk.Frame(c, bg=FIELD)
        seg.pack(fill="x", padx=14)
        self.seg = {}
        for key in ("auto", "man"):
            b = RBtn(seg, font=self.fb, relief="flat", bd=0, pady=5, cursor="hand2",
                          command=lambda k=key: self.set_mode("auto" if k == "auto" else "manual"))
            b.pack(side=S_, fill="x", expand=True, padx=2, pady=2)
            self.reg.append((b, key))
            self.seg[key] = b
        self.dbody = tk.Frame(c, bg=CARD)
        self.dbody.pack(fill="x", padx=14, pady=(8, 2))

        # auto panel
        self.pa = tk.Frame(self.dbody, bg=CARD)
        top = tk.Frame(self.pa, bg=CARD)
        top.pack(fill="x")
        self.chip_lbl = tk.Label(top, font=self.fb, bg=WA_BG, fg=WA, padx=10, pady=3)
        self.chip_lbl.pack(side=S_)
        self.bscan = RBtn(top, font=self.f, relief="flat", bg=BTN, fg=TX, cursor="hand2", padx=10,
                               command=self.start_detect)
        self.bscan.pack(side=E_)
        self.reg.append((self.bscan, "scan"))
        self.dinfo = tk.Label(self.pa, font=self.fm, bg=CARD, fg=TX, anchor=A_, justify="left")
        self.dinfo.pack(fill="x", pady=(8, 0))
        self.dhint = tk.Label(self.pa, font=self.f, bg=CARD, fg=WA, anchor=A_, justify=J_, wraplength=int(330 * K))
        self.dhint.pack(fill="x", pady=(6, 0))

        # manual panel
        self.pm = tk.Frame(self.dbody, bg=CARD)
        rows = [("port", ttk.Combobox(self.pm, textvariable=self.port, width=26)),
                ("chip", ttk.Combobox(self.pm, textvariable=self.chip, width=14, state="readonly",
                                      values=["esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2"])),
                ("off", ttk.Entry(self.pm, textvariable=self.off, width=16)),
                ("size", ttk.Entry(self.pm, textvariable=self.size, width=16))]
        self.port_cb = rows[0][1]
        lc, ec, rc = (2, 1, 0) if rtl else (0, 1, 2)
        for i, (k, w) in enumerate(rows):
            self.label(self.pm, k, self.f, fg=TX2, anchor=A_, width=7).grid(row=i, column=lc, sticky=A_, pady=2)
            w.grid(row=i, column=ec, sticky="we", pady=2)
        self.brefresh = RBtn(self.pm, font=self.f, relief="flat", bg=BTN, fg=TX, cursor="hand2",
                                  command=self.refresh_ports)
        self.brefresh.grid(row=0, column=rc, padx=(0, 6) if rtl else (6, 0))
        self.reg.append((self.brefresh, "refresh"))
        self.pm.columnconfigure(ec, weight=1)
        self.err_lbl = tk.Label(self.pm, font=self.f, bg=CARD, fg=ER, anchor=A_, wraplength=int(330 * K), justify=J_)
        self.err_lbl.grid(row=4, column=0, columnspan=3, sticky=A_)

        # shared: baud
        bf = tk.Frame(c, bg=CARD)
        bf.pack(fill="x", padx=14, pady=(0, 10))
        self.label(bf, "baud", self.f, fg=TX2, anchor=A_, width=7).pack(side=S_)
        ttk.Combobox(bf, textvariable=self.baud, width=10, values=["115200", "230400", "460800", "921600"]).pack(side=S_)
        self.refresh_ports()

    # -------------------------------------------------------------- dialogs ---
    def dark_title(self, w):
        try:
            import ctypes
            w.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(w.winfo_id())
            v = ctypes.c_int(1)
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)
        except Exception:
            pass

    def make_dialog(self, title, text, buttons, icon="⚠", color=None, status=False):
        """Themed modal dialog. buttons=[(label, value, primary)]. Returns (win, res, status_label)."""
        A_, J_, S_ = self.A, self.J, self.S
        color = color or WA
        out = [None]
        w = tk.Toplevel(self)
        w.title(title)
        w.configure(bg=BG)
        w.resizable(False, False)
        try:
            w.iconbitmap(res("app.ico"))
        except Exception:
            pass
        w.transient(self)
        box = tk.Frame(w, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        box.pack(padx=14, pady=14)
        col = tk.Frame(box, bg=CARD)
        col.pack(padx=int(26 * K), pady=int(22 * K))
        wl = int(400 * K)
        tk.Frame(col, width=wl, height=1, bg=CARD).pack()
        head = tk.Frame(col, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text=icon, font=tkfont.Font(family="Segoe UI Symbol", size=22), bg=CARD, fg=color).pack(side=S_)
        tk.Label(head, text=self.shape(title, font=self.fh), font=tkfont.Font(family="Segoe UI Semibold", size=13),
                 bg=CARD, fg=TX).pack(side=S_, padx=12)
        tk.Frame(col, bg=LINE, height=1).pack(fill="x", pady=(12, 12))
        tk.Label(col, text=self.shape(text, wrap=wl - 10, font=self.fh), font=self.fh, bg=CARD, fg=TX, wraplength=wl,
                 justify=J_, anchor=A_).pack(fill="x")
        st = None
        if status:
            st = tk.Label(col, text="", font=self.fb, bg=ACC_BG, fg=ACC, wraplength=wl, justify="center",
                          pady=int(8 * K))
            st.pack(fill="x", pady=(14, 0))
        row = tk.Frame(col, bg=CARD)
        row.pack(fill="x", pady=(18, 0))

        def pick(v):
            out[0] = v
            try: w.destroy()
            except Exception: pass
        for label, val, primary in buttons:
            RBtn(row, text=label, font=self.fb, bg=ACC if primary else BTN, fg=ON_ACC if primary else TX,
                 padx=22, pady=5, command=lambda v=val: pick(v)).pack(side=S_, padx=(0, 10) if not self.rtl else (10, 0))
        w.protocol("WM_DELETE_WINDOW", lambda: pick(None))
        w.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - w.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - w.winfo_height()) // 3
        w.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.dark_title(w)
        w.grab_set()
        w.focus_set()
        return w, out, st

    def ask(self, title, text, buttons, icon="⚠", color=None):
        w, out, _ = self.make_dialog(title, text, buttons, icon, color)
        self.wait_window(w)
        return out[0]

    # --------------------------------------------------------------- about ---
    def show_about(self):
        if getattr(self, "_about", None) and self._about.winfo_exists():
            self._about.lift()
            return
        A_, J_, S_ = self.A, self.J, self.S
        w = tk.Toplevel(self)
        self._about = w
        w.title(self.rw("about"))
        w.configure(bg=BG)
        w.resizable(False, False)
        try:
            w.iconbitmap(res("app.ico"))
        except Exception:
            pass
        w.transient(self)
        box = tk.Frame(w, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        box.pack(padx=14, pady=14)
        col = tk.Frame(box, bg=CARD)
        col.pack(padx=int(30 * K), pady=int(24 * K))
        wl = int(380 * K)
        tk.Frame(col, width=wl, height=1, bg=CARD).pack()
        self._about_img = self.load_logo("logo-iot-hero.png", 44)
        tk.Label(col, image=self._about_img, bg=CARD).pack(anchor=A_)
        tk.Frame(col, bg=LINE, height=1).pack(fill="x", pady=(14, 14))
        tk.Label(col, text=self.shape(self.rw("about_text"), wrap=wl - 10, font=self.fh), font=self.fh, bg=CARD, fg=TX, wraplength=wl,
                 justify=J_, anchor=A_).pack(fill="x")
        tk.Label(col, text=self.tr("contact"), font=self.f, bg=CARD, fg=TX2, anchor=A_, justify=J_).pack(fill="x", pady=(16, 6))
        row = tk.Frame(col, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text="09113262377", font=tkfont.Font(family="Segoe UI Semibold", size=14), bg=CARD, fg=ACC).pack(side=S_)
        cp = RBtn(row, text=self.tr("copy"), font=self.f, bg=BTN, fg=TX, padx=10, pady=3,
                  command=lambda: self.copy_phone(cp))
        cp.pack(side=S_, padx=12)
        link = tk.Label(col, text="WWW.IOTHERO.IR", font=tkfont.Font(family="Segoe UI Semibold", size=12, underline=True),
                        bg=CARD, fg=ACC, cursor="hand2", anchor=A_)
        link.pack(fill="x", pady=(10, 18))
        link.bind("<Button-1>", lambda e: webbrowser.open("https://www.iothero.ir"))
        RBtn(col, text=self.tr("close"), font=self.fb, bg=ACC, fg=ON_ACC, padx=26, pady=5,
             command=w.destroy).pack(anchor=A_)
        w.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - w.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - w.winfo_height()) // 3
        w.geometry(f"+{max(0, x)}+{max(0, y)}")
        w.grab_set()

    def copy_phone(self, btn):
        self.clipboard_clear()
        self.clipboard_append("09113262377")
        btn.config(text=self.tr("copied"))
        self.after(1500, lambda: btn.winfo_exists() and btn.config(text=self.tr("copy")))

    # ----------------------------------------------------------- rendering ---
    def apply_lang(self):
        for w, k in self.reg:
            w.config(text=self.tr(k))
        self.langbtn.config(text="EN" if self.lang == "fa" else "FA")
        self.update_folder_label()
        self.render_all()

    def render_all(self):
        self.render_guide()
        self.draw_steps()
        self.render_checks()
        self.render_buttons()
        self.render_device()
        self.render_modes()
        self.status.config(text=self.tr("st%d" % self.stage))

    def toggle_lang(self):
        self.lang = "en" if self.lang == "fa" else "fa"
        snap = dict(mv=[v.cget("text") for v in self.mv], ulbl=self.ulbl.cget("text"), ubar=self.ubar.val,
                    pb=(self.pb.val, self.pb.color), pct=self.pct.cget("text"),
                    log=self.log.get("1.0", "end-1c"))
        for w in self.winfo_children():
            w.destroy()
        self.reg = []
        self.build_ui()
        for v, t in zip(self.mv, snap["mv"]):
            v.config(text=t)
        self.ulbl.config(text=snap["ulbl"])
        self.ubar.set(snap["ubar"])
        self.pb.set(*snap["pb"])
        self.pct.config(text=snap["pct"])
        self.log.config(state="normal")
        self.log.insert("end", snap["log"])
        self.log.see("end")
        self.log.config(state="disabled")
        self.apply_lang()
        self.render_sound()
        self.save_cfg()

    def toggle_sound(self):
        self.cfg["sound"] = not self.cfg.get("sound", True)
        self.render_sound()
        if self.cfg["sound"]:
            self.play("ready")
        self.save_cfg()

    def render_sound(self):
        on = self.cfg.get("sound", True)
        self.sndbtn.config(text="♪" if on else "×", fg=ACC if on else TX2, bg=ACC_BG if on else FIELD)

    def play(self, kind):
        if not winsound or not self.cfg.get("sound", True):
            return
        seq = {"ready": [(880, 120), (1175, 160)],
               "done": [(784, 130), (988, 130), (1319, 130), (1568, 280)],
               "error": [(400, 220), (300, 220), (220, 380)]}[kind]

        def run():
            try:
                for f, d in seq:
                    winsound.Beep(f, d)
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def set_guide(self, key, kind="info", **kw):
        if (key, kind) != self.g[:2]:
            if kind == "err":
                self.play("error")
            elif key == "g4":
                self.play("done")
            elif key == "g2":
                self.play("ready")
        self.g = (key, kind, kw)
        self.render_guide()

    def render_guide(self):
        key, kind, kw = self.g
        t, s = S[self.lang][key]
        if kw:
            s = s.format(**kw)
        t = self.shape(t, font=self.fh)
        s = self.shape(s, wrap=int(660 * K))
        bg, fg, ic = KIND[kind]
        for w in (self.gbox, self.gicon, self.gtitle, self.gsub, self.gtitle.master):
            w.config(bg=bg)
        self.gicon.config(text=ic, fg=fg)
        self.gtitle.config(text=t, fg=fg)
        self.gsub.config(text=s, fg=fg)

    def draw_steps(self):
        c = self.stp
        c.delete("all")
        w = max(c.winfo_width(), 400)
        n = 4
        xs = [int(w * (i + .5) / n) for i in range(n)]
        sg = 1
        if self.rtl:
            xs.reverse()
            sg = -1
        k = K
        for i in range(n - 1):
            col = OK if self.steps[i] == "ok" else LINE
            c.create_line(xs[i] + sg * 18 * k, 18 * k, xs[i + 1] - sg * 18 * k, 18 * k, fill=col, width=max(2, int(2 * k)))
        for i, x in enumerate(xs):
            s = self.steps[i]
            fill, fg, txt = {"todo": (CARD, TX2, str(i + 1)), "on": (ACC, ON_ACC, str(i + 1)),
                             "ok": (OK_BG, OK, "✔"), "err": (ER_BG, ER, "✖")}[s]
            ring = {"todo": LINE, "on": ACC, "ok": OK, "err": ER}[s]
            c.create_oval(x - 14 * k, 4 * k, x + 14 * k, 32 * k, fill=fill, outline=ring, width=2)
            c.create_text(x, 18 * k, text=txt, fill=fg, font=self.fb)
            c.create_text(x, 46 * k, text=self.tr("s%d" % (i + 1)), fill=ACC if s == "on" else (OK if s == "ok" else (ER if s == "err" else TX2)), font=self.fb if s != "todo" else self.f)

    def render_checks(self):
        for i, (ic, tx) in enumerate(self.chk):
            st = self.checks[i]
            sym, col, fg = {"todo": ("○", TX2, TX2), "ok": ("✔", OK, TX), "err": ("✖", ER, ER), "warn": ("⚠", WA, WA)}[st]
            ic.config(text=sym, fg=col)
            key = "c4w" if (i == 3 and st == "warn") else f"c{i + 1}"
            tx.config(text=self.tr(key), fg=fg)

    def render_buttons(self):
        stage = self.stage
        if self.busy:
            txt, bg, state = self.tr("b_busy"), DIM, "disabled"
        elif stage in (0, 1):
            txt, bg, state = self.tr("b_pick"), ACC, "normal"
        elif stage == 2:
            txt, bg, state = self.tr("b_prog"), ACC, "normal"
        else:
            txt, bg, state = self.tr("b_new"), OK, "normal"
        self.go.config(text=txt, bg=bg, state=state)

    def render_modes(self):
        m = self.mode.get()
        for k, b in self.seg.items():
            on = (k == "auto") == (m == "auto")
            b.config(bg=ACC_BG if on else FIELD, fg=ACC if on else TX2)
        self.pa.pack_forget(); self.pm.pack_forget()
        (self.pa if m == "auto" else self.pm).pack(fill="x")

    def render_device(self):
        d, st = self.dev, self.dev_state
        self.dhint.config(text="")
        if st == "scan":
            self.chip_lbl.config(text=self.tr("dev_scan"), bg=ACC_BG, fg=ACC)
            self.dinfo.config(text="")
        elif d and st == "ok":
            self.chip_lbl.config(text=self.sym("✔", self.tr("dev_ok")), bg=OK_BG, fg=OK)
            lines = [f"{d['port']}  ·  {d['chip_name']}", f"Flash: {d.get('flash') or '?'}   MAC: {d.get('mac') or '?'}"]
            if d.get("pt_off") is not None:
                lines.append(f"Partition: {hex(d['pt_off'])} / {fmt_size(d['pt_size'])}  ({d.get('pt_name', '')})")
            else:
                self.dhint.config(text=self.tr("dev_nopt"))
            self.dinfo.config(text="\n".join(lines))
        elif d and st == "partial":
            busy = d.get("reason") == "busy"
            self.chip_lbl.config(text=self.sym("⚠", self.tr("dev_busy" if busy else "dev_partial").replace("{port}", d["port"])),
                                 bg=ER_BG if busy else WA_BG, fg=ER if busy else WA)
            self.dinfo.config(text=f"{d['port']}  ·  {d.get('desc', '')}")
            self.dhint.config(text=self.tr("dev_busy_hint" if busy else "dev_hint"))
        else:
            msg = self.tr("dev_noport") if st == "noport" else self.tr("dev_none")
            self.chip_lbl.config(text=self.sym("⚠", msg), bg=WA_BG, fg=WA)
            self.dinfo.config(text="")

    # ------------------------------------------------------------ plumbing ---
    def pump(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.after(80, self.pump)

    def ui(self, fn, *a, **k):
        self.q.put(lambda: fn(*a, **k))

    def logw(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")
        self.report.append(text)

    def set_progress(self, v, green=False):
        self.pb.set(v, OK if green else ACC)
        self.pct.config(text=f"{int(v)}%")

    def set_stage(self, n):
        self.stage = n
        self.status.config(text=self.tr("st%d" % n))
        self.render_buttons()

    # ---------------------------------------------------------------- modes ---
    def set_mode(self, m):
        self.mode.set(m)
        self.render_modes()
        self.after(10, self.maybe_rebuild)

    def on_manual_edit(self):
        self.err_lbl.config(text="")
        self.after(600, self.maybe_rebuild)

    def refresh_ports(self):
        import serial.tools.list_ports as lp
        vals = [f"{p.device} — {p.description}" for p in sorted(lp.comports(), key=lambda p: p.device)]
        self.port_cb.config(values=vals)
        if vals and not self.port.get():
            self.port.set(vals[0])

    def poll_ports(self):
        try:
            import serial.tools.list_ports as lp
            cur = frozenset(p.device for p in lp.comports())
            if self.known_ports is not None and cur != self.known_ports:
                self.refresh_ports()
                if self.mode.get() == "auto" and not self.busy and not self.detecting:
                    self.start_detect()
            self.known_ports = cur
        except Exception:
            pass
        self.after(2000, self.poll_ports)

    # --------------------------------------------------------------- detect ---
    def start_detect(self):
        if self.detecting or self.busy:
            return
        self.detecting = True
        self.dev_state = "scan"
        self.render_device()
        threading.Thread(target=self.detect_worker, daemon=True).start()

    def detect_worker(self):
        try:
            import serial.tools.list_ports as lp
            ports = sorted(lp.comports(), key=lambda p: p.device)
            if not ports:
                self.ui(self.detect_done, None, "noport")
                return
            pref = [p for p in ports if p.vid in PREF_VIDS] or ports[:3]
            tmp = os.path.join(tempfile.gettempdir(), "lfsprog_pt.bin")
            last_out = ""
            busy = False
            for p in pref:
                if busy:
                    break
                for mode in ("default-reset", "no-reset"):
                    base = ["--port", p.device, "--baud", "115200", "--before", mode, "--connect-attempts", "3"]
                    try:
                        if os.path.exists(tmp): os.remove(tmp)
                    except Exception:
                        pass
                    code, out = run_stream(esp_cmd(*base, "read-flash", "0x8000", "0xC00", tmp), timeout=60)
                    if code == 0 and os.path.isfile(tmp):
                        m = re.search(r"Chip type:\s*(.+)", out) or re.search(r"Chip is\s*(.+)", out)
                        chip_name = m.group(1).strip() if m else "ESP32"
                        mm = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", out)
                        d = dict(port=p.device, desc=p.description, chip_name=chip_name, chip=chip_family(chip_name),
                                 flash=None, mac=mm.group(1) if mm else None, pt_off=None, pt_size=None)
                        part = pick_fs_partition(parse_ptable(open(tmp, "rb").read()))
                        if part:
                            d.update(pt_off=part["offset"], pt_size=part["size"], pt_name=part["name"])
                        c2, o2 = run_stream(esp_cmd(*base, "flash-id"), timeout=40)
                        mf = re.search(r"Detected flash size:\s*(\S+)", o2) if c2 == 0 else None
                        if mf:
                            d["flash"] = mf.group(1)
                        self.ui(self.detect_done, d, "ok")
                        return
                    last_out = out
                    if re.search(r"Could not open|port is busy|Access is denied|PermissionError", out, re.I):
                        busy = True
                        pref = [p]
                        break
            for l in last_out.splitlines()[-3:]:
                self.ui(self.logw, "[detect] " + l)
            first = pref[0]
            d = dict(port=first.device, desc=first.description, chip_name="ESP32 ?", chip="auto", flash=None,
                     mac=None, pt_off=None, pt_size=None, unverified=True,
                     reason="busy" if busy else "noanswer")
            self.ui(self.detect_done, d, "partial")
        except Exception as e:
            self.ui(self.logw, f"[detect] {e}")
            self.ui(self.detect_done, None, "none")

    def detect_done(self, d, state):
        self.detecting = False
        self.dev = d
        self.dev_state = state
        if d:
            self.logw(f"Device: {d['port']} {d['chip_name']} flash={d.get('flash')} partition="
                      f"{hex(d['pt_off']) if d.get('pt_off') is not None else 'n/a'}"
                      + ("  (port only, board did not answer)" if state == "partial" else ""))
            if d.get("pt_off") is not None:
                self.off.set(hex(d["pt_off"])); self.size.set(hex(d["pt_size"]))
            if d["chip"] != "auto":
                self.chip.set(d["chip"])
            self.port.set(d["port"])
        self.render_device()
        self.update_check4()
        self.render_checks()
        self.maybe_rebuild()

    # ------------------------------------------------------------- settings ---
    def current(self):
        """Return (settings dict, error_key)."""
        mode = self.mode.get()
        try:
            if mode == "auto" and self.dev and self.dev.get("pt_off") is not None:
                off, size = self.dev["pt_off"], self.dev["pt_size"]
            else:
                off, size = parse_int(self.off.get()), parse_int(self.size.get())
            if off < 0 or size <= 0 or size % BLOCK or off % BLOCK:
                raise ValueError
        except Exception:
            return None, "e_off"
        if mode == "auto":
            port = self.dev["port"] if self.dev else None
            chip = self.dev["chip"] if self.dev else "auto"
        else:
            m = re.match(r"\s*(COM\d+)", self.port.get(), re.I)
            port = m.group(1).upper() if m else (self.port.get().strip() or None)
            chip = self.chip.get() or "auto"
        return dict(off=off, size=size, port=port, chip=chip, baud=self.baud.get() or "460800"), None

    def update_check4(self):
        cur, err = self.current()
        if self.checks[3] == "err" and False:
            return
        if not cur or not self.dev or self.dev.get("pt_off") is None:
            self.checks[3] = "todo"
        else:
            self.checks[3] = "ok" if (cur["off"], cur["size"]) == (self.dev["pt_off"], self.dev["pt_size"]) else "err"

    def maybe_rebuild(self):
        if self.busy or not self.folder or self.stage not in (2, 4):
            return
        cur, err = self.current()
        if cur and cur["size"] != self.built_size:
            self.logw(f"Partition size changed ({fmt_size(self.built_size)} -> {fmt_size(cur['size'])}); rebuilding.")
            self.start_pipeline()

    # --------------------------------------------------------------- folder ---
    def update_folder_label(self):
        self.folder_lbl.config(text=self.folder or self.tr("nofolder"),
                               fg=TX if self.folder else TX2, anchor=self.A, justify=self.J)

    def choose_folder(self):
        if self.busy:
            return
        init = self.cfg.get("folder") or os.path.expanduser("~")
        d = filedialog.askdirectory(title=self.rw("pick_title"), initialdir=init if os.path.isdir(init) else None)
        if not d:
            return
        self.folder = os.path.normpath(d)
        self.update_folder_label()
        self.save_cfg()
        self.start_pipeline()

    def on_go(self):
        if self.busy:
            return
        if self.stage in (0, 1):
            self.choose_folder()
        elif self.stage == 2:
            self.start_program()
        else:
            self.new_job()

    def new_job(self):
        self.stage = 0
        self.folder = None
        self.bin_path = None
        self.built_size = None
        self.steps = ["on", "todo", "todo", "todo"]
        self.checks = ["todo"] * 4
        self.report = []
        for v in self.mv: v.config(text="—")
        self.ulbl.config(text="—"); self.ubar.set(0)
        self.set_progress(0)
        self.update_folder_label()
        self.set_guide("g0", "info")
        self.render_all()

    # ------------------------------------------------------------- pipeline ---
    def start_pipeline(self):
        cur, err = self.current()
        if err:
            self.set_guide("e_off", "err")
            return
        self.busy = True
        self.stage = 1
        self.steps = ["ok", "on", "todo", "todo"]
        self.checks = ["todo"] * 4
        self.set_progress(0)
        self.set_guide("g1", "busy")
        self.render_all()
        threading.Thread(target=self.pipeline_worker, args=(self.folder, cur), daemon=True).start()

    def fail(self, step, check_idx, key, **kw):
        def f():
            self.busy = False
            self.stage = 1
            self.steps = ["ok"] + ["todo"] * 3
            self.steps[step] = "err"
            for i in range(step): self.steps[i] = "ok"
            if check_idx is not None:
                self.checks[check_idx] = "err"
            self.set_guide(key, "err", **kw)
            self.render_all()
            self.set_progress(0)
        self.ui(f)

    def pipeline_worker(self, folder, cur):
        size = cur["size"]
        L = lambda t: self.ui(self.logw, t)
        try:
            self.ui(lambda: self.report.clear())
            L(f"=== {APP} {VERSION} · {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===")
            L(f"Folder: {folder}")
            tool = find_tool()
            if not tool:
                self.fail(1, 0, "e_tool"); return
            self.ui(lambda: (self.checks.__setitem__(0, "ok"), self.render_checks()))
            files = tree(folder)
            if not files:
                self.fail(1, None, "e_nodata"); return
            total = sum(os.path.getsize(p) for p in files.values())
            self.ui(lambda: self.show_metrics(len(files), total, size))
            if total > size:
                self.fail(1, None, "e_size", data=fmt_size(total), part=fmt_size(size)); return
            self.ui(self.set_progress, 15)
            out_dir = output_dir()
            out = os.path.join(out_dir, "littlefs.bin")
            if os.path.exists(out): os.remove(out)
            cmd = [tool, "-c", folder, "-b", str(BLOCK), "-p", str(PAGE), "-s", str(size), out]
            L("> mklittlefs -c <folder> -b %d -p %d -s %d littlefs.bin" % (BLOCK, PAGE, size))
            code, txt = run_stream(cmd, lambda l: None)
            for l in txt.splitlines()[:60]: L(l)
            if code != 0 or not os.path.isfile(out):
                self.fail(1, 1, "e_build"); return
            self.ui(self.set_progress, 45)
            real = os.path.getsize(out)
            if real != size:
                L(f"Size mismatch: {real} != {size}")
                self.fail(1, 1, "e_build"); return
            L(f"littlefs.bin = {real:,} bytes   SHA-256 {sha256_file(out)}")
            self.ui(lambda: (self.checks.__setitem__(1, "ok"), self.steps.__setitem__(1, "ok"),
                             self.steps.__setitem__(2, "on"), self.render_checks(), self.draw_steps()))
            # verify by unpacking
            tmp = tempfile.mkdtemp(prefix="lfsverify_")
            try:
                # mklittlefs prefixes "./" to the unpack dir, so use a relative path
                code, txt = run_stream([tool, "-u", "unpacked", "-b", str(BLOCK), "-p", str(PAGE), "-s", str(size), out], cwd=tmp)
                self.ui(self.set_progress, 75)
                if code != 0:
                    for l in txt.splitlines()[:40]: L(l)
                    self.fail(2, 2, "e_verify"); return
                got = tree(os.path.join(tmp, "unpacked"))
                bad = []
                for rel, p in files.items():
                    if rel not in got: bad.append(f"missing: {rel}")
                    elif sha256_file(p) != sha256_file(got[rel]): bad.append(f"differs: {rel}")
                for rel in got:
                    if rel not in files: bad.append(f"extra: {rel}")
                if bad:
                    for b in bad[:40]: L(b)
                    self.fail(2, 2, "e_verify"); return
                for rel in sorted(files):
                    L(f"  ✔ /{rel}  {fmt_size(os.path.getsize(files[rel]))}  {sha256_file(files[rel])[:12]}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            L(f"Verify OK: {len(files)} files identical.")
            self.ui(self.pipeline_done, out, size)
        except Exception as e:
            L(f"[error] {e}")
            self.fail(1, 1, "e_build")

    def show_metrics(self, n, total, size):
        self.mv[0].config(text=str(n))
        self.mv[1].config(text=fmt_size(total))
        self.mv[2].config(text=fmt_size(size))
        pct = min(100, total * 100 / size)
        self.ubar.set(pct)
        self.ulbl.config(text=f"{fmt_size(total)} / {fmt_size(size)}  ({pct:.0f}%)")
        self.metrics = (n, total, size)

    def pipeline_done(self, out, size):
        self.bin_path, self.built_size = out, size
        self.busy = False
        self.steps = ["ok", "ok", "ok", "on"]
        self.checks[2] = "ok"
        self.update_check4()
        self.set_progress(100, green=True)
        self.set_stage(2)
        self.set_guide("g2", "ok")
        self.render_all()
        self.write_report()

    # -------------------------------------------------------------- program ---
    def start_program(self):
        cur, err = self.current()
        if err:
            self.set_guide(err, "err"); return
        if not self.bin_path or not os.path.isfile(self.bin_path):
            self.start_pipeline(); return
        if cur["size"] != self.built_size:
            self.start_pipeline(); return
        if not cur["port"]:
            if self.mode.get() == "auto":
                self.set_guide("e_dev", "err"); self.start_detect()
            else:
                self.set_guide("e_port", "err")
            return
        flash = self.dev.get("flash") if self.dev else None
        if flash:
            mm = re.match(r"(\d+)\s*MB", flash, re.I)
            if mm and cur["off"] + cur["size"] > int(mm.group(1)) * 1024 * 1024:
                self.set_guide("e_fit", "err", flash=flash); return
        msg = self.rw("confirm", name="littlefs.bin", size=fmt_size(cur["size"]), port=cur["port"],
                      chip=cur["chip"].upper(), off="‎" + hex(cur["off"]) + "‎")
        if self.dev and self.dev.get("pt_off") is not None and self.dev["pt_off"] != cur["off"]:
            msg += self.rw("confirm_warn", pt=hex(self.dev["pt_off"]))
        if self.ask(self.rw("confirm_t"), msg, [(self.tr("b_go"), True, True), (self.tr("b_cancel"), False, False)]) is not True:
            return
        self.busy = True
        self.stage = 3
        self.steps = ["ok", "ok", "ok", "on"]
        self.set_progress(0)
        self.set_guide("g3", "busy")
        self.render_all()
        threading.Thread(target=self.program_worker, args=(cur,), daemon=True).start()

    def boot_assist(self, port):
        """Ask the user to press BOOT and detect the board entering download mode.
        Returns "ok" (detected) | "try" (user wants to try anyway) | "cancel" | "busy"."""
        built = threading.Event()
        h = {}

        def build():
            w, rs, st = self.make_dialog(self.rw("boot_t"), self.rw("boot_msg"),
                                          [(self.tr("b_try"), "try", False), (self.tr("b_cancel"), "cancel", False)],
                                          icon="⚠", status=True)
            st.config(text=self.tr("boot_wait"))
            h.update(w=w, res=rs, st=st)
            built.set()
        self.ui(build)
        built.wait(10)
        self.ui(self.play, "ready")

        def setst(key, bg=None, fg=None):
            def f():
                try:
                    h["st"].config(text=self.tr(key), **({"bg": bg} if bg else {}), **({"fg": fg} if fg else {}))
                except Exception:
                    pass
            self.ui(f)

        def close():
            self.ui(lambda: h["w"].winfo_exists() and h["w"].destroy())
        result, ser = None, None
        try:
            import serial
            ser = serial.Serial()
            ser.port, ser.baudrate, ser.timeout = port, 115200, 0.25
            ser.dtr, ser.rts = False, False
            ser.open()
        except Exception:
            ser = None
            setst("boot_busy", ER_BG, ER)
        buf = b""
        t0 = time.time()
        while time.time() - t0 < 300:
            if h["res"][0] is not None:
                result = h["res"][0]
                break
            try:
                if not h["w"].winfo_exists():
                    result = "cancel"
                    break
            except Exception:
                result = "cancel"
                break
            if ser is None:
                time.sleep(0.3)
                continue
            try:
                buf = (buf + ser.read(512))[-4000:]
            except Exception:
                break
            if re.search(rb"waiting for download|DOWNLOAD_BOOT|download\(", buf, re.I):
                setst("boot_seen", OK_BG, OK)
                time.sleep(0.8)
                result = "ok"
                break
            if re.search(rb"SPI_FAST_FLASH_BOOT|boot:0x1[0-9a-f]\b", buf, re.I):
                setst("boot_normal", WA_BG, WA)
                buf = b""
        try:
            if ser: ser.close()
        except Exception:
            pass
        close()
        time.sleep(0.4)
        return result or "cancel"

    def program_worker(self, cur):
        L = lambda t: self.ui(self.logw, t)
        port, baud, off = cur["port"], cur["baud"], hex(cur["off"])
        chip = ["--chip", cur["chip"]] if cur["chip"] != "auto" else []
        conn_err = r"Failed to connect|Wrong boot mode|No serial data received|Timed out waiting for packet|Invalid head of packet"
        busy_re = r"Could not open|port is busy|Access is denied|PermissionError|in use"
        last = [0]

        def on_line(l):
            m = re.search(r"(\d{1,3})\s?%", l)
            if m:
                v = int(m.group(1))
                if v != last[0]:
                    last[0] = v
                    self.ui(self.set_progress, v)
                if ("Writing at" in l or "Read" in l) and v % 20 != 0:
                    return
            L(l)
        txt, ok, boot_used, rb_ok, rb_ran = "", False, False, False, False
        try:
            # 1) connect (automatic reset first)
            L("Connecting to the board...")
            code, txt = run_stream(esp_cmd(*chip, "--port", port, "--baud", baud, "--connect-attempts", "2", "flash-id"),
                                   lambda l: None, timeout=60)
            before = []
            if code != 0:
                for l in txt.splitlines()[-3:]: L("[connect] " + l)
                if not re.search(busy_re, txt, re.I):
                    # 2) manual BOOT assistance
                    L("Board not in download mode - asking the user to press BOOT.")
                    r = self.boot_assist(port)
                    if r == "cancel":
                        L("Cancelled by user.")
                        txt = "cancelled"
                        raise RuntimeError("cancel")
                    if r == "busy":
                        txt = "Could not open port"
                    else:
                        before = ["--before", "no-reset"]
                        boot_used = True
            if not re.search(busy_re, txt if code != 0 and not boot_used else "", re.I):
                # 3) write (stay in download mode afterwards so we can read back)
                L(f"> esptool write-flash {off} littlefs.bin")
                last[0] = 0
                code, txt = run_stream(esp_cmd(*chip, "--port", port, "--baud", baud, *before, "--after", "no-reset",
                                               "write-flash", off, self.bin_path), on_line, timeout=600)
                ok = code == 0 and "Hash of data verified" in txt
            if ok:
                # 4) read back and compare with the original bin
                self.ui(self.set_guide, "g5", "busy")
                self.ui(self.set_progress, 0)
                last[0] = 0
                rb_ran = True
                tmpd = tempfile.mkdtemp(prefix="lfsreadback_")
                try:
                    rb = os.path.join(tmpd, "readback.bin")
                    L(f"> esptool read-flash {off} {hex(cur['size'])} (verification)")
                    c2, t2 = run_stream(esp_cmd(*chip, "--port", port, "--baud", baud, "--before", "no-reset",
                                                "read-flash", off, hex(cur["size"]), rb), on_line, timeout=600)
                    if c2 == 0 and os.path.isfile(rb):
                        a, b = sha256_file(rb), sha256_file(self.bin_path)
                        rb_ok = a == b
                        L(f"Chip SHA-256  {a}")
                        L(f"File SHA-256  {b}")
                        L("READ-BACK " + ("MATCH: the data on the chip is identical to littlefs.bin"
                                          if rb_ok else "MISMATCH: the chip does not contain the same data!"))
                        tool = find_tool()
                        if tool:
                            c3, t3 = run_stream([tool, "-u", "unpacked", "-b", str(BLOCK), "-p", str(PAGE),
                                                 "-s", str(cur["size"]), rb], cwd=tmpd)
                            got = tree(os.path.join(tmpd, "unpacked")) if c3 == 0 else {}
                            L(f"Files found on the chip: {len(got)}")
                            for rel in sorted(got)[:60]:
                                L(f"  /{rel}  {fmt_size(os.path.getsize(got[rel]))}")
                    else:
                        for l in t2.splitlines()[-3:]: L("[read-back] " + l)
                        L("Read-back could not be completed (write itself was verified by the chip's MD5).")
                finally:
                    shutil.rmtree(tmpd, ignore_errors=True)
                # 5) restart the board (works when RTS/EN is wired; harmless otherwise)
                try:
                    run_stream(esp_cmd(*chip, "--port", port, "--baud", "115200", "--before", "no-reset",
                                       "--after", "hard-reset", "chip-id"), lambda l: None, timeout=30)
                except Exception:
                    pass
        except RuntimeError:
            pass
        except Exception as e:
            code, txt = -1, str(e)
        if rb_ran and not rb_ok:
            ok = False
            txt += "\nread-back mismatch"

        def done():
            self.busy = False
            if ok:
                self.steps = ["ok"] * 4
                self.set_progress(100, green=True)
                self.set_stage(4)
                self.set_guide("g4v" if rb_ok else "g4", "ok")
                self.logw("Programming OK - " + ("chip read-back verified." if rb_ok else "hash verified."))
                self.render_all()
                self.write_report(result="OK")
                self.after(300, lambda: self.ask(self.rw("fin_t"), self.rw("fin_msg"),
                                                 [(self.tr("b_ok"), True, True)], icon="✔", color=OK))
                return
            self.stage = 2
            self.steps = ["ok", "ok", "ok", "err"]
            busy_port = re.search(busy_re, txt, re.I)
            boot = re.search(conn_err, txt, re.I) or txt == "cancelled"
            self.set_guide("e_busy_port" if busy_port else ("e_boot" if boot else "e_write"), "err")
            self.set_progress(0)
            self.render_buttons()
            self.render_all()
            self.write_report(result="FAILED")
        self.ui(done)

    # --------------------------------------------------------------- report ---
    def write_report(self, result=None):
        try:
            p = os.path.join(output_dir(), "report.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(self.report))
                if result:
                    f.write(f"\n\nProgramming result: {result}\n")
            return p
        except Exception:
            return None

    def save_report(self):
        p = self.write_report()
        if p:
            self.logw(f"Report saved: {p}")
            try: os.startfile(os.path.dirname(p))
            except Exception: pass

    def open_output(self):
        try: os.startfile(output_dir())
        except Exception: pass


if __name__ == "__main__":
    App().mainloop()
