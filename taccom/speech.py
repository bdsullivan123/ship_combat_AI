"""Spoken output.

Alerts go into a queue that a background thread drains. A more urgent alert
cuts off whatever is being said, and alerts that waited too long are dropped:
"shields down" from eight seconds ago is worse than silence.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time

from .alerts import Alert, Priority

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
    """Silent speaker for --no-voice, and the fallback when SAPI is missing.

    The app prints every alert anyway, so there is nothing left to do here.
    """

    def say(self, alert: Alert) -> None:
        pass

    def close(self) -> None:
        pass


class SapiSpeaker:
    """Windows text-to-speech through SAPI, on its own thread."""

    SVSF_ASYNC = 1
    SVSF_PURGE_BEFORE_SPEAK = 2

    def __init__(self, rate: int = 2, voice: str | None = None):
        import win32com.client  # noqa: F401  -- fail now, not on the worker thread

        self.queue = SpeechQueue()
        self.rate = rate
        self.voice_name = voice
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="taccom-speech", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        if self._error:
            raise self._error

    def say(self, alert: Alert) -> None:
        self.queue.push(alert, time.monotonic())

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Rate = self.rate
            if self.voice_name:
                for token in voice.GetVoices():
                    if self.voice_name.lower() in token.GetDescription().lower():
                        voice.Voice = token
                        break
        except Exception as exc:  # pragma: no cover - depends on the machine
            self._error = exc
            self._ready.set()
            return
        self._ready.set()

        speaking: Priority | None = None
        while not self._stop.is_set():
            if speaking is not None and voice.WaitUntilDone(0):
                speaking = None
            alert = self.queue.next(time.monotonic(), speaking)
            if alert is None:
                time.sleep(0.05)
                continue
            flags = self.SVSF_ASYNC
            if speaking is not None:
                flags |= self.SVSF_PURGE_BEFORE_SPEAK
            voice.Speak(alert.text, flags)
            speaking = alert.priority
        voice.Speak("", self.SVSF_ASYNC | self.SVSF_PURGE_BEFORE_SPEAK)
        pythoncom.CoUninitialize()


def list_voices() -> list[str]:
    import win32com.client

    return [t.GetDescription() for t in win32com.client.Dispatch("SAPI.SpVoice").GetVoices()]


def make_speaker(enabled: bool, rate: int = 2, voice: str | None = None):
    if not enabled:
        return ConsoleSpeaker()
    try:
        return SapiSpeaker(rate=rate, voice=voice)
    except Exception as exc:
        print(f"[taccom] Voice unavailable ({exc}); printing alerts only.")
        return ConsoleSpeaker()
