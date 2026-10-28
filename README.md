# TACCOM — Tactical Combat Operations Manager

An AI tactical officer for **Elite Dangerous**. TACCOM watches your ship's state during combat and talks to you: it calls out urgent warnings on its own, and answers tactical questions when you ask.

It is an **advisor, not an autopilot**. It never sends input to the game.

> **Status:** Stage 1 (alerts from game files) works, with a natural neural voice. Stage 2 (screen reading) reads ammo, heat and hull on the Fer-de-Lance; shield rings and the target's health bars are next. Stage 3 not started.

---

## Quick start

Needs Windows and Python 3.10 or newer.

```powershell
git clone https://github.com/bdsullivan123/ship_combat_AI.git
cd ship_combat_AI
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m taccom setup        # downloads the neural voice, about 350 MB, once

.venv\Scripts\python -m taccom
```

Start it before or during a game session. It reads the current journal silently to learn your ship, then says "TACCOM online" and starts watching. Ctrl+C stops it.

| Command | What it does |
|---|---|
| `python -m taccom` | Watch the game live, read the HUD, speak alerts, and record the session to `sessions/` |
| `python -m taccom --voice bf_emma --speed 1.2` | Choose the voice and how fast it talks |
| `python -m taccom --no-voice` | Print alerts only |
| `python -m taccom --engine sapi` | Use the built-in Windows voice instead of the neural one |
| `python -m taccom --no-hud` | Don't read the screen; game files only |
| `python -m taccom --capture` | Also save labelled HUD screenshots during combat, for calibrating new screen readers |
| `python -m taccom samples` | Write a WAV of each recommended voice to `voice_samples/` so you can pick one |
| `python -m taccom voices` | List every available voice |
| `python -m taccom hud <screenshot>` | Read the HUD from a screenshot and print what was found |
| `python -m taccom replay <file>` | Run a recorded session or a game journal through TACCOM instantly and print what it would have said |
| `python -m taccom replay <file> --since 2026-10-26T23:38:00Z --until 2026-10-26T23:46:00Z` | Replay only one fight. Earlier events still build ship state, silently |
| `python -m taccom replay <file> --realtime 1` | Replay at real speed, out loud |

With no file, `replay` uses your newest game journal. Run the tests with `.venv\Scripts\python -m pytest`.

## The voice

TACCOM speaks with [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), a small open-source neural voice that runs entirely on your PC, with no subscription and no internet needed. The default is `af_heart`. Run `python -m taccom samples` and listen to the files in `voice_samples/` to choose another; British voices start with `bf_`/`bm_`, American with `af_`/`am_`.

A neural voice takes about half a second to say something new on CPU, which is too slow for "shields down". So every line is saved to disk the first time it's said and plays instantly after that, and TACCOM renders all its stock lines ("Hull 1 percent" through "Hull 100 percent", "Shields down", ammo and heat calls) in the background while nothing is happening. The first session with a new voice fills this cache over a few minutes; after that, everything except one-off lines like target scans plays at once.

If the neural voice can't load, TACCOM falls back to the Windows voice, and then to printing.

## What it says (Stage 1)

| Situation | Says | Priority | Repeats after |
|---|---|---|---|
| Shields drop | "Shields down. Four pips to systems." (pip advice only if SYS isn't already full) | High | 5 s |
| Shields return | "Shields back online." | Normal | 5 s |
| Hull damaged, 50% or more | "Hull 80 percent." | Normal | 2 s |
| Hull below 50% | "Hull 36 percent. Consider disengaging." | High | 2 s |
| Hull below 25% | "Hull critical, 18 percent. Disengage now. Four pips to engines." | Critical | 2 s |
| Overheating | "Overheating. Heat sink advised." (if fitted) or "Silent running is on." | High | 20 s |
| Heat damage | "Heat damage! Fire a heat sink or cool down now." | Critical | 10 s |
| Shot at, after a minute of peace | "Taking fire." | Normal | once per engagement |
| Wanted / enemy ship fully scanned | "Wanted Imperial Clipper. Caution, Dangerous pilot. Shields 65 percent. Bounty 566 thousand credits." | Low | once per ship per instance |
| Being interdicted | "Interdiction! Evade or submit." | Critical | 30 s |
| Low fuel, canopy breach, fighter lost, kill confirmed, ship lost | short calls | varies | varies |

How it stays out of your way:

- **Urgent alerts interrupt.** If a less urgent alert is being spoken, a more urgent one cuts it off.
- **Escalation gets through.** A worse alert on the same subject skips its cooldown, so "hull 55%" never swallows "hull critical".
- **Old alerts are dropped.** Anything still queued after 4–12 seconds is discarded instead of spoken late.
- **No calls when parked or self-destructing.** It stays silent while docked or landed, and from a self-destruct until you respawn.
- **Clean ships are ignored.** Scanning a clean ship produces no announcement.

## What it reads off the screen (Stage 2)

The game files don't report live ammunition or heat below 100%, but the HUD does. While your hardpoints are deployed, TACCOM grabs the Elite window every 1.5 seconds and reads the text panels with [RapidOCR](https://github.com/RapidAI/RapidOCR), which runs on 2 CPU threads in about 0.4 s per read.

| Situation | Says | Priority |
|---|---|---|
| Heat 85% or more (before it overheats) | "Heat 88 percent." | High |
| Ammo for a weapon type at 50 / 25 / 10 / 0% | "Multicannon ammo 25 percent." / "Multicannon ammo out." | Normal, then High from 10% |
| One gun of several runs dry | "One multicannon is dry." | Normal |
| Heat sinks or chaff used up | "Heat sinks empty." | Normal |
| Overheating with no heat sinks left | "Overheating. No heat sinks left." | High |

How it avoids acting on a bad read:

- **Two reads must agree.** Nothing is said until the same condition shows up in two reads in a row, so one misread "1200" as "120" stays quiet.
- **Full ammo comes from your loadout.** It's tracked per fire group, because the panel only shows the current group's weapons.
- **It only reads when the combat HUD is showing.** That means hardpoints out, not in analysis mode, no panel or map open, and only while Elite is the active window, so nothing else on your desktop is ever captured.

Screen positions are calibrated for the **Fer-de-Lance at 1920×1080 with the default orange HUD**. Other ships put the panels elsewhere, so the HUD reader switches itself off in them until they're calibrated.

### Not read yet

- **Your shield ring percentage.** It's a graphic, not text. The files say up or down.
- **Your target's live shield and hull bars.** `ShipTargeted` gives a snapshot when you lock on or the scan advances.
- **Radar contacts.**

These need frames from real fights. `python -m taccom --capture` saves a screenshot every 2 seconds in combat, plus one at every hull hit, shield change and target scan. Each comes with a JSON file of what the game files said at that instant, which is the answer key for calibrating the next readers.

---

## What it should feel like

> "Commander, shields are weakening. Four pips to SYS recommended. Your target is nearly destroyed. Finish him, then disengage to recharge."

TACCOM works as a hybrid copilot:

- **Speaks up on its own** for things that matter right now: shields down, hull critical, overheating, low fuel, ammunition running out.
- **Stays quiet otherwise.** Cooldowns and priorities keep it from chattering through a fight.
- **Answers when asked:** "Should I retreat?", "How much ammo do I have?", "What's my target's hull?"
- **Says when it isn't sure.** If it can't confirm something, it says so instead of guessing.

The first version is tuned for one ship (a Fer-de-Lance) and one HUD setup. It will be generalised later.

---

## How it gets information

TACCOM uses two sources, and prefers the first whenever it can.

### 1. Game data files (reliable, fast, free)

Elite Dangerous writes live game data to:

```
C:\Users\<you>\Saved Games\Frontier Developments\Elite Dangerous\
```

| File | What it gives us |
|---|---|
| `Status.json` | Rewritten continuously. Power pips, fire group, hardpoints deployed, shields up/down, overheating, low fuel, in danger, being interdicted, fuel levels, and other flight states. |
| `Journal.*.log` | One JSON event per line. Hull damage, shield state changes, being attacked, ship targeted (including the target's shield and hull health once the scan finishes), bounties, kills, deaths, loadout, and more. |

Reading these needs no game modification. Most of the warnings in Stage 1 come from here alone.

### 2. Screen capture (for what the files don't say)

Some things only appear on the HUD: your own shield ring percentage, radar contacts, where the target is relative to you. For these, TACCOM captures small regions of the game window and analyses them:

- Fast local image processing (OpenCV) runs several times a second on gauges.
- A vision-capable AI model looks at the bigger picture less often, for harder questions.

Screen reading depends on resolution and HUD colour settings, so it is calibrated per setup and tested against recorded gameplay before it's trusted in a fight.

---

## Architecture

```
 Elite Dangerous
   │
   ├── Status.json + Journal ──► Game-state monitor ──┐
   │                                                  │
   └── Screen (HUD regions) ───► Screen analyser ─────┤
                                                      ▼
                                          Tactical engine
                                    (rules + priorities + cooldowns,
                                     AI model for open questions)
                                                      │
                     Microphone ──► Speech-to-text ───┤
                                                      ▼
                                          Text-to-speech ──► Your headset
```

Urgent warnings run locally and use fixed rules, so they never wait on a network call. The AI model is only used for things that can afford a second or two of delay, like answering a spoken question.

---

## Planned stack

| Part | Tool |
|---|---|
| Main application | Python |
| Watching game files | Polling at 10 Hz (`json`). Simpler and steadier than file-change events for a file rewritten several times a second |
| Screen capture | `dxcam` (DXGI), falling back to `mss` |
| Image processing | OpenCV |
| Text on the HUD | RapidOCR (PP-OCR models on ONNX Runtime) |
| Reasoning and vision | A vision-capable AI model (API) |
| Speech recognition | Whisper (local) |
| Spoken output | Kokoro neural voice (`kokoro-onnx`, `sounddevice`), with Windows SAPI as fallback |
| Desktop UI (later) | PySide6, or React + Tauri |

---

## Roadmap

### Stage 1 — Ship-state awareness ✅
- [x] Find and tail the newest journal file; watch `Status.json` for changes
- [x] Parse status flags and journal events into one live ship-state object
- [x] Rule-based alerts: shields down, hull thresholds, overheating, low fuel, interdiction, under attack
- [x] Spoken output with priorities and cooldowns
- [x] Log every session so alerts can be replayed and tested offline
- [x] Regression test built from a real fight (`tests/fixtures/clipper_fight_2026-10-26.log`)

### Stage 2 — Combat HUD interpretation (in progress)
- [x] Natural neural voice with instant playback for stock lines
- [x] Capture the Elite window only, and only while it is active
- [x] Calibrate HUD regions for the Fer-de-Lance at 1920×1080
- [x] Read ammunition, heat sinks, chaff, heat %, hull % and target panel text
- [x] Heat, ammo and empty-utility alerts, with two-read confirmation
- [x] Labelled frame capture during combat (`--capture`)
- [ ] Read own shield ring percentage
- [ ] Read target shield and hull bars live ("target nearly destroyed")
- [ ] Layouts for other ships
- [ ] Experimental: radar contacts and "enemy behind you" warnings
- [ ] Test every visual reader against recorded footage before enabling it live

### Stage 3 — Conversational tactical officer
- [ ] Push-to-talk voice questions
- [ ] Feed the current ship state into an AI model for tactical answers
- [ ] Advice such as pip management, weapon choice, and when to disengage
- [ ] Confidence reporting: say "I can't confirm that" rather than guess

---

## Code layout

| File | Job |
|---|---|
| `taccom/sources.py` | Find the journal folder, tail the newest journal, read `Status.json` |
| `taccom/flags.py` | `Status.json` bit flags |
| `taccom/state.py` | The live ship state, updated from both sources |
| `taccom/alerts.py` | The alert rules, priorities and cooldowns. Edit here to change what TACCOM says |
| `taccom/speech.py` | Speech queue, neural voice with on-disk cache, Windows voice fallback |
| `taccom/capture.py` | Elite window capture and labelled frame collection |
| `taccom/hud.py` | HUD layouts, the OCR reader, and the background HUD watcher |
| `taccom/app.py` | Wiring, live loop, session recording and replay |
| `taccom/__main__.py` | Command line |

---

## Design rules

1. **Accuracy over talk.** A wrong instruction in a dogfight is worse than silence.
2. **Game files first, pixels second.** Only read the screen for things the files don't provide.
3. **Local for anything urgent.** No network round-trip between danger and warning.
4. **Never controls the ship.** TACCOM advises; the pilot flies.
5. **Quiet by default.** Every alert has a priority and a cooldown.

---

## Related projects

Worth studying before building parts from scratch:

- [EDDI](https://github.com/EDCD/EDDI) — turns journal and status data into spoken reports; integrates with VoiceAttack.
- [EDCoPilot](https://www.razzafrag.com/) — a voice-driven ship assistant with route tools and information panels.

Neither reads the HUD or gives live tactical advice, which is the gap TACCOM aims to fill.

---

## Disclaimer

TACCOM is a fan project and is not affiliated with or endorsed by Frontier Developments. It only reads files the game writes for third-party tools and the image on your own screen.
