"""
audio_io.py
-----------
Thin wrapper around `sounddevice` (PortAudio bindings that ship prebuilt
wheels for Windows/macOS/Linux, unlike PyAudio which often fails to build)
for two jobs:

1. Continuously capture microphone audio in small chunks (for streaming up
   to the Gemini Live session).
2. Play back the raw PCM audio chunks Gemini streams down, while reporting a
   rolling volume level so the GUI's waveform can react to it in real time.

Playback runs on its own thread with a queue. (Writing to the audio device
directly from the asyncio loop blocked the whole session -- mic upload,
websocket reads and tool calls all froze while audio was playing.) The player
also knows how long the queued audio will keep playing, which the session uses
to mute the microphone while Amy talks so she never hears her own voice.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

import config

_BYTES_PER_SAMPLE = 2  # int16


def _rms_level(int16_array: np.ndarray) -> float:
    if int16_array.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(int16_array.astype(np.float32) ** 2)))
    return min(rms / 4000.0, 1.0)


class MicStream:
    """Captures microphone audio and hands each chunk to `on_chunk(bytes, level)`.

    `on_chunk` is called from PortAudio's audio thread, so it must be quick and
    must not touch asyncio/Qt objects directly.
    """

    def __init__(self, on_chunk: Callable[[bytes, float], None]):
        self.on_chunk = on_chunk
        self._stream: Optional[sd.InputStream] = None

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=config.MIC_SAMPLE_RATE,
            channels=config.CHANNELS,
            dtype="int16",
            blocksize=config.CHUNK_SIZE,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        try:
            self.on_chunk(indata.tobytes(), _rms_level(indata))
        except Exception:  # noqa: BLE001 - never let an exception kill the audio thread
            pass

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()


class SpeakerPlayer:
    """Plays raw PCM16 audio chunks as they arrive, reporting output level."""

    def __init__(self, level_callback: Optional[Callable[[float], None]] = None):
        self.level_callback = level_callback or (lambda level: None)
        self._q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._lock = threading.Lock()
        self._busy_until = 0.0  # monotonic time at which queued audio finishes playing
        self._closed = False

        self._stream = sd.OutputStream(
            samplerate=config.SPEAKER_SAMPLE_RATE,
            channels=config.CHANNELS,
            dtype="int16",
        )
        self._stream.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="amy-speaker")
        self._thread.start()

    # -- public API ---------------------------------------------------------
    def play_chunk(self, pcm_bytes: bytes) -> None:
        """Queue a chunk for playback. Returns immediately."""
        if self._closed or not pcm_bytes:
            return
        # PCM16 chunks must be an even number of bytes.
        if len(pcm_bytes) % _BYTES_PER_SAMPLE:
            pcm_bytes = pcm_bytes[: -(len(pcm_bytes) % _BYTES_PER_SAMPLE)]
        duration = len(pcm_bytes) / _BYTES_PER_SAMPLE / config.SPEAKER_SAMPLE_RATE / config.CHANNELS
        with self._lock:
            self._busy_until = max(self._busy_until, time.monotonic()) + duration
        self._q.put(pcm_bytes)

    def is_busy(self, tail: float = 0.0) -> bool:
        """True while audio is queued/playing (plus `tail` seconds afterwards)."""
        with self._lock:
            return time.monotonic() < self._busy_until + tail

    def interrupt(self) -> None:
        """Drop everything that hasn't been played yet (user barged in)."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            self._busy_until = 0.0
        self._safe_level(0.0)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.interrupt()
        self._q.put(None)
        self._thread.join(timeout=1.5)
        try:
            self._stream.stop()
        finally:
            self._stream.close()

    # -- internals ----------------------------------------------------------
    def _safe_level(self, level: float) -> None:
        try:
            self.level_callback(level)
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                arr = np.frombuffer(item, dtype=np.int16)
                if arr.size:
                    self._safe_level(_rms_level(arr))
                    self._stream.write(arr.reshape(-1, config.CHANNELS))
            except Exception:  # noqa: BLE001 - a bad chunk shouldn't kill playback
                pass
            if self._q.empty():
                self._safe_level(0.0)
