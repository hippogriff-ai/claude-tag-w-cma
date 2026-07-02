# Weekend Window — Essence Spec

A **Claude-Tag-style teammate in a Slack channel** that a few friends use to plan a weekend ride. The demo exists
to show the three things that make Claude Tag special — nothing more:

1. **Multiplayer** — one shared agent both people talk to; it holds the *group's* context, not per-person silos.
2. **Memory** — two kinds, mapped to two CMA primitives:
   - *conversation memory* — the running channel context ("Alice said Saturday afternoon") = the **CMA session**;
   - *organization memory* — durable group knowledge that survives restarts ("Bob won't ride below 40°F") = a
     **CMA memory store** the model reads/writes itself. No predefined schema; the model owns what it keeps.
3. **Async + proactive** — you ask it to watch the weather, walk away, and it **pings the channel on its own when
   the forecast changes.** (This is the hard part and the whole point.)

## The demo (one slice)

- Alice and Bob tag **@weekend-window** in a private Slack channel with their availability and a place. Locations
  live as **threads** (the humans' structure — the agent is just tagged wherever); logistics chat stays in-thread.
- It confirms a plan (mutual time + place), remembering **both** of them.
- One rider states a durable preference early (*"I won't ride below 40°F"*) — the agent applies it to a later
  forecast, **and still knows it after a bot restart** (that's the memory store earning its keep on stage).
- Someone says *"keep an eye on the weather and ping us if it changes."*
- It sets up a recurring watch and goes quiet.
- When the forecast for that plan changes (sunny → thunderstorm 3–4pm), it **posts to the thread unprompted.**
- *"stop"* cancels it.

## Stack (real, not mocked)

- **Slack** — real, via **Bolt + Socket Mode** (no public URL / ngrok). Two rider identities = two accounts made
  with `you+alice@gmail.com` / `you+bob@gmail.com` email aliases, so distinct Slack `user` IDs → real per-user
  memory.
- **CMA — the primitives ARE the demo.** This is a teaching build of Claude Tag *on Claude Managed Agents*, so
  each Tag behavior must be visibly carried by a CMA primitive (mirroring the `cwc-workshops/research-desk`
  pattern):

  | CMA primitive | Role here |
  |---|---|
  | `agents.create` (once, versioned) | the **weekend-window** agent: system prompt + the custom tools (`get_forecast` · `schedule_monitor` · `cancel_monitor` · `list_monitors`) live on the agent object |
  | `environments.create` (once) | required by sessions; **no network access needed** — weather/geocoding run broker-side |
  | `sessions.create` — **one durable session per channel** | multiplayer + conversation memory; every @mention is a `user.message` labelled with the speaker |
  | `memory_stores.create`, mounted on each channel session | organization memory — the model reads/writes notes itself; survives restarts |
  | custom-tool round-trip (`agent.custom_tool_use` → idle at `requires_action` → `user.custom_tool_result`) | how the agent reaches the outside world: the **broker** answers its tool calls |

- **The broker** (our long-lived Slack process) is the orchestrator, exactly the research-desk watcher pattern: it
  relays @mentions into the session, holds the session's event stream (reconnect + backlog catch-up), answers
  `schedule_monitor` / `cancel_monitor` tool calls by driving the local async spine, and posts `agent.message`
  text back to the thread.
- **Weather** — Open-Meteo (free, no key).
- **Async** — `schedule_monitor(location, cadence, until)` / `cancel_monitor` — custom tools the agent calls **on
  request**; the broker's timer (the async spine) is just the clockwork behind them. (Native CMA scheduled
  Deployments don't work here — a scheduled run is a fresh session with no broker; see the appendix.)
  When a watch detects a change, the broker feeds the change **into the session** as a message and the **model
  phrases the channel post** — so the agent also remembers what it last told the group, in its own context, not
  in a side ledger.

## What "changed" means

Weather is a **category**: `good/sunny`, `rain`, `hazard` (e.g. thunderstorm, with the hours), `slippery`. The
agent posts only when the category — or a hazard's hours — changes, and **never repeats the same state**. Division
of labor: the **spine** keeps the last posted state (deterministic change detection, covered by seeded tests); the
**session** keeps what was actually *said* — so the model can reference its own earlier warnings naturally, with
no separate ledger.

## Deliberately cut (was busy work for a *demo*)

Per-location subagent fan-out, **session-per-thread** (one session per *channel*; threads are just where replies
land, via `thread_ts`), geocoding precision, exactly-once / crash-safety proofs, the metric batteries, and the
bespoke web UI. Add depth only if the demo actually needs it.

## Build order

1. **The async spine** (`app/`) — recurring watch + change detection + proactive notify
   (runnable credential-free: `python run_demo.py`; seeded asserts: `python test_spine.py`).
2. Wire **real Slack** (Bolt + Socket Mode).
3. Make it **conversational** (a real model decides to call `schedule_monitor` from a natural request, attributes
   memory per speaker, composes replies) — first on the plain Messages API (`agent.py`), a **stepping stone, not
   the destination**: it uses none of the CMA primitives and stays as the offline fallback.
4. **Port the seam onto CMA** (the actual point): `provision.py` (environment + agent + memory store, once, IDs
   saved to config) → session-per-channel → `slack_app.py` becomes the broker/orchestrator answering the custom
   tools → proactive pings routed through the session so the model phrases them. Spine, tool schemas, system
   prompt, and Slack wiring carry over unchanged.

## Passing criteria

Adapted from the earlier detailed spec's criteria to the revised architecture. Design principle unchanged: **prefer verifiable
over judged** — deterministic seeded tests for the pure core, live scenarios for the CMA wiring, rubrics only for
the genuinely fuzzy parts. Written so each line is an independently gradeable criterion (usable verbatim as a
goal/outcome rubric later). M-numbers are preserved from the reference where the metric survives; dropped numbers
were cut with the architecture (UI battery, crash-safety proofs, quiet hours, per-thread subagent fan-out,
gated NL parsing).

### A. Deterministic metrics (seeded fixtures, no model, CI-able — `test_spine.py`)

| # | Metric | Definition | Target |
|---|---|---|---|
| **M2** | Classifier reproducibility | `classify(frozen_forecast)` → identical weather-state every run | bit-identical |
| **M3** | Classifier correctness | category matches hand-labeled forecasts; **0 hazards missed** | hard gate |
| **M4** | Hazard pinpointing | flagged hour-range matches the fixture (thunderstorm 15–17) | exact bounds |
| **M6/M7** | Post-on-change, never repeat | a watch posts **iff** the state changed; identical consecutive states post nothing; `good→hazard→good` = exactly 3 posts | 0 repeats, 0 missed |
| **M10** | Fault tolerance | a failing weather check logs + retries; the watch survives; other watches unaffected | 0 uncaught exceptions |
| **M15** | Cancellation | `cancel_monitor` stops the checks; no post after cancel | 0 posts after cancel |

### B. CMA-primitive criteria (the new core — each primitive visibly does its job; verify live)

| # | Primitive | Criterion (checkable) |
|---|---|---|
| **C1** | Agent object | Exactly **one** `agents.create` at provision time; re-running provisioning creates **no duplicate** (idempotent, IDs from config); system prompt + the custom tools live **on the agent object**, none passed per-request. |
| **C2** | Session per channel | One durable session per channel, **reused across turns and across broker restarts** (session id persisted); a second channel gets a **different** session; no cross-channel context leakage. |
| **C3** | Custom-tool round-trip | A watch request produces `agent.custom_tool_use(schedule_monitor)` → session idles at `requires_action` → broker answers `user.custom_tool_result` → the agent's reply reflects the tool result. **Zero permanently-unanswered tool calls** (backlog catch-up on broker reconnect). |
| **C4** | Memory store | Mounted at session create. A rider's stated preference is written by **the model itself** (no host-side write), and is recalled/applied **after a bot restart with a fresh session** — the on-stage beat. |
| **C5** | Proactive-through-the-session | A weather change is fed **into the session**; the **model phrases** the channel post (not a hardcoded template); the post appears with **no human turn**; the agent can later reference what it previously told the group. |

### C. Acceptance scenarios (end-to-end, real Slack + real CMA; each proves the tagged criteria)

| # | Scenario | Expectation | Proves |
|---|---|---|---|
| **S1** | Alice + Bob post availability | agent confirms a mutual window, referencing **both** riders' statements | multiplayer + C2 |
| **S2** | *"keep an eye on the weather there"* | `schedule_monitor` called with the right place resolved **from conversation context** | C3 + conversational tool choice |
| **S3** | Forecast changes on a watched place | **one** unprompted post, in the thread where the watch was asked; another watched place stays silent | C5 + M6/M7 live |
| **S4** | Re-check, no change | silence | M7 live |
| **S5** | Storm clears | one more post (return-to-good is a change) | M7 live |
| **S6** | *"stop"* | `cancel_monitor` called; checks cease | C3 + M15 |
| **S7** | Bot restart | same channel → session resumed or fresh session **with the memory store**; the S1 preference still known — asked by a **third rider (carol)** who never heard it, proving memory is group-shared, not per-person. (Active watches do **not** survive a restart — accepted; the rider re-asks. Crash-safety proofs stay cut.) | C2 + C4 + multiplayer |
| **S8** | Weather API error on one watch | that watch silent but alive; other watches keep posting | M10 live |

### D. Rubrics (LLM-judge + human spot-check, 1/3/5; target ≥ 4; R1–R2 release-gating)

- **R1 — Conversational, not a command bot.** `1`: only reacts to exact trigger phrases. `3`: handles weather asks
  but stilted otherwise. `5`: answers real questions (weather and general), asks a clarifying question when the
  place is genuinely ambiguous, small talk works, knows when to just reply vs. call a tool.
- **R2 — Update quality.** `1`: vague/spammy or wrong thread. `3`: right thread but missing hours or advice.
  `5`: one useful line — outlook, the bad hours, what to do ("wrap by 3") — in the right thread, never a repeat.
- **R3 — Memory-in-use (fabrication check).** `1`: ignores or invents preferences/availability. `3`: recalls but
  recites. `5`: applies stored context meaningfully ("storm only hits 3–4 and Alice's window is 12–6, so ride
  12–3") without inventing. *(No deterministic backstop — memory is model-managed.)*
- **R4 — Thread etiquette / autonomy.** `1`: hijacks the riders' logistics chatter or over-posts. `3`: mostly
  appropriate. `5`: contributes only weather, only on real change, never derails the humans.

### E. Definition of Done

1. Section A green in CI (`python test_spine.py`).
2. **C1–C5 each proven by at least one passing scenario** — this is the teaching bar: every CMA primitive in the
   §Stack table visibly does its job.
3. S1–S8 pass live (real CMA, real model, real spine, real geocoding) — `scenarios.py` runs the battery with
   scripted weather so changes happen in seconds; Slack's own event transport (humans @-mentioning in a
   workspace) is exercised by the manual walkthrough (`python slack_app.py`).
4. Rubrics ≥ 4/5; R1 and R2 gate release.

---

## Appendix — verified platform facts (don't re-derive)

Established by research + adversarial verification during design; the constraints the architecture is built around.

**Claude Tag** (the product being reconstructed): one shared Claude per channel, invoked by @tag; per-channel
memory; schedules tasks for itself over hours/days; proactive/ambient posting (gated by an "ambient" setting);
**no per-action HITL** (governed by admin allow-lists, token-spend caps, audit log); **no documented dedup or
delivery guarantee** — "post only on change, never repeat" is a correctness property this demo adds. Context pull
on mention: last 20 channel messages (channel mention) / last 50 thread messages (thread mention).

**CMA**: the custom-tool round-trip is `requires_action → agent.custom_tool_use → user.custom_tool_result`
(under the `managed-agents-2026-04-01` beta header). A memory store attaches at **session-create only** — no
hot-swap onto a running session. **No session fork** (that's an Agent-SDK concept); context editing is
Messages-API-only. `user.message` has no thread field — an in-thread @mention routes to the coordinator, so
per-thread addressing is **broker-emulated**, not native delivery. **Scheduled Deployments don't fit this demo**:
a scheduled run starts a fresh session with no connected broker, so nothing can answer the agent's custom tools
or post to Slack — hence the broker-side timer behind `schedule_monitor`.

**Slack**: Socket Mode needs no public URL. `chat.postMessage` has **no idempotency key** (a crash between post
and state-write can drop/duplicate one update — accepted). A bot can't DM two *other* humans → use a private
channel. Two rider identities = two accounts via Gmail `+alias` addresses.
