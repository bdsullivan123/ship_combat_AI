"""Command line: python -m taccom [run|replay|console|align|jump|voices|samples|hud|setup]"""

from __future__ import annotations

import argparse
import sys
import wave
from datetime import datetime
from pathlib import Path

from .alerts import AlertEngine
from .app import SessionRecorder, Taccom, read_inputs, replay, run_live
from .sources import default_journal_dir, newest_journal
from .speech import DEFAULT_KOKORO_VOICE, list_voices, make_speaker

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "sessions"
SAMPLES_DIR = ROOT / "voice_samples"
COMMANDS = ("run", "replay", "console", "align", "jump", "voices", "samples", "hud", "setup")
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"

SAMPLE_LINE = ("Commander, wanted Imperial Clipper, Dangerous pilot. Shields down. Four pips to systems. "
               "Hull critical, 18 percent. Disengage now.")
SAMPLE_VOICES = ["af_heart", "af_bella", "af_nicole", "af_sarah", "bf_emma", "bf_isabella",
                 "am_michael", "am_fenrir", "am_onyx", "bm_george", "bm_fable", "bm_lewis"]


def voice_options(p: argparse.ArgumentParser, default_engine: str) -> None:
    p.add_argument("--engine", choices=["kokoro", "sapi", "none"], default=default_engine,
                   help=f"kokoro = natural neural voice, sapi = Windows voice, none = print only "
                        f"(default {default_engine})")
    p.add_argument("--no-voice", dest="engine", action="store_const", const="none", help="same as --engine none")
    p.add_argument("--voice", help=f"voice name (default {DEFAULT_KOKORO_VOICE}; see: taccom voices)")
    p.add_argument("--speed", type=float, default=1.1, help="neural voice speed (default 1.1)")
    p.add_argument("--rate", type=int, default=2, help="Windows voice rate, -10 to 10 (default 2)")


def speaker_from(args, engine: str | None = None):
    return make_speaker(engine or args.engine, args.voice, args.rate, args.speed,
                        prewarm=AlertEngine.stock_phrases())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taccom", description="Elite Dangerous tactical combat copilot.")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="watch the game live (default)")
    run.add_argument("--journal-dir", type=Path, default=default_journal_dir())
    run.add_argument("--no-record", dest="record", action="store_false", help="don't save a session file")
    run.add_argument("--no-hud", dest="hud", action="store_false",
                     help="don't read the screen (ammo, heat); game files only")
    run.add_argument("--capture", action="store_true",
                     help="save labelled HUD frames during combat (Stage 2 calibration data)")
    run.add_argument("--capture-nav", action="store_true",
                     help="save labelled frames while flying and jumping (autopilot calibration data)")
    voice_options(run, "kokoro")

    rep = sub.add_parser("replay", help="replay a recorded session or a game journal")
    rep.add_argument("file", nargs="?", type=Path, help="session .jsonl or Journal.*.log (default: newest journal)")
    rep.add_argument("--since", help="ISO time to start from, e.g. 2026-10-26T23:38:00Z")
    rep.add_argument("--until", help="ISO time to stop at")
    rep.add_argument("--realtime", type=float, metavar="X",
                     help="play back at X times real speed, speaking aloud (default: instantly, print only)")
    voice_options(rep, "kokoro")

    con = sub.add_parser("console", help="open the ship's-computer console and talk to Claude Code")
    con.add_argument("--port", type=int, default=7700)
    con.add_argument("--speak", action="store_true", help="read replies aloud from the start (toggle in the window)")
    con.add_argument("--voice", help=f"voice for replies (default {DEFAULT_KOKORO_VOICE})")
    con.add_argument("--speed", type=float, default=1.1, help="voice speed (default 1.1)")
    con.add_argument("--model", help="Claude model for the console (default: Claude Code's)")
    con.add_argument("--journal-dir", type=Path, default=default_journal_dir())
    con.add_argument("--no-window", dest="window", action="store_false", help="don't open a window")

    aln = sub.add_parser("align", help="autopilot test: turn to face the targeted system, then stop (Right Ctrl aborts)")
    aln.add_argument("--delay", type=float, default=5.0, help="seconds to click back into the game (default 5)")
    aln.add_argument("--journal-dir", type=Path, default=default_journal_dir())

    jmp = sub.add_parser("jump", help="autopilot test: one jump to the targeted system, then clear the star")
    jmp.add_argument("--delay", type=float, default=5.0, help="seconds to click back into the game (default 5)")
    jmp.add_argument("--journal-dir", type=Path, default=default_journal_dir())

    sub.add_parser("setup", help="download the neural voice model (about 350 MB, once)")
    sub.add_parser("voices", help="list available voices")

    hud = sub.add_parser("hud", help="read the HUD from a screenshot and print what was found")
    hud.add_argument("image", type=Path)
    hud.add_argument("--layout", default="ferdelance")

    smp = sub.add_parser("samples", help="write a WAV of each neural voice to voice_samples/")
    smp.add_argument("--text", default=SAMPLE_LINE)
    smp.add_argument("--speed", type=float, default=1.1)
    smp.add_argument("voices", nargs="*", help=f"voices to render (default: {len(SAMPLE_VOICES)} picks)")

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in (*COMMANDS, "-h", "--help"):
        argv.insert(0, "run")  # `python -m taccom --no-voice` means `run --no-voice`
    args = parser.parse_args(argv)

    if args.command == "voices":
        for engine, names in list_voices().items():
            print(f"{engine}:")
            for name in names:
                print(f"  {name}")
        return 0

    if args.command == "console":
        from .console import serve

        return serve(args.port, args.voice, args.speed, args.speak, args.model, args.journal_dir, args.window)

    if args.command in ("align", "jump"):
        import time

        from .autopilot import Aborted, align, jump_once

        print(f"Click into Elite now. Starting in {args.delay:.0f} seconds. Right Ctrl aborts.")
        time.sleep(args.delay)
        try:
            fly = align if args.command == "align" else jump_once
            return 0 if fly(args.journal_dir) else 1
        except Aborted as exc:
            print(f"Aborted: {exc}. Controls released.")
            return 1

    if args.command == "setup":
        return download_models()

    if args.command == "samples":
        return write_samples(args.voices or SAMPLE_VOICES, args.text, args.speed)

    if args.command == "hud":
        import time

        import cv2

        from .hud import HudReader

        frame = cv2.imread(str(args.image))
        if frame is None:
            print(f"Can't open {args.image}")
            return 1
        reader = HudReader(args.layout)
        start = time.perf_counter()
        reading = reader.read(frame)
        print(f"Read in {(time.perf_counter() - start) * 1000:.0f} ms (first read includes model load)")
        for w in reading.weapons:
            print(f"  {w.name:<14} {w.clip}/{w.reserve}")
        print(f"  heat {reading.heat}%  hull {reading.hull}%")
        print(f"  target {reading.target_lines} {reading.target_distance or ''}")
        return 0

    if args.command == "replay":
        path = args.file or newest_journal(default_journal_dir())
        if path is None or not path.exists():
            print("No file to replay.")
            return 1
        speaker = speaker_from(args, engine=args.engine if args.realtime else "none")
        app = Taccom(speaker)
        print(f"[taccom] Replaying {path.name}")
        try:
            replay(app, read_inputs(path, args.since, args.until), args.realtime)
        except KeyboardInterrupt:
            pass
        finally:
            speaker.close()
        return 0

    speaker = speaker_from(args)
    recorder = SessionRecorder(SESSIONS_DIR) if args.record else None
    collector = None
    if args.capture or args.capture_nav:
        from .capture import FrameCollector

        kind = "nav" if args.capture_nav else "frames"
        frames_dir = SESSIONS_DIR / f"{kind}_{datetime.now():%Y-%m-%d_%H%M%S}"
        collector = FrameCollector(frames_dir, interval=1.0 if args.capture_nav else 2.0, nav=args.capture_nav)
        print(f"[taccom] Capturing labelled HUD frames to {frames_dir}")
    watcher = None
    if args.hud:
        try:
            from .hud import HudWatcher

            watcher = HudWatcher()
            print("[taccom] Reading the HUD (ammo, heat) while hardpoints are deployed.")
        except Exception as exc:
            print(f"[taccom] Screen reading unavailable ({exc}); game files only.")
    app = Taccom(speaker, recorder, collector=collector, hud=watcher)
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
        if watcher:
            watcher.close()
        if collector:
            collector.close()
            print(f"[taccom] Saved {collector.saved} frames "
                  f"({collector.skipped_not_focused} skipped while Elite wasn't the active window).")
    return 0


def download_models() -> int:
    import urllib.request

    from .speech import KOKORO_MODEL, KOKORO_VOICES

    KOKORO_MODEL.parent.mkdir(exist_ok=True)
    for path in (KOKORO_MODEL, KOKORO_VOICES):
        if path.exists():
            print(f"  have {path.name}")
            continue
        print(f"  downloading {path.name} ...")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL + path.name, tmp)
        tmp.replace(path)
    print("Neural voice ready. Try: python -m taccom samples")
    return 0


def write_samples(voices: list[str], text: str, speed: float) -> int:
    import numpy as np
    from kokoro_onnx import Kokoro

    from .speech import KOKORO_MODEL, KOKORO_VOICES

    kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
    SAMPLES_DIR.mkdir(exist_ok=True)
    for voice in voices:
        lang = "en-gb" if voice.startswith("b") else "en-us"
        audio, rate = kokoro.create(text, voice=voice, speed=speed, lang=lang)
        path = SAMPLES_DIR / f"{voice}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
