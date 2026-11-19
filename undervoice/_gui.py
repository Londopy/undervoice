"""The undervoice window: station details, sound cards and PTT, transmit and a receive log."""

from __future__ import annotations

import math
import queue
import sys
import threading
import time
import tkinter as tk
import tomllib
from pathlib import Path
from tkinter import filedialog, font, ttk
from typing import Any

from . import _config, _updater
from ._audio import read_wav
from ._live import Listener, Transmitter, cards, play, stop_playing
from ._message import Message
from ._modulator import LEVEL_DB, SHAPE
from ._ptt import Keyer, SetupError, choices, open_keyer
from ._waveform import RATE

# Windows 11's own semantic colors, so the window reads like the rest of the desktop
LIGHT = {"tx": "#c42b1c", "rx": "#0f7b0f", "muted": "#5d5d5d", "meter": "#e5e5e5"}
DARK = {
    "bg": "#202020",
    "field": "#2d2d2d",
    "line": "#454545",
    "ink": "#ffffff",
    "muted": "#c5c5c5",
    "tx": "#ff99a4",
    "rx": "#6ccb5f",
    "meter": "#3a3a3a",
}
NO_PTT = "None: VOX (SignaLink and similar) or I key the radio myself"
CHANNELS = ["stable releases", "beta (pre-releases too)"]


def _windows_setting(path: str, name: str) -> int | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return int(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return None


def _dark_mode() -> bool:
    themes = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    return _windows_setting(themes, "AppsUseLightTheme") == 0


def _accent(dark: bool) -> str:
    value = _windows_setting(r"Software\Microsoft\Windows\DWM", "AccentColor")
    if value is None:
        return "#4cc2ff" if dark else "#005fb8"
    # stored as 0xAABBGGRR
    r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
    if dark:
        # Windows shows a lighter shade of the accent on dark backgrounds
        r, g, b = (int(v + (255 - v) * 0.45) for v in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _dark_title_bar(root: tk.Tk) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    on = ctypes.c_int(1)
    # DWMWA_USE_IMMERSIVE_DARK_MODE; Windows versions without it just ignore the call
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))


def _icon(accent: str) -> tk.PhotoImage:
    # a speech waveform with a thin steady data line under it
    img = tk.PhotoImage(width=32, height=32)
    img.put(accent, to=(0, 0, 32, 32))
    for x in range(4, 28, 3):
        h = int(4 + 9 * abs(math.sin(x / 4.0)))
        img.put("#ffffff", to=(x, 15 - h, x + 2, 15 + h))
    img.put("#ffffff", to=(4, 27, 28, 28))
    return img


class App:
    def __init__(self, root: tk.Tk, settings_path: Path | None = None):
        self.root = root
        self.dark = _dark_mode()
        self.c = {**(DARK if self.dark else LIGHT), "accent": _accent(self.dark)}
        self.listener: Listener | None = None
        self.tx: Transmitter | None = None
        self.ptt: Keyer | None = None
        self.sending = False
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.record_path: str | None = None
        self.settings_path = settings_path or _config.folder() / "config.toml"
        self.checking = False
        self.available: _updater.Release | None = None
        settings_problem = None
        try:
            self.settings = _config.load(self.settings_path)
        except tomllib.TOMLDecodeError as e:
            self.settings = _config.defaults()
            settings_problem = f"{self.settings_path} is not valid TOML ({e}); using defaults."

        root.title("undervoice")
        self._icon = _icon(self.c["accent"])
        root.iconphoto(True, self._icon)
        root.minsize(720, 640)
        root.geometry("860x720")
        self._style()
        if self.dark:
            _dark_title_bar(root)

        self.inputs: list[tuple[int, str]] = []
        self.outputs: list[tuple[int, str]] = []
        self.keyers: list[tuple[str, str | None]] = [(NO_PTT, None)]
        setup_problem = self._load_devices()

        outer = ttk.Frame(root, padding=(12, 10, 12, 0))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        self._station(outer).grid(row=0, column=0, sticky="ew")
        self._radio(outer).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._receive(outer).grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self._updates(outer).grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self._status_bar(root)
        self._apply_settings()

        root.bind("<Escape>", lambda e: self.stop_sending())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._check_message()
        _updater.tidy()
        if setup_problem or settings_problem:
            self.say(setup_problem or settings_problem or "", error=True)
        elif self.call.get():
            self.say("Ready.")
        else:
            self.say("Fill in your station and pick the sound cards.")
        root.after(100, self._poll)

    def _style(self) -> None:
        s = ttk.Style(self.root)
        self.mono = next(
            (f for f in ("Consolas", "Menlo", "DejaVu Sans Mono") if f in font.families()),
            "TkFixedFont",
        )
        if not self.dark:
            # the native look where there is one; clam is the most neutral elsewhere
            s.theme_use("vista" if "vista" in s.theme_names() else "clam")
            s.configure("Muted.TLabel", foreground=self.c["muted"])
            s.configure("Treeview", rowheight=22, font=(self.mono, 9))
            return
        c = self.c
        s.theme_use("clam")
        # clam draws every edge with these colors; left alone they stay light gray
        s.configure(
            ".",
            background=c["bg"],
            foreground=c["ink"],
            fieldbackground=c["field"],
            bordercolor=c["line"],
            lightcolor=c["field"],
            darkcolor=c["field"],
            troughcolor=c["bg"],
            arrowcolor=c["ink"],
            insertcolor=c["ink"],
            selectbackground=c["accent"],
            selectforeground="#000000",
        )
        s.map(".", background=[("disabled", c["bg"])], foreground=[("disabled", "#787878")])
        s.configure("TLabelframe", background=c["bg"], bordercolor=c["line"])
        s.configure("TLabelframe.Label", background=c["bg"], foreground=c["ink"])
        s.configure("Muted.TLabel", foreground=c["muted"])
        s.configure("TButton", background=c["field"], padding=(10, 4))
        s.map("TButton", background=[("pressed", c["line"]), ("active", "#353535")])
        s.map("TCombobox", fieldbackground=[("readonly", c["field"])])
        s.configure("TCheckbutton", indicatorbackground=c["field"], indicatorforeground=c["ink"])
        s.map("TCheckbutton", background=[("active", c["bg"])])
        s.configure("Treeview", background=c["field"], rowheight=22, font=(self.mono, 9))
        s.configure("Treeview.Heading", background=c["bg"], foreground=c["muted"], relief="flat")
        s.map("Treeview.Heading", background=[("active", c["field"])])
        s.map(
            "Treeview", background=[("selected", c["accent"])], foreground=[("selected", "#000000")]
        )
        s.configure("Vertical.TScrollbar", background=c["line"], arrowcolor=c["muted"])
        self.root.configure(background=c["bg"])
        self.root.option_add("*TCombobox*Listbox.background", c["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["ink"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#000000")

    def _load_devices(self) -> str | None:
        self.keyers += choices()
        try:
            self.inputs, self.outputs = cards()
        except SetupError as e:
            return str(e)
        return None

    def _field(self, parent: tk.Misc, text: str, widget: tk.Widget, row: int, col: int) -> None:
        ttk.Label(parent, text=text).grid(row=row, column=col, sticky="w", padx=(0, 10))
        widget.grid(row=row + 1, column=col, sticky="ew", padx=(0, 10), pady=(2, 6))

    def _station(self, parent: ttk.Frame) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="Station", padding=(10, 6))
        f.columnconfigure(2, weight=1)
        self.call = tk.StringVar()
        self.grid_sq = tk.StringVar()
        self.text = tk.StringVar()
        for var in (self.call, self.grid_sq, self.text):
            var.trace_add("write", lambda *a: self._check_message())
        self._field(f, "Callsign", ttk.Entry(f, textvariable=self.call, width=12), 0, 0)
        self._field(f, "Grid", ttk.Entry(f, textvariable=self.grid_sq, width=8), 0, 1)
        self._field(f, "Status (32 characters)", ttk.Entry(f, textvariable=self.text), 0, 2)
        self.message_error = ttk.Label(f, foreground=self.c["tx"])
        self.message_error.grid(row=2, column=0, columnspan=3, sticky="w")
        return f

    def _combo(self, parent: tk.Misc, names: list[str], default: str | None) -> ttk.Combobox:
        box = ttk.Combobox(parent, values=names, state="readonly")
        if names:
            box.set(default if default in names else names[0])
        return box

    def _radio(self, parent: ttk.Frame) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="Radio", padding=(10, 6))
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        in_names = [n for _, n in self.inputs]
        out_names = [n for _, n in self.outputs]
        usb_out = next((n for n in out_names if "USB" in n), None)
        usb_in = next((n for n in in_names if "USB" in n), None)
        self.mic = self._combo(f, in_names, None)
        self.to_radio = self._combo(f, out_names, usb_out)
        self.from_radio = self._combo(f, in_names, usb_in)
        self.ptt_box = self._combo(f, [label for label, _ in self.keyers], NO_PTT)
        self._field(f, "Microphone", self.mic, 0, 0)
        self._field(f, "PTT", self.ptt_box, 0, 1)
        self._field(f, "Audio to radio", self.to_radio, 2, 0)
        self._field(f, "Audio from radio", self.from_radio, 2, 1)

        tuning = ttk.Frame(f)
        tuning.grid(row=4, column=0, columnspan=2, sticky="w")
        self.level = tk.StringVar(value=f"{LEVEL_DB:g}")
        self.shape = tk.StringVar(value=f"{SHAPE:g}")
        self.txdelay = tk.StringVar(value="0.3")
        for col, (label, var, lo, hi, step) in enumerate(
            (
                ("Data level (dB)", self.level, -36, -6, 1),
                ("Shape", self.shape, 0, 0.9, 0.1),
                ("TX delay (s)", self.txdelay, 0, 2, 0.1),
            )
        ):
            box = ttk.Spinbox(tuning, from_=lo, to=hi, increment=step, textvariable=var, width=7)
            self._field(tuning, label, box, 0, col)

        actions = ttk.Frame(f)
        actions.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        self.tx_button = ttk.Button(actions, text="Transmit", width=18, command=self.toggle_tx)
        self.tx_button.grid(row=0, column=0, padx=(0, 8))
        self.send_button = ttk.Button(
            actions, text="Send recording...", width=18, command=self.toggle_recording
        )
        self.send_button.grid(row=0, column=1)
        ttk.Label(actions, text="Esc stops transmitting", style="Muted.TLabel").grid(
            row=0, column=2, padx=(12, 0)
        )
        return f

    def _receive(self, parent: ttk.Frame) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="Received", padding=(10, 6))
        f.columnconfigure(2, weight=1)
        f.rowconfigure(1, weight=1)
        self.rx_button = ttk.Button(f, text="Listen", width=18, command=self.toggle_rx)
        self.rx_button.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.record = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            f, text="Record to file", variable=self.record, command=self._choose_record
        ).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 6))
        meter = ttk.Frame(f)
        meter.grid(row=0, column=2, sticky="e", pady=(0, 6))
        ttk.Label(meter, text="Input").grid(row=0, column=0, padx=(0, 6))
        self.meter = tk.Canvas(
            meter, width=160, height=8, background=self.c["meter"], highlightthickness=0
        )
        self.meter.grid(row=0, column=1)
        self.meter_text = ttk.Label(meter, text="", style="Muted.TLabel", width=9, anchor="e")
        self.meter_text.grid(row=0, column=2)

        columns = ("time", "call", "grid", "status", "snr")
        self.log = ttk.Treeview(f, columns=columns, show="headings", height=8)
        for col, label, width, stretch in (
            ("time", "UTC", 80, False),
            ("call", "Callsign", 100, False),
            ("grid", "Grid", 70, False),
            ("status", "Status", 280, True),
            ("snr", "SNR", 70, False),
        ):
            self.log.heading(col, text=label, anchor="w")
            self.log.column(col, width=width, stretch=stretch, anchor="w")
        self.log.grid(row=1, column=0, columnspan=3, sticky="nsew")
        scroll = ttk.Scrollbar(f, orient="vertical", command=self.log.yview)
        scroll.grid(row=1, column=3, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self.empty = ttk.Label(
            f,
            text="Nothing received yet. Pick the audio from the radio and press Listen.",
            style="Muted.TLabel",
        )
        self.empty.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        return f

    def _updates(self, parent: ttk.Frame) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="Updates", padding=(10, 6))
        f.columnconfigure(6, weight=1)
        u = self.settings["updater"]
        self.auto_update = tk.BooleanVar(value=bool(u["enabled"]))
        ttk.Checkbutton(f, text="Check every", variable=self.auto_update).grid(row=0, column=0)
        self.interval = tk.StringVar(value=str(u["check_interval_h"]))
        ttk.Spinbox(f, from_=1, to=168, increment=1, textvariable=self.interval, width=5).grid(
            row=0, column=1, padx=(4, 4)
        )
        ttk.Label(f, text="hours on the").grid(row=0, column=2)
        self.channel = ttk.Combobox(f, values=CHANNELS, state="readonly", width=20)
        self.channel.set(CHANNELS[1] if u["channel"] == "beta" else CHANNELS[0])
        self.channel.grid(row=0, column=3, padx=(4, 10))
        ttk.Button(f, text="Check now", command=lambda: self.check_updates(manual=True)).grid(
            row=0, column=4
        )
        self.update_button = ttk.Button(f, command=self.install_update)
        self.update_button.grid(row=0, column=5, padx=(8, 0))
        self.update_button.grid_remove()
        self.update_status = ttk.Label(
            f,
            text=f"This is undervoice {_updater.current()}. "
            "Updates are checked against their SHA-256 before they install.",
            style="Muted.TLabel",
        )
        self.update_status.grid(row=1, column=0, columnspan=7, sticky="w", pady=(4, 0))
        return f

    def _apply_settings(self) -> None:
        station = self.settings.get("station", {})
        self.call.set(station.get("call", ""))
        self.grid_sq.set(station.get("grid", ""))
        self.text.set(station.get("status", ""))
        radio = self.settings.get("radio", {})
        for box, key in (
            (self.mic, "mic"),
            (self.to_radio, "to_radio"),
            (self.from_radio, "from_radio"),
        ):
            if radio.get(key) in box.cget("values"):
                box.set(radio[key])
        label = next((lb for lb, how in self.keyers if how and how == radio.get("ptt")), None)
        if label:
            self.ptt_box.set(label)
        for var, key in ((self.level, "level"), (self.shape, "shape"), (self.txdelay, "txdelay")):
            if key in radio:
                var.set(str(radio[key]))

    def _save_settings(self) -> None:
        how = next((h for lb, h in self.keyers if lb == self.ptt_box.get()), None)
        self.settings["station"] = {
            "call": self.call.get().strip(),
            "grid": self.grid_sq.get().strip(),
            "status": self.text.get().strip(),
        }
        self.settings["radio"] = {
            "mic": self.mic.get(),
            "to_radio": self.to_radio.get(),
            "from_radio": self.from_radio.get(),
            "ptt": how or "",
            "level": self.level.get(),
            "shape": self.shape.get(),
            "txdelay": self.txdelay.get(),
        }
        u = self.settings["updater"]
        u["enabled"] = self.auto_update.get()
        u["channel"] = "beta" if self.channel.get() == CHANNELS[1] else "stable"
        try:
            u["check_interval_h"] = min(max(int(float(self.interval.get())), 1), 168)
        except ValueError:
            pass
        try:
            _config.save(self.settings, self.settings_path)
        except OSError as e:
            self.say(f"Can't save settings to {self.settings_path}: {e}", error=True)

    def check_updates(self, manual: bool = False) -> None:
        if self.checking:
            return
        self.checking = True
        u = self.settings["updater"]
        u["last_check"] = int(time.time())
        self._save_settings()
        channel = u["channel"]
        if manual:
            self.update_status.configure(text="Checking for updates...", foreground="")

        def work() -> None:
            try:
                self.events.put(("update", (_updater.newer(channel), manual)))
            except _updater.UpdateError as e:
                self.events.put(("update-failed", (str(e), manual)))

        threading.Thread(target=work, daemon=True).start()

    def _update_due(self) -> bool:
        u = self.settings["updater"]
        if not self.auto_update.get() or self.checking:
            return False
        try:
            hours = min(max(float(self.interval.get()), 1), 168)
        except ValueError:
            return False
        return time.time() - float(u["last_check"]) >= hours * 3600

    def _show_update(self, release: _updater.Release | None, manual: bool) -> None:
        self.checking = False
        self.available = release
        if release is None:
            if manual:
                self.update_status.configure(
                    text=f"undervoice {_updater.current()} is the newest version.",
                    foreground=self.c["rx"],
                )
            return
        if _updater.how_installed() == "pip":
            self.update_status.configure(
                text=f"undervoice {release.version} is out. This copy came from pip: "
                "run pip install -U undervoice[live] to update.",
                foreground=self.c["accent"],
            )
            return
        self.update_status.configure(
            text=f"undervoice {release.version} is out (you have {_updater.current()}).",
            foreground=self.c["accent"],
        )
        self.update_button.configure(text=f"Download and install {release.version}")
        self.update_button.grid()

    def install_update(self) -> None:
        release = self.available
        if release is None:
            return
        self.stop_sending()
        self._save_settings()
        self.update_button.state(["disabled"])
        self.update_status.configure(text=f"Downloading undervoice {release.version}...")

        def work() -> None:
            try:
                _updater.install(release)
                self.events.put(("restart", None))
            except (_updater.UpdateError, OSError) as e:
                self.events.put(("install-failed", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _status_bar(self, root: tk.Tk) -> None:
        bar = ttk.Frame(root, padding=(12, 6))
        bar.pack(fill="x", side="bottom")
        bar.columnconfigure(1, weight=1)
        self.state = ttk.Label(bar, width=13)
        self.state.grid(row=0, column=0, sticky="w")
        self.status = ttk.Label(bar, anchor="w")
        self.status.grid(row=0, column=1, sticky="ew")

    def say(self, text: str, error: bool = False) -> None:
        self.status.configure(text=text, foreground=self.c["tx"] if error else "")

    def _message(self) -> Message | None:
        try:
            return Message(self.call.get(), self.grid_sq.get(), self.text.get())
        except ValueError as e:
            self.message_error.configure(text=str(e))
            return None

    def _check_message(self) -> None:
        if not self.call.get().strip():
            self.message_error.configure(text="Enter your callsign to transmit.")
            ok = False
        else:
            ok = self._message() is not None
            if ok:
                self.message_error.configure(text="")
        self.message_ok = ok
        self._show_state()

    def _numbers(self) -> tuple[float, float, float] | None:
        try:
            level, shape, delay = (
                float(self.level.get()),
                float(self.shape.get()),
                float(self.txdelay.get()),
            )
        except ValueError:
            self.say("Data level, shape and TX delay must be numbers.", error=True)
            return None
        if not 0 <= shape < 1:
            self.say("Shape must be from 0 up to (not including) 1.", error=True)
            return None
        if not 0 <= delay <= 5:
            self.say("TX delay must be between 0 and 5 seconds.", error=True)
            return None
        return level, shape, delay

    def _index(self, box: ttk.Combobox, devices: list[tuple[int, str]]) -> int | None:
        return next((i for i, n in devices if n == box.get()), None)

    def _open_ptt(self) -> Keyer | None:
        how = next((h for label, h in self.keyers if label == self.ptt_box.get()), None)
        return None if how is None else open_keyer(how)

    def _show_state(self) -> None:
        c = self.c
        if self.tx or self.sending:
            text, color = "Transmitting", c["tx"]
        elif self.listener:
            text, color = "Listening", c["rx"]
        else:
            text, color = "Idle", c["muted"]
        self.state.configure(text=text, foreground=color)
        ready = self.message_ok and bool(self.outputs)
        self.tx_button.configure(text="Stop" if self.tx else "Transmit")
        self.tx_button.state(
            ["!disabled"] if (ready and not self.sending) or self.tx else ["disabled"]
        )
        self.send_button.configure(text="Stop" if self.sending else "Send recording...")
        self.send_button.state(
            ["!disabled"] if (ready and not self.tx) or self.sending else ["disabled"]
        )
        self.rx_button.configure(text="Stop listening" if self.listener else "Listen")
        self.rx_button.state(["!disabled"] if self.inputs else ["disabled"])

    def toggle_tx(self) -> None:
        if self.tx:
            self.stop_sending()
            return
        message, numbers = self._message(), self._numbers()
        if message is None or numbers is None:
            return
        level, shape, delay = numbers
        try:
            self.ptt = self._open_ptt()
            self.tx = Transmitter(
                message,
                level,
                shape,
                self._index(self.mic, self.inputs),
                self._index(self.to_radio, self.outputs),
                self.ptt,
                delay,
            )
            self.tx.key()
        except SetupError as e:
            self._release_ptt()
            self.say(str(e), error=True)
            return
        self._save_settings()
        how = f"keyed by {self.ptt.name}" if self.ptt else "key the radio yourself"
        self.say(f"Sending {message} under the microphone audio ({how}).")
        self._show_state()

    def toggle_recording(self) -> None:
        if self.sending:
            stop_playing()
            return
        message, numbers = self._message(), self._numbers()
        if message is None or numbers is None:
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Send recording", filetypes=[("WAV audio", "*.wav")]
        )
        if not path:
            return
        try:
            speech = read_wav(path)
        except (OSError, ValueError) as e:
            self.say(f"Can't read {path}: {e}", error=True)
            return
        level, shape, delay = numbers
        try:
            self.ptt = self._open_ptt()
        except SetupError as e:
            self.say(str(e), error=True)
            return
        out = self._index(self.to_radio, self.outputs)
        ptt = self.ptt

        def work() -> None:
            try:
                play(speech, message, level, shape, out, ptt, delay)
            except SetupError as e:
                self.events.put(("error", str(e)))
            finally:
                self.events.put(("sent", path))

        self.sending = True
        threading.Thread(target=work, daemon=True).start()
        self.say(f"Sending {path} ({len(speech) / RATE:.0f} s) with {message}.")
        self._show_state()

    def stop_sending(self) -> None:
        if self.tx:
            self.tx.close()
            self.tx = None
            self._release_ptt()
            self.say("Stopped transmitting.")
        if self.sending:
            stop_playing()
        self._show_state()

    def _release_ptt(self) -> None:
        if self.ptt:
            self.ptt.close()
            self.ptt = None

    def _choose_record(self) -> None:
        if not self.record.get():
            self.record_path = None
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Record received audio to",
            defaultextension=".wav",
            initialfile=time.strftime("undervoice-%Y%m%d-%H%M%S.wav"),
            filetypes=[("WAV audio", "*.wav")],
        )
        if path:
            self.record_path = path
            self.say(f"Listening will record to {path}.")
        else:
            self.record.set(False)

    def toggle_rx(self) -> None:
        if self.listener:
            self.listener.stop()
            self.listener = None
            self.say("Stopped listening.")
            self._show_state()
            return
        numbers = self._numbers()
        if numbers is None:
            return
        try:
            self.listener = Listener(
                numbers[1], self._index(self.from_radio, self.inputs), self.record_path
            )
        except SetupError as e:
            self.say(str(e), error=True)
            return
        self._save_settings()
        self.say(
            f"Listening on {self.from_radio.get()}"
            + (f", recording to {self.record_path}." if self.record_path else ".")
        )
        self._show_state()

    def _poll(self) -> None:
        if self.listener:
            while not self.listener.heard.empty():
                h = self.listener.heard.get()
                m = h.frame.message
                self.log.insert(
                    "",
                    0,
                    values=(
                        time.strftime("%H:%M:%S", time.gmtime(h.when)),
                        m.call,
                        m.grid,
                        m.status,
                        f"{h.frame.snr_db:.1f} dB",
                    ),
                )
                self.empty.grid_remove()
            while not self.listener.notes.empty():
                self.say(self.listener.notes.get(), error=True)
            self._draw_meter(self.listener.peak)
            if not self.listener.running:
                self.listener.stop()
                self.listener = None
                self._show_state()
        else:
            self._draw_meter(None)
        if self.tx:
            while not self.tx.sender.problems.empty():
                self.say(f"Sound card: {self.tx.sender.problems.get()}", error=True)
            if not self.tx.active:
                self.stop_sending()
                self.say("The sound card stopped while transmitting.", error=True)
        while not self.events.empty():
            self._event(*self.events.get())
        if self._update_due():
            self.check_updates()
        self.root.after(100, self._poll)

    def _event(self, kind: str, detail: Any) -> None:
        if kind == "error":
            self.say(str(detail), error=True)
        elif kind == "sent":
            self.sending = False
            self._release_ptt()
            self._show_state()
        elif kind == "update":
            release, manual = detail
            self._show_update(release, manual)
        elif kind == "update-failed":
            message, manual = detail
            self.checking = False
            if manual:
                self.update_status.configure(text=message, foreground=self.c["tx"])
        elif kind == "install-failed":
            self.update_button.state(["!disabled"])
            self.update_status.configure(text=str(detail), foreground=self.c["tx"])
        elif kind == "restart":
            self.close()

    def _draw_meter(self, peak: float | None) -> None:
        self.meter.delete("all")
        if peak is None:
            self.meter_text.configure(text="")
            return
        db = 20 * math.log10(max(peak, 1e-6))
        width = int(160 * min(max((db + 60) / 60, 0), 1))
        color = self.c["tx"] if peak > 0.98 else self.c["accent"]
        self.meter.create_rectangle(0, 0, width, 8, fill=color, width=0)
        self.meter_text.configure(text=f"{db:.0f} dBFS")

    def close(self) -> None:
        self._save_settings()
        self.stop_sending()
        if self.listener:
            self.listener.stop()
        self._release_ptt()
        self.root.destroy()


def run() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()
