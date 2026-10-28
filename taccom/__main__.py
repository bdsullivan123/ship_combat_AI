"""Command line: python -m taccom [run|replay|voices]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .app import SessionRecorder, Taccom, read_inputs, replay, run_live
from .sources import default_journal_dir, newest_journal
from .speech import list_voices, make_speaker

SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taccom", description="Elite Dangerous tactical combat copilot.")
    sub = parser.add_subparsers(dest="command")

    def voice_options(p, default_on: bool):
        if default_on:
            p.add_argument("--no-voice", dest="voice", action="store_false", help="print alerts only")
        else:
            p.add_argument("--voice", dest="voice", action="store_true", help="speak alerts aloud")
        p.add_argument("--rate", type=int, default=2, help="speech rate, -10 to 10 (default 2)")
        p.add_argument("--voice-name", help="part of a voice's name, e.g. Zira (see: taccom voices)")

    run = sub.add_parser("run", help="watch the game live (default)")
    run.add_argument("--journal-dir", type=Path, default=default_journal_dir())
    run.add_argument("--no-record", dest="record", action="store_false", help="don't save a session file")
    voice_options(run, default_on=True)

    rep = sub.add_parser("replay", help="replay a recorded session or a game journal")
    rep.add_argument("file", nargs="?", type=Path, help="session .jsonl or Journal.*.log (default: newest journal)")
    rep.add_argument("--since", help="ISO time to start from, e.g. 2026-10-26T23:38:00Z")
    rep.add_argument("--until", help="ISO time to stop at")
    rep.add_argument("--speed", type=float, help="play back in real time times this (default: instantly)")
    voice_options(rep, default_on=False)

    sub.add_parser("voices", help="list installed Windows voices")

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("run", "replay", "voices", "-h", "--help"):
        argv.insert(0, "run")  # `python -m taccom --no-voice` means `run --no-voice`
    args = parser.parse_args(argv)
    command = args.command

    if command == "voices":
        for name in list_voices():
            print(name)
        return 0

    if command == "replay":
        path = args.file or newest_journal(default_journal_dir())
        if path is None or not path.exists():
            print("No file to replay.")
            return 1
        speaker = make_speaker(args.voice and args.speed is not None, args.rate, args.voice_name)
        app = Taccom(speaker)
        print(f"[taccom] Replaying {path.name}")
        try:
            replay(app, read_inputs(path, args.since, args.until), args.speed)
        except KeyboardInterrupt:
            pass
        finally:
            speaker.close()
        return 0

    speaker = make_speaker(args.voice, args.rate, args.voice_name)
    recorder = SessionRecorder(SESSIONS_DIR) if args.record else None
    app = Taccom(speaker, recorder)
    if recorder:
        print(f"[taccom] Recording to {recorder.path}")
    try:
        run_live(app, args.journal_dir)
    except KeyboardInterrupt:
        print("\n[taccom] Standing down.")
    finally:
        speaker.close()
        if recorder:
            recorder.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
