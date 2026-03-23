"""
Voice to Text Mac v3.0
======================
Option (⌥) cift tiklama ile aktif olan, Whisper API + GPT-4o-mini ile
cok dilli ses tanima ve ceviri yapan macOS uygulamasi.

Kisayollar:
    Option x2   → Ses kaydi baslat/durdur
    Ctrl x2     → Secili metni Turkceye cevir (popup)
    Ctrl+Option → Secili metni Ingilizceye cevir (yerine yapistir)
    ESC         → Iptal
"""

import sys
import os
import math
import random
import signal
import time
import threading
import subprocess
import fcntl
import tempfile
import tkinter as tk

import numpy as np
import sounddevice as sd
from scipy.io import wavfile
import pyperclip
from openai import OpenAI
from PIL import Image, ImageDraw, ImageTk

# macOS native keyboard monitoring
import Quartz
from Quartz import (
    CGEventTapCreate, kCGSessionEventTap, kCGHeadInsertEventTap,
    kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged,
    CGEventGetIntegerValueField, kCGKeyboardEventKeycode,
    CGEventGetFlags, kCGEventFlagMaskAlternate, kCGEventFlagMaskControl,
    CFMachPortCreateRunLoopSource, CFRunLoopGetCurrent, CFRunLoopAddSource,
    kCFRunLoopDefaultMode, CFRunLoopRun,
)

import config


# =============================================================================
# macOS Helpers
# =============================================================================

def run_applescript(script: str) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def get_frontmost_app() -> str:
    result = run_applescript(
        'tell application "System Events" to get name of first application process whose frontmost is true'
    )
    if result and result.lower() in ("python", "python3", "python3.14"):
        result = run_applescript('''
            tell application "System Events"
                set appList to every application process whose visible is true
                repeat with proc in appList
                    set procName to name of proc
                    if procName is not "Python" and procName is not "python3" and procName is not "python3.14" then
                        return procName
                    end if
                end repeat
            end tell
        ''')
    return result


def activate_app(app_name: str):
    if app_name:
        run_applescript(f'''
            tell application "{app_name}"
                activate
            end tell
            delay 0.3
        ''')


def send_cmd_v():
    run_applescript('tell application "System Events" to keystroke "v" using {command down}')


def send_cmd_c():
    run_applescript('tell application "System Events" to keystroke "c" using {command down}')


def send_cmd_a():
    run_applescript('tell application "System Events" to keystroke "a" using {command down}')


def paste_to_app(app_name: str):
    if app_name:
        run_applescript(f'''
            tell application "System Events"
                tell process "{app_name}"
                    set frontmost to true
                end tell
                delay 0.2
                keystroke "v" using {{command down}}
            end tell
        ''')
    else:
        send_cmd_v()


# =============================================================================
# macOS Keyboard Monitor (Quartz CGEventTap — no pynput needed)
# =============================================================================

# macOS keycodes
KC_ESCAPE = 53
KC_OPTION_L = 58
KC_OPTION_R = 61
KC_CONTROL_L = 59
KC_CONTROL_R = 62


class KeyboardMonitor:
    """Quartz CGEventTap ile global keyboard monitoring."""

    def __init__(self, on_press_cb, on_release_cb, on_flags_cb):
        self._on_press = on_press_cb
        self._on_release = on_release_cb
        self._on_flags = on_flags_cb
        self._thread = None
        self._running = False
        self._prev_flags = 0

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _callback(self, proxy, event_type, event, refcon):
        try:
            if event_type == kCGEventKeyDown:
                keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                self._on_press(keycode)
            elif event_type == kCGEventKeyUp:
                keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                self._on_release(keycode)
            elif event_type == kCGEventFlagsChanged:
                flags = CGEventGetFlags(event)
                keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                self._on_flags(keycode, flags, self._prev_flags)
                self._prev_flags = flags
        except Exception as e:
            print(f"Keyboard callback hatasi: {e}")
        return event

    def _run(self):
        mask = (1 << kCGEventKeyDown) | (1 << kCGEventKeyUp) | (1 << kCGEventFlagsChanged)
        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            0,  # listenOnly=0, ama biz event'i degistirmiyoruz
            mask,
            self._callback,
            None,
        )
        if tap is None:
            print("HATA: CGEventTap olusturulamadi! Accessibility izni gerekli.")
            print("System Settings > Privacy & Security > Accessibility > Warp etkinlestir")
            return

        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopDefaultMode)
        print("Keyboard monitor aktif.")
        CFRunLoopRun()


# =============================================================================
# Audio Recorder
# =============================================================================

class AudioRecorder:
    def __init__(self):
        self.frames = []
        self.is_recording = False
        self.recording_thread = None

    def start_recording(self):
        if self.is_recording:
            return
        self.frames = []
        self.is_recording = True
        self.recording_thread = threading.Thread(target=self._record, daemon=True)
        self.recording_thread.start()

    def _record(self):
        try:
            def callback(indata, frames, time_info, status):
                if self.is_recording:
                    self.frames.append(indata.copy())

            with sd.InputStream(
                samplerate=config.SAMPLE_RATE,
                channels=config.CHANNELS,
                dtype="int16",
                callback=callback,
                blocksize=config.CHUNK_SIZE,
            ):
                start_time = time.time()
                while self.is_recording:
                    if time.time() - start_time > config.MAX_RECORDING_DURATION:
                        self.is_recording = False
                        break
                    time.sleep(0.1)
        except Exception as e:
            print(f"Kayit hatasi: {e}")
            self.is_recording = False

    def stop_recording(self) -> str | None:
        if not self.is_recording and not self.frames:
            return None
        self.is_recording = False
        if self.recording_thread:
            self.recording_thread.join(timeout=2.0)
        if not self.frames:
            return None

        audio_data = np.concatenate(self.frames, axis=0)
        duration = len(audio_data) / config.SAMPLE_RATE
        print(f"Kayit suresi: {duration:.2f}s")

        wav_path = config.TEMP_AUDIO_FILE
        wavfile.write(wav_path, config.SAMPLE_RATE, audio_data)
        return wav_path

    def cleanup(self):
        self.is_recording = False
        self.frames = []


# =============================================================================
# Whisper Transcriber
# =============================================================================

class WhisperTranscriber:
    def __init__(self):
        if not config.OPENAI_API_KEY:
            raise ValueError(config.MSG_NO_API_KEY)
        self.client = OpenAI(api_key=config.OPENAI_API_KEY)

    def transcribe(self, audio_path: str, language: str = None) -> str:
        with open(audio_path, "rb") as audio_file:
            params = {"model": config.WHISPER_MODEL, "file": audio_file}
            if language:
                params["language"] = language
            response = self.client.audio.transcriptions.create(**params)
        return response.text


# =============================================================================
# Floating Indicator — Figma v3.0
# =============================================================================

class FloatingIndicator:
    COLOR_RECORDING = "#FF3B30"
    COLOR_PROCESSING = "#FF9500"
    COLOR_SUCCESS = "#30D158"
    BAR_COLOR = "#d9d9d9"

    LANG_ACTIVE_BORDER = "#00af17"
    LANG_ACTIVE_TEXT = "#00af17"
    LANG_INACTIVE_BORDER = "#5d5d5d"
    LANG_INACTIVE_TEXT = "#5d5d5d"

    LANGUAGES = [("TR", "tr"), ("RU", "ru"), ("EN", "en")]

    def __init__(self, recorder=None):
        self.root = None
        self.canvas = None
        self._is_visible = False
        self.recording_start_time = None
        self.timer_running = False
        self.wave_bars = []
        self.bar_heights = []
        self.recorder = recorder
        self.num_bars = 24
        self.phase = 0.0
        self.pulse_phase = 0.0
        self.timer_text = None
        self.record_dot = None
        self.on_cancel = None
        self.selected_lang = "tr"
        self.lang_buttons = {}

    def set_recorder(self, recorder):
        self.recorder = recorder

    def get_selected_language(self):
        return self.selected_lang

    def _select_language(self, lang_code):
        self.selected_lang = lang_code
        for code, items in self.lang_buttons.items():
            if code == lang_code:
                self.canvas.itemconfig(items["rect"], outline=self.LANG_ACTIVE_BORDER)
                self.canvas.itemconfig(items["text"], fill=self.LANG_ACTIVE_TEXT)
            else:
                self.canvas.itemconfig(items["rect"], outline=self.LANG_INACTIVE_BORDER)
                self.canvas.itemconfig(items["text"], fill=self.LANG_INACTIVE_TEXT)

    def show(self, message: str):
        if self.root is None:
            self._create_window()
        self.recording_start_time = time.time()
        self.timer_running = True
        self.bar_heights = [2.0] * self.num_bars
        self.phase = 0.0
        self.pulse_phase = 0.0

        if self.record_dot:
            self.canvas.itemconfig(self.record_dot, fill=self.COLOR_RECORDING)
        if self.timer_text:
            self.canvas.itemconfig(self.timer_text, fill="#d9d9d9")
        for bar in self.wave_bars:
            self.canvas.itemconfig(bar, fill=self.BAR_COLOR)
        self._select_language(self.selected_lang)

        if not self._is_visible:
            self.root.deiconify()
            self.root.lift()
            self._is_visible = True
        self._update_timer()
        self._animate()
        self.root.update()

    def hide(self):
        self.timer_running = False
        if self.root and self._is_visible:
            self.root.withdraw()
            self._is_visible = False

    def update_message(self, message: str):
        self.timer_running = False
        if not self.canvas:
            return
        if "Tamam" in message:
            color = self.COLOR_SUCCESS
        elif "Hata" in message:
            color = "#FF3B30"
        else:
            color = self.COLOR_PROCESSING
        if self.record_dot:
            self.canvas.itemconfig(self.record_dot, fill=color)
        self.root.update()

    def _get_audio_level(self):
        if self.recorder and self.recorder.frames:
            try:
                last_frame = self.recorder.frames[-1]
                arr = np.frombuffer(last_frame, dtype=np.int16)
                return min(np.abs(arr).mean() / 3000, 1.0)
            except Exception:
                pass
        return 0.03

    def _update_timer(self):
        if not self.timer_running or not self._is_visible:
            return
        elapsed = time.time() - self.recording_start_time
        m, s = int(elapsed // 60), int(elapsed % 60)
        if self.timer_text:
            self.canvas.itemconfig(self.timer_text, text=f"{m}:{s:02d}")
        if self.root:
            self.root.after(100, self._update_timer)

    def _animate(self):
        if not self.timer_running or not self._is_visible:
            return
        audio = self._get_audio_level()
        self.phase += 0.1
        self.pulse_phase += 0.06

        p = 0.5 + 0.5 * math.sin(self.pulse_phase)
        r = 255
        g = int(59 + 30 * p)
        b = int(48 + 30 * p)
        if self.record_dot:
            self.canvas.itemconfig(self.record_dot, fill=f"#{r:02x}{g:02x}{b:02x}")

        self.bar_heights.pop(0)
        base = 2 + audio * 16
        wave_val = math.sin(self.phase) * 2.5
        new_h = max(1.5, (base + wave_val) * random.uniform(0.7, 1.3))
        self.bar_heights.append(new_h)

        cy = self._h // 2
        for i, bar in enumerate(self.wave_bars):
            h = self.bar_heights[i]
            x = self._wave_start_x + i * self._wave_gap
            self.canvas.coords(bar, x, cy - h, x + self._wave_bw, cy + h)
            t = min(1.0, h / 18)
            c = int(180 + 37 * t)
            self.canvas.itemconfig(bar, fill=f"#{c:02x}{c:02x}{c:02x}")

        if self.root:
            self.root.after(50, self._animate)

    def _make_gradient_pill(self, w, h, radius, border=2,
                            bg_left=(8, 8, 10), bg_right=(14, 40, 56),
                            stroke_left=(20, 20, 24), stroke_right=(22, 101, 141)):
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=(0, 0, 0, 255))
        angle_rad = math.radians(45)
        cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
        for x in range(w):
            for y in range(h):
                px = img.getpixel((x, y))
                if px[3] > 0:
                    t = ((x / w) * cos_a + (y / h) * sin_a + 1) / 2
                    t = max(0.0, min(1.0, t))
                    r = int(stroke_left[0] + (stroke_right[0] - stroke_left[0]) * t)
                    g = int(stroke_left[1] + (stroke_right[1] - stroke_left[1]) * t)
                    b = int(stroke_left[2] + (stroke_right[2] - stroke_left[2]) * t)
                    img.putpixel((x, y), (r, g, b, 255))

        inner = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        inner_draw = ImageDraw.Draw(inner)
        inner_draw.rounded_rectangle(
            [border, border, w - 1 - border, h - 1 - border],
            radius=max(radius - border, 1), fill=(0, 0, 0, 255),
        )
        for x in range(w):
            t = x / max(w - 1, 1)
            t_curved = max(0, (t - 0.4) / 0.6) if t > 0.4 else 0
            r = int(bg_left[0] + (bg_right[0] - bg_left[0]) * t_curved)
            g = int(bg_left[1] + (bg_right[1] - bg_left[1]) * t_curved)
            b = int(bg_left[2] + (bg_right[2] - bg_left[2]) * t_curved)
            for y in range(h):
                px = inner.getpixel((x, y))
                if px[3] > 0:
                    inner.putpixel((x, y), (r, g, b, 255))
        return Image.alpha_composite(img, inner)

    def _create_window(self):
        self.root = tk.Tk()
        self.root.title("")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        w, h = 390, 50
        self._h = h
        pad = 18
        gap = 12

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = sh - h - 120

        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Pencere ve canvas bg'yi pill ic rengiyle ayni yap — kose artefaktlari gorulmez
        PILL_BG = "#060608"
        self.root.configure(bg=PILL_BG)
        self.root.attributes("-alpha", 0.95)

        self._drag_x = 0
        self._drag_y = 0

        self.canvas = tk.Canvas(self.root, width=w, height=h, bg=PILL_BG, highlightthickness=0, bd=0)
        self.canvas.pack()

        # Always on top
        def _keep_on_top():
            if self.root:
                try:
                    from AppKit import NSApp, NSFloatingWindowLevel
                    for nsw in NSApp.windows():
                        nsw.setLevel_(NSFloatingWindowLevel)
                except Exception:
                    self.root.attributes("-topmost", True)
                if self._is_visible:
                    self.root.lift()
                self.root.after(1000, _keep_on_top)
        self.root.after(500, _keep_on_top)

        # Pill — tam pencere boyutunda, kose artefaktlari minimumda
        self._rounded_rect(0, 0, w, h, h // 2,
                          fill="#0a0a0c", outline="#125070", width=2)

        cy = h // 2
        cursor_x = pad

        # Kayit noktasi
        dot_r = 6
        dot_cx = cursor_x + dot_r
        self.record_dot = self.canvas.create_oval(
            dot_cx - dot_r, cy - dot_r, dot_cx + dot_r, cy + dot_r,
            fill=self.COLOR_RECORDING, outline="", width=0,
        )
        cursor_x = dot_cx + dot_r + gap

        # Ses dalgasi
        self._wave_bw = 2
        self._wave_gap = 4
        self._wave_start_x = cursor_x
        self.wave_bars = []
        self.bar_heights = [2.0] * self.num_bars
        for i in range(self.num_bars):
            bx = cursor_x + i * self._wave_gap
            bar = self.canvas.create_rectangle(
                bx, cy - 2, bx + self._wave_bw, cy + 2,
                fill=self.BAR_COLOR, outline="", width=0,
            )
            self.wave_bars.append(bar)
        cursor_x += self.num_bars * self._wave_gap + gap

        # Timer
        self.timer_text = self.canvas.create_text(
            cursor_x, cy, text="0:00",
            font=("SF Pro Display", 11, "bold"), fill="#d9d9d9", anchor="w",
        )
        cursor_x += 42 + gap

        # Dil butonlari
        btn_size = 32
        btn_gap = 8
        btn_round = 14
        sz_x = 7
        right_edge = w - pad - sz_x - 20
        total_btns_w = 3 * btn_size + 2 * btn_gap
        available_space = right_edge - cursor_x
        btn_start_x = cursor_x + (available_space - total_btns_w) // 2 - 5

        self.lang_buttons = {}
        for idx, (label, code) in enumerate(self.LANGUAGES):
            bx = btn_start_x + idx * (btn_size + btn_gap)
            is_active = (code == self.selected_lang)
            border_color = self.LANG_ACTIVE_BORDER if is_active else self.LANG_INACTIVE_BORDER
            text_color = self.LANG_ACTIVE_TEXT if is_active else self.LANG_INACTIVE_TEXT

            rect = self._rounded_rect(
                bx, cy - btn_size // 2, bx + btn_size, cy + btn_size // 2,
                btn_round, fill="#0a0a0a", outline=border_color, width=2,
            )
            txt = self.canvas.create_text(
                bx + btn_size // 2, cy, text=label,
                font=("SF Pro Display", 9), fill=text_color, anchor="center",
            )
            hit = self.canvas.create_rectangle(
                bx - 2, cy - btn_size // 2 - 2,
                bx + btn_size + 2, cy + btn_size // 2 + 2,
                fill="", outline="", width=0,
            )
            self.lang_buttons[code] = {"rect": rect, "text": txt, "hit": hit}

            def make_click_handler(c):
                return lambda event: self._select_language(c)
            self.canvas.tag_bind(hit, "<ButtonRelease-1>", make_click_handler(code))
            self.canvas.tag_bind(txt, "<ButtonRelease-1>", make_click_handler(code))
            self.canvas.tag_bind(rect, "<ButtonRelease-1>", make_click_handler(code))

        # X butonu
        sz = 7
        cx_btn = w - pad - sz
        sep_x = cx_btn - 18
        self.canvas.create_line(sep_x, 10, sep_x, h - 10, fill="#3a3a3c", width=1)
        self.x_line1 = self.canvas.create_line(
            cx_btn - sz, cy - sz, cx_btn + sz, cy + sz,
            fill="#FF453A", width=2, capstyle="round",
        )
        self.x_line2 = self.canvas.create_line(
            cx_btn - sz, cy + sz, cx_btn + sz, cy - sz,
            fill="#FF453A", width=2, capstyle="round",
        )
        self.x_hit = self.canvas.create_rectangle(
            cx_btn - 10, cy - 10, cx_btn + 10, cy + 10,
            fill="", outline="", width=0,
        )
        self.canvas.tag_bind(self.x_hit, "<Enter>", lambda e: (
            self.canvas.itemconfig(self.x_line1, fill="#FF6961"),
            self.canvas.itemconfig(self.x_line2, fill="#FF6961"),
        ))
        self.canvas.tag_bind(self.x_hit, "<Leave>", lambda e: (
            self.canvas.itemconfig(self.x_line1, fill="#FF453A"),
            self.canvas.itemconfig(self.x_line2, fill="#FF453A"),
        ))
        for item in (self.x_hit, self.x_line1, self.x_line2):
            self.canvas.tag_bind(item, "<ButtonRelease-1>",
                                lambda e: self.on_cancel() if self.on_cancel else None)

        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.root.withdraw()

    def _on_drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag_move(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def _rounded_rect(self, x1, y1, x2, y2, r=10, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def destroy(self):
        self.timer_running = False
        if self.root:
            self.root.destroy()
            self.root = None


# =============================================================================
# Translation Popup
# =============================================================================

class TranslationPopup:
    def __init__(self):
        self.win = None
        self._follow_id = None

    def show(self, text: str, parent_root=None):
        self.hide()
        if parent_root is None:
            return
        self.win = tk.Toplevel(parent_root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="black")
        self.win.attributes("-alpha", 0.95)

        frame = tk.Frame(self.win, bg="#1c1c1e", highlightbackground="#3a3a3c",
                         highlightthickness=1, bd=0)
        frame.pack(padx=3, pady=3)
        inner = tk.Frame(frame, bg="#1c1c1e")
        inner.pack(padx=14, pady=10)
        tk.Label(inner, text=text, font=("SF Pro Display", 13),
                fg="#f5f5f7", bg="#1c1c1e", wraplength=324,
                justify="left", anchor="nw").pack()

        self.win.update_idletasks()
        self._update_position()
        self._follow_mouse()

    def _update_position(self):
        if not self.win:
            return
        mx, my = self.win.winfo_pointerx(), self.win.winfo_pointery()
        ww, wh = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        px = mx + 16 if mx + 16 + ww < sw - 10 else mx - ww - 8
        py = my + 16 if my + 16 + wh < sh - 10 else my - wh - 8
        self.win.geometry(f"+{max(5, px)}+{max(5, py)}")

    def _follow_mouse(self):
        if not self.win:
            return
        self._update_position()
        self._follow_id = self.win.after(50, self._follow_mouse)

    def hide(self):
        if self.win:
            if self._follow_id:
                try:
                    self.win.after_cancel(self._follow_id)
                except Exception:
                    pass
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None
            self._follow_id = None


# =============================================================================
# API Key helpers
# =============================================================================

def get_env_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

def save_api_key(api_key: str):
    with open(get_env_path(), "w", encoding="utf-8") as f:
        f.write(f"OPENAI_API_KEY={api_key}\n")

def load_api_key() -> str:
    env_path = get_env_path()
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("OPENAI_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""


def show_settings_window(blocking=False, on_save_callback=None):
    BG, TEXT, TEXT_DIM, GREEN, GRAY_BTN = "#1c1c1e", "#f5f5f7", "#86868b", "#30D158", "#3a3a3c"
    win = tk.Tk() if blocking else tk.Toplevel()
    win.title("Voice to Text — Ayarlar")
    win.configure(bg=BG)
    win.attributes("-topmost", True)

    tk.Label(win, text="Ayarlar", font=("SF Pro Display", 18, "bold"), fg=TEXT, bg=BG).pack(padx=40, pady=(30, 0), anchor="w")
    tk.Label(win, text="OpenAI API anahtarinizi girin.", font=("SF Pro Display", 11), fg=TEXT_DIM, bg=BG).pack(padx=40, pady=(4, 0), anchor="w")
    tk.Label(win, text="API Key", font=("SF Pro Display", 12, "bold"), fg=TEXT, bg=BG).pack(padx=40, pady=(24, 0), anchor="w")

    entry = tk.Entry(win, font=("Menlo", 12), bg="#0a0a0a", fg=TEXT, insertbackground=TEXT, relief="solid", bd=1, highlightthickness=0)
    entry.pack(padx=40, pady=(6, 0), fill="x")
    entry.insert(0, load_api_key())
    entry.focus_set()

    status_label = tk.Label(win, text="", font=("SF Pro Display", 10), fg=TEXT_DIM, bg=BG, anchor="w")
    status_label.pack(padx=40, pady=(8, 0), fill="x")

    def on_save():
        key = entry.get().strip()
        if not key:
            status_label.config(text="API key bos olamaz.", fg="#FF3B30"); return
        if not key.startswith("sk-"):
            status_label.config(text="Gecersiz format.", fg="#FF9500"); return
        save_api_key(key)
        config.OPENAI_API_KEY = key
        status_label.config(text="Kaydedildi.", fg=GREEN)
        if on_save_callback:
            on_save_callback(key)
        win.after(1000, win.destroy)

    btn_frame = tk.Frame(win, bg=BG)
    btn_frame.pack(padx=40, pady=(24, 30), fill="x")
    tk.Button(btn_frame, text="  Kaydet  ", command=on_save, font=("SF Pro Display", 12, "bold"),
              bg=GREEN, fg="#000", relief="flat", bd=0, padx=16, pady=8).pack(side="right")
    tk.Button(btn_frame, text="  Iptal  ", command=win.destroy, font=("SF Pro Display", 12),
              bg=GRAY_BTN, fg=TEXT, relief="flat", bd=0, padx=16, pady=8).pack(side="right", padx=(0, 10))

    win.bind("<Return>", lambda e: on_save())
    win.bind("<Escape>", lambda e: win.destroy())
    win.update_idletasks()
    win.minsize(460, win.winfo_reqheight())
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    ww, wh = max(460, win.winfo_reqwidth()), win.winfo_reqheight()
    win.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{(sh - wh) // 2}")
    win.resizable(False, False)
    if blocking:
        win.mainloop()


# =============================================================================
# Main App
# =============================================================================

class VoiceToTextApp:
    def __init__(self):
        if not config.OPENAI_API_KEY:
            show_settings_window(blocking=True)
            if not config.OPENAI_API_KEY:
                print("API anahtari girilmedi.")
                sys.exit(1)

        self.recorder = AudioRecorder()
        self.transcriber = WhisperTranscriber()
        self.indicator = FloatingIndicator()
        self.indicator.set_recorder(self.recorder)
        self.indicator.on_cancel = self._cancel_recording
        self.translation_popup = TranslationPopup()

        self.is_recording = False
        self.recording_start_app = None
        self._clipboard_backup = None

        # Option double-tap state
        self.opt_press_time = 0
        self.opt_last_release_time = 0
        self.opt_other_key = False

        # Ctrl double-tap state
        self.ctrl_press_time = 0
        self.ctrl_last_release_time = 0
        self.ctrl_other_key = False

        # Ctrl+Option combo
        self.ctrl_held = False
        self.opt_held = False
        self.combo_triggered = False

    # --- Quartz keyboard callbacks ---
    def _on_key_press(self, keycode):
        if keycode == KC_ESCAPE:
            if self.is_recording:
                self._cancel_recording()
            elif self.translation_popup.win:
                self.translation_popup.hide()
        else:
            self.opt_other_key = True
            self.ctrl_other_key = True

    def _on_key_release(self, keycode):
        pass  # Normal keys don't need release handling

    def _on_flags_changed(self, keycode, flags, prev_flags):
        """Modifier tuslari (Option, Ctrl) icin flag-based algilama."""
        now = time.time()
        opt_down = bool(flags & kCGEventFlagMaskAlternate)
        ctrl_down = bool(flags & kCGEventFlagMaskControl)
        was_opt = bool(prev_flags & kCGEventFlagMaskAlternate)
        was_ctrl = bool(prev_flags & kCGEventFlagMaskControl)

        # --- Option pressed ---
        if opt_down and not was_opt:
            self.opt_held = True
            self.opt_press_time = now
            self.opt_other_key = False
            if self.ctrl_held and not self.combo_triggered:
                self.combo_triggered = True
                threading.Thread(target=self._handle_english_translation, daemon=True).start()

        # --- Option released ---
        if not opt_down and was_opt:
            self.opt_held = False
            if self.combo_triggered:
                if not self.ctrl_held:
                    self.combo_triggered = False
                self.opt_last_release_time = 0
                return

            hold_ms = (now - self.opt_press_time) * 1000
            if hold_ms > config.MAX_TAP_HOLD_MS or self.opt_other_key:
                self.opt_last_release_time = 0
                return

            gap_ms = (now - self.opt_last_release_time) * 1000
            if self.opt_last_release_time > 0 and gap_ms < config.DOUBLE_TAP_INTERVAL_MS:
                self.opt_last_release_time = 0
                self._toggle_recording()
            else:
                self.opt_last_release_time = now

        # --- Ctrl pressed ---
        if ctrl_down and not was_ctrl:
            self.ctrl_held = True
            self.ctrl_press_time = now
            self.ctrl_other_key = False
            if self.opt_held and not self.combo_triggered:
                self.combo_triggered = True
                threading.Thread(target=self._handle_english_translation, daemon=True).start()

        # --- Ctrl released ---
        if not ctrl_down and was_ctrl:
            self.ctrl_held = False
            if self.combo_triggered:
                if not self.opt_held:
                    self.combo_triggered = False
                self.ctrl_last_release_time = 0
                return

            hold_ms = (now - self.ctrl_press_time) * 1000
            if hold_ms > config.MAX_TAP_HOLD_MS or self.ctrl_other_key:
                self.ctrl_last_release_time = 0
                return

            gap_ms = (now - self.ctrl_last_release_time) * 1000
            if self.ctrl_last_release_time > 0 and gap_ms < config.DOUBLE_TAP_INTERVAL_MS:
                self.ctrl_last_release_time = 0
                threading.Thread(target=self._handle_translation, daemon=True).start()
            else:
                self.ctrl_last_release_time = now

    # --- Recording ---
    def _toggle_recording(self):
        if self.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        try:
            self._clipboard_backup = pyperclip.paste()
        except Exception:
            self._clipboard_backup = None
        self.recording_start_app = get_frontmost_app()
        self.is_recording = True
        if self.indicator.root:
            self.indicator.root.after(0, lambda: self.indicator.show(config.MSG_LISTENING))
        self.recorder.start_recording()

    def _cancel_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        self.recorder.stop_recording()
        self.recorder.cleanup()
        if self.indicator.root:
            self.indicator.root.after(0, self.indicator.hide)

    def _stop_recording(self):
        self.is_recording = False
        if self.indicator.root:
            self.indicator.root.after(0, lambda: self.indicator.update_message(config.MSG_PROCESSING))

        audio_path = self.recorder.stop_recording()
        if not audio_path:
            if self.indicator.root:
                self.indicator.root.after(0, lambda: self.indicator.update_message(config.MSG_RECORDING_TOO_SHORT))
            threading.Timer(1.0, lambda: self.indicator.root.after(0, self.indicator.hide) if self.indicator.root else None).start()
            return

        threading.Thread(target=self._process_audio, args=(audio_path,), daemon=True).start()

    def _process_audio(self, audio_path: str):
        try:
            target_lang = self.indicator.get_selected_language()
            text = self.transcriber.transcribe(audio_path, language=None)

            if text:
                translated = self._translate_to_target(text, target_lang)
                pyperclip.copy(translated)
                time.sleep(0.2)

                target_app = self.recording_start_app
                if target_app:
                    activate_app(target_app)
                    time.sleep(0.3)
                    paste_to_app(target_app)
                else:
                    send_cmd_v()

                if self.indicator.root:
                    self.indicator.root.after(0, lambda: self.indicator.update_message(config.MSG_SUCCESS))
            else:
                if self.indicator.root:
                    self.indicator.root.after(0, lambda: self.indicator.update_message(config.MSG_ERROR))
        except Exception as e:
            print(f"Hata: {e}")
            if self.indicator.root:
                self.indicator.root.after(0, lambda: self.indicator.update_message(config.MSG_ERROR))

        time.sleep(1)
        if self.indicator.root:
            self.indicator.root.after(0, self.indicator.hide)

    # --- Translation ---
    def _translate_to_target(self, text: str, target_lang: str) -> str:
        lang_names = {"tr": "Turkish", "ru": "Russian", "en": "English"}
        target_name = lang_names.get(target_lang, "Turkish")
        try:
            response = self.transcriber.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": f"You are a translator. Translate the given text to {target_name}. Only output the translation, nothing else. If the text is already in {target_name}, return it as-is."},
                    {"role": "user", "content": text},
                ],
                max_tokens=1000, temperature=0.3,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"Ceviri hatasi: {e}")
            return text

    def _get_selected_text(self) -> str:
        old_clip = ""
        try:
            old_clip = pyperclip.paste()
        except Exception:
            pass
        send_cmd_c()
        time.sleep(0.2)
        try:
            new_clip = pyperclip.paste()
        except Exception:
            return ""
        try:
            pyperclip.copy(old_clip)
        except Exception:
            pass
        if not new_clip or new_clip == old_clip:
            return ""
        text = new_clip.strip()
        return text[:2000] if len(text) > 2000 else text

    def _handle_translation(self):
        selected = self._get_selected_text()
        if not selected:
            return
        try:
            response = self.transcriber.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a translator. Translate the given text to Turkish. Only output the translation, nothing else."},
                    {"role": "user", "content": selected},
                ],
                max_tokens=1000, temperature=0.3,
            )
            result = response.choices[0].message.content.strip()
        except Exception as e:
            result = f"Hata: {e}"
        if self.indicator.root:
            self.indicator.root.after(0, lambda: self.translation_popup.show(result, self.indicator.root))

    def _handle_english_translation(self):
        send_cmd_a()
        time.sleep(0.15)
        send_cmd_c()
        time.sleep(0.2)
        try:
            text = pyperclip.paste()
        except Exception:
            return
        if not text or not text.strip():
            return
        text = text.strip()
        try:
            response = self.transcriber.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a translator. Translate the given text to English. Only output the translation, nothing else."},
                    {"role": "user", "content": text},
                ],
                max_tokens=1000, temperature=0.3,
            )
            result = response.choices[0].message.content.strip()
        except Exception as e:
            result = f"Error: {e}"
        if result:
            pyperclip.copy(result)
            time.sleep(0.1)
            send_cmd_a()
            time.sleep(0.15)
            send_cmd_v()

    # --- Run ---
    def run(self):
        print("Voice to Text v3.0 baslatiliyor...")

        # Keyboard monitor (Quartz — ayri thread)
        self.kb_monitor = KeyboardMonitor(
            on_press_cb=self._on_key_press,
            on_release_cb=self._on_key_release,
            on_flags_cb=self._on_flags_changed,
        )
        self.kb_monitor.start()

        # Kill file watcher
        threading.Thread(target=self._watch_kill_file, daemon=True).start()

        # tkinter — ANA THREAD
        self.indicator._create_window()
        print("Hazir. Option x2 = kayit, Ctrl x2 = ceviri")
        self.indicator.root.mainloop()

    def _watch_kill_file(self):
        while True:
            if os.path.exists(KILL_FILE):
                try:
                    os.remove(KILL_FILE)
                except Exception:
                    pass
                os._exit(0)
            time.sleep(1)

    def stop(self):
        self.kb_monitor.stop()
        self.recorder.cleanup()
        if self.indicator.root:
            self.indicator.root.after(0, self.indicator.destroy)


# =============================================================================
# Single Instance
# =============================================================================

KILL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kill")

def check_single_instance():
    if os.path.exists(KILL_FILE):
        os.remove(KILL_FILE)
    lock_path = os.path.join(tempfile.gettempdir(), "voice_to_text_mac.lock")
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except OSError:
        return None


def main():
    if "--kill" in sys.argv:
        with open(KILL_FILE, "w") as f:
            f.write("kill")
        print("Kill istegi gonderildi.")
        time.sleep(2)
        return

    lock = check_single_instance()
    if lock is None:
        print("Zaten calisiyor.")
        sys.exit(0)

    try:
        app = VoiceToTextApp()
        signal.signal(signal.SIGINT, lambda s, f: os._exit(0))
        signal.signal(signal.SIGTERM, lambda s, f: os._exit(0))
        app.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Hata: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if lock:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


if __name__ == "__main__":
    main()
