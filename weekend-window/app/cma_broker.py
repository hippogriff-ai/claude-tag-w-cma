"""
cma_broker.py — the CMA port: real Claude Managed Agents primitives (SPEC C1–C5).

Mirrors the cwc-workshops/research-desk pattern (provision.ts + orchestrator.ts):

  · provision()        — ONCE, idempotent: environments.create + agents.create (system
                         prompt + the two custom tools live ON the agent object) +
                         memory_stores.create. IDs persisted to cma_config.json.   (C1)
  · Broker.run_turn()  — one durable session per Slack channel, reused across turns
                         and restarts (session id persisted; memory store mounted
                         at session create).                                       (C2, C4)
                         Stream-first drain loop: `agent.custom_tool_use` → the broker
                         runs the handler → answers `user.custom_tool_result` on the
                         same stream; collects `agent.message` text for Slack.     (C3)
  · Broker.proactive_update() — a weather change is fed INTO the session as a
                         "[weather-watch update …]" message and the MODEL phrases
                         the channel ping (and remembers what it said).            (C5)

The spine (weather.py / spine.py) is untouched: the broker answers schedule_monitor /
cancel_monitor by driving it, exactly the "keep execution host-side via custom tools"
pattern. Requires ANTHROPIC_API_KEY (beta header is set by the SDK automatically).
"""
from __future__ import annotations

import asyncio
import json
import os

from agent import SYSTEM, TOOLS

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cma_config.json")

# The CMA agent is the star of the demo — default to Opus; override for cheaper runs
# (e.g. WEEKEND_WINDOW_CMA_MODEL=claude-haiku-4-5).
CMA_MODEL = os.environ.get("WEEKEND_WINDOW_CMA_MODEL", "claude-opus-4-8")

# Extends the shared persona for the CMA substrate: durable memory + the watch-update convention.
CMA_SYSTEM = SYSTEM + (
    "\n\nYou have a persistent memory directory mounted for this channel — it is the group's "
    "durable memory across conversations and restarts. When a rider states a lasting preference "
    "(e.g. 'I won't ride below 40°F'), a standing constraint, or availability worth keeping, "
    "write a short note there with your file tools, and check your notes when they'd change an "
    "answer. Keep notes small; organize them however you see fit.\n\n"
    "Messages that begin with \"[weather-watch update\" are results from YOUR OWN weather watch, "
    "not a human. Reply with one short channel ping: what changed, the bad hours if any, and what "
    "the riders should do. Don't address anyone as if they sent it — just write the ping.\n\n"
    "Some turns start with catch-up blocks — \"[in the main channel since your last look]\" and/or "
    "\"[in this thread]\": that's what the riders said in Slack while you weren't tagged. Read them "
    "as conversation you were present for, then respond to the tagged request after \"[tagging you]\"."
)


# ── config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ── provisioning (C1 — once, idempotent; re-runs create nothing new) ─────────
def _agent_tools() -> list:
    return [
        {
            # file tools for the memory mount; the agent needs no network (weather runs broker-side)
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {"name": "web_fetch", "enabled": False},
                {"name": "web_search", "enabled": False},
            ],
        },
        *[{"type": "custom", **t} for t in TOOLS],   # schedule_monitor / cancel_monitor
    ]


async def provision(client) -> dict:
    """Create environment + agent + memory store once; reuse IDs from cma_config.json."""
    cfg = load_config()

    if not cfg.get("environment_id"):
        env = await client.beta.environments.create(
            name="weekend-window-env",
            config={
                "type": "cloud",
                # deny-by-default egress: the agent itself never touches the network
                "networking": {"type": "limited", "allowed_hosts": [],
                               "allow_package_managers": False, "allow_mcp_servers": False},
            },
        )
        cfg["environment_id"] = env.id
        save_config(cfg)
    print(f"   environment  {cfg['environment_id']}")

    if not cfg.get("agent_id"):
        agent = await client.beta.agents.create(
            name="weekend-window",
            model=CMA_MODEL,
            system=CMA_SYSTEM,
            tools=_agent_tools(),
        )
        cfg["agent_id"] = agent.id
        cfg["agent_version"] = getattr(agent, "version", None)
        save_config(cfg)
    else:
        # sync: if the local tool set or system prompt drifted, publish a new agent
        # version and retire the existing channel sessions (they pin the old version;
        # the memory store carries the durable knowledge into the fresh ones).
        remote = await client.beta.agents.retrieve(cfg["agent_id"])
        remote_tools = {getattr(t, "name", "") for t in (getattr(remote, "tools", []) or [])
                        if getattr(t, "type", "") == "custom"}
        local_tools = {t["name"] for t in TOOLS}
        if remote_tools != local_tools or getattr(remote, "system", None) != CMA_SYSTEM:
            updated = await client.beta.agents.update(
                cfg["agent_id"],
                version=getattr(remote, "version", 1),
                system=CMA_SYSTEM,
                tools=_agent_tools(),
            )
            cfg["agent_version"] = getattr(updated, "version", None)
            n = len(cfg.pop("sessions", {}) or {})
            save_config(cfg)
            print(f"   agent updated to version {cfg['agent_version']} "
                  f"(tools: {sorted(local_tools)}); {n} session(s) retired")
    print(f"   agent        {cfg['agent_id']} (model {CMA_MODEL})")

    if not cfg.get("memory_store_id"):
        store = await client.beta.memory_stores.create(
            name="weekend-window-memory",
            description=(
                "The riding group's durable memory: rider preferences (e.g. temperature limits), "
                "standing constraints, and notes worth keeping across conversations. One small "
                "file per topic. Check before answering questions that a stored preference would change."
            ),
        )
        cfg["memory_store_id"] = store.id
        save_config(cfg)
    print(f"   memory store {cfg['memory_store_id']}")

    return cfg


# ── the broker ────────────────────────────────────────────────────────────────
class Broker:
    """Session-per-channel orchestrator: relays turns, answers custom tools, posts replies."""

    TURN_TIMEOUT = 600   # seconds; CMA turns include container work

    def __init__(self, client):
        self._client = client
        self._locks: dict[str, asyncio.Lock] = {}
        self._caught_up: set[str] = set()

    def _lock(self, channel: str) -> asyncio.Lock:
        return self._locks.setdefault(channel, asyncio.Lock())

    async def ensure_session(self, channel: str) -> str:
        """One durable session per channel (C2), persisted across broker restarts."""
        cfg = load_config()
        sessions = cfg.setdefault("sessions", {})
        sid = sessions.get(channel)
        if sid:
            try:
                existing = await self._client.beta.sessions.retrieve(sid)
                if getattr(existing, "status", "") != "terminated":
                    if sid not in self._caught_up:
                        await self._answer_backlog(sid)
                        self._caught_up.add(sid)
                    return sid
            except Exception:
                pass   # fall through: create a fresh session (memory store carries continuity)

        session = await self._client.beta.sessions.create(
            agent=cfg["agent_id"],
            environment_id=cfg["environment_id"],
            title=f"weekend-window · {channel}",
            resources=[{
                "type": "memory_store",
                "memory_store_id": cfg["memory_store_id"],
                "access": "read_write",
                "instructions": "This channel's durable group memory. Check it before answering "
                                "questions a stored preference would change; write lasting facts here.",
            }],
        )
        sessions[channel] = session.id
        save_config(cfg)
        self._caught_up.add(session.id)
        print(f"   session for {channel}: {session.id}")
        return session.id

    async def _answer_backlog(self, session_id: str) -> None:
        """C3: zero permanently-unanswered tool calls — unblock calls orphaned by a broker restart."""
        page = await self._client.beta.sessions.events.list(session_id=session_id)
        events = list(getattr(page, "data", []) or [])
        answered = {getattr(e, "custom_tool_use_id", "") for e in events
                    if getattr(e, "type", "") == "user.custom_tool_result"}
        stale = [e for e in events
                 if getattr(e, "type", "") == "agent.custom_tool_use" and e.id not in answered]
        for e in stale:
            await self._client.beta.sessions.events.send(session_id=session_id, events=[{
                "type": "user.custom_tool_result",
                "custom_tool_use_id": e.id,
                "content": [{"type": "text",
                             "text": "tool error: the watch broker restarted before this call was "
                                     "served — ask the rider to repeat the request."}],
            }])
            print(f"   answered stale tool call {e.id}")

    async def run_turn(self, channel: str, text: str, handlers: dict) -> str:
        """Send one user turn into the channel's session; answer its tool calls; return reply text.

        Serialized per channel so a proactive update and a mention never interleave streams.
        """
        async with self._lock(channel):
            session_id = await self.ensure_session(channel)
            return await asyncio.wait_for(
                self._drain(session_id, text, handlers), timeout=self.TURN_TIMEOUT)

    async def _drain(self, session_id: str, text: str, handlers: dict) -> str:
        """Stream-first: open the stream, send the turn, answer tools on the same stream,
        collect agent text, stop when the session idles for a reason other than requires_action."""
        texts: list[str] = []
        # stream-first: the connection is open before the kickoff is sent
        stream = await self._client.beta.sessions.events.stream(session_id=session_id)
        try:
            await self._client.beta.sessions.events.send(session_id=session_id, events=[
                {"type": "user.message", "content": [{"type": "text", "text": text}]},
            ])
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "agent.message":
                    t = "".join(b.text for b in (event.content or [])
                                if getattr(b, "type", "") == "text").strip()
                    if t:
                        texts.append(t)
                elif etype == "agent.custom_tool_use":
                    out = await self._run_handler(handlers, event.name, event.input or {})
                    await self._client.beta.sessions.events.send(session_id=session_id, events=[{
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": event.id,
                        "content": [{"type": "text", "text": out}],
                    }])
                elif etype == "session.status_idle":
                    reason = getattr(getattr(event, "stop_reason", None), "type", "")
                    if reason != "requires_action":   # requires_action = waiting on us; keep going
                        break
                elif etype == "session.status_terminated":
                    break
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
        return "\n".join(texts).strip() or "…"

    @staticmethod
    async def _run_handler(handlers: dict, name: str, args: dict) -> str:
        fn = handlers.get(name)
        if fn is None:
            return f"tool error: no handler for {name}"
        try:
            res = fn(**args)
            if asyncio.iscoroutine(res):
                res = await res
            return str(res)
        except Exception as e:   # report to the model, never kill the stream
            return f"tool error: {e}"

    async def proactive_update(self, channel: str, place: str, state_line: str,
                               first: bool, handlers: dict) -> str:
        """C5: feed a watch result INTO the session; the model phrases the channel ping."""
        kind = "first outlook" if first else "CHANGE detected"
        text = (f"[weather-watch update — {place} ({kind}): {state_line}. "
                f"Compose the one-line channel ping.]")
        return await self.run_turn(channel, text, handlers)
