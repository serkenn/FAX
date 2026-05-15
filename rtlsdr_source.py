"""RTL-SDR IQ source with AM demodulation for WEFAX HF reception."""

from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np
from scipy import signal

try:
    from rtlsdr import RtlSdr
    RTLSDR_AVAILABLE = True
except ImportError:
    RTLSDR_AVAILABLE = False

RTL_SAMPLE_RATE = 240_000   # IQ samples / second from the dongle
AUDIO_RATE      = 24_000    # Audio samples / second after decimation
DECIMATION      = RTL_SAMPLE_RATE // AUDIO_RATE   # = 10
BLOCK_IQ        = 32_768    # IQ samples per RTL-SDR callback (~136 ms)


class RTLSDRSource:
    """RTL-SDR source that streams AM-demodulated audio to a callback.

    For HF WEFAX (< 28 MHz), set direct_sampling=True to use Q-branch direct
    sampling mode, bypassing the tuner. For frequencies above ~25 MHz use
    direct_sampling=False (standard tuner mode).
    """

    def __init__(
        self,
        center_freq_hz: float,
        device_index: int = 0,
        gain: str | float = "auto",
        direct_sampling: bool = True,
    ) -> None:
        if not RTLSDR_AVAILABLE:
            raise RuntimeError(
                "pyrtlsdr is not installed.\n\nRun: pip install pyrtlsdr"
            )

        self.center_freq      = float(center_freq_hz)
        self.device_index     = int(device_index)
        self.gain             = gain
        self.direct_sampling  = direct_sampling

        self._sdr:    Optional[RtlSdr]    = None
        self._thread: Optional[threading.Thread] = None
        self._stop    = threading.Event()
        self._callback: Optional[Callable[[np.ndarray], None]] = None

        # FIR anti-aliasing low-pass before decimation (cutoff = audio Nyquist)
        cutoff    = (AUDIO_RATE / 2.0) / (RTL_SAMPLE_RATE / 2.0)
        self._fir = signal.firwin(127, cutoff, window="hamming").astype(np.float32)
        # lfilter initial state for FIR (a = [1.0])
        self._fir_state = np.zeros(len(self._fir) - 1, dtype=np.float64)

        # Software AGC state
        self._agc_gain = 1.0

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def audio_rate(self) -> int:
        return AUDIO_RATE

    def set_callback(self, cb: Callable[[np.ndarray], None]) -> None:
        self._callback = cb

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="rtlsdr-rx", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 4.0) -> None:
        self._stop.set()
        sdr = self._sdr
        if sdr is not None:
            try:
                sdr.cancel_read_async()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ── Private ───────────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            sdr = RtlSdr(self.device_index)
            self._sdr = sdr

            sdr.sample_rate = RTL_SAMPLE_RATE
            sdr.center_freq = self.center_freq
            sdr.gain        = "auto" if self.gain == "auto" else float(self.gain)

            if self.direct_sampling:
                # Q-branch direct sampling: best sensitivity for HF < 28 MHz
                sdr.direct_sampling = "q"

            sdr.read_samples_async(self._iq_callback, num_samples=BLOCK_IQ)
        except Exception as exc:
            print(f"[RTL-SDR] {exc}")
        finally:
            sdr = self._sdr
            if sdr is not None:
                try:
                    sdr.close()
                except Exception:
                    pass
            self._sdr = None

    def _iq_callback(self, iq_samples: np.ndarray, _ctx: object) -> None:
        if self._stop.is_set():
            raise StopIteration  # exits read_samples_async

        # AM envelope demodulation: magnitude of complex IQ
        envelope = np.abs(iq_samples).astype(np.float64)

        # Remove DC offset introduced by direct sampling / dongle imbalance
        envelope -= envelope.mean()

        # Anti-alias low-pass filter (FIR, preserves filter state between blocks)
        filtered, self._fir_state = signal.lfilter(
            self._fir.astype(np.float64),
            np.array([1.0]),
            envelope,
            zi=self._fir_state,
        )

        # Decimate to audio rate
        audio = filtered[::DECIMATION]

        # Software AGC: slowly track signal level, normalise to ±0.5
        peak = float(np.max(np.abs(audio)))
        if peak > 1e-6:
            target = 0.5
            error  = target / peak - self._agc_gain
            self._agc_gain += 0.05 * error   # slow attack/release
            audio = np.clip(audio * self._agc_gain, -1.0, 1.0)

        cb = self._callback
        if cb is not None:
            cb(audio.astype(np.float32))
