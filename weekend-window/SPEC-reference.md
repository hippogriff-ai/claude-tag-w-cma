# Weekend Window — Spec

**A "Claude Tag"-style teammate for two riders planning a weekend ride: they set their availability and branch out
into a thread per candidate place; the agent watches each place's weather and keeps that thread posted.**
Reference date: 2026-07-01 · Status: spec + passing criteria (no implementation yet).

> **What this repo is for.** A minimal, *runnable* teaching demo of how the **Claude Tag** experience is
> reconstructed on top of **Claude Managed Agents (CMA)** primitives: a channel becomes a durable agent, **each
> location thread is watched by its own subagent**, the channel gets shared memory, and the agent acts
> **asynchronously** — re-checking each place's forecast and posting, *in that place's thread*, when conditions
> change.
>
> **Slack itself is deliberately stubbed out.** The focus is the technically interesting part — the CMA agent:
> sessions, threads-as-subagents, model-managed memory, agent-scheduled monitoring, and the weather logic — not the
> Slack integration plumbing. Slack sits behind a thin `SlackPort` adapter so it can be mocked for the demo and
> swapped for the real API later (see §8/§9).
>
> The domain is chosen because its logic is a **pure function of free, replayable data** (Open-Meteo, no API key)
> and **each location-thread is named for its place** (see below), so the agent never has to *infer* where. That
> makes most "did the agent do the right thing?" questions **deterministically verifiable in CI**.

**Load-bearing assumption.** **A location-thread's name *is* its place** — a real location like *Central Park* or
*Prospect Park*, not coordinates. The agent **geocodes the thread name once** to coordinates (a deterministic
lookup) and **pins** it. The coordinates are internal — the chat uses the place name. The agent never re-derives
the place from the conversation. (One-time geocoding of a named place is deterministic; per-turn inference of
"which place is this thread about" is the fragile thing we avoid.)

Throughout, each CMA capability is one of: **generally available**, **in CMA beta** (verify shapes before
building), or **plumbing you build yourself** around CMA. Which is which is stated where it matters.

---

## 1. The scenario

**`#weekend-ride`** — a private Slack channel (see [§5.1](#51-per-channel-namespace--isolation) for why it must be
private) with three members: **Alice**, **Bob**, and **@weekend-window**.

1. **Availability → a mutual window (channel level, the *when*).**
   - Alice: *"@weekend-window I'm free Sat afternoon."*
   - Bob: *"@weekend-window I've got Sat and Sun."*
   → the agent computes the **mutual window = Sat afternoon** and confirms it in the channel.
2. **Branch out into a thread per place (the *where*).** The riders open a thread for each place they're
   considering — 🧵 **Central Park**, 🧵 **Prospect Park** — and use each thread naturally: where to meet, food
   after, route details. The agent geocodes each thread's name once and pins it.
3. **Tag it with a real question — it's conversational.** `@weekend-window` is a generally-capable Claude in the
   channel; you ask real questions in plain language and it answers (it also clarifies, chats, and handles
   non-weather asks). Ask about a place **in that place's thread** and it replies **there**:
   > Bob: *@weekend-window will it rain in Central Park Sat afternoon?*
   > *(in 🧵 Central Park)* ☀️ Mostly dry and 75°F early — but a **thunderstorm 3–4pm**. I'd aim to wrap by 3.
   It also posts on its own when the forecast changes (step 4).
4. **Ask it to keep watching — then updates come on change, in the same thread.** A rider asks the agent to
   monitor (*"@weekend-window keep an eye on this hourly for Sat and ping us if it changes"*); it stands up a
   recurring check (calling `schedule_monitor` — hourly, until Saturday's ride; the rider can say "stop" anytime)
   and **posts an update only when that place's forecast meaningfully changes** (e.g. sunny → hazard), pinpointing
   the bad hours:
   > *(in 🧵 Central Park)* ⚠️ **Update:** a thunderstorm is now forecast **3–4pm Sat** — you'd want to start
   > earlier or wrap by 3.
5. Everything about a place stays **in its thread**; nothing is surfaced to the channel and no place is compared
   against another — each thread self-contains its discussion and its weather.

**The agent is conversational first.** It is a generally-capable Claude you invoke by tagging it
(`@weekend-window`) with real questions — in the channel or in a thread — and it answers naturally, asks a
clarifying question when it needs to, and handles non-weather asks too (it is not a fixed-command bot). On top of
that base sit **two specialized behaviors** the demo showcases: **(A) consensus on the *when*** — a mutual window
from the two riders' availability; and **(B) a weather watcher per place** — one subagent per location-thread that
answers weather questions asked in that thread *and* proactively posts the outlook and every meaningful change
*in that thread*. What these exercise of CMA is in [§2.1](#21-what-these-two-behaviors-exercise).

---

## 2. How "Claude Tag" is implemented by CMA features

| Claude Tag behavior | CMA feature that implements it |
|---|---|
| Tag lives in a channel and **remembers it between messages** | **One durable Session per channel** (`session_id ≡ channel_id`); an Anthropic-hosted append-only event log |
| Tag **replies in a thread**, and a thread is a focused unit of work | A **Thread inside the session**. Root thread = the **coordinator / main agent**; each **location-thread = a weather-watcher subagent** on its own child thread |
| Tag **spins up helpers** for sub-tasks | The coordinator's **`multiagent` roster** spawns **one watcher subagent per location-thread** |
| Tag **calls tools** to get real data / take actions | **Custom tools** the model requests and the **broker fulfils**: the session stalls at `requires_action`, your server runs the handler, then answers with a `user.custom_tool_result` event |
| Tag works **asynchronously "over hours or days"**, proactively | A **scheduler** appends a wake message to the **existing** durable session; the connected broker fulfils the watchers' tools and posts back (see [§3](#3-architecture)) |
| Tag has **per-channel memory** shared by everyone in the channel | A **Memory Store** scoped per channel and attached to the session — the same store attached to a channel's session makes memory shared (see [§5.1](#51-per-channel-namespace--isolation)) |

**Maturity, in plain terms.** Sessions, threads/subagents, custom-tool fulfilment (`requires_action` →
`agent.custom_tool_use` → `user.custom_tool_result`), and the memory tool are documented and confirmed in current
docs (all under CMA's `managed-agents-2026-04-01` beta header). A memory store is **attached at session creation**
and **can't be added to or removed from a running session** (documented).

**What Claude Tag gives you vs. what this demo adds.** Anthropic documents Tag as capable of **proactive and
self-scheduled posting** (ambient mode; scheduling tasks over hours or days). It documents **no de-duplication and
no delivery guarantee.** So "post a place's weather when it *changes*, and never repeat the same state" is a
correctness property **this demo implements on top of** Tag-style proactive posting — see
[§3.1](#31-notification-model--delivery-guarantee).

**Monitoring is a task the agent creates on request — not a blind loop.** A rider asks in plain language
("*@weekend-window keep an eye on Central Park hourly and ping us if it changes*"); the agent calls a
**`schedule_monitor`** tool that stands up a recurring check for that thread at the **cadence it chose**, until a
**stop time** (the ride's over) — and the rider can say "stop" → **`cancel_monitor`**. This mirrors Claude Tag
scheduling tasks for itself. The recurring *execution* is a **cron for now** (mechanism unsettled) behind that tool
— an implementation detail, not something running repetitively for no reason. Note: **CMA's own scheduled
Deployments don't work here** — a scheduled run is a fresh session with no broker, so it can't answer the agent's
tools or post back. CMA is in public beta; Claude Tag is in beta — verify SDK shapes at build time.

### 2.1 What these two behaviors exercise

✔ = genuinely exercised; ~ = trivial/indirect; ✗ = not exercised.

| CMA primitive | Behavior A (mutual window) | Behavior B (per-place watchers) |
|---|---|---|
| Durable session per channel | ✔ receives the @mention turn | ✔ woken with no human turn; restart-durable |
| Thread / subagent | ✔ coordinator handles it on the root | ✔ **one watcher subagent per location-thread** |
| Multiagent roster | ~ single step | ✔ **fan-out** — one independent watcher per place (no reduce; each thread self-contained) |
| Broker-fulfilled custom tools | ✔ `record_availability`, `compute_mutual_window` | ✔ `register_location`, `poll_weather`, `post_update` |
| Async scheduled wake | ✗ | ✔ scheduler → existing session |
| Mounted per-channel memory store | ✔ writes members/*, mutual window | ✔ reads locations + writes per-thread weather state |
| **Session resume / reconnect** | ✗ | ✔ built-in CMA property; used by the restart test (S5) |
| **HITL tool-confirmation** (`user.tool_confirmation`) | ✗ | ✗ — a real CMA primitive, **deliberately not used** (nothing here is destructive or spends money) |

**Not CMA capabilities at all** (so not omissions): **session fork** is a Claude *Agent SDK* concept, not a CMA
feature; **context editing** is a Messages-API feature, not a CMA-session one.

---

## 3. Architecture

Monitoring is a **task the agent creates on request**: when a rider asks it to watch a place, the agent calls
`schedule_monitor` (choosing the cadence — e.g. hourly — and a stop time) and `cancel_monitor` to stop. Behind that
tool a scheduler re-triggers the agent's **existing** channel session on cadence — **a cron for now** (mechanism
unsettled; §2). CMA's native scheduled Deployments **don't fit** (a scheduled run has no broker to answer the
agent's tools or post back), so the timer lives on our side.

```
        ┌──────────── SLACK (private channel) ─────────┐
        │  # weekend-ride            Alice · Bob · ✳W  │  app_mention / message events  ─► INGRESS ─► broker
        │  ── availability → mutual window ──          │  ◄─ chat.postMessage(thread_ts) ─  SLACK POSTER
        │  🧵 Central Park     🧵 Prospect Park        │  (each agent update posts INTO its location thread)
        └──────────────────────────────────────────────┘
                     ▲  weather update posted in a location thread (no human turn)
                     │
   ┌─────────────────┴──────────── BROKER (always-on server) ──────────────────────────┐
   │  • channel_id → session_id     • fulfils requires_action (custom tools)            │
   │  • location-thread ↔ {slack_thread_ts, cma_thread_id, lat, lon}                     │
   │  • no dedup ledger — the MODEL tracks "what I last told each thread" in its memory  │
   └───┬───────────────────────┬───────────────────────┬───────────────────────────────┘
       │ create/append/stream   │ answer custom tools    │ read/write memory
       ▼                        ▼                        ▼
 ┌───────────────┐   ┌──────────────────────┐   ┌────────────────────┐
 │  CMA SESSION  │   │  CUSTOM TOOLS         │   │  MEMORY STORE      │
 │  coordinator  │   │  poll_weather ──────► │   │  (per channel)     │
 │  (root thread)│   │    ↳ classify_wx()    │   │  MODEL-MANAGED —   │
 │   ├ watcher:  │   │       + Open-Meteo    │   │  no fixed schema:  │
 │   │  Ctrl Pk  │   │  record_availability  │   │  availability,     │  ← the model decides
 │   └ watcher:  │   │  compute_mutual_window│   │  mutual window,    │    what to keep &
 │     Prospect  │   │  register_location    │   │  per-place notes,  │    how to organize
 │               │   │  post_update (on chg) │   │  last-told state   │
 └───────────────┘   └──────────────────────┘   └────────────────────┘
       ▲  a cron re-triggers the EXISTING session to re-check (rider asked it to monitor)
   ┌───┴────────────────┐
   │ CRON (for now,     │   (native CMA Deployments don't work — no broker on a
   │ user-requested)    │    scheduled run; re-fire mechanism unsettled — see §2 / §10)
   └────────────────────┘
```

**In-thread tagging routes to the coordinator.** CMA has no way to inject a free-form user turn into a child
(watcher) thread — `user.message` has no thread field and always lands on the primary/coordinator thread; the only
per-thread writes are `user.interrupt` and correlation-routed tool results. So when a rider @-mentions the agent
*inside* a location-thread, the broker appends it as a session-level `user.message` to the coordinator with the
Slack `thread_ts` + the thread's location binding as context, and maps the watcher's outbound update back into that
Slack thread via `location-thread ↔ {slack_thread_ts, cma_thread_id, lat, lon}`. Per-thread addressing is
**broker-emulated, not native CMA delivery** — mirroring Claude Tag, where a thread is a turn container, not a
separately-addressable agent. Riders' own chatter in a thread (food, logistics) needs no agent action; the agent
only contributes weather.

### 3.1 Notification model

**Post a place's weather when it changes, never repeat.** For a `(location, mutual_window)` the agent computes a
discretized **weather-state** — an outlook category plus any hazards with hour-ranges: `sunny`/`good`, `rain`,
`hazard` (e.g. thunderstorm 3–4pm), `slippery`. It **reads its own memory** for what it last told that thread and
posts **in the thread** only if the state changed. Identical consecutive states post nothing; `sunny → hazard
(storm 3–4) → sunny` is two meaningful posts.

Because the recurring check is a **single cron run per schedule** (sequential, not concurrent) and **the model owns
this state in its memory** (§5), a memory note is enough — no separate broker ledger. This is **effectively-once by
the model checking its memory**, not a hard guarantee: Slack has no idempotency key, so a crash between posting and
recording the new state could drop or repeat one update. Accepted for now; heavy concurrency would need a stronger
idempotency store (out of scope).

**Two ways the agent is triggered:**
- **Tag-triggered — a human @-mentions `@weekend-window`** (`app_mention` is the ingress). In the **channel** it
  records availability and computes the mutual window (Flow A). In a **location-thread** it replies **in that
  thread** with that place's weather for the mutual window. (Recall §"in-thread tagging": the mention routes to the
  coordinator with the thread's location binding as context; the reply is mapped back into that Slack thread.)
- **Recurring — set up when a rider asks the agent to monitor** (Flow B), then it re-fires with no human turn (a
  cron for now; native Deployments don't work — §2). On each run the coordinator's per-location watcher calls
  `poll_weather` (→ `classify_wx`) and, **only if that place's weather-state changed vs. what it last told the
  thread (from its own memory)**, posts an update **in that thread**.

---

## 4. Tools (custom tools the broker fulfils)

| Tool | Purpose | Key inputs | Output | Pure? |
|---|---|---|---|---|
| `record_availability` | Persist one rider's dated availability | `channel_id, user, target_weekend, windows[]` | normalized windows | yes (over memory) |
| `compute_mutual_window` | Intersect riders → the shared window | `channel_id, target_weekend` | `mutual_window`, `both_submitted:bool` | yes |
| `register_location` | Geocode a location-thread's **name** to coordinates once and pin it (with a confirm) | `channel_id, thread_id, thread_name` | `{name, lat, lon}` stored | no (one geocoding lookup) |
| `classify_wx` | **Pure:** turn a forecast into a discretized weather-state (outlook + hazards w/ hour-ranges) | `forecast, date, window, prefs` | `{outlook, hazards[], summary}` | **yes — no I/O, clock, RNG** |
| `poll_weather` | Fetch Open-Meteo for a location's coords over the window, then classify | `channel_id, thread_id, date, window` | `classify_wx` output; `{isError:true}` on failure | no (network) |
| `post_update` | Post a weather update **in a location's thread** (the agent calls it only when its memory says the state changed) | `channel_id, thread_id, weather_state, text` | `{sent}` | no (posts) |
| `schedule_monitor` | **Agent-created on request:** stand up a recurring check for a thread at a cadence, until a stop time | `channel_id, thread_id, cadence, until` | `{monitor_id}` | yes (registers a job) |
| `cancel_monitor` | Tear down a monitor the rider no longer wants | `channel_id, thread_id \| monitor_id` | `{cancelled:bool}` | yes |

`register_location` geocodes the thread's name **once** at creation, so the watcher reads pinned coordinates rather
than re-deriving a place from chat. `poll_weather` reads those coordinates from the binding. The agent calls
`post_update` **only when its memory says that thread's weather-state changed**, and every post is routed to
`thread_id`, never the channel root. **`schedule_monitor` / `cancel_monitor`** let the agent stand up and tear down
a recurring watch **on a rider's request** — the cadence (e.g. hourly) and stop time are the agent's choice, so
nothing runs repetitively without a reason, and the rider can stop it anytime.

**Generic conversation is not a tool.** Tagging the agent with a general, clarifying, or non-weather question is
answered by the model **directly** — no custom tool — so it behaves like the real (conversational) Claude Tag, not
a fixed-command bot. The tools above are the *specialization* the model reaches for when a question is actually
about a place's conditions. (An optional `web_search` MCP tool can back general questions; out of scope for v1's
verifiable core.)

---

## 5. Memory model (per-channel = the "org")

The agent has a **per-channel memory store** — the shared "org" memory for that channel's members, via the memory
tool. **What to remember and how to organize it is the model's job:** it decides what to keep (each rider's
availability, the mutual window, per-place notes, what it last told a thread, standing preferences…) and how to
structure it. We **do not predefine a schema or file layout** — prescribing one would fight the model. The only
thing we define is the store's **scoping and isolation** (below), which is infrastructure, not content.

One consequence worth stating: the "have I already told this thread about the storm?" state lives in the model's
memory too — the agent reads its own notes to judge whether a weather-state is new before posting (§3.1), rather
than us maintaining a separate ledger. (A stronger idempotency store would only be needed under heavy concurrency —
out of scope here, since the async path is a single cron run per schedule.)

### 5.1 Per-channel namespace & isolation

Every Slack channel maps to exactly one CMA session, one memory store, and one thread namespace, keyed by the Slack
`channel_id` (a stable `C…`/`G…` id):

| Axis | Key | Scheme | Isolation guarantee |
|---|---|---|---|
| **Session** | `channel_id → session_id` | 1:1, in the broker State DB; created once on bot-join | one durable event log per channel |
| **Memory store** | `channel_id` | **Server-side rooting:** the model sees a plain `/memories/…`; the store is rooted at `/data/memories/slack/<channel_id>/`. Attaching the same store to the session makes memory shared. | **cross-channel leakage is structurally impossible** |
| **Location-thread ↔ CMA thread** | `request_id ↔ {slack_thread_ts, cma_thread_id, lat, lon}` | bound **by request in the broker**; each thread carries its geocoded place | updates route back to the right Slack thread; the watcher reads its bound coordinates |

**Isolation invariant:** memory, session events, and thread routing for channel *C* can never observe or affect
channel *C'*. **Developer duties:** a **path-traversal guard** rooted at the channel dir (reject `../`, `..\`,
`%2e%2e%2f`); size caps; never persist secrets. **Why a private channel, not a DM:** a bot can't post into a DM
between two *other* humans, and a 2-person IM can only be one human + bot — so it must be a private channel (or
`mpim`).

---

## 6. Passing criteria

Design principle: **prefer verifiable over judged.** The mutual window is set intersection, weather classification
is a pure function of a frozen forecast, and per-thread dedup is a state comparison — so most acceptance questions
are answered by a deterministic test over a **replayable fixture**, not an LLM judge. Rubrics ([§7](#7-rubrics))
cover the fuzzy parts.

**Scope of these metrics.** M1–M13 validate the **deterministic pure functions** (`classify_wx`,
`compute_mutual_window`, geocoding) over committed fixtures — independent of the model. The agent itself runs on
**real CMA** (a real model), so its natural-language understanding, tool-selection, and conversation are evaluated
by the rubrics (R1–R7) and integration, **not** by these unit metrics. The one place we score NL parsing on a
labeled set is **M14 (gated)** — availability extraction (place is never inferred; the thread name is the place).
The **conversational capability** (the base "Claude Tag" behavior) is judged by R3/R7.

### 6.1 Quantifiable / verifiable metrics

| # | Metric | Definition | Target | How it's verified |
|---|---|---|---|---|
| **M1** | Mutual-window correctness | computed window == true intersection of riders' availability | exact, 100% | unit test over labeled fixtures |
| **M2** | Classifier reproducibility | `classify_wx(frozen_forecast)` → bit-identical weather-state every run | bit-identical | run twice on a snapshot (no clock/RNG) |
| **M3** | Classifier correctness | outlook + hazards match a hand-labeled forecast set (incl. the storm's hour-range) | **hard gate: 0 hazards missed**; ≥ 0.95 on outlook | confusion matrix vs frozen labels |
| **M4** | Hazard pinpointing | a hazard's flagged hour-range matches the fixture (e.g. thunderstorm 15–16) | exact bounds | assert hazard windows vs fixture |
| **M5** | Location → coordinates fidelity | each watcher fetches the coordinates bound to **its** thread (never another thread's) | 100% | assert `poll_weather` args == `locations/<id>` coords |
| **M6** | Per-thread routing | every `post_update` lands in **its** location-thread, never the channel root or another thread | 100% | assert post `thread_ts` == the location's `thread_ts` |
| **M7** | Post-on-change, no repeat | a thread's update posts **iff** its weather-state changed; identical consecutive states post nothing | 0 repeats, 0 missed changes | replay a per-place weather timeline; assert post set == change set |
| **M8** | Crash-safety / idempotency | after a restart — incl. a crash **between posting and recording the new state in memory** — no weather-state is posted twice in a thread | 0 double-posts | inject crash after post/before memory-write; on re-run the last-recorded state governs (v1 accepts ≤1 missed update — §3.1) |
| **M9** | Fact fidelity (tool outputs) | the deterministic facts — the computed mutual window and each thread's geocoded coords — are correct and used as-is | 100% | assert tool outputs vs fixtures (free-form memory content is judged by R5, not asserted) |
| **M10** | Fault tolerance | on Open-Meteo timeout/5xx for one thread, `poll_weather` returns `{isError:true}`; that thread posts nothing; other threads continue | uncaught exceptions = 0 | fault-injection on one thread |
| **M11** | Update latency | detection→post = 0 extra wake cycles (posted in the same wake) unless deferred by quiet hours | 0 extra cycles (or a logged defer) | event timestamps |
| **M12** | Bounded work | custom-tool calls per wake ≤ (#location-threads) | within bound | count tool-use events per wake |
| **M13** | Quiet-hours compliance | no proactive post in configured quiet hours; deferred posts fire at the next allowed time | 0 violations | wake timestamp in quiet hours → assert queued |
| **M14** *(gated — real-LLM path)* | Availability-extraction faithfulness | NL message → structured availability matches a hand-labeled parse | ≥ 0.9 F1 | labeled NL fixtures; **not required for v1** |

**Acceptance scenarios** (each must pass; tagged with the CMA feature it proves):

| Scenario | Expectation | Proves / bound to |
|---|---|---|
| **S1** Both submit availability | mutual window computed + confirmed in channel | consensus + memory (M1) |
| **S2** No overlap | agent says "no shared window" + proposes alternatives | graceful logic |
| **S3** First outlook per thread | each location-thread gets one outlook post for the window | fan-out + per-thread routing (M5/M6) |
| **S4** Weather changes in one thread | a storm appears in Central Park → one update **in that thread**; Prospect Park unaffected → silent | post-on-change + isolation (M4/M6/M7) |
| **S5** Restart mid-watch | no duplicate post in any thread; mutual window + locations intact | durability + idempotency (M7/M8) |
| **S6** Open-Meteo error on one thread | that thread stays silent; other threads still post | fault tolerance (M10) |
| **S7** Storm clears again | the return-to-good is one new post (state changed back) | post-on-change (M7) |
| **S8** Crash between posting and recording state | re-run does not double-post | memory-state protocol (M8/§3.1) |
| **S9** Two location-threads concurrently | each fetched with its own coords; each posts only in its own thread | fan-out fidelity (M5/M6) |

### 6.2 Definition of Done (initial version)

- **M1–M13** green in CI against committed fixtures (M14 gated; not required for v1).
- **S1–S9** pass end-to-end in the runnable demo.
- Each of the six primitives in the §2 map is exercised by ≥ 1 passing scenario (async via the cron; native CMA
  scheduled Deployments out of scope).
- Rubric dimensions ([§7](#7-rubrics)) scored ≥ 4/5; **R1 and R4 are release-gating**.
- **UI checks U1–U8** pass in replay mode; **UI rubrics V1–V5** scored ≥ 4/5 (**V3, V5 demo-critical**) — see §8.

---

## 7. Rubrics (non-quantifiable aspects)

**1 / 3 / 5** with anchors, scored by an LLM-judge (pinned model + prompt) on the demo transcript, spot-checked by
a human. Deterministic backstops noted.

**R1 — Weather-update quality.** `1`: vague/spammy, wrong thread, or fires in quiet hours. `3`: clear and in the
right thread but missing the hour-range or over/under-communicates. `5`: one useful line — outlook, the bad hours
(e.g. "storm 3–4pm"), and what to do — in the correct thread, never a repeat. *Backstops: M4, M6, M7, M13.*
**Release-gating.**

**R2 — Mutual-window communication.** `1`: reports a non-shared window or no rationale. `3`: correct, thin
explanation. `5`: shows the shared window, references each rider's stated availability, and on no-overlap proposes
concrete alternatives. *Backstop: M1.*

**R3 — "Feels like Claude Tag" (channel-native).** `1`: acts like a slash-command bot. `3`: threads and remembers
but rarely proactive. `5`: replies in the right thread, remembers across days, and **proactively** posts when a
place's weather changes.

**R4 — Autonomy & thread etiquette.** `1`: acts with no basis, or hijacks the riders' food/logistics chatter. `3`:
mostly appropriate; occasionally over-posts. `5`: contributes weather to a thread **without derailing** the humans'
discussion, and only when there's a real change. **Release-gating.**

**R5 — Personalization / memory-in-use.** `1`: ignores or fabricates prefs. `3`: recalls but recites. `5`: uses
stored context meaningfully ("you both said afternoons — the storm only hits 3–4, so 12–3 is clear") without
inventing. *(No deterministic backstop — memory is model-managed/free-form; this rubric is the fabrication check.)*

**R6 — Graceful degradation.** `1`: silent/misleading on a washout or API outage. `3`: reports it unhelpfully.
`5`: honest and specific ("Central Park is wet all afternoon; Prospect Park has a clear 1–3pm"). *Backstop: M10.*

**R7 — Conversational quality (generic).** `1`: rigid/command-like — only reacts to exact weather triggers, fumbles
or ignores real questions. `3`: answers weather questions but is stilted on anything else. `5`: converses naturally
— answers real questions (weather *and* general), asks a clarifying question when it needs to, handles small talk,
and knows when to just reply vs. when to call a weather tool. *(Real-LLM path; judged, not asserted.)*

---

## 8. Demo UI (interaction & observation surface)

The UI lets one operator **role-play both riders**, converse with the agent, control time, and **watch the agent's
state live** — it drives and verifies the demo without a real Slack workspace. It is one implementation of the
`SlackPort` (production Slack is another) and holds **no agent logic**: it renders channel state and emits *member
messages* and *scheduler ticks*.

**Run modes** (same UI, different backend): **chat** — a real model interprets free-form messages; **replay** — a
deterministic scripted timeline the automated UI checks assert against.

### 8.1 Layout

```
┌────────────┬─────────────────────────────────────────────────┬──────────────────────────────┐
│ WEEKEND    │  # weekend-ride               Alice · Bob · ✳W   │  What @weekend-window knows  │
│ WINDOW     │ ────────────────────────────────────────────────│ ─────────────────────────────│
│            │  Alice · 09:14  I'm free Sat afternoon           │  Availability                 │
│ Channels   │  Bob · 09:15    Sat and Sun for me               │   Alice Sat PM · Bob Sat/Sun  │
│ #weekend-  │   ✳ mutual window: Sat 12:00–18:00               │  Mutual window                │
│   ride     │                                                  │   Sat 12:00–18:00             │
│            │  ─ open a thread per place ─                     │  Location threads (watchers)  │
│ Threads    │  🧵 Central Park                                 │   Central Park  ⚠ storm 3–4pm │
│ 🧵 CentrlPk │     Bob: tacos after? 🌮                         │   Prospect Park ☀ clear       │
│ 🧵 Prospct  │     ✳ ☀ Sat PM looks good — 75°F, light wind    │  Last posted / thread          │
│            │     ✳ ⚠ update: thunderstorm 3–4pm — wrap by 3   │   CentrlPk: storm3-4 · Prospt:ok│
│ Members    │  🧵 Prospect Park                                │  Event log                    │
│  ● Alice   │     Alice: flatter, good for a recovery spin     │   poll CtrlPk → storm (post)  │
│  ● Bob     │     ✳ ☀ Sat PM clear — 74°F                      │   poll Prospt → clear (post)  │
│  ✳ weekend │ ┌ clock: Thu → [advance to Sat AM re-scan] ───┐  │   re-poll → no change (quiet) │
│  [reset]   │ │ [Run weather scan]  weather:(•)Live ( )Storm │  │ ── maps to CMA ──             │
│  mode:chat │ └──────────────────────────────────────────────┘  │  channel = session           │
│            │ ┌ posting as: [ Alice | (Bob) ]  [in: 🧵CtrlPk]┐  │  🧵 location-thread = subagent│
│            │ │ type a message…                       [Send] │  │  panel = memory store        │
│            │ └──────────────────────────────────────────────┘  │                              │
└────────────┴─────────────────────────────────────────────────┴──────────────────────────────┘
```

- **Left rail:** channel, the **location-threads**, member roster, reset, mode badge.
- **Center:** the channel (availability → mutual window at the root) and, inside each **location-thread**, the
  riders' own chatter **plus** the agent's weather posts for that place; a scheduler/time strip; a composer with an
  **identity switcher** *and a thread selector* (post as Alice/Bob into a chosen thread).
- **Right — "What @weekend-window knows":** a live read-only mirror of the model's memory (availability, mutual window, per-place notes, last-told state).

### 8.2 Components
- **Identity + thread selector — the core primitive.** `posting as [ Alice | Bob ]` and `in [thread]`; every
  message has one author and one thread. One operator role-plays both riders across threads.
- **Location-threads.** Each thread's **name is its place (📍)**; it shows the riders' discussion and the agent's
  weather posts for that place (coordinates live in the panel, not the chat). The agent posts **only** into the
  relevant thread.
- **Scheduler / time control.** Virtual clock + **Run weather scan** + a weather source toggle (**Live / inject a
  storm / washout**) so the outlook, a mid-window change, and the no-repeat behavior are all demonstrable.
- **Knowledge panel.** Availability · mutual window · per-location weather-state + last-posted · event log · a
  legend (channel = session · 🧵 location-thread = subagent · panel = memory store).

### 8.3 States the UI renders (each maps to a §6 scenario)

| UI state | What the operator sees | Scenario |
|---|---|---|
| One rider submitted | agent "waiting for <other>"; no mutual window yet | S1 |
| Both submitted | mutual window shown at channel root | S1 |
| First outlook | each location-thread gets one outlook post | S3 |
| Storm injected in one place | one update **in that thread**; the other thread stays silent | S4 |
| Re-scan, no change | no post; log shows "no change" | S4/S5 |
| Storm clears | one new post in that thread (state changed back) | S7 |
| One thread's API errors | that thread silent; the other still posts | S6 |

### 8.4 Visual direction
Clean, modern, lightly outdoors-themed — calm neutrals with one fresh accent, rounded initial avatars in per-member
colors, generous spacing, a clear type scale. Agent posts inside a thread read as a helpful teammate, not a
takeover. Original, polished channel UI, not a Slack reskin. Accessible: contrast, keyboard send, visible focus,
authorship announced.

### 8.5 UI passing criteria

**Verifiable (replay mode — DOM / state assertions):**

| # | Behavior | Target | How it's verified |
|---|---|---|---|
| **U1** | Attribution | a message sent as Alice renders under Alice and updates *alice's* state only | assert author + member keys |
| **U2** | Wait-for-both | after one rider submits, a "waiting" state shows; no mutual window | assert panel state |
| **U3** | Mutual-window fidelity | the panel's window equals the true intersection (M1) | compare to computed intersection |
| **U4** | Thread routing of agent posts | each agent weather post renders **inside its location-thread**, never the channel root (M6) | assert DOM parent == that thread |
| **U5** | Post-on-change visibility | a re-scan with no change → no new post + a "no change" log entry (M7) | count posts + log entries |
| **U6** | Clock control | advancing the clock triggers a scan and adds a poll event to the log | assert clock text + log entry |
| **U7** | Panel fidelity | availability / mutual window / per-thread weather-state equal the broker state | diff panel vs state snapshot |
| **U8** | Error resilience | a forced fetch error on one thread → that thread silent, UI interactive | inject error; assert no crash |

**Rubrics (1 / 3 / 5):** **V1** feels like a real channel · **V2** weather-post clarity (outlook + bad hours) ·
**V3** observability · **V4** visual polish · **V5** role-play ergonomics (switch rider *and* thread easily). Target
≥ 4/5; **V3 and V5 gate the demo**.

---

## 9. Scope — what's real vs simulated

| Piece | Initial version |
|---|---|
| Open-Meteo data + `classify_wx` | **Real** (free, no key) — the verifiable core |
| Mutual window + weather classification | **Real** deterministic pure functions + fixtures (memory itself is **model-managed** — §5) |
| Location = the thread name | geocoded **once** at thread creation (deterministic + confirmed); never inferred from chat |
| "Don't repeat" state | The **model tracks it in its own memory** (§5) — no separate ledger; fine for a single cron run per schedule; heavy concurrency would need a stronger store (out of scope) |
| Async wake | A **cron** (user-requested monitoring); re-fire mechanism unsettled (§2/§10). Native CMA scheduled Deployments **don't work** (no broker on a scheduled run) |
| CMA session/thread/tool-fulfilment | **Real CMA** — the demo targets real sessions, threads, and custom-tool fulfilment; verify SDK shapes at build |
| Slack ingress/poster | **Stubbed** — prints a channel + threads transcript with `thread_ts` routing so per-thread posting is assertable |
| Agent tool-selection reasoning | Scripted per thread-type; a note marks where a real CMA model drives it |

**Reality check.** The demo runs on **real CMA**; the pieces still to harden are a **settled recurring-trigger** (a
cron works for now) and — only under heavy concurrency — a stronger idempotency store than the model's memory.
Slack can be the real API or the stubbed `SlackPort` the UI harness uses.

**Explicitly out of scope for v1:** native CMA scheduled Deployments (they don't work here), HITL
tool-confirmation, location inference from chat (the thread name is the place), **the agent opening/creating
threads** (riders open them), cross-thread comparison / "best place" recommendation, multi-channel scaling,
booking/payment, anything beyond a weekend ride.

---

## 10. Decisions & open questions

**Decided:**
- **Run on real CMA** — real CMA is the whole point; the demo targets it directly (not a simulation). The pure
  functions (`classify_wx`, `compute_mutual_window`) still unit-test without credentials.
- **Async trigger: a cron for now** — user-requested monitoring; the re-fire mechanism is unsettled and we use a
  cron. Native CMA scheduled Deployments **don't work** (no broker on a scheduled run).
- **Weather-state granularity** — a "meaningful change" is a transition between categories: `good`/`sunny` ↔
  `rain` ↔ `hazard` (e.g. thunderstorm) ↔ `slippery`, or a hazard appearing/clearing within the window. Nothing
  finer.
- **The agent does NOT open threads** — riders open location-threads; the agent only participates in threads that
  already exist. (Out of scope.)
- **Memory is model-managed** — the model decides what to remember and how; no predefined schema (§5).

**Still open:**
1. **Quiet-hours defaults** — concrete values feeding M13/R1/R4.
2. **Threads in the UI** — full collapsible threads, or a simplified single-level affordance for v1?
