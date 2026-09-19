"""
wake_word.py
------------
Passive listener that sits in the background and waits for the user to say
"Amy" (or a configured variant). Once heard, it fires a callback so the GUI
can open a live Gemini session for the actual conversation.

This uses `sounddevice` for raw microphone capture (no PyAudio anywhere) plus
a small hand-rolled energy-based voice-activity detector: audio above a
calibrated noise floor starts an utterance, and enough trailing silence ends
it. The finished utterance is wrapped into a `speech_recognition.AudioData`
object and sent to a recognizer engine -- this works fine without PyAudio
because we never touch `speech_recognition.Microphone`.

Two engines are supported:

  * "google"  -- SpeechRecognition's free Google Web Speech endpoint.
                 Zero setup, needs internet, fine for personal use.
  * "vosk"    -- fully offline recognition using a small local model,
                 also fed from the same sounddevice capture loop.

While a conversation with Gemini is open the listener is *paused* (it releases
the microphone completely) and resumed afterwards, so two streams never fight
over the same device and Amy's own voice is never fed to the recognizer.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

import config

FRAME_MS = 30
FRAME_SIZE = int(config.MIC_SAMPLE_RATE * FRAME_MS / 1000)
PRE_ROLL_FRAMES = 10        # ~300 ms kept from before the speech started
MIN_SPEECH_FRAMES = 4       # ignore clicks / very short noises
MAX_UTTERANCE_SECONDS = 8.0 # give up on endless noise instead of buffering forever
WAKE_COOLDOWN_SECONDS = 3.0

# ---------------------------------------------------------------------------
# Text normalisation + whole-word matching
# ---------------------------------------------------------------------------
_CHAR_MAP = str.maketrans({
    "ي": "ی", "ى": "ی", "ئ": "ی",   # Arabic yeh variants -> Persian yeh
    "ك": "ک",                        # Arabic kaf -> Persian kaf
    "ۀ": "ه", "ة": "ه",
    "آ": "ا", "أ": "ا", "إ": "ا",   # alef variants -> plain alef
})
_STRIP_RE = re.compile(r"[\u064B-\u065F\u0670\u200c\u200d\u0640]")  # harakat, ZWNJ, tatweel


def _normalize(text: str) -> str:
    text = (text or "").translate(_CHAR_MAP)
    text = _STRIP_RE.sub("", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _wake_regex() -> re.Pattern:
    words = sorted({_normalize(w) for w in config.WAKE_WORDS if _normalize(w)}, key=len, reverse=True)
    alternation = "|".join(re.escape(w) for w in words)
    # (?<!\w) / (?!\w) = whole-word match, so "ایمیل" (email) and "family" don't trigger.
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


def _contains_wake_word(text: str) -> bool:
    norm = _normalize(text)
    return bool(norm) and _wake_regex().search(norm) is not None


def _strip_wake_word(text: str) -> str:
    """Everything that was said besides the wake word, e.g. 'Amy open chrome' -> 'open chrome'."""
    return _wake_regex().sub(" ", _normalize(text)).strip()


class WakeWordListener:
    """Runs on a background thread, calling `on_wake(remaining_text)` whenever
    a wake word is detected. `remaining_text` is whatever else was said in the
    same utterance (e.g. "Amy, open Chrome" -> "open chrome"), so the user
    doesn't have to say the wake word and the command as two separate turns.
    """

    def __init__(self, on_wake: Callable[[str], None], on_status: Optional[Callable[[str], None]] = None):
        self.on_wake = on_wake
        self.on_status = on_status or (lambda s: None)
        self._stop_event = threading.Event()
        self._paused = threading.Event()
        self._released = threading.Event()   # set while the mic stream is closed
        self._released.set()
        self._thread: Optional[threading.Thread] = None
        self._threshold: Optional[float] = None
        self._last_wake = 0.0

    # -- public API ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="amy-wake")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def pause(self, wait: bool = False, timeout: float = 4.0) -> None:
        """Release the microphone (optionally blocking until it's really free)."""
        self._paused.set()
        if wait:
            self._released.wait(timeout)

    def resume(self) -> None:
        self._paused.clear()

    # -- internals ----------------------------------------------------------
    def _run(self) -> None:
        engine = "vosk" if config.WAKE_WORD_ENGINE == "vosk" else "google"
        recognizer_state = {}
        while not self._stop_event.is_set():
            if self._paused.is_set():
                time.sleep(0.1)
                continue
            try:
                engine = self._listen_until_paused(engine, recognizer_state)
            except Exception as exc:  # noqa: BLE001
                self.on_status(f"Wake-word listener error: {exc} (retrying...)")
                time.sleep(3.0)

    def _calibrate(self, audio_q: "queue.Queue[np.ndarray]") -> float:
        if config.VAD_THRESHOLD is not None:
            return config.VAD_THRESHOLD
        if self._threshold is not None:
            return self._threshold  # calibrated once; don't redo it after every conversation
        self.on_status("Calibrating microphone...")
        samples = []
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                chunk = audio_q.get(timeout=0.2)
                samples.append(np.abs(chunk).mean())
            except queue.Empty:
                continue
        baseline = float(np.mean(samples)) if samples else 50.0
        self._threshold = max(baseline * 3.0, 150.0)
        return self._threshold

    def _load_vosk(self, state: dict):
        if "vosk" in state:
            return state["vosk"]
        try:
            from vosk import KaldiRecognizer, Model

            model = Model(config.VOSK_MODEL_PATH)
            state["vosk"] = KaldiRecognizer(model, config.MIC_SAMPLE_RATE)
        except Exception as exc:  # noqa: BLE001 - missing package OR missing model folder
            self.on_status(f"Vosk unavailable ({exc}); using Google recognition instead.")
            state["vosk"] = None
        return state["vosk"]

    def _listen_until_paused(self, engine: str, state: dict) -> str:
        """Open the mic and listen until paused/stopped. Returns the engine in use."""
        vosk_recognizer = self._load_vosk(state) if engine == "vosk" else None
        if engine == "vosk" and vosk_recognizer is None:
            engine = "google"

        import speech_recognition as sr

        recognizer = sr.Recognizer()
        audio_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)

        def callback(indata, frames, time_info, status):
            try:
                audio_q.put_nowait(indata.copy())
            except queue.Full:
                pass  # recognizer is busy; dropping old audio beats unbounded memory

        self._released.clear()
        try:
            with sd.InputStream(
                samplerate=config.MIC_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SIZE,
                callback=callback,
            ):
                threshold = self._calibrate(audio_q)
                self.on_status(f"Listening for '{config.ASSISTANT_NAME}'...")

                pre_roll: list[np.ndarray] = []
                buffer: list[np.ndarray] = []
                triggered = False
                silence_time = 0.0
                speech_frames = 0

                while not self._stop_event.is_set() and not self._paused.is_set():
                    try:
                        chunk = audio_q.get(timeout=0.3)
                    except queue.Empty:
                        continue

                    if engine == "vosk" and vosk_recognizer is not None:
                        import json

                        if vosk_recognizer.AcceptWaveform(chunk.tobytes()):
                            text = json.loads(vosk_recognizer.Result()).get("text", "")
                            if _contains_wake_word(text):
                                self._fire(_strip_wake_word(text))
                        continue

                    # -- google engine: manual VAD -> finished utterance -> recognize
                    level = float(np.abs(chunk).mean())
                    if level > threshold:
                        if not triggered:
                            buffer = list(pre_roll)
                            speech_frames = 0
                        triggered = True
                        silence_time = 0.0
                        speech_frames += 1
                        buffer.append(chunk)
                    elif triggered:
                        buffer.append(chunk)
                        silence_time += FRAME_MS / 1000.0
                    else:
                        pre_roll.append(chunk)
                        del pre_roll[:-PRE_ROLL_FRAMES]

                    if triggered:
                        too_long = len(buffer) * FRAME_MS / 1000.0 > MAX_UTTERANCE_SECONDS
                        if silence_time >= config.SILENCE_TIMEOUT or too_long:
                            audio_np = np.concatenate(buffer)
                            enough = speech_frames >= MIN_SPEECH_FRAMES
                            buffer, pre_roll = [], []
                            triggered = False
                            silence_time = 0.0
                            if enough:
                                self._recognize_and_dispatch(recognizer, audio_np)
        finally:
            self._released.set()
        return engine

    def _fire(self, remainder: str) -> None:
        now = time.time()
        if now - self._last_wake < WAKE_COOLDOWN_SECONDS:
            return
        self._last_wake = now
        self.on_wake(remainder)

    def _recognize_and_dispatch(self, recognizer, audio_np: np.ndarray) -> None:
        import speech_recognition as sr

        audio_data = sr.AudioData(audio_np.tobytes(), config.MIC_SAMPLE_RATE, 2)
        # Amy is said in English, but the recogniser may hear it as Persian
        # ("ایمی"/"امی") or as English ("Amy"). Try Persian first, then English.
        for language in ("fa-IR", "en-US"):
            try:
                text = recognizer.recognize_google(audio_data, language=language)
            except Exception:  # noqa: BLE001 - "could not understand" / network hiccup
                continue
            if _contains_wake_word(text):
                self._fire(_strip_wake_word(text))
                return
