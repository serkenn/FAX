"""Tkinter GUI for the RTL-SDR WEFAX decoder."""

from __future__ import annotations

import datetime
import pathlib
import queue
from typing import Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from fax_decoder import (
    WEFAXDecoder, State,
    PIXELS_PER_LINE, IMAGE_HEIGHT, LPM_120, LPM_90,
)
from rtlsdr_source import RTLSDRSource, RTLSDR_AVAILABLE, AUDIO_RATE

# ── Common WEFAX broadcast frequencies (kHz) ─────────────────────────────────
PRESETS: dict[str, float] = {
    "JMH Kyoto  3.6 MHz (Japan)":   3622.5,
    "JMH Kyoto  7.8 MHz (Japan)":   7795.0,
    "JMH Kyoto 13.9 MHz (Japan)":  13988.0,
    "CFH Halifax 4.3 MHz (Canada)": 4271.0,
    "CFH Halifax 6.5 MHz (Canada)": 6496.4,
    "NMF Boston  4.6 MHz (USA)":    4610.0,
    "NMF Boston  8.1 MHz (USA)":    8110.0,
    "NMC Point Reyes 4.3 (USA)":    4346.0,
    "DDH Hamburg 3.9 MHz (Germany)": 3855.0,
    "DDH Hamburg 7.9 MHz (Germany)": 7880.0,
    "FUG La Régine 4.6 MHz (France)": 4610.0,
    "BMF Taiwan 4.6 MHz":           4616.0,
}

POLL_MS = 50   # GUI queue poll interval


class WEFAXApp(tk.Tk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("WEFAX Decoder — RTL-SDR")
        self.minsize(800, 500)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._source:  Optional[RTLSDRSource] = None
        self._decoder: Optional[WEFAXDecoder] = None
        self._running  = False
        self._gui_q: queue.Queue = queue.Queue()

        self._fax_img: Optional[Image.Image] = (
            Image.new("L", (PIXELS_PER_LINE, IMAGE_HEIGHT), 0)
            if PIL_AVAILABLE else None
        )

        self._build_ui()
        self._poll_queue()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        ctrl = ttk.Frame(self, padding=8)
        ctrl.grid(row=0, column=0, sticky="nsew")
        self._build_controls(ctrl)

        img_frame = ttk.Frame(self, relief="sunken", borderwidth=1)
        img_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 4), pady=4)
        self._build_canvas(img_frame)

        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(
            self, textvariable=self._status_var,
            anchor="w", relief="sunken", padding=(4, 1),
        ).grid(row=1, column=0, columnspan=2, sticky="ew")

    def _build_controls(self, p: ttk.Frame) -> None:
        r = 0

        def sep(text: str) -> None:
            nonlocal r
            ttk.Separator(p, orient="horizontal").grid(
                row=r, column=0, columnspan=2, sticky="ew", pady=(6, 2)
            )
            r += 1
            ttk.Label(p, text=text, font=("", 9, "bold")).grid(
                row=r, column=0, columnspan=2, sticky="w"
            )
            r += 1

        def row(label: str, w: tk.Widget) -> None:
            nonlocal r
            ttk.Label(p, text=label).grid(row=r, column=0, sticky="w", padx=(2, 4))
            w.grid(row=r, column=1, sticky="ew", pady=2)
            r += 1

        p.columnconfigure(1, weight=1)

        # ── RTL-SDR ──
        sep("RTL-SDR")
        self._dev_var = tk.IntVar(value=0)
        row("Device:", ttk.Spinbox(p, from_=0, to=9,
                                   textvariable=self._dev_var, width=4))

        self._direct_var = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(p, text="Direct sampling – Q branch (HF)",
                              variable=self._direct_var)
        cb.grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        r += 1

        self._gain_var = tk.StringVar(value="auto")
        row("Gain:", ttk.Entry(p, textvariable=self._gain_var, width=7))

        # ── Frequency ──
        sep("Frequency")
        self._preset_var = tk.StringVar()
        preset_cb = ttk.Combobox(
            p, textvariable=self._preset_var,
            values=list(PRESETS.keys()), state="readonly",
        )
        preset_cb.grid(row=r, column=0, columnspan=2, sticky="ew", pady=2)
        preset_cb.bind("<<ComboboxSelected>>", self._on_preset)
        r += 1

        freq_frame = ttk.Frame(p)
        freq_frame.grid(row=r, column=0, columnspan=2, sticky="ew")
        r += 1
        ttk.Label(freq_frame, text="kHz:").pack(side="left")
        self._freq_var = tk.StringVar(value="8140")
        ttk.Entry(freq_frame, textvariable=self._freq_var, width=10).pack(
            side="left", padx=(4, 0)
        )

        # ── FAX ──
        sep("FAX")
        self._lpm_var = tk.StringVar(value="120")
        row("LPM:", ttk.Combobox(
            p, textvariable=self._lpm_var,
            values=["120", "90"], state="readonly", width=6,
        ))

        # ── Controls ──
        sep("Controls")
        self._btn_start = ttk.Button(p, text="▶  Start", command=self._on_start)
        self._btn_start.grid(row=r, column=0, padx=2, pady=3, sticky="ew")
        self._btn_stop = ttk.Button(
            p, text="■  Stop", command=self._on_stop, state="disabled"
        )
        self._btn_stop.grid(row=r, column=1, padx=2, pady=3, sticky="ew")
        r += 1
        ttk.Button(p, text="Save…", command=self._on_save).grid(
            row=r, column=0, padx=2, pady=2, sticky="ew"
        )
        ttk.Button(p, text="Clear", command=self._on_clear).grid(
            row=r, column=1, padx=2, pady=2, sticky="ew"
        )
        r += 1

        # ── Status ──
        sep("Status")
        self._state_var = tk.StringVar(value="IDLE")
        row("State:", ttk.Label(p, textvariable=self._state_var, width=14))
        self._lines_var = tk.StringVar(value="0")
        row("Lines:", ttk.Label(p, textvariable=self._lines_var))

    def _build_canvas(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(parent, bg="#111", cursor="crosshair")
        self._canvas.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(parent, orient="vertical",   command=self._canvas.yview)
        hsb = ttk.Scrollbar(parent, orient="horizontal", command=self._canvas.xview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._canvas.configure(
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
            scrollregion=(0, 0, PIXELS_PER_LINE, 1),
        )
        self._tk_img: Optional[ImageTk.PhotoImage] = None
        self._canvas_item: Optional[int] = None

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_preset(self, _e: object = None) -> None:
        name = self._preset_var.get()
        if name in PRESETS:
            self._freq_var.set(str(PRESETS[name]))

    def _on_start(self) -> None:
        if self._running:
            return
        if not RTLSDR_AVAILABLE:
            messagebox.showerror(
                "RTL-SDR not found",
                "pyrtlsdr is not installed.\n\nRun:\n  pip install pyrtlsdr",
            )
            return
        if not PIL_AVAILABLE:
            messagebox.showerror(
                "Pillow not found",
                "Pillow is required for image display.\n\nRun:\n  pip install Pillow",
            )
            return

        try:
            freq_khz = float(self._freq_var.get())
            if freq_khz <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid frequency", "Enter a positive number in kHz.")
            return

        gain_str = self._gain_var.get().strip().lower()
        gain: str | float = "auto"
        if gain_str != "auto":
            try:
                gain = float(gain_str)
            except ValueError:
                messagebox.showerror("Invalid gain", "Enter a number or 'auto'.")
                return

        lpm = LPM_90 if self._lpm_var.get() == "90" else LPM_120

        self._decoder = WEFAXDecoder(
            sample_rate = AUDIO_RATE,
            lpm         = lpm,
            on_line     = self._cb_line,
            on_status   = self._cb_status,
            on_complete = self._cb_complete,
        )

        try:
            self._source = RTLSDRSource(
                center_freq_hz  = freq_khz * 1_000.0,
                device_index    = self._dev_var.get(),
                gain            = gain,
                direct_sampling = self._direct_var.get(),
            )
        except RuntimeError as exc:
            messagebox.showerror("RTL-SDR error", str(exc))
            self._decoder = None
            return

        self._source.set_callback(self._audio_cb)

        try:
            self._source.start()
        except Exception as exc:
            messagebox.showerror("RTL-SDR start error", str(exc))
            self._source  = None
            self._decoder = None
            return

        self._running = True
        self._btn_start.state(["disabled"])
        self._btn_stop.state(["!disabled"])
        direct_label = " (direct sampling)" if self._direct_var.get() else ""
        self._set_status(
            f"Receiving {freq_khz:.1f} kHz{direct_label} – waiting for 300 Hz start tone…"
        )

    def _on_stop(self) -> None:
        if not self._running:
            return
        src = self._source
        if src:
            src.stop()
        self._source  = None
        self._decoder = None
        self._running = False
        self._btn_start.state(["!disabled"])
        self._btn_stop.state(["disabled"])
        self._set_status("Stopped")

    def _on_save(self) -> None:
        if self._fax_img is None:
            messagebox.showinfo("No image", "No image data to save.")
            return
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            initialfile=f"wefax_{ts}.png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("All files", "*.*")],
        )
        if path:
            self._fax_img.save(path)
            self._set_status(f"Saved: {pathlib.Path(path).name}")

    def _on_clear(self) -> None:
        dec = self._decoder
        if dec:
            dec.reset()
        if PIL_AVAILABLE:
            self._fax_img = Image.new("L", (PIXELS_PER_LINE, IMAGE_HEIGHT), 0)
            self._redraw(1)
        self._lines_var.set("0")
        self._state_var.set("IDLE")

    def _on_close(self) -> None:
        self._on_stop()
        self.destroy()

    # ── Decoder callbacks (RTL-SDR thread) ────────────────────────────────────

    def _audio_cb(self, audio: np.ndarray) -> None:
        dec = self._decoder
        if dec is not None:
            dec.process(audio)

    def _cb_line(self, y: int, row: np.ndarray) -> None:
        self._gui_q.put(("line", y, row))

    def _cb_status(self, msg: str) -> None:
        self._gui_q.put(("status", msg))

    def _cb_complete(self, image: np.ndarray, lines: int) -> None:
        self._gui_q.put(("complete", image, lines))

    # ── Queue polling (main thread) ───────────────────────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                self._dispatch(self._gui_q.get_nowait())
        except queue.Empty:
            pass
        finally:
            self.after(POLL_MS, self._poll_queue)

    def _dispatch(self, item: tuple) -> None:
        kind = item[0]

        if kind == "line":
            _, y, row_data = item
            if self._fax_img is not None:
                pil_row = Image.fromarray(row_data, mode="L")
                self._fax_img.paste(pil_row, (0, y))
                if y % 8 == 0:
                    dec = self._decoder
                    h   = dec._line_y if dec else IMAGE_HEIGHT
                    self._redraw(max(h, 1))
            dec = self._decoder
            if dec:
                self._lines_var.set(str(dec.line_count))
                self._state_var.set(dec.state.name)

        elif kind == "status":
            self._set_status(item[1])
            dec = self._decoder
            if dec:
                self._state_var.set(dec.state.name)

        elif kind == "complete":
            _, image, lines = item
            if PIL_AVAILABLE:
                self._fax_img = Image.fromarray(image, mode="L")
                self._redraw(lines if lines > 0 else IMAGE_HEIGHT)
            self._lines_var.set(str(lines))
            self._state_var.set(State.IDLE.name)

    def _redraw(self, height: int) -> None:
        if self._fax_img is None or not PIL_AVAILABLE:
            return
        cropped     = self._fax_img.crop((0, 0, PIXELS_PER_LINE, height))
        self._tk_img = ImageTk.PhotoImage(cropped)
        if self._canvas_item is None:
            self._canvas_item = self._canvas.create_image(
                0, 0, anchor="nw", image=self._tk_img
            )
        else:
            self._canvas.itemconfig(self._canvas_item, image=self._tk_img)
        self._canvas.configure(
            scrollregion=(0, 0, PIXELS_PER_LINE, height)
        )

    def _set_status(self, msg: str) -> None:
        self._status_var.set(msg)
