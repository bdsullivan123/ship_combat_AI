# TACCOM — Tactical Combat Operations Manager

An AI tactical officer for **Elite Dangerous**. TACCOM watches your ship's state during combat and talks to you: it calls out urgent warnings on its own, and answers tactical questions when you ask.

It is an **advisor, not an autopilot**. It never sends input to the game.

> **Status:** Stage 1 (ship-state alerts from game files) works. Stages 2 and 3 not started.

---

## Quick start

Needs Windows and Python 3.10 or newer.

```powershell
git clone https://github.com/bdsullivan123/ship_combat_AI.git
cd ship_combat_AI
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"

.venv\Scripts\python -m taccom
```

Start it before or during a game session. It reads the current journal silently to learn your ship, then says "TACCOM online" and starts watching. Ctrl+C stops it.

| Command | What it does |
|---|---|
| `python -m taccom` | Watch the game live, speak alerts, and record the session to `sessions/` |
| `python -m taccom --no-voice` | Same, but print alerts only |
| `python -m taccom --voice-name Zira --rate 3` | Choose a voice and speaking speed |
| `python -m taccom voices` | List the Windows voices installed |
| `python -m taccom replay <file>` | Run a recorded session or a game journal through TACCOM instantly and print what it would have said |
| `python -m taccom replay <file> --since 2026-10-26T23:38:00Z --until 2026-10-26T23:46:00Z` | Replay only one fight. Earlier events still build ship state, silently |
| `python -m taccom replay <file> --speed 1 --voice` | Replay in real time, out loud |

With no file, `replay` uses your newest game journal. Run the tests with `.venv\Scripts\python -m pytest`.

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

### What Stage 1 can't know yet

These need screen reading (Stage 2), because the game files don't carry them:

- **Your shield percentage.** The files only say whether shields are up or down.
- **Your target's live health.** `ShipTargeted` reports shields and hull when you lock on or the scan advances, not continuously.
- **Live ammunition.** The loadout records ammo once and isn't updated while you shoot.

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
| Screen capture | `dxcam` or `mss` |
| Image processing | OpenCV |
| Reasoning and vision | A vision-capable AI model (API) |
| Speech recognition | Whisper (local) |
| Spoken output | Windows SAPI through `pywin32` to start, neural voice later |
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

### Stage 2 — Combat HUD interpretation
- [ ] Calibrate HUD regions for the target resolution
- [ ] Read own shield and hull gauges from screen
- [ ] Read target info when it isn't already in the journal
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
| `taccom/speech.py` | Speech queue and Windows voice |
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
