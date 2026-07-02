# Claude Tag, rebuilt on Claude Managed Agents

[![seeded tests](https://img.shields.io/badge/seeded_tests-17%2F17-brightgreen)](weekend-window/async_spine/test_spine.py)
[![live scenarios](https://img.shields.io/badge/live_scenarios_S1–S8-18%2F18-brightgreen)](weekend-window/async_spine/scenarios.py)
[![CMA](https://img.shields.io/badge/Claude_Managed_Agents-beta-b399f5)](https://platform.claude.com/docs/en/managed-agents/overview)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](weekend-window/async_spine/requirements.txt)

A minimal, runnable teaching demo of how the **Claude Tag** experience — a shared AI teammate living in a Slack
channel — is reconstructed from **Claude Managed Agents (CMA)** primitives. The scenario: two friends planning a
weekend bike ride ask **@weekend-window** to watch the weather; it pings the channel **on its own** when the
forecast changes.

Three pillars, each carried by a named CMA primitive:

| Claude Tag behavior | CMA primitive here |
|---|---|
| **Multiplayer** — one shared agent everyone talks to | one durable **session per channel** (`sessions.create`, reused across turns *and* restarts) |
| **Memory** — conversation context + durable group knowledge | the session (conversation) + a **memory store** the model reads/writes itself (survives restarts) |
| **Async + proactive** — watch requested in plain language, pings unprompted on change | **custom tools** `schedule_monitor`/`cancel_monitor` answered by a broker via the `agent.custom_tool_use → requires_action → user.custom_tool_result` round-trip; changes are fed back into the session so the **model phrases the ping** |

The agent object itself is created **once** (`agents.create`, versioned) with the system prompt and tools on it,
in an environment with **no network access** — weather and geocoding run broker-side, the "keep execution
host-side via custom tools" pattern.

On every @mention the broker performs a **conversation catch-up pull** — the unseen main-channel messages plus,
for thread mentions, the unseen thread messages — so the agent has the group's full context without being a
listener on every message. How that works (and how it differs from stock Claude Tag) is documented in
[`weekend-window/async_spine/README.md` → Context management](weekend-window/async_spine/README.md#context-management--how-main-chat-and-thread-context-are-taken-in).

## Layout

```
weekend-window/
  SPEC.md              the essence spec + passing criteria (Definition of Done: met 2026-07-01)
  SPEC-reference.md    the full earlier spec — verified CMA/Slack/memory facts, original criteria
  async_spine/         the implementation (see its README for architecture + setup)
.claude/skills/setup/  a /setup coach that walks the Slack app + token setup
```

## Quickstart

```bash
cd weekend-window/async_spine
python run_demo.py                  # no credentials: watch → change → proactive post, in seconds

# the real thing (Slack app + tokens: see async_spine/README.md or /setup)
cp .env.example .env.local          # fill SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ANTHROPIC_API_KEY
pip install -r requirements.txt
python provision.py                 # once, idempotent: CMA environment + agent + memory store
python slack_app.py                 # the broker — @mention the bot in your channel
```

## Verification (SPEC §6, all green)

- `python test_spine.py` — seeded deterministic core, 17/17 (classifier, change detection, fault tolerance, cancellation).
- Live CMA primitive checks C1–C5 — 12/12 (idempotent provisioning; session reuse across broker restarts; the
  tool round-trip; the model writing memory a fresh session recalls; model-phrased proactive pings).
- `python scenarios.py` — the S1–S8 acceptance battery + LLM-judged rubrics, run live against real CMA: 18/18,
  rubrics 5/5/5/4.

## What this demo is (and isn't)

It exists to show the **essence of Claude Tag on CMA primitives** with the simplest possible domain (free,
keyless weather data). Deliberately cut: per-location subagent fan-out, session-per-thread, crash-safety proofs,
and any bespoke UI — `weekend-window/SPEC.md` is the source of truth for scope and criteria.
