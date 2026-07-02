# Weekend Window — the async spine

The essence of Claude Tag in a few hundred lines: a shared agent that, when two riders ask it to watch the weather,
**posts to the channel on its own when the forecast changes** — and never repeats the same state.

Three pillars, nothing else: **multiplayer** (one shared agent), **memory** (it remembers what it last told you),
**async + proactive** (it comes back to you unprompted).

## Run it now (no credentials)

```
python run_demo.py
```

Shows the recurring watch posting **only on change** (scripted so the change happens in seconds), then one real
Open-Meteo pull to prove the live path.

## Wire real Slack (~15 min, Socket Mode — no public URL)

1. Create a Slack app at api.slack.com/apps → enable **Socket Mode** → generate an app-level token (`connections:write`).
2. Bot scopes: `app_mentions:read`, `chat:write`, `groups:history`, `users:read`, `channels:history`;
   subscribe to the `app_mention` event; install to the workspace.
3. Two riders = two accounts via `you+alice@gmail.com` / `you+bob@gmail.com` email aliases; make a **private channel**, invite the bot + both.
4. `cp .env.example .env.local` and fill `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` + `ANTHROPIC_API_KEY`;
   install deps into a venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
   (There's also a `/setup` skill that coaches the whole thing, and `setup_check.py` verifies tokens
   without printing them.) Then, from this directory:

```
.venv/bin/python provision.py     # once, idempotent: CMA environment + agent + memory store
.venv/bin/python slack_app.py     # START THE BOT — long-lived; run in your own terminal
.venv/bin/python scenarios.py     # optional: the S1–S8 acceptance battery + rubric judge, live
```

(Activated venv or system python with the deps installed? Plain `python slack_app.py` works too.)

@-mention it in the channel with a real question — *"can you keep an eye on Central Park and tell us if it turns bad?"*
… *"when is alice free again?"* … *"never mind, stop watching"*. It reads the whole channel, decides when to watch,
and words its own replies.

## The CMA architecture (the point of the demo)

Every Claude-Tag behavior is carried by a **real CMA primitive** (`cma_broker.py`, mirroring the
`cwc-workshops/research-desk` pattern):

- **`agents.create`** (once, via `provision.py`; idempotent — IDs live in `cma_config.json`, gitignored):
  the system prompt and the two custom tools live **on the agent object**. Default model `claude-opus-4-8`;
  override with `WEEKEND_WINDOW_CMA_MODEL`.
- **One durable session per channel**, reused across turns *and broker restarts* — multiplayer + conversation memory.
- **A memory store** mounted on every channel session — organization memory: the *model* writes rider preferences
  to it with its file tools, and a brand-new session still knows them (verified live).
- **The custom-tool round-trip**: `agent.custom_tool_use` → session idles at `requires_action` → the broker runs
  geocoding + the spine and answers `user.custom_tool_result` (with backlog catch-up after a broker restart).
- **Proactive pings go through the session**: a watch's weather change is fed in as a `[weather-watch update …]`
  message and the **model phrases the channel ping** — so it also remembers what it told the group.

The agent needs **no network access** (its environment is deny-by-default) — weather and geocoding run broker-side,
the "keep execution host-side via custom tools" pattern.

Fallback chain if CMA isn't provisioned: the Messages-API brain (`agent.py`, in-process transcript) → a regex
intent-parser (no key at all). The spine (`weather.py` + `spine.py`) is identical in all three modes.

## Context management — how main-chat and thread context are taken in

The agent only *hears* anything when it's @-mentioned. So on every mention the broker performs a
**conversation catch-up pull** before relaying the turn, and composes one message for the session:

```
[in the main channel since your last look]        ← unseen CHANNEL-ROOT messages (last 20)
U0BEH8URSE9: hey hey, excited to join, you wanna go for 30mile ride this sunday @U03EM54G5K2
U03EM54G5K2: sure thing! I have time Sun 8am to 2pm
U03EA18E0UR: Maybe sun 9-11 1-3 or sat night
U03EM54G5K2: how about GW bridge?
[in this thread]                                  ← unseen THREAD messages (last 50, thread mentions only)
U03EM54G5K2: how about GW bridge?
U0BEH8URSE9: ok
[tagging you] U0BEH8URSE9: what's the weather     ← the actual mention
```

(A real composed turn from a live session — with the `users:read` scope granted, the `U…` ids become
display names, which is also what makes the model's memory notes name-keyed and legible.)

The rules, and the reasoning behind them:

- **Both scopes, always.** Claude Tag's documented pull is last-20 channel messages on a channel mention and
  last-50 thread messages on a thread mention (*thread-only* in threads). We deliberately do **more** on thread
  mentions — root catch-up **plus** the thread — because in this demo availability is discussed in the main chat
  while logistics live in threads; a thread-tagged agent blind to the root would miss half the group's context.
- **Only what's new — per-channel and per-thread cursors.** Claude Tag is stateless per invocation, so it re-reads
  history every time. Our CMA session is **stateful** (it remembers every prior turn), so re-sending history would
  duplicate context and burn tokens. The broker persists a cursor per channel root and per thread
  (`context_cursors` in `cma_config.json`) and relays only messages the session hasn't seen.
- **Filtered out:** the bot's own posts (already in the session as agent turns), other threads' replies when
  composing the root view, and the triggering mention itself (sent separately after `[tagging you]`).
- **Untagged chatter is heard late, not never:** messages sent between mentions arrive as catch-up on the *next*
  mention. The agent is not a listener on every message — only `app_mention` events wake it (by design: that's the
  Tag invocation model, and it keeps humans' chatter out of the model until relevant).
- **Slack markup is cleaned:** `<@U…>` mentions become people's names (the bot's own tag is dropped), so the model
  reads natural conversation.

The same catch-up feeds **memory**: the model decides what's durable and writes it to the mounted store itself —
e.g. a rider saying *"I don't ride in the rain"* produced an `Edit` of `/rider-preferences.md` in the session
transcript, and a brand-new session (after a restart) still knows it. The identity it memorizes is the speaker
label it saw — one more reason to grant `users:read`.

## Files

| File | What |
|---|---|
| `weather.py` | forecast → a small comparable **weather-state** (real Open-Meteo + a scripted source) |
| `spine.py` | **`schedule_monitor` / `cancel_monitor`** — recurring watch + change detection (the essence) |
| `cma_broker.py` | **the CMA port** — provisioning (agent/env/memory store) + the session broker (stream, answer tools, proactive-through-session) |
| `provision.py` | one-time idempotent CMA provisioning CLI |
| `agent.py` | Messages-API fallback brain (also the source of the shared `SYSTEM` + `TOOLS`) |
| `slack_app.py` | real Slack Bolt (Socket Mode) adapter — wires a brain to Slack + supplies the tool handlers |
| `run_demo.py` | runnable, credential-free proof |
| `setup_check.py` | no-secrets doctor: verifies `.env.local`, token shapes, and Slack auth |
| `test_spine.py` | seeded deterministic suite (SPEC §A: M2–M15) |
| `scenarios.py` | the SPEC §C/§D battery run live — S1–S8 with scripted weather + LLM-judged rubrics (real CMA, real model, real spine; Slack transport simulated) |

The stripped concept lives in `../SPEC.md`; the full, verified-facts reference in `../SPEC-reference.md`.
