"""The undervoice window: station details, sound cards and PTT, transmit, text messages and a
receive log."""

from __future__ import annotations

import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk
from typing import Any

from . import _config, _updater
from ._audio import read_wav
from ._check import MicCheck, MicResult, finish, key_briefly, play_back
from ._cwid import ID_EVERY, due
from ._frames import Frames
from ._modes import SUB, VOICE, frame_length, mix
from ._live import Heard, Listener, Transmitter, cards, play, stop_playing
from ._message import MAX_PARTS, PART_LEN, Message, Part, normalize, split
from ._messages import Inbox, append, heard_line, line, stamp
from ._modulator import LEVEL_DB, SHAPE
from ._ptt import Keyer, SetupError, choices, open_keyer
from ._waveform import RATE

# Windows 11's own semantic colors, so the window reads like the rest of the desktop
LIGHT = {
    "tx": "#c42b1c",
    "rx": "#0f7b0f",
    "muted": "#5d5d5d",
    "meter": "#e5e5e5",
    "line": "#8a8a8a",
}
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
# the data level kept up through pauses, in dB below the recent speech, or off to let it stop
PAUSES = ("off", "-45", "-40", "-35", "-30")
MODE_NAMES = {VOICE: "Under the voice", SUB: "Sub-audible (data port)"}
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


def _dark_title_bar(root: tk.Tk | tk.Toplevel) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    on = ctypes.c_int(1)
    # DWMWA_USE_IMMERSIVE_DARK_MODE; Windows versions without it just ignore the call
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))


def _close(tx: Transmitter | None, ptt: Keyer | None, identify: bool = False) -> bool:
    """Close the transmitter, then the keyer even if unkeying failed. True if a CW ID went out."""
    try:
        return tx is not None and tx.close(identify)
    finally:
        if ptt:
            ptt.close()


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _about(when: datetime, sender: str, to: str, gaps: str = "") -> str:
    about = f"From {sender} to {to or 'everyone'}, {stamp(when)}"
    return f"{about}, {gaps}" if gaps else about


def _recording_parts(going: int, left: int) -> str:
    """What a recording carries of the queued message parts, for the status line."""
    text = f" and {_plural(going, 'message part')}" if going else ""
    if left:
        waits = "waits" if left == 1 else "wait"
        text += f"; {_plural(left, 'part')} that won't fit {waits} for your next over"
    return text


def _left_at_default(radio: dict[str, Any]) -> bool:
    """Whether the saved data level is just the default of the version that saved it, so it
    should follow a new default. Settings from before level_default was saved had -18."""
    try:
        level = float(radio["level"])
        saved_default = float(radio.get("level_default", -18.0))
    except (KeyError, ValueError):
        return False
    return level == saved_default != LEVEL_DB


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
    def __init__(self, root: tk.Tk | tk.Toplevel, settings_path: Path | None = None):
        self.root = root
        self.dark = _dark_mode()
        self.c = {**(DARK if self.dark else LIGHT), "accent": _accent(self.dark)}
        self.listener: Listener | None = None
        self.tx: Transmitter | None = None
        self.ptt: Keyer | None = None
        self.sending = False
        self.held = False
        self.release_pending: str | None = None
        self.identifying = False
        self.mic_check: MicCheck | None = None
        self.testing_ptt = False
        # whichever thread is holding the radio for the CW ID, the PTT test or a recording
        self.helper: threading.Thread | None = None
        # monotonic times for station ID: the last CW ID sent, the last reminder to identify,
        # and the last over's end
        self.last_id: float | None = None
        self.reminded: float | None = None
        self.last_over: float | None = None
        # set when the user stops a recording, which cuts off the CW ID at its end
        self.recording_cut = False
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.record_path: str | None = None
        self.settings_path = settings_path or _config.folder() / "config.toml"
        self.checking = False
        self.available: _updater.Release | None = None
        self.inbox = Inbox()
        # message parts held for the next over, those mixed into the recording going out now,
        # the messages queued and not all gone out yet, the last one queued for Send again,
        # and who the last to go out all the way was for
        self.unsent: list[Part] = []
        self.playing: list[Part] = []
        self.outbox: list[list[Part]] = []
        self.last_message: list[Part] = []
        self.sent_to: str | None = None
        self.draft_parts: list[Part] = []
        # for each row of the messages list, who and when, and the whole text
        self.details: dict[str, tuple[str, str]] = {}
        settings_problem = None
        try:
            self.settings = _config.load(self.settings_path)
        except tomllib.TOMLDecodeError as e:
            self.settings = _config.defaults()
            settings_problem = f"{self.settings_path} is not valid TOML ({e}); using defaults."
        number = self.settings["messages"]["next_number"]
        self.next_number = number % 64 if isinstance(number, int) else 0

        root.title("undervoice")
        self._icon = _icon(self.c["accent"])
        root.iconphoto(True, self._icon)
        root.minsize(760, 600)
        root.geometry("900x700")
        self._style()
        if self.dark:
            _dark_title_bar(root)

        self.inputs: list[tuple[int, str]] = []
        self.outputs: list[tuple[int, str]] = []
        self.keyers: list[tuple[str, str | None]] = [(NO_PTT, None)]
        setup_problem = self._load_devices()

        # packed first, so a short window takes its room from the tabs and never from it
        self._status_bar(root)
        outer = ttk.Frame(root, padding=(12, 10, 12, 0))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        self._station(outer).grid(row=0, column=0, sticky="ew")
        self._radio(outer).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._tabs(outer).grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self._apply_settings()

        root.bind("<Escape>", lambda e: self.stop_sending())
        root.bind("<KeyPress-space>", self._space_down)
        root.bind("<KeyRelease-space>", self._space_up)
        # Space is the talk key here, so buttons take Enter instead of Space
        root.unbind_class("TButton", "<Key-space>")
        root.bind_class("TButton", "<Return>", lambda e: e.widget.tk.call(str(e.widget), "invoke"))
        root.protocol("WM_DELETE_WINDOW", self.close_window)
        self._check_message()
        _updater.tidy()
        if setup_problem or settings_problem:
            self.say(setup_problem or settings_problem or "", error=True)
        elif self.call.get():
            self.say("Ready.")
        else:
            self.say("Fill in your station and pick the sound cards.")
        if self.listen_always.get() and self.inputs and not setup_problem:
            # after the window is drawn, so a sound card error shows up in the status bar
            root.after(300, self._listen_on_open)
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
        s.configure("TNotebook", background=c["bg"], bordercolor=c["line"])
        s.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"], padding=(12, 4))
        s.map(
            "TNotebook.Tab",
            background=[("selected", c["field"]), ("active", "#353535")],
            foreground=[("selected", c["ink"])],
        )
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
        self.pause = tk.StringVar(value=PAUSES[0])
        pauses = ttk.Spinbox(tuning, values=PAUSES, textvariable=self.pause, width=7)
        pauses.configure(state="readonly")
        self._field(tuning, "In pauses (dB)", pauses, 0, 3)
        self.cw_id = tk.BooleanVar(value=False)
        ttk.Checkbutton(tuning, text="CW ID before unkeying, when due", variable=self.cw_id).grid(
            row=1, column=4, sticky="w", padx=(4, 0), pady=(2, 6)
        )
        self.mode = ttk.Combobox(tuning, values=list(MODE_NAMES.values()), state="readonly")
        self.mode.set(MODE_NAMES[VOICE])
        self.mode.bind("<<ComboboxSelected>>", lambda e: self._mode_changed())
        self._field(tuning, "Mode", self.mode, 0, 5)

        actions = ttk.Frame(f)
        actions.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        actions.columnconfigure(2, weight=1)
        self.tx_button = ttk.Button(actions, text="Transmit", width=18, command=self.toggle_tx)
        self.tx_button.grid(row=0, column=0, padx=(0, 8))
        self.send_button = ttk.Button(
            actions, text="Send recording...", width=18, command=self.toggle_recording
        )
        self.send_button.grid(row=0, column=1)
        ttk.Label(actions, text="or hold Space to talk; Esc stops", style="Muted.TLabel").grid(
            row=0, column=2, sticky="w", padx=(12, 0)
        )
        # here rather than with Received, so the mic check shows its level on any tab
        meter = ttk.Frame(actions)
        meter.grid(row=0, column=3, sticky="e")
        ttk.Label(meter, text="Input").grid(row=0, column=0, padx=(0, 6))
        self.meter = tk.Canvas(
            meter, width=160, height=8, background=self.c["meter"], highlightthickness=0
        )
        self.meter.grid(row=0, column=1)
        self.meter_text = ttk.Label(meter, text="", style="Muted.TLabel", width=9, anchor="e")
        self.meter_text.grid(row=0, column=2)

        tests = ttk.Frame(f)
        tests.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 2))
        self.mic_button = ttk.Button(
            tests, text="Test microphone", width=18, command=self.test_microphone
        )
        self.mic_button.grid(row=0, column=0, padx=(0, 8))
        self.ptt_button = ttk.Button(tests, text="Test PTT", width=18, command=self.test_ptt)
        self.ptt_button.grid(row=0, column=1)
        ttk.Label(
            tests,
            text="records you, plays back what would be sent and decodes it; "
            "Test PTT transmits for 2 s",
            style="Muted.TLabel",
        ).grid(row=0, column=2, padx=(12, 0))
        return f

    def _tabs(self, parent: ttk.Frame) -> ttk.Notebook:
        self.tabs = ttk.Notebook(parent)
        self.pages = {
            "Messages": self._messages(self.tabs),
            "Received": self._receive(self.tabs),
            "Updates": self._updates(self.tabs),
        }
        for name, page in self.pages.items():
            self.tabs.add(page, text=name)
        self.unseen = dict.fromkeys(self.pages, 0)
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self._seen())
        return self.tabs

    def _new_on(self, name: str) -> None:
        """Count what arrives on a tab that isn't showing, in the tab's name."""
        page = self.pages[name]
        if self.tabs.select() != str(page):
            self.unseen[name] += 1
            self.tabs.tab(page, text=f"{name} ({self.unseen[name]})")

    def _seen(self) -> None:
        name = next(n for n, page in self.pages.items() if str(page) == self.tabs.select())
        self.unseen[name] = 0
        self.tabs.tab(self.pages[name], text=name)

    def _messages(self, parent: ttk.Notebook) -> ttk.Frame:
        f = ttk.Frame(parent, padding=(10, 8))
        f.columnconfigure(1, weight=1)
        f.rowconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        self._compose(f).grid(row=0, column=0, rowspan=3, sticky="nw", padx=(0, 12))
        columns = ("time", "from", "to", "text")
        # the list and the message under it share whatever height the window has
        self.message_list = ttk.Treeview(
            f, columns=columns, show="headings", height=2, selectmode="browse"
        )
        for col, label, width, stretch in (
            ("time", "UTC", 50, False),
            ("from", "From", 80, False),
            ("to", "To", 80, False),
            ("text", "Message", 200, True),
        ):
            self.message_list.heading(col, text=label, anchor="w")
            self.message_list.column(col, width=width, stretch=stretch, anchor="w")
        # bold too, since some accents are close to the text color
        self.message_list.tag_configure(
            "mine", foreground=self.c["accent"], font=(self.mono, 9, "bold")
        )
        self.message_list.grid(row=0, column=1, sticky="nsew")
        scroll = ttk.Scrollbar(f, orient="vertical", command=self.message_list.yview)
        scroll.grid(row=0, column=2, sticky="ns")
        self.message_list.configure(yscrollcommand=scroll.set)
        self.message_list.bind("<<TreeviewSelect>>", lambda e: self._show_detail())
        # a row shows about 32 characters of up to 384, so the whole message goes here
        self.detail = self._text_box(f, height=4, width=20, takefocus=0)
        self.detail.tag_configure("about", foreground=self.c["muted"])
        self.detail.grid(row=1, column=1, sticky="nsew", pady=(6, 0))
        scroll = ttk.Scrollbar(f, orient="vertical", command=self.detail.yview)
        scroll.grid(row=1, column=2, sticky="ns", pady=(6, 0))
        self.detail.configure(yscrollcommand=scroll.set)
        self._show_detail()
        self.progress = ttk.Label(f)
        self.progress.grid(row=2, column=1, columnspan=2, sticky="w", pady=(4, 0))
        self._messages_file(f).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        return f

    def _compose(self, parent: ttk.Frame) -> ttk.Frame:
        f = ttk.Frame(parent)
        to = ttk.Frame(f)
        to.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.to_call = tk.StringVar()
        self.to_call.trace_add("write", lambda *a: self._check_draft())
        ttk.Label(to, text="To").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(to, textvariable=self.to_call, width=12).grid(row=0, column=1)
        ttk.Label(to, text="blank for everyone", style="Muted.TLabel").grid(
            row=0, column=2, padx=(8, 0)
        )
        self.draft = self._text_box(f, height=3, width=44)
        self.draft.grid(row=1, column=0, sticky="ew")
        self.draft.bind("<<Modified>>", self._draft_edited)
        buttons = ttk.Frame(f)
        buttons.grid(row=1, column=1, rowspan=2, sticky="n", padx=(8, 0))
        self.send_msg_button = ttk.Button(buttons, text="Send", width=12, command=self.send_message)
        self.again_button = ttk.Button(
            buttons, text="Send again", width=12, command=self.send_again
        )
        self.cancel_button = ttk.Button(
            buttons, text="Cancel", width=12, command=self.cancel_message
        )
        for row, button in enumerate((self.send_msg_button, self.again_button, self.cancel_button)):
            button.grid(row=row, column=0, pady=(0, 4))
        # an error wraps under the box, beside the buttons, instead of widening it
        self.counter = ttk.Label(f, style="Muted.TLabel", wraplength=self.draft.winfo_reqwidth())
        self.counter.grid(row=2, column=0, sticky="w", pady=(4, 0))
        return f

    def _text_box(self, parent: tk.Misc, **options: Any) -> tk.Text:
        """A text box drawn to match the ttk fields around it."""
        c = self.c
        box = tk.Text(
            parent,
            wrap="word",
            font="TkTextFont",
            relief="flat",
            borderwidth=0,
            padx=6,
            pady=4,
            highlightthickness=1,
            highlightbackground=c["line"],
            highlightcolor=c["accent"],
            **options,
        )
        if self.dark:
            box.configure(
                background=c["field"],
                foreground=c["ink"],
                insertbackground=c["ink"],
                selectbackground=c["accent"],
                selectforeground="#000000",
                inactiveselectbackground=c["line"],
            )
        return box

    def _show_detail(self) -> None:
        """The message picked in the list, or the newest when none is, in full."""
        rows = self.message_list.selection() or self.message_list.get_children()[:1]
        about, text = self.details[rows[0]] if rows else ("No messages yet.", "")
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", about, "about")
        if text:
            self.detail.insert("end", "\n" + text)
        self.detail.configure(state="disabled")

    def _messages_file(self, parent: ttk.Frame) -> ttk.Frame:
        f = ttk.Frame(parent)
        f.columnconfigure(1, weight=1)
        saved = self.settings["messages"]
        self.save_messages = tk.BooleanVar(value=bool(saved["save"]))
        # beside config.toml, so a portable copy that is moved takes its messages along
        self.default_log = self.settings_path.parent / "messages.txt"
        self.messages_path = str(saved["path"]) or str(self.default_log)
        ttk.Checkbutton(f, text="Save messages to a text file", variable=self.save_messages).grid(
            row=0, column=0, sticky="w"
        )
        self.messages_file = ttk.Label(f, text=self.messages_path, style="Muted.TLabel")
        self.messages_file.grid(row=0, column=1, sticky="w", padx=(10, 10))
        ttk.Button(f, text="Choose...", command=self.choose_messages_file).grid(row=0, column=2)
        self.open_button = ttk.Button(f, text="Open", command=self.open_messages_file)
        self.open_button.grid(row=0, column=3, padx=(8, 0))
        self._show_open()
        return f

    def _receive(self, parent: ttk.Notebook) -> ttk.Frame:
        f = ttk.Frame(parent, padding=(10, 8))
        f.columnconfigure(2, weight=1)
        f.rowconfigure(1, weight=1)
        self.rx_button = ttk.Button(f, text="Listen", width=18, command=self.toggle_rx)
        self.rx_button.grid(row=0, column=0, sticky="w", pady=(0, 6))
        options = ttk.Frame(f)
        options.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 6))
        always = bool(self.settings["receive"]["always"])
        self.listen_always = tk.BooleanVar(value=always)
        ttk.Checkbutton(
            options, text="Listen whenever undervoice is open", variable=self.listen_always
        ).grid(row=0, column=0, sticky="w")
        self.record = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options, text="Record to file", variable=self.record, command=self._choose_record
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

        columns = ("time", "call", "grid", "status", "snr")
        self.log = ttk.Treeview(f, columns=columns, show="headings", height=4)
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

    def _updates(self, parent: ttk.Notebook) -> ttk.Frame:
        f = ttk.Frame(parent, padding=(10, 8))
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
        if radio.get("mode") in MODE_NAMES:
            self.mode.set(MODE_NAMES[radio["mode"]])
        if radio.get("pause") in PAUSES:
            self.pause.set(radio["pause"])
        self.cw_id.set(bool(radio.get("cw_id", False)))
        for var, key in ((self.level, "level"), (self.shape, "shape"), (self.txdelay, "txdelay")):
            if key in radio:
                var.set(str(radio[key]))
        if _left_at_default(radio):
            self.level.set(f"{LEVEL_DB:g}")

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
            "level_default": LEVEL_DB,
            "shape": self.shape.get(),
            "txdelay": self.txdelay.get(),
            "pause": self.pause.get(),
            "mode": self._mode(),
            "cw_id": self.cw_id.get(),
        }
        self.settings["receive"] = {"always": self.listen_always.get()}
        self.settings["messages"] = {
            "save": self.save_messages.get(),
            "path": "" if Path(self.messages_path) == self.default_log else self.messages_path,
            "next_number": self.next_number,
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

    def _status_bar(self, root: tk.Tk | tk.Toplevel) -> None:
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
        # messages go out under this callsign
        self._check_draft()

    def _draft_edited(self, event: tk.Event) -> None:
        if self.draft.edit_modified():
            self.draft.edit_modified(False)
            self._check_draft()

    def _check_draft(self) -> None:
        text = normalize(self.draft.get("1.0", "end"))
        self.draft_parts = []
        error = False
        if not text:
            note = f"Up to {PART_LEN * MAX_PARTS} characters, {PART_LEN} to a frame."
        elif not self.call.get().strip():
            note = "Enter your callsign to send messages."
        else:
            try:
                self.draft_parts = split(self.call.get(), self.to_call.get(), text, 0)
                note = self._draft_note(len(text), len(self.draft_parts))
            except ValueError as e:
                note, error = str(e), True
        self.counter.configure(text=note, foreground=self.c["tx"] if error else "")
        self._show_state()

    def _draft_note(self, characters: int, frames: int) -> str:
        mode = self._mode()
        seconds = math.ceil(frames * frame_length(mode) / RATE)
        doing = "transmitting" if mode == SUB else "talking"
        return (
            f"{_plural(characters, 'character')}, {_plural(frames, 'frame')}, "
            f"about {seconds} s of {doing}"
        )

    def _numbers(self) -> tuple[float, float, float, float | None] | None:
        try:
            level, shape, delay = (
                float(self.level.get()),
                float(self.shape.get()),
                float(self.txdelay.get()),
            )
            pause = None if self.pause.get() == PAUSES[0] else float(self.pause.get())
        except ValueError:
            self.say("Data level, shape and TX delay must be numbers.", error=True)
            return None
        if not 0 <= shape < 1:
            self.say("Shape must be from 0 up to (not including) 1.", error=True)
            return None
        if not 0 <= delay <= 5:
            self.say("TX delay must be between 0 and 5 seconds.", error=True)
            return None
        if pause is not None and pause > level and self._mode() == VOICE:
            self.say(
                f"In pauses ({pause:g} dB) can't be above the data level ({level:g} dB).",
                error=True,
            )
            return None
        return level, shape, delay, pause

    def _index(self, box: ttk.Combobox, devices: list[tuple[int, str]]) -> int | None:
        return next((i for i, n in devices if n == box.get()), None)

    def _mode(self) -> str:
        return next((m for m, name in MODE_NAMES.items() if name == self.mode.get()), VOICE)

    def _open_ptt(self) -> Keyer | None:
        how = next((h for label, h in self.keyers if label == self.ptt_box.get()), None)
        return None if how is None else open_keyer(how)

    def _busy(self) -> bool:
        # keying while the CW ID or the PTT test holds the radio would open a second keyer that
        # the first then unkeys, and a mic check holds the microphone a transmitter would use
        return self.identifying or self.testing_ptt or self.mic_check is not None

    def _show_state(self) -> None:
        c = self.c
        if self.identifying:
            text, color = "Sending CW ID", c["tx"]
        elif self.tx or self.sending:
            text, color = "Transmitting", c["tx"]
        elif self.listener:
            text, color = "Listening", c["rx"]
        else:
            text, color = "Idle", c["muted"]
        self.state.configure(text=text, foreground=color)
        ready = self.message_ok and bool(self.outputs) and not self._busy()
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
        self._show_messages()

    def _show_messages(self) -> None:
        # parts only go out in an over, which needs a valid station and a sound card
        sendable = self.message_ok and bool(self.outputs)
        self.send_msg_button.state(["!disabled"] if sendable and self.draft_parts else ["disabled"])
        again = sendable and bool(self.last_message) and self.last_message not in self.outbox
        self.again_button.state(["!disabled"] if again else ["disabled"])
        going = self.tx.frames.waiting if self.tx else []
        waiting = going or self.unsent or self.playing
        self.cancel_button.state(["!disabled"] if waiting else ["disabled"])
        self.progress.configure(text=self._progress(going))

    def _progress(self, going: list[Part]) -> str:
        if going:
            p = going[0]
            return f"Sending part {p.index + 1} of {p.total} to {p.to or 'everyone'}"
        said = []
        if self.playing:
            said.append(f"Sending {_plural(len(self.playing), 'part')} in the recording")
        if self.unsent:
            n = len(self.unsent)
            said.append(f"{_plural(n, 'part')} wait{'s' if n == 1 else ''} for your next over")
        if said:
            return "; ".join(said)
        if self.sent_to is not None:
            return f"Sent to {self.sent_to or 'everyone'}."
        return ""

    def _typing(self) -> bool:
        focus = self.root.focus_get()
        # the message shown under the list is read-only, so Space there still talks
        if focus is self.detail:
            return False
        return isinstance(focus, (ttk.Entry, tk.Entry, tk.Text, ttk.Checkbutton))

    def _space_down(self, event: tk.Event) -> str | None:
        if self._typing():
            return None
        if self.release_pending:
            # key repeat on some systems sends release-press pairs while the key is held
            self.root.after_cancel(self.release_pending)
            self.release_pending = None
        elif not self.tx and not self.sending and self.message_ok:
            self.toggle_tx()
            self.held = self.tx is not None
        return "break"

    def _space_up(self, event: tk.Event) -> str | None:
        if self._typing() or not self.held:
            return None
        self.release_pending = self.root.after(80, self._release_talk)
        return "break"

    def _release_talk(self) -> None:
        self.release_pending = None
        self.held = False
        self.stop_sending()

    def toggle_tx(self) -> None:
        if self.tx:
            self.stop_sending()
            return
        if self.sending or self._busy():
            return
        message, numbers = self._message(), self._numbers()
        if message is None or numbers is None:
            return
        level, shape, delay, pause = numbers
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
                pause,
                self._mode(),
            )
            self.tx.frames.send(self.unsent)
            self.unsent = []
            self.tx.key()
        except (SetupError, OSError) as e:
            self.say(str(e), error=True)
            self._release()
            return
        self._save_settings()
        how = f"keyed by {self.ptt.name}" if self.ptt else "key the radio yourself"
        self.say(f"Sending {message} under the microphone audio ({how}).")
        self._show_state()

    def toggle_recording(self) -> None:
        if self.sending:
            self._stop_recording()
            return
        if self.tx or self._busy():
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
        level, shape, delay, pause = numbers
        try:
            self.ptt = self._open_ptt()
        except SetupError as e:
            self.say(str(e), error=True)
            return
        out = self._index(self.to_radio, self.outputs)
        ptt = self.ptt
        identify = self._id_due()
        mode = self._mode()
        # the queued parts the recording carries; the rest wait
        fit = len(mix(mode, speech, message, level, shape, pause, self.unsent)[1])
        self.playing, self.unsent = self.unsent[:fit], self.unsent[fit:]
        parts = self.playing

        def work() -> None:
            played = False
            try:
                play(speech, message, level, shape, out, ptt, delay, pause, identify, mode, parts)
                played = True
            except (SetupError, OSError) as e:
                self.events.put(("error", str(e)))
            finally:
                self.events.put(("sent", (played, identify)))

        self.sending = True
        self.recording_cut = False
        self.helper = threading.Thread(target=work, daemon=True)
        self.helper.start()
        self.say(
            f"Sending {path} ({len(speech) / RATE:.0f} s) with {message}"
            f"{_recording_parts(len(parts), len(self.unsent))}."
        )
        self._show_state()

    def _stop_recording(self) -> None:
        self.recording_cut = True
        stop_playing()

    def _id_due(self) -> bool:
        return self.cw_id.get() and due(self.last_id)

    def stop_sending(self) -> None:
        self.held = False
        if self.tx:
            self.last_over = time.monotonic()
            if self._id_due():
                tx, ptt = self.tx, self.ptt
                self.tx, self.ptt = None, None
                self.identifying = True
                self.say("Sending your callsign in Morse before unkeying.")
                self.helper = threading.Thread(target=self._identify, args=(tx, ptt), daemon=True)
                self.helper.start()
                # with the ID and unkeying under way, so a log write that hangs or fails
                # can't hold them up
                self._take_back(tx)
            elif self._release():
                self.say("Stopped transmitting.")
        if self.sending:
            self._stop_recording()
        self._show_state()

    def _identify(self, tx: Transmitter, ptt: Keyer | None) -> None:
        sent, error = False, None
        try:
            sent = _close(tx, ptt, identify=True)
        except (SetupError, OSError) as e:
            error = str(e)
        finally:
            self.events.put(("identified", (sent, error)))

    def _remind_id(self) -> None:
        if self.identifying:
            return
        now = time.monotonic()
        on_air = self.tx is not None or self.sending
        recent = self.last_over is not None and now - self.last_over < ID_EVERY
        # 97.119: with CW ID on, an ID goes out as each over that needs one ends, so only an
        # over running past the time needs a nudge; without it, the whole QSO does
        if not on_air and (self.cw_id.get() or not recent):
            return
        if self.reminded is None:
            # reminders count from the last ID, or from when the station is first in use
            self.reminded = self.last_id or now
        if due(max(self.reminded, self.last_id or 0.0)):
            self.reminded = now
            stop = ", or stop to send the CW ID" if self.cw_id.get() else ""
            self.say(f"Time to identify: say your callsign{stop}.", error=True)

    def _release(self) -> bool:
        """Close the transmitter and PTT now, then keep the parts the over didn't send. False,
        with the reason shown, if unkeying failed."""
        tx, ptt = self.tx, self.ptt
        self.tx, self.ptt = None, None
        try:
            _close(tx, ptt)
        except (SetupError, OSError) as e:
            self.say(f"Could not unkey the radio: {e}", error=True)
            return False
        finally:
            # after unkeying, so a log write that hangs or fails can't hold the radio keyed
            if tx:
                self._take_back(tx)
        return True

    def _take_back(self, tx: Transmitter) -> None:
        """Keep the parts this over didn't send for the next one."""
        self.unsent = tx.frames.cancel() + self.unsent
        self._check_sent(tx.frames)

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

    def _listen_on_open(self) -> None:
        if not self.listener:
            self.toggle_rx()

    def _mode_changed(self) -> None:
        self._check_draft()
        if self.listener:
            # a listener decodes one mode only, so listen again in the new one
            self.listener.stop()
            self.listener = None
            self.toggle_rx()

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
                numbers[1],
                self._index(self.from_radio, self.inputs),
                self.record_path,
                self._mode(),
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

    def _received(self, h: Heard) -> None:
        m = h.frame.message
        if isinstance(m, Part):
            self._received_part(m, datetime.fromtimestamp(h.when, UTC))
        else:
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
            self._new_on("Received")
        self.empty.grid_remove()

    def _received_part(self, part: Part, when: datetime) -> None:
        m = self.inbox.add(part, when)
        if m is None:
            return
        row = f"m{id(m)}"
        values = (f"{m.first:%H:%M}", m.sender, m.to or "ALL", m.text)
        mine = bool(m.to) and m.to == self.call.get().strip().upper()
        if self.message_list.exists(row):
            self.message_list.item(row, values=values)
        else:
            self.message_list.insert("", 0, iid=row, values=values, tags=["mine"] if mine else [])
        self.details[row] = (_about(m.first, m.sender, m.to, m.gaps), m.text)
        self._show_detail()
        self._new_on("Messages")
        if mine:
            self.say(f"Message from {m.sender}.")
        if m.complete:
            m.logged = self._log(heard_line(m))

    def send_message(self) -> None:
        text = self.draft.get("1.0", "end")
        try:
            parts = split(self.call.get(), self.to_call.get(), text, self.next_number)
        except ValueError as e:
            self.say(str(e), error=True)
            return
        # counted, and saved straight away, so messages close together never share a number
        self.next_number = (self.next_number + 1) % 64
        self._save_settings()
        self.draft.delete("1.0", "end")
        self._queue(parts)

    def send_again(self) -> None:
        # the same number, so a station that missed a part fills the gap
        self._queue(self.last_message)

    def _queue(self, parts: list[Part]) -> None:
        self.last_message = parts
        self.outbox.append(parts)
        self.sent_to = None
        if self.tx:
            self.tx.frames.send(parts)
        else:
            self.unsent += parts
        self._show_state()

    def cancel_message(self) -> None:
        # parts already mixed into a playing recording go out with it, but no longer count
        dropped = len(self.unsent) + len(self.playing)
        self.unsent, self.playing = [], []
        if self.tx:
            dropped += len(self.tx.frames.cancel())
            # a message whose last part went out just now has still gone
            self._check_sent(self.tx.frames)
        self.outbox = []
        self.say(f"Cancelled {_plural(dropped, 'message part')} that had not gone out.")
        self._show_state()

    def _check_sent(self, frames: Frames) -> None:
        while not frames.sent.empty():
            self._part_sent(frames.sent.get())

    def _part_sent(self, part: Part) -> None:
        # parts go out in the order they were queued, so the oldest message finishes first
        if self.outbox and part == self.outbox[0][-1]:
            self._sent(self.outbox.pop(0))
        self._show_state()

    def _played(self, whole: bool) -> None:
        """The parts in a recording have gone once all of it has played; otherwise they go
        again in the next over."""
        parts, self.playing = self.playing, []
        if not whole:
            self.unsent = parts + self.unsent
            return
        for part in parts:
            self._part_sent(part)

    def _sent(self, parts: list[Part]) -> None:
        when = datetime.now(UTC)
        sender, to = parts[0].sender, parts[0].to
        text = "".join(p.text for p in parts)
        row = self.message_list.insert("", 0, values=(f"{when:%H:%M}", sender, to or "ALL", text))
        self.details[row] = (_about(when, sender, to), text)
        self._show_detail()
        self.sent_to = to
        self._log(line(when, sender, to, text))

    def _log(self, text: str) -> bool:
        """Add a line to the messages file when saving is on. True if it was written."""
        if not self.save_messages.get():
            return False
        try:
            append(self.messages_path, text)
        except OSError as e:
            self.say(f"Can't save messages to {self.messages_path}: {e.strerror or e}", error=True)
            return False
        self._show_open()
        return True

    def _show_open(self) -> None:
        found = sys.platform == "win32" and os.path.isfile(self.messages_path)
        self.open_button.state(["!disabled"] if found else ["disabled"])

    def choose_messages_file(self) -> None:
        current = Path(self.messages_path)
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save messages to",
            initialdir=str(current.parent),
            initialfile=current.name,
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt")],
            # messages are added to the end of the file, so an existing one is not replaced
            confirmoverwrite=False,
        )
        if not path:
            return
        self.messages_path = path
        self.messages_file.configure(text=path)
        self.save_messages.set(True)
        self._show_open()
        self.say(f"Messages will be saved to {path}.")

    def open_messages_file(self) -> None:
        if sys.platform == "win32":
            try:
                os.startfile(self.messages_path)
            except OSError as e:
                self.say(f"Can't open {self.messages_path}: {e.strerror or e}", error=True)

    def _poll(self) -> None:
        if self.listener:
            # read first: frames queued just before the input stopped are still to be shown
            running = self.listener.running
            while not self.listener.heard.empty():
                self._received(self.listener.heard.get())
            while not self.listener.notes.empty():
                self.say(self.listener.notes.get(), error=True)
            self._draw_meter(self.mic_check.peak if self.mic_check else self.listener.peak)
            if not running:
                self.listener.stop()
                self.listener = None
                self._show_state()
        else:
            self._draw_meter(self.mic_check.peak if self.mic_check else None)
        self._watch_mic_check()
        if self.tx:
            self._check_sent(self.tx.frames)
            while not self.tx.sender.problems.empty():
                self.say(f"Sound card: {self.tx.sender.problems.get()}", error=True)
            if not self.tx.active:
                self.stop_sending()
                self.say("The sound card stopped while transmitting.", error=True)
        while not self.events.empty():
            self._event(*self.events.get())
        if self._update_due():
            self.check_updates()
        self._remind_id()
        self.root.after(100, self._poll)

    def _event(self, kind: str, detail: Any) -> None:
        if kind == "error":
            self.say(str(detail), error=True)
        elif kind == "sent":
            played, identified = detail
            self.sending = False
            self.last_over = time.monotonic()
            whole = played and not self.recording_cut
            if whole and identified:
                self.last_id = self.last_over
            self._release()
            self._played(whole)
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
        elif kind == "mic-checked":
            self._show_mic_check(detail)
        elif kind == "ptt-tested":
            self.testing_ptt = False
            self.ptt_button.state(["!disabled"])
            self.say(detail, error=detail.startswith("PTT test failed"))
            self._show_state()
        elif kind == "identified":
            sent, error = detail
            self.identifying = False
            if error:
                self.say(f"Could not unkey the radio: {error}", error=True)
            elif sent:
                self.last_id = time.monotonic()
                self.say("Sent your callsign in Morse and unkeyed.")
            self._show_state()

    def test_microphone(self) -> None:
        if self.mic_check:
            return
        if self._message() is None:
            self.say("Enter your callsign first: the test mixes your data into the recording.")
            return
        try:
            self.mic_check = MicCheck(self._index(self.mic, self.inputs))
        except SetupError as e:
            self.say(str(e), error=True)
            return
        self.mic_button.state(["disabled"])
        self.say("Recording: talk normally until the countdown ends.")
        self._show_state()

    def _mic_ready(self) -> None:
        self.mic_button.configure(text="Test microphone")
        self.mic_button.state(["!disabled"])

    def _watch_mic_check(self) -> None:
        check = self.mic_check
        if check is None:
            return
        if not check.done:
            self.mic_button.configure(text=f"Recording {check.left:.0f} s")
            return
        self.mic_check = None
        self._show_state()
        self.mic_button.configure(text="Checking...")
        try:
            audio = check.audio()
        except SetupError as e:
            self._mic_ready()
            self.say(str(e), error=True)
            return
        message, numbers = self._message(), self._numbers()
        if message is None or numbers is None:
            self._mic_ready()
            return
        level, shape, _, pause = numbers
        mode = self._mode()

        def work() -> None:
            self.events.put(("mic-checked", finish(audio, message, level, shape, pause, mode)))

        threading.Thread(target=work, daemon=True).start()

    def _show_mic_check(self, result: MicResult) -> None:
        self._mic_ready()
        decoded = (
            "the data decoded"
            if result.frames
            else "the data did not decode (talk through the whole countdown)"
        )
        self.say(
            f"Mic peaks {result.peak_db:.0f} dBFS, speech {result.speech_db:.0f} dBFS. "
            f"{result.advice} Playing back what would be sent; {decoded}.",
            error=not result.frames or "good" not in result.advice,
        )
        try:
            play_back(result.mixed)
        except SetupError as e:
            self.say(f"Can't play it back: {e}", error=True)

    def test_ptt(self) -> None:
        if self.tx or self.sending or self._busy():
            return
        try:
            keyer = self._open_ptt()
        except SetupError as e:
            self.say(str(e), error=True)
            return
        if keyer is None:
            self.say("Pick how undervoice keys the radio under PTT first.", error=True)
            return
        self.testing_ptt = True
        self.ptt_button.state(["disabled"])
        self.say(f"Keying the radio for 2 s through {keyer.name}.")
        self._show_state()

        def work() -> None:
            outcome = "PTT test failed"
            try:
                # leaving the with block unkeys again and lets the interface go
                with keyer:
                    key_briefly(keyer)
                outcome = f"PTT works through {keyer.name}: the radio should have transmitted."
            except (SetupError, OSError) as e:
                outcome = f"PTT test failed: {e}"
            finally:
                self.events.put(("ptt-tested", outcome))

        self.helper = threading.Thread(target=work, daemon=True)
        self.helper.start()

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

    def close_window(self) -> None:
        """The window's close button: asks first when message parts have not gone out."""
        going = self.tx.frames.waiting if self.tx else []
        n = len(going) + len(self.unsent) + len(self.playing)
        if n and not messagebox.askyesno(
            "Close undervoice",
            f"{_plural(n, 'message part')} {'has' if n == 1 else 'have'} not gone out and "
            "will be lost. Close anyway?",
            icon=messagebox.WARNING,
            parent=self.root,
        ):
            return
        self.close()

    def close(self) -> None:
        self._save_settings()
        self.stop_sending()
        if self.helper:
            # it holds PTT until the CW ID, PTT test or recording is over, and quitting first
            # could leave the radio keyed; the longest callsign takes 13.5 s in Morse
            self.root.update_idletasks()
            self.helper.join(timeout=15)
        if self.listener:
            self.listener.stop()
        self._release()
        # those missing parts, and any whose write failed earlier
        for m in self.inbox.messages:
            if not m.logged:
                m.logged = self._log(heard_line(m))
        self.root.destroy()


def run() -> None:
    root = tk.Tk()
    App(root)
    # a 1366x768 laptop has about 720 px above its taskbar, too little for the window where
    # Windows first puts it
    if root.winfo_screenheight() < 850:
        if root.tk.call("tk", "windowingsystem") == "x11":
            root.attributes("-zoomed", True)
        else:
            root.state("zoomed")
    root.mainloop()
