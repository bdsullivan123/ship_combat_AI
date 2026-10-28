import json
import os

from taccom.sources import JournalTailer, StatusReader


def write(path, text, mtime=None):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_tailer_waits_for_complete_lines(tmp_path):
    log = tmp_path / "Journal.2026-10-27T100000.01.log"
    write(log, '{"event":"Fileheader"}\n{"event":"Hull')
    tailer = JournalTailer(tmp_path)
    assert [e["event"] for e in tailer.read_existing()] == ["Fileheader"]
    assert tailer.poll() == []
    write(log, 'Damage","Health":0.5}\n')
    assert tailer.poll() == [{"event": "HullDamage", "Health": 0.5}]


def test_tailer_switches_to_new_journal(tmp_path):
    old = tmp_path / "Journal.2026-10-27T100000.01.log"
    write(old, '{"event":"A"}\n', mtime=1000)
    tailer = JournalTailer(tmp_path)
    tailer.read_existing()
    write(old, '{"event":"B"}\n', mtime=1001)
    new = tmp_path / "Journal.2026-10-27T110000.01.log"
    write(new, '{"event":"C"}\n', mtime=1002)
    assert [e["event"] for e in tailer.poll()] == ["B", "C"]
    assert tailer.path == new


def test_tailer_with_no_journal(tmp_path):
    tailer = JournalTailer(tmp_path)
    assert tailer.read_existing() == []
    assert tailer.poll() == []


def test_status_reader_ignores_partial_and_unchanged(tmp_path):
    path = tmp_path / "Status.json"
    reader = StatusReader(path)
    assert reader.poll() is None  # missing file
    path.write_text('{ "Flags": 16')
    assert reader.poll() is None  # mid-write
    path.write_text(json.dumps({"Flags": 16}))
    assert reader.poll() == {"Flags": 16}
    assert reader.poll() is None  # unchanged
