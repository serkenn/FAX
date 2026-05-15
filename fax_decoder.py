"""WEFAX (HF Weather FAX) FM subcarrier demodulator and image assembler."""

from __future__ import annotations

import threading
from enum import Enum, auto
from typing import Callable, Optional

import numpy as np
from scipy import signal

# ── WEFAX protocol constants ─────────────────────────────────────────────────
BLACK_FREQ      = 1500   # Hz  – darkest tone
WHITE_FREQ      = 2300   # Hz  – brightest tone
CENTER_FREQ     = 1900   # Hz  – FM deviation centre
START_TONE      = 300    # Hz  – image start marker (3+ seconds)
STOP_TONE       = 450    # Hz  – image stop marker
PIXELS_PER_LINE = 1809   # Standard WEFAX line width
IMAGE_HEIGHT    = 1200   # Pre-allocated row count
LPM_120         = 120    # Standard speed
LPM_90          = 90     # Slow speed

ENERGY_THRESHOLD = 1e-4  # Tone detector: active above this normalised energy


class State(Enum):
    IDLE      = auto()
    PHASING   = auto()
    IMAGE     = auto()
    STOP_TONE = auto()


class ToneDetector:
    """Single-frequency energy estimator using DFT projection + IIR smoothing."""

    def __init__(self, freq_hz: float, sample_rate: int, smoothing: float = 0.15) -> None:
        self._freq_hz    = freq_hz
        self._sample_rate = sample_rate
        self._alpha      = smoothing
        self._energy     = 0.0

    def process(self, samples: np.ndarray) -> float:
        """Return smoothed energy estimate for this block."""
        N = len(samples)
        if N == 0:
            return self._energy
        omega    = 2.0 * np.pi * self._freq_hz / self._sample_rate
        phasors  = np.exp(1j * omega * np.arange(N))
        raw_energy = (abs(np.dot(samples.astype(np.float64), phasors)) ** 2) / N
        self._energy += self._alpha * (raw_energy - self._energy)
        return self._energy

    def reset(self) -> None:
        self._energy = 0.0


class WEFAXDecoder:
    """Streaming WEFAX decoder.

    Feed blocks of float32 audio (AM-demodulated RF envelope, ±1.0 range) via
    process(). Callbacks are invoked from the calling thread; use a queue if the
    caller is not the GUI thread.
    """

    def __init__(
        self,
        sample_rate: int = 24_000,
        lpm: int = LPM_120,
        on_line:     Optional[Callable[[int, np.ndarray], None]] = None,
        on_status:   Optional[Callable[[str],             None]] = None,
        on_complete: Optional[Callable[[np.ndarray, int], None]] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.lpm         = lpm
        self.on_line     = on_line
        self.on_status   = on_status
        self.on_complete = on_complete

        nyq = sample_rate / 2.0

        # Band-pass to isolate the FM subcarrier (1400–2500 Hz)
        lo = 1400.0 / nyq
        hi = min(2500.0 / nyq, 0.99)
        self._bp_sos = signal.butter(4, [lo, hi], btype="bandpass", output="sos")
        self._bp_zi  = signal.sosfilt_zi(self._bp_sos)

        # FM discriminator: mix with 1900 Hz NCO, then low-pass I and Q
        lp_cut = min(650.0 / nyq, 0.99)
        self._lp_sos  = signal.butter(4, lp_cut, btype="low", output="sos")
        self._lp_zi_i = signal.sosfilt_zi(self._lp_sos)
        self._lp_zi_q = signal.sosfilt_zi(self._lp_sos)
        self._nco_step  = 2.0 * np.pi * CENTER_FREQ / sample_rate
        self._nco_phase = 0.0
        self._prev_iq   = complex(1.0, 0.0)

        # Tone detectors
        self._start_det = ToneDetector(START_TONE, sample_rate)
        self._stop_det  = ToneDetector(STOP_TONE,  sample_rate)

        self._samples_per_pixel = sample_rate / (PIXELS_PER_LINE * lpm / 60.0)
        self._lock = threading.Lock()
        self._init_state()

    # ── Public API ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        with self._lock:
            self._init_state()
            self._start_det.reset()
            self._stop_det.reset()
            self._prev_iq   = complex(1.0, 0.0)
            self._nco_phase = 0.0
            self._bp_zi     = signal.sosfilt_zi(self._bp_sos)
            self._lp_zi_i   = signal.sosfilt_zi(self._lp_sos)
            self._lp_zi_q   = signal.sosfilt_zi(self._lp_sos)

    @property
    def state(self) -> State:
        return self._state

    def process(self, audio: np.ndarray) -> None:
        """Process a block of audio samples (float32 or float64, ±1.0)."""
        if audio.size == 0:
            return
        with self._lock:
            audio = audio.astype(np.float64)
            bp, self._bp_zi = signal.sosfilt(self._bp_sos, audio, zi=self._bp_zi)
            grey            = self._fm_demod(bp)
            start_e         = self._start_det.process(audio)
            stop_e          = self._stop_det.process(audio)
            self._update_state(start_e, stop_e, len(audio))
            if self._state == State.IMAGE:
                for g in grey:
                    self._push_pixel(float(g))

    # ── Private ───────────────────────────────────────────────────────────────

    def _init_state(self) -> None:
        self._state         = State.IDLE
        self.line_count     = 0
        self._pixel_pos     = 0
        self._line_y        = 0
        self._pixel_accum   = 0.0
        self._pixel_n       = 0
        self._start_hold    = 0
        self._phasing_n     = 0
        self.image_data     = np.zeros(
            (IMAGE_HEIGHT, PIXELS_PER_LINE), dtype=np.uint8
        )

    def _fm_demod(self, audio: np.ndarray) -> np.ndarray:
        """NCO mix → low-pass → phase derivative → normalised grey (0–1)."""
        n      = np.arange(len(audio))
        phases = self._nco_phase + n * self._nco_step
        self._nco_phase = float(phases[-1] + self._nco_step) % (2.0 * np.pi)

        # Mix with exp(-j*phase): negative sign on Q is the correct
        # downconversion convention so that a tone above center maps to
        # positive baseband frequency (not inverted).
        i_raw =  audio * np.cos(phases)
        q_raw = -audio * np.sin(phases)

        i_filt, self._lp_zi_i = signal.sosfilt(self._lp_sos, i_raw, zi=self._lp_zi_i)
        q_filt, self._lp_zi_q = signal.sosfilt(self._lp_sos, q_raw, zi=self._lp_zi_q)

        iq      = i_filt + 1j * q_filt
        iq_prev = np.empty_like(iq)
        iq_prev[0] = self._prev_iq
        iq_prev[1:] = iq[:-1]
        self._prev_iq = complex(iq[-1])

        deviation = np.angle(iq * np.conj(iq_prev))  # rad / sample
        freq_hz   = CENTER_FREQ + deviation * self.sample_rate / (2.0 * np.pi)
        return np.clip(
            (freq_hz - BLACK_FREQ) / float(WHITE_FREQ - BLACK_FREQ),
            0.0, 1.0,
        )

    def _update_state(self, start_e: float, stop_e: float, n: int) -> None:
        sr = self.sample_rate

        if self._state == State.IDLE:
            if start_e > ENERGY_THRESHOLD:
                self._start_hold += n
                if self._start_hold > sr * 3:
                    self._state       = State.PHASING
                    self._phasing_n   = 0
                    self._start_hold  = 0
                    self._notify("Phasing detected – waiting for image start…")
            else:
                self._start_hold = max(0, self._start_hold - n // 2)

        elif self._state == State.PHASING:
            self._phasing_n += n
            if self._phasing_n > sr * 5:
                self._state     = State.IMAGE
                self._pixel_pos = 0
                self._line_y    = 0
                self._notify("Receiving WEFAX image…")

        elif self._state == State.IMAGE:
            if stop_e > ENERGY_THRESHOLD:
                self._state = State.STOP_TONE
                img_copy    = self.image_data.copy()
                n_lines     = self.line_count
                self._notify(f"Image complete — {n_lines} lines received")
                if self.on_complete:
                    self.on_complete(img_copy, n_lines)

        elif self._state == State.STOP_TONE:
            if stop_e < ENERGY_THRESHOLD / 10:
                self._state = State.IDLE
                self._notify("Idle – waiting for 300 Hz start tone")

    def _push_pixel(self, grey: float) -> None:
        self._pixel_accum += grey
        self._pixel_n     += 1
        threshold = max(1, int(self._samples_per_pixel))
        if self._pixel_n < threshold:
            return

        value = int(np.clip(self._pixel_accum / self._pixel_n * 255.0, 0, 255))
        self._pixel_accum = 0.0
        self._pixel_n     = 0

        if self._pixel_pos < PIXELS_PER_LINE and self._line_y < IMAGE_HEIGHT:
            self.image_data[self._line_y, self._pixel_pos] = value

        self._pixel_pos += 1
        if self._pixel_pos >= PIXELS_PER_LINE:
            row = self.image_data[self._line_y, :].copy()
            y   = self._line_y
            if self.on_line:
                self.on_line(y, row)
            self._line_y    = (self._line_y + 1) % IMAGE_HEIGHT
            self._pixel_pos = 0
            self.line_count += 1

    def _notify(self, msg: str) -> None:
        if self.on_status:
            self.on_status(msg)
