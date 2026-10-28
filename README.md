# TACCOM — Tactical Combat Operations Manager

An AI tactical officer for **Elite Dangerous**. TACCOM watches your ship's state during combat and talks to you: it calls out urgent warnings on its own, and answers tactical questions when you ask.

It is an **advisor, not an autopilot**. It never sends input to the game.

> **Status:** planning. No code yet.

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
| Watching game files | `watchdog` + `json` |
| Screen capture | `dxcam` or `mss` |
| Image processing | OpenCV |
| Reasoning and vision | A vision-capable AI model (API) |
| Speech recognition | Whisper (local) |
| Spoken output | Windows TTS to start, neural voice later |
| Desktop UI (later) | PySide6, or React + Tauri |

---

## Roadmap

### Stage 1 — Ship-state awareness
- [ ] Find and tail the newest journal file; watch `Status.json` for changes
- [ ] Parse status flags and journal events into one live ship-state object
- [ ] Rule-based alerts: shields down, hull thresholds, overheating, low fuel, interdiction, under attack
- [ ] Spoken output with priorities and cooldowns
- [ ] Log every session so alerts can be replayed and tested offline

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
