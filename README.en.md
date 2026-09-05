# Ahem

> *Ahem.* The one in the meeting who dares to interrupt.　　[中文](README.md)

Ahem is an AI **chair** that runs a live meeting in real time. It joins a Discord voice channel, listens to every participant, decides when to speak, and then actually speaks: it cuts off whoever has been talking too long, calls on whoever has gone quiet, pulls a drifting discussion back on topic, and rules on a deadlock before time runs out.

It is not a meeting assistant. Assistants take notes, track the agenda and summarise afterwards. A chair manages the **group process**, and has the authority to rule.

![Ahem spectator view: the chair quotes what was said to pull a drifting discussion back on topic; on the right, every decision it made and the speaking share](docs/images/spectator.png)

*Spectator view replaying [`examples/hackathon-planning.events.jsonl`](examples/hackathon-planning.events.jsonl), the same meeting as the submission video, with the group-dynamics drawer opened. Only the chair is real: the four participants and every line they speak come from a script, so the chair's judgements are the only thing the recording is evidence of.*

## Why a chair

Meetings fail to converge not because nobody is taking notes, but because **nobody is willing to be the bad guy**: nobody interrupts the senior person, nobody says "that's off topic", nobody calls a decision when the room is stuck. So everyone pretends to agree, the meeting ends, and it happens again next week.

An AI has no career at stake and no face to lose. That is its structural advantage over a human chair, not "being better at notes".

Ahem has to be willing to do four things:

1. Allocate the floor and the time
2. Interrupt drift and overrun
3. Call on whoever has not spoken, and get an answer
4. Rule on a deadlock before the deadline, and say why

**It is explicitly not** a transcription tool, a post-meeting summary, a chat bot, or an assistant that only suggests. **Real time is non-negotiable**: a product that analyses the recording afterwards is not this project.

## How it works

```
Discord voice (one track per participant)
   → ElevenLabs Scribe real-time Mandarin transcript
   → two judgement paths
        fast path: pure rules, zero latency   overrun / agenda / neglected / room silence
        slow path: LLM, every 5 s             off-topic / repetition / false consensus / deadlock / factual error / floor imbalance
   → quiet gates (any one blocks speech)
        meeting winding down / STT dead / cooldown / no acceptable phrasing
   → speak: a hard interruption plays a chime first; a soft one waits for a pause and speaks directly
   → live spectator view → two records after the meeting
```

**Live transcript.** While someone speaks, the spectator view shows the sentence so far (Scribe partials, about once a second); the committed text replaces it at the next pause. Judgement only ever uses committed text, never the draft.

**The slow path is two calls.** The first only judges (three-axis scores and a type); only after the gates pass does the second write what the chair will say, and that line **must quote, verbatim, something actually said in the transcript**. If no acceptable phrasing comes back, the intervention is dropped; there is no canned fallback.

**The spectator view** is for judges and operators: every "spoke / blocked / held back" decision with its reason, a timeline, speaking share, and silent term cards: the quotes are assembled by code from the transcript, character for character; the external gloss comes from an LLM with web search, must carry a source link, and is discarded whole without one.

**Two records** are written afterwards: the meeting output (decisions, action items, open questions, positions) and the chairing record (the time, type and reason of every intervention). The second is unique to this project: it records how the meeting was steered.

## Quick start

Requirements: Python 3.11 or 3.12 (3.13 needs `audioop-lts`, listed in requirements), a Discord bot invited to your server with voice permissions, and ElevenLabs and OpenAI API keys.

```bash
git clone https://github.com/Chuanyin1202/ahem.git && cd ahem
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # set ELEVENLABS_API_KEY, OPENAI_API_KEY, DISCORD_BOT_TOKEN
```

Chair a meeting (have participants join the voice channel first, or pass the channel id):

```bash
PYTHONPATH=src .venv/bin/python -u -m meeting_host.live \
    --topic "Hackathon planning" --duration 30 --say-hello --spectator-port 8765 \
    [--channel <id>] [--keyterms term1 term2] [--phase 發散期|呻吟區|收斂期] [--auto-phase suggest|apply] [--style strict|gentle|efficient] [--no-llm]
```

The spectator view is at `http://localhost:8765`. `Ctrl-C` ends the meeting and writes the records to `meetings/`.

There are two tiers of token, and startup prints a URL for each:

| | Can do | Where it comes from |
|---|---|---|
| **Participant** | read (transcript, chair judgements, minutes) | `--view-token` / `AHEM_VIEW_TOKEN`, random per start if unset |
| **Operator** | read + switch phase + end the meeting | `--spectator-token` / `AHEM_SPECTATOR_TOKEN`, random per start if unset |

**Reads are private by default**: `GET /events` requires either token, passed in the query string as `?k=` (SSE's `EventSource` cannot set custom headers). Everything real — transcript, chair judgements, minutes — leaves through that one endpoint, so gating it gates the content. `GET /` is deliberately left open: it is an empty shell, and gating it would break refresh, because the page stores the token in `sessionStorage` and strips it from the address bar (the screen gets projected). `POST /phase` and `POST /end` accept only the operator token, via the `X-Ahem-Token` header.

How the participant token reaches people is up to you. Pasting the URL into the meeting's own Discord text chat scopes access to whoever can reach that channel — borrowing Discord's existing membership instead of building an account system. Tokens live with the process and die when the meeting ends.

`--public-read` removes the read gate entirely, for a demo where the audience is not in the Discord channel. Writes are unaffected.

> **Network exposure**: the server binds `0.0.0.0`. Behind a public domain (reverse proxy, tunnel), reads are gated by the tokens above; `--public-read` makes the full transcript world-readable, so use it only when that is the intent.

The view also works without Discord, replaying any event log:

```bash
PYTHONPATH=src .venv/bin/python -m meeting_host.spectator --replay examples/synthetic-meeting.events.jsonl --port 8765 --speed 8
```

Tests:

```bash
.venv/bin/python -m pytest tests/ -q
```

A fresh clone gives **608 passed, 21 skipped, 2 xfailed** — the figure on the badge, because it is the one anyone can reproduce. Of the skips, 17 replay a real recording and enable themselves once data is placed under `experiments/holdout/` (see [Data policy](#data-policy)); the other 4 need a live Discord connection and `MEETING_HOST_RUN_REAL_DISCORD=1`. With the holdout data in place it is 625 passed, 4 skipped, 2 xfailed.

## Where it stands

Ahem has chaired and been measured on two real Discord meetings (14 and 43 minutes, both hand-labelled). Full figures and method are in [docs/validation-results.md](docs/validation-results.md) (Chinese); the conclusions:

| Aspect | Status |
|---|---|
| Phrasing quality | Solved. After the split into two calls, 32 of 34 scoring points quote the transcript verbatim (2 of 34 before) |
| Judgement stability | **Still the main open problem.** Re-running the same scoring points five times, the chair speaks between 1 and 5 times; three judgement-prompt variants (coarser scale, explicit criteria, two-stage) each improved one recording at the other's expense: raising sensitivity raises false positives in step |
| "Should have spoken but didn't" | Half solved. One cause was a missing slot in the type list: the model decided to intervene, found no type that fit, answered "none", and was killed by its own type gate (64% of all "intervene" verdicts on the 8/31 recording went that way). Adding a "floor imbalance" type plus code-computed structural signals took the labelled monologue window from 0/5 rounds to 5/5; the price is slow-path false positives rising from a median of 3 to 5. The other half (the three axes tying and being vetoed) is unsolved |
| Housekeeping misfires | Fixed. Adjusting audio or locating a file was read as off-topic in 5 of 5 rounds; now 0 of 5, with no loss on genuine off-topic detection |
| Intervention coverage | Of the seven types, "false consensus" and "factual error" have never fired in a real meeting |
| STT failure detection | Implemented; verified offline only |
| Phase detection | First version, suggest mode; 0 spurious switches across 36 readings on two recordings that stay divergent; criteria exclude conflict aimed at the chair |

**Not done:**

- **Automatic detection of the group-process phase** (divergent / groan zone / convergent, after Sam Kaner's Diamond model), the basis of the positioning. A first detector exists (`--auto-phase suggest`: one reading a minute, two agreeing readings before a suggestion, no judgement when only one person is speaking or nobody is). It suggests by default and a person confirms in the spectator view; `apply` switches automatically. **Only negatively validated** so far (no spurious switches on recordings that stay divergent); positive validation needs a meeting that actually crosses phases.
- Chairing style presets exist as a first version (`--style`, three combinations of existing fast-path thresholds) but are **untuned**: which suits which meeting needs real-meeting measurement.

**Settled design decisions**: one chair; Mandarin only; no persona; no avatar; no local fallback (cloud services are assumed available). Reasoning in [docs/product-definition.md](docs/product-definition.md) and [docs/development-plan.md](docs/development-plan.md) (Chinese).

## Data policy

Raw transcripts of real meetings and the measurements derived from them **are not in this repository**. The few quoted excerpts and participant names in the documentation are used with the participants' consent.

**Data flow**: meeting audio goes to ElevenLabs for live transcription and speech synthesis; transcript excerpts go to OpenAI for judgement and phrasing, and term lookups additionally use OpenAI web search. All records are written only to the local `meetings/` directory; retention and deletion are up to the operator. Inform participants and obtain consent before use.

To evaluate Ahem on your own meetings: chair one to obtain `meetings/*.events.jsonl`, place it under `experiments/holdout/<case>/`, label the "should speak" and "must not speak" windows as described in [experiments/holdout/README.md](experiments/holdout/README.md), then:

```bash
PYTHONPATH=src .venv/bin/python experiments/rescore_slow_path.py experiments/holdout/<case>/meeting.events.jsonl \
    --labels experiments/holdout/<case>/labels.json --rounds 5
```

`--rounds 5` is not optional: a single run is one draw, and every stability conclusion in this project comes from repeated rounds.

## Layout

```
src/meeting_host/
  live.py             main loop: wiring, both paths, gates, events, graceful shutdown
  discord_source.py   one audio track per participant (the only wired source)   stt.py   ElevenLabs Scribe stream pool
  fast_path.py        the four fast rules                  slow_path.py  slow path: judge and phrase calls
  phrasing.py         fast-path phrase bank                hearing.py    STT failure detection
  phase.py            phase detection (LLM readings with hysteresis; suggests by default)
  style.py            chairing style presets (three fast-path threshold sets, untuned)
  speaker.py          chime, TTS, Chair state machine      glossary.py   silent term cards
  script_source.py    scripted participants, so the chair can be tested without a room full of people
  events.py           event schema (the seam between modules)   minutes.py    the two records
  spectator.py        spectator view and replay server     state.py      meeting state
examples/
  hackathon-planning.events.jsonl        the meeting in the submission video, screenshot and public demo
  synthetic-meeting.events.jsonl         event log of a fictional meeting, for replay and as a format sample
  synthetic-phases.events.jsonl          a fictional three-phase meeting with phase suggestion and switch events
  scripts/                               scripted meetings that drive the chair without STT
experiments/
  rescore_slow_path.py / score_run.py    re-scoring and window-based scoring
  score_script_run.py                    scoring a scripted run against its expected windows
  record_replay.py / make_highlight.py   film any past meeting; cut it into a highlight reel
  holdout/                               your own meeting data (not versioned)
docs/  (Chinese)
  product-definition.md    positioning: why a chair, versus Teams Facilitator
  interruption-design.md   interruption mechanics: scoring, phase awareness, the chime
  tech-architecture.md     architecture and choices
  development-plan.md      plan and completion status
  demo-runbook.md          on-site runbook: pre-flight, start, failures, shutdown (Chinese)
  validation-results.md    validation summary (current figures and per-round conclusions)
  validation-log.md        full engineering log, by verification round
  results.json             machine-readable figures
  evaluation.md            evaluation method
  prior-art.md             related research and open source
  specs/                   three design specs
  design/                  spectator design files and principles (design/README.md)
```

## Contributing and security

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Report security problems privately as described in [SECURITY.md](SECURITY.md).

## About

Built for FUTUREMODE BUILDMODE GEN-AI HACKATHON 2026 (Taipei, 4–6 September)
by team **分身有術** (T043), on the **AI for Everyday Life** track, for the
**ElevenLabs** sponsor challenge.

| Name | Role |
| --- | --- |
| Alex Huang (contact) | Core |
| 周逸達 | Front-end UI |
| Billis | Back-end UI |
| Jax | Testing |

Submission video: <https://youtu.be/fBwspxwBTws> (94 seconds, unlisted).
Live demo: <https://ahem.eighti.app> — a permanent replay of the meeting above.

License: [MIT](LICENSE). Every dependency, font and asset is listed with its
licence in the Chinese [README](README.md#第三方服務資料與素材); all 27 packages
are permissive and none are copyleft. That table is maintained in one place
rather than duplicated here, so it cannot drift between the two languages.
