"""Spoken output.

Alerts go into a queue that a background thread drains. A more urgent alert
cuts off whatever is being said, and alerts that waited too long are dropped:
"shields down" from eight seconds ago is worse than silence.

Two voices sit behind the same queue:
- Kokoro: a local neural voice. Natural, but takes about a second to render a
  line on CPU, so every line is cached on disk and the stock phrases are
  rendered ahead of time while the speaker is idle.
- SAPI: the built-in Windows voice. Robotic, instant, needs nothing downloaded.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import threading
import time
from pathlib import Path

from .alerts import Alert, Priority

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
KOKORO_MODEL = MODELS_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = MODELS_DIR / "voices-v1.0.bin"
DEFAULT_KOKORO_VOICE = "bf_isabella"

# How long an alert stays worth saying after it happened, in seconds.
MAX_AGE = {
    Priority.LOW: 4.0,
    Priority.NORMAL: 6.0,
    Priority.HIGH: 8.0,
    Priority.CRITICAL: 12.0,
}


class SpeechQueue:
    """Priority queue with staleness. Pure logic, so it can be tested without audio."""

    def __init__(self):
        self._heap: list[tuple] = []
        self._seq = itertools.count()
        self._lock = threading.Lock()

    def push(self, alert: Alert, queued_at: float) -> None:
        with self._lock:
            heapq.heappush(self._heap, (-alert.priority, next(self._seq), queued_at, alert))

    def next(self, now: float, speaking: Priority | None = None) -> Alert | None:
        """The alert to say now, or None.

        If something is already being spoken, only return an alert that is
        more urgent than it (the caller will interrupt). Otherwise return the
        most urgent alert that is still fresh.
        """
        with self._lock:
            while self._heap:
                neg_priority, _, queued_at, alert = self._heap[0]
                if now - queued_at > MAX_AGE[alert.priority]:
                    heapq.heappop(self._heap)
                    continue
                if speaking is not None and alert.priority <= speaking:
                    return None
                heapq.heappop(self._heap)
                return alert
            return None


class ConsoleSpeaker:
    """Silent speaker for --no-voice, and the fallback when no voice works.

    The app prints every alert anyway, so there is nothing left to do here.
    """

    def say(self, alert: Alert) -> None:
        pass

    def close(self) -> None:
        pass


# -- voice backends -----------------------------------------------------------
# A backend is created on the speech thread and used only there. start() begins
# speaking and replaces anything already playing; busy() says whether audio is
# still playing; idle() is called when there is nothing to say.

class SapiBackend:
    SVSF_ASYNC = 1
    SVSF_PURGE_BEFORE_SPEAK = 2

    def __init__(self, rate: int = 2, voice: str | None = None):
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        self._voice = win32com.client.Dispatch("SAPI.SpVoice")
        self._voice.Rate = rate
        if voice:
            for token in self._voice.GetVoices():
                if voice.lower() in token.GetDescription().lower():
                    self._voice.Voice = token
                    break

    def start(self, text: str) -> None:
        self._voice.Speak(text, self.SVSF_ASYNC | self.SVSF_PURGE_BEFORE_SPEAK)

    def busy(self) -> bool:
        return not self._voice.WaitUntilDone(0)

    def idle(self) -> None:
        pass

    def close(self) -> None:
        self.start("")
        import pythoncom

        pythoncom.CoUninitialize()


class KokoroBackend:
    def __init__(self, voice: str = DEFAULT_KOKORO_VOICE, speed: float = 1.1,
                 prewarm: list[str] | None = None):
        import sounddevice
        from kokoro_onnx import Kokoro

        if not KOKORO_MODEL.exists() or not KOKORO_VOICES.exists():
            raise FileNotFoundError(f"Kokoro model files missing from {MODELS_DIR} (see README)")
        self._sd = sounddevice
        self._kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        if voice not in self._kokoro.get_voices():
            raise ValueError(f"Unknown Kokoro voice {voice!r} (see: taccom voices)")
        self.voice = voice
        self.speed = speed
        # British voices (bf_/bm_) need British pronunciation rules.
        self.lang = "en-gb" if voice.startswith("b") else "en-us"
        self.cache = VoiceCache(MODELS_DIR / "voice_cache" / f"{voice}_{speed:g}")
        self._prewarm = [p for p in (prewarm or []) if not self.cache.has(p)]

    def render(self, text: str, cache: bool = True):
        # One-off lines (console replies) skip the cache so it only holds repeatable alerts.
        audio = self.cache.get(text) if cache else None
        if audio is None:
            audio, _ = self._kokoro.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
            if cache:
                self.cache.put(text, audio)
        return audio

    def start(self, text: str) -> None:
        self.play(self.render(text))

    def play(self, audio) -> None:
        self._sd.stop()
        self._sd.play(audio, VoiceCache.SAMPLE_RATE)

    def busy(self) -> bool:
        try:
            return self._sd.get_stream().active
        except RuntimeError:  # nothing has played yet
            return False

    def idle(self) -> None:
        # Render one stock phrase per idle tick, so a real alert never waits
        # behind more than one of them.
        if self._prewarm:
            self.render(self._prewarm.pop(0))

    @property
    def pending_prewarm(self) -> int:
        return len(self._prewarm)

    def close(self) -> None:
        self._sd.stop()


class VoiceCache:
    """Rendered lines on disk, one .npy file per line of text."""

    SAMPLE_RATE = 24000  # Kokoro's output rate

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def _path(self, text: str) -> Path:
        return self.directory / (hashlib.sha1(text.encode("utf-8")).hexdigest() + ".npy")

    def has(self, text: str) -> bool:
        return self._path(text).exists()

    def get(self, text: str):
        import numpy as np

        path = self._path(text)
        try:
            return np.load(path) if path.exists() else None
        except (OSError, ValueError):
            return None  # half-written file from a crash: render it again

    def put(self, text: str, audio) -> None:
        import numpy as np

        tmp = self._path(text).with_suffix(".tmp.npy")
        np.save(tmp, audio.astype(np.float32))
        tmp.replace(self._path(text))


class QueuedSpeaker:
    """Runs a voice backend on its own thread, fed by a SpeechQueue."""

    def __init__(self, make_backend):
        self.queue = SpeechQueue()
        self.backend = None
        self._make_backend = make_backend
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="taccom-speech", daemon=True)
        self._thread.start()
        self._ready.wait(30)
        if self._error:
            raise self._error

    def say(self, alert: Alert) -> None:
        self.queue.push(alert, time.monotonic())

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self) -> None:
        try:
            self.backend = self._make_backend()
        except Exception as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()

        speaking: Priority | None = None
        while not self._stop.is_set():
            if speaking is not None and not self.backend.busy():
                speaking = None
            alert = self.queue.next(time.monotonic(), speaking)
            if alert is not None:
                self.backend.start(alert.text)
                speaking = alert.priority
            elif speaking is None:
                self.backend.idle()
                time.sleep(0.02)
            else:
                time.sleep(0.02)
        self.backend.close()


def list_voices() -> dict[str, list[str]]:
    voices: dict[str, list[str]] = {}
    try:
        from kokoro_onnx import Kokoro

        voices["kokoro"] = sorted(v for v in Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES)).get_voices()
                                  if v[:2] in ("af", "am", "bf", "bm"))
    except Exception as exc:
        voices["kokoro"] = [f"(unavailable: {exc})"]
    try:
        import win32com.client

        voices["sapi"] = [t.GetDescription() for t in win32com.client.Dispatch("SAPI.SpVoice").GetVoices()]
    except Exception as exc:
        voices["sapi"] = [f"(unavailable: {exc})"]
    return voices


def make_speaker(engine: str, voice: str | None = None, rate: int = 2, speed: float = 1.1,
                 prewarm: list[str] | None = None):
    """engine is 'kokoro', 'sapi' or 'none'. Kokoro falls back to SAPI, SAPI to silence."""
    if engine == "none":
        return ConsoleSpeaker()
    if engine == "kokoro":
        try:
            return QueuedSpeaker(lambda: KokoroBackend(voice or DEFAULT_KOKORO_VOICE, speed, prewarm))
        except Exception as exc:
            print(f"[taccom] Neural voice unavailable ({exc}); using the Windows voice.")
            voice = None
    try:
        return QueuedSpeaker(lambda: SapiBackend(rate, voice))
    except Exception as exc:
        print(f"[taccom] Voice unavailable ({exc}); printing alerts only.")
        return ConsoleSpeaker()
