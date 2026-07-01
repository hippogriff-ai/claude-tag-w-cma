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
   `pip install -r requirements.txt` (there's also a `/setup` skill that coaches the whole thing, and
   `python setup_check.py` to verify tokens without printing them); then:

```
python provision.py     # once, idempotent: CMA environment + agent + memory store
python slack_app.py     # the broker — long-lived; run in your own terminal
python scenarios.py     # optional: the S1–S8 acceptance battery + rubric judge, live
```

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
