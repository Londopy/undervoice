"""The undervoice window: station details, sound cards and PTT, transmit and a receive log."""

from __future__ import annotations

import math
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font, ttk

from ._audio import read_wav
from ._live import Listener, Transmitter, cards, play, stop_playing
from ._message import Message
from ._modulator import LEVEL_DB, SHAPE
from ._ptt import Ptt, SetupError, describe, find
from ._waveform import RATE

LIGHT = {
    "bg": "#eef1f4",
    "panel": "#ffffff",
    "ink": "#17202a",
    "muted": "#5a6675",
    "line": "#d3d9e0",
    "accent": "#0b6470",
    "accent_ink": "#ffffff",
    "tx": "#b3361b",
    "rx": "#2f6b3a",
    "error": "#a3301a",
}
DARK = {
    "bg": "#13181e",
    "panel": "#1b222a",
    "ink": "#e3e8ed",
    "muted": "#98a4b1",
    "line": "#2d3640",
    "accent": "#4fb6c0",
    "accent_ink": "#0c1a1d",
    "tx": "#f0876a",
    "rx": "#86c991",
    "error": "#f0876a",
}
NO_PTT = "None, I key the radio myself"


def _dark_mode() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def _family(*names: str) -> str:
    available = set(font.families())
    return next((n for n in names if n in available), names[-1])


def _icon(c: dict[str, str]) -> tk.PhotoImage:
    # a speech waveform with a thin steady data line under it
    img = tk.PhotoImage(width=32, height=32)
    img.put(c["accent"], to=(0, 0, 32, 32))
    for x in range(4, 28, 3):
        h = int(4 + 9 * abs(math.sin(x / 4.0)))
        img.put(c["accent_ink"], to=(x, 15 - h, x + 2, 15 + h))
    img.put(c["accent_ink"], to=(4, 27, 28, 28))
    return img


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.c = DARK if _dark_mode() else LIGHT
        self.listener: Listener | None = None
        self.tx: Transmitter | None = None
        self.ptt: Ptt | None = None
        self.sending = False
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.record_path: str | None = None

        root.title("undervoice")
        self._icon = _icon(self.c)
        root.iconphoto(True, self._icon)
        root.configure(background=self.c["bg"])
        root.minsize(760, 720)
        root.geometry("900x800")
        self._style()

        self.inputs: list[tuple[int, str]] = []
        self.outputs: list[tuple[int, str]] = []
        self.ptts: list[str] = [NO_PTT]
        setup_problem = self._load_devices()

        outer = ttk.Frame(root, padding=(20, 16))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        self._header(outer).grid(row=0, column=0, sticky="ew")
        self._station(outer).grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self._radio(outer).grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._receive(outer).grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        self.status = ttk.Label(outer, style="Muted.TLabel", anchor="w")
        self.status.grid(row=4, column=0, sticky="ew", pady=(10, 0))

        root.bind("<Escape>", lambda e: self.stop_sending())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._check_message()
        if setup_problem:
            self.say(setup_problem, error=True)
        else:
            self.say("Fill in your station, pick the sound cards, then transmit or listen.")
        root.after(100, self._poll)

    def _style(self) -> None:
        c = self.c
        body = _family("Segoe UI", "Helvetica Neue", "DejaVu Sans", "TkDefaultFont")
        self.heading = _family("Bahnschrift", "Segoe UI Semibold", "DejaVu Sans", "TkDefaultFont")
        self.mono = _family("Consolas", "Menlo", "DejaVu Sans Mono", "TkFixedFont")
        s = ttk.Style(self.root)
        s.theme_use("clam")
        # clam draws every edge with these three colors; left alone they stay light gray,
        # which glares in the dark palette
        s.configure(
            ".",
            background=c["bg"],
            foreground=c["ink"],
            font=(body, 10),
            bordercolor=c["line"],
            lightcolor=c["panel"],
            darkcolor=c["panel"],
            troughcolor=c["bg"],
        )
        s.configure("TFrame", background=c["bg"])
        s.configure("Panel.TFrame", background=c["panel"], relief="solid", borderwidth=1)
        s.configure("TLabel", background=c["bg"], foreground=c["ink"])
        s.configure("Panel.TLabel", background=c["panel"])
        s.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
        s.configure("PanelMuted.TLabel", background=c["panel"], foreground=c["muted"])
        s.configure("Error.TLabel", background=c["panel"], foreground=c["error"])
        s.configure("Title.TLabel", font=(self.heading, 20), foreground=c["ink"])
        s.configure("Section.TLabel", background=c["panel"], font=(self.heading, 12))
        s.configure(
            "State.TLabel", font=(self.heading, 10), padding=(10, 4), foreground=c["accent_ink"]
        )
        s.configure("TEntry", fieldbackground=c["panel"], foreground=c["ink"], insertcolor=c["ink"])
        s.configure(
            "TCombobox",
            fieldbackground=c["panel"],
            foreground=c["ink"],
            arrowcolor=c["ink"],
            background=c["panel"],
        )
        s.map("TCombobox", fieldbackground=[("readonly", c["panel"])])
        s.configure(
            "TSpinbox", fieldbackground=c["panel"], foreground=c["ink"], arrowcolor=c["ink"]
        )
        s.configure(
            "TCheckbutton",
            background=c["panel"],
            foreground=c["ink"],
            indicatorbackground=c["panel"],
            indicatorforeground=c["ink"],
        )
        s.configure(
            "Vertical.TScrollbar", background=c["panel"], arrowcolor=c["muted"], gripcount=0
        )
        s.map("TCheckbutton", background=[("active", c["panel"])])
        s.configure(
            "TButton",
            background=c["panel"],
            foreground=c["ink"],
            bordercolor=c["line"],
            padding=(12, 6),
        )
        s.map("TButton", background=[("active", c["line"])])
        s.configure(
            "Accent.TButton",
            background=c["accent"],
            foreground=c["accent_ink"],
            font=(self.heading, 12),
            padding=(18, 10),
        )
        s.map(
            "Accent.TButton",
            background=[("disabled", c["line"]), ("active", c["accent"])],
            foreground=[("disabled", c["muted"])],
        )
        s.configure(
            "Treeview",
            background=c["panel"],
            fieldbackground=c["panel"],
            foreground=c["ink"],
            font=(self.mono, 10),
            rowheight=24,
            bordercolor=c["line"],
        )
        s.configure("Treeview.Heading", background=c["bg"], foreground=c["muted"], font=(body, 9))
        s.map(
            "Treeview",
            background=[("selected", c["accent"])],
            foreground=[("selected", c["accent_ink"])],
        )
        self.root.option_add("*TCombobox*Listbox.background", c["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["ink"])

    def _load_devices(self) -> str | None:
        try:
            self.inputs, self.outputs = cards()
        except SetupError as e:
            return str(e)
        try:
            self.ptts = [NO_PTT, *(describe(d) for d in find())]
        except SetupError:
            self.ptts = [f"{NO_PTT} (install hidapi to key it from here)"]
        return None

    def _header(self, parent: ttk.Frame) -> ttk.Frame:
        f = ttk.Frame(parent)
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text="undervoice", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            f,
            text="Callsign, grid and status hidden under your FM voice",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w")
        self.state = ttk.Label(f, style="State.TLabel")
        self.state.grid(row=0, column=1, rowspan=2, sticky="e")
        return f

    def _panel(self, parent: ttk.Frame, title: str) -> ttk.Frame:
        f = ttk.Frame(parent, style="Panel.TFrame", padding=(16, 12))
        ttk.Label(f, text=title, style="Section.TLabel").grid(
            row=0, column=0, columnspan=8, sticky="w"
        )
        return f

    def _labeled(self, parent: ttk.Frame, text: str, widget: tk.Widget, row: int, col: int) -> None:
        ttk.Label(parent, text=text, style="PanelMuted.TLabel").grid(
            row=row, column=col, sticky="w", pady=(8, 2), padx=(0, 12)
        )
        widget.grid(row=row + 1, column=col, sticky="ew", padx=(0, 12))

    def _station(self, parent: ttk.Frame) -> ttk.Frame:
        f = self._panel(parent, "Station")
        f.columnconfigure(2, weight=1)
        self.call = tk.StringVar()
        self.grid_sq = tk.StringVar()
        self.text = tk.StringVar()
        for var in (self.call, self.grid_sq, self.text):
            var.trace_add("write", lambda *a: self._check_message())
        self._labeled(f, "Callsign", ttk.Entry(f, textvariable=self.call, width=12), 1, 0)
        self._labeled(f, "Grid square", ttk.Entry(f, textvariable=self.grid_sq, width=9), 1, 1)
        self._labeled(f, "Status (up to 32 characters)", ttk.Entry(f, textvariable=self.text), 1, 2)
        self.message_error = ttk.Label(f, style="Error.TLabel")
        self.message_error.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        return f

    def _combo(self, parent: ttk.Frame, names: list[str], default: str | None) -> ttk.Combobox:
        box = ttk.Combobox(parent, values=names, state="readonly", width=34)
        if names:
            box.set(default if default in names else names[0])
        return box

    def _radio(self, parent: ttk.Frame) -> ttk.Frame:
        f = self._panel(parent, "Radio")
        for col in (0, 1):
            f.columnconfigure(col, weight=1)
        in_names = [n for _, n in self.inputs]
        out_names = [n for _, n in self.outputs]
        self.mic = self._combo(f, in_names, None)
        self.to_radio = self._combo(f, out_names, next((n for n in out_names if "USB" in n), None))
        self.from_radio = self._combo(f, in_names, next((n for n in in_names if "USB" in n), None))
        self.ptt_box = self._combo(f, self.ptts, NO_PTT)
        self._labeled(f, "Your microphone", self.mic, 1, 0)
        self._labeled(f, "Audio to the radio", self.to_radio, 1, 1)
        self._labeled(f, "Audio from the radio", self.from_radio, 3, 0)
        self._labeled(f, "PTT", self.ptt_box, 3, 1)

        tuning = ttk.Frame(f, style="Panel.TFrame", borderwidth=0)
        tuning.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.level = tk.StringVar(value=f"{LEVEL_DB:g}")
        self.shape = tk.StringVar(value=f"{SHAPE:g}")
        self.txdelay = tk.StringVar(value="0.3")
        self._labeled(
            tuning,
            "Data level, dB under speech",
            ttk.Spinbox(tuning, from_=-36, to=-6, increment=1, textvariable=self.level, width=8),
            0,
            0,
        )
        self._labeled(
            tuning,
            "Shape (match the other station)",
            ttk.Spinbox(tuning, from_=0, to=0.9, increment=0.1, textvariable=self.shape, width=8),
            0,
            1,
        )
        self._labeled(
            tuning,
            "TX delay, s",
            ttk.Spinbox(tuning, from_=0, to=2, increment=0.1, textvariable=self.txdelay, width=8),
            0,
            2,
        )

        actions = ttk.Frame(f, style="Panel.TFrame", borderwidth=0)
        actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.tx_button = ttk.Button(
            actions, text="Transmit", style="Accent.TButton", command=self.toggle_tx
        )
        self.tx_button.grid(row=0, column=0, padx=(0, 10))
        self.send_button = ttk.Button(
            actions, text="Send a recording...", command=self.toggle_recording
        )
        self.send_button.grid(row=0, column=1)
        ttk.Label(
            actions,
            text="Esc stops transmitting",
            style="PanelMuted.TLabel",
        ).grid(row=0, column=2, padx=(14, 0))
        return f

    def _receive(self, parent: ttk.Frame) -> ttk.Frame:
        f = self._panel(parent, "Heard")
        f.columnconfigure(2, weight=1)
        f.rowconfigure(2, weight=1)
        self.rx_button = ttk.Button(f, text="Start listening", command=self.toggle_rx)
        self.rx_button.grid(row=1, column=0, sticky="w", pady=(8, 8))
        self.record = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            f, text="Record to a file", variable=self.record, command=self._choose_record
        ).grid(row=1, column=1, sticky="w", padx=(14, 0))
        meter = ttk.Frame(f, style="Panel.TFrame", borderwidth=0)
        meter.grid(row=1, column=2, sticky="e")
        ttk.Label(meter, text="Input", style="PanelMuted.TLabel").grid(row=0, column=0, padx=(0, 8))
        self.meter = tk.Canvas(
            meter, width=180, height=10, background=self.c["line"], highlightthickness=0
        )
        self.meter.grid(row=0, column=1)
        self.meter_text = ttk.Label(meter, text="-- dBFS", style="PanelMuted.TLabel", width=10)
        self.meter_text.grid(row=0, column=2, padx=(8, 0))

        columns = ("time", "call", "grid", "status", "snr")
        self.log = ttk.Treeview(f, columns=columns, show="headings", height=8)
        for col, label, width, stretch in (
            ("time", "UTC", 90, False),
            ("call", "Callsign", 110, False),
            ("grid", "Grid", 80, False),
            ("status", "Status", 300, True),
            ("snr", "SNR", 70, False),
        ):
            self.log.heading(col, text=label, anchor="w")
            self.log.column(col, width=width, stretch=stretch, anchor="w")
        self.log.grid(row=2, column=0, columnspan=3, sticky="nsew")
        scroll = ttk.Scrollbar(f, orient="vertical", command=self.log.yview)
        scroll.grid(row=2, column=3, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self.empty = ttk.Label(
            f,
            text="Frames you hear show up here, newest first. Pick the radio's audio and start listening.",
            style="PanelMuted.TLabel",
        )
        self.empty.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        return f

    def say(self, text: str, error: bool = False) -> None:
        self.status.configure(text=text, foreground=self.c["error"] if error else self.c["muted"])

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

    def _open_ptt(self) -> Ptt | None:
        choice = self.ptts.index(self.ptt_box.get()) if self.ptt_box.get() in self.ptts else 0
        return None if choice == 0 else Ptt(choice - 1)

    def _show_state(self) -> None:
        c = self.c
        if self.tx or self.sending:
            text, color = "TRANSMITTING", c["tx"]
        elif self.listener:
            text, color = "LISTENING", c["rx"]
        else:
            text, color = "IDLE", c["muted"]
        self.state.configure(text=text, background=color)
        ready = self.message_ok and bool(self.outputs)
        self.tx_button.configure(text="Stop transmitting" if self.tx else "Transmit")
        self.tx_button.state(
            ["!disabled"] if (ready and not self.sending) or self.tx else ["disabled"]
        )
        self.send_button.configure(text="Stop sending" if self.sending else "Send a recording...")
        self.send_button.state(
            ["!disabled"] if (ready and not self.tx) or self.sending else ["disabled"]
        )
        self.rx_button.configure(text="Stop listening" if self.listener else "Start listening")
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
        how = f"keyed through {self.ptt.name}" if self.ptt else "key the radio now"
        self.say(f"Transmitting {message}: speak into the microphone ({how}).")
        self._show_state()

    def toggle_recording(self) -> None:
        if self.sending:
            stop_playing()
            return
        message, numbers = self._message(), self._numbers()
        if message is None or numbers is None:
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Send a recording", filetypes=[("WAV audio", "*.wav")]
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
            kind, detail = self.events.get()
            if kind == "error":
                self.say(detail, error=True)
            else:
                self.sending = False
                self._release_ptt()
                self._show_state()
        self.root.after(100, self._poll)

    def _draw_meter(self, peak: float | None) -> None:
        self.meter.delete("all")
        if peak is None:
            self.meter_text.configure(text="-- dBFS")
            return
        db = 20 * math.log10(max(peak, 1e-6))
        width = int(180 * min(max((db + 60) / 60, 0), 1))
        color = self.c["tx"] if peak > 0.98 else self.c["accent"]
        self.meter.create_rectangle(0, 0, width, 10, fill=color, width=0)
        self.meter_text.configure(text=f"{db:.0f} dBFS")

    def close(self) -> None:
        self.stop_sending()
        if self.listener:
            self.listener.stop()
        self._release_ptt()
        self.root.destroy()


def run() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()
