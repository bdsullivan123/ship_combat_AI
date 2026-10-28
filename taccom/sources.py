"""Readers for the files Elite Dangerous writes while it runs.

Both readers poll rather than use filesystem notifications: the game rewrites
Status.json in place several times a second, and polling a file's size is
cheaper and more predictable than reacting to every write notification.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def default_journal_dir() -> Path:
    return Path(os.path.expanduser("~")) / "Saved Games" / "Frontier Developments" / "Elite Dangerous"


def newest_journal(journal_dir: Path) -> Path | None:
    # Names sort chronologically (Journal.2026-10-27T154844.01.log), but older
    # installs used a different scheme, so modification time is the safer key.
    journals = list(journal_dir.glob("Journal.*.log"))
    if not journals:
        return None
    return max(journals, key=lambda p: (p.stat().st_mtime, p.name))


class JournalTailer:
    """Follows the newest journal file, switching when the game starts a new one."""

    def __init__(self, journal_dir: Path):
        self.journal_dir = journal_dir
        self.path: Path | None = None
        self._pos = 0
        self._partial = b""

    def read_existing(self) -> list[dict]:
        """Open the newest journal and return everything already in it."""
        self.path = newest_journal(self.journal_dir)
        self._pos = 0
        self._partial = b""
        return self._read_new() if self.path else []

    def poll(self) -> list[dict]:
        events = self._read_new() if self.path else []
        newest = newest_journal(self.journal_dir)
        if newest is not None and newest != self.path:
            # A new game session started. Finish the old file (done above),
            # then read the new one from the beginning.
            self.path = newest
            self._pos = 0
            self._partial = b""
            events += self._read_new()
        return events

    def _read_new(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                chunk = f.read()
        except OSError:
            return []
        self._pos += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        # The last piece has no newline yet: the game may still be writing it.
        self._partial = lines.pop()
        events = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events


class StatusReader:
    """Returns the contents of Status.json whenever it changes."""

    def __init__(self, path: Path):
        self.path = path
        self._last_raw: bytes | None = None

    def poll(self) -> dict | None:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return None
        if not raw.strip() or raw == self._last_raw:
            return None
        try:
            status = json.loads(raw)
        except json.JSONDecodeError:
            # Caught the game mid-write; the next poll will see the full file.
            return None
        self._last_raw = raw
        return status
