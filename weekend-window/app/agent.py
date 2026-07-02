"""
agent.py — the conversational brain (THE "CMA SEAM", now wired to a real model).

This replaces the regex `interpret()` stand-in with an actual Claude agent
(Anthropic Messages API + tool use). The agent reads the channel's running
conversation, decides on its own whether to call `schedule_monitor` /
`cancel_monitor`, and phrases its own replies. That is what makes weekend-window
feel like Claude Tag: one shared teammate that two people talk to, that remembers
what each of them said, and that acts through its own tools.

Why the Messages API and not `client.beta.sessions` (CMA): the conversational
essence — a real model choosing tools and wording the replies — is identical
either way. The Messages API is GA, runs on the ANTHROPIC_API_KEY already in
.env.local, and is testable end-to-end today. Moving to a hosted CMA session is a
substrate swap, not a redesign:

    · TOOLS + SYSTEM below become the agent config on `client.beta.agents.create`.
    · `self._history[channel]` (the per-channel transcript = the memory) becomes a
      CMA session-per-channel plus a memory_store attached at session-create.
    · the tool_use → tool_result loop below is the same shape as CMA's
      `agent.custom_tool_use` → `user.custom_tool_result` round-trip; our
      schedule_monitor / cancel_monitor stay the broker-side custom tools.

The spine (weather.py + spine.py) is unchanged — the model drives it, it doesn't
know or care that a model is upstream.
"""
from __future__ import annotations

import asyncio
import os

# Fast/cheap by default — this is a short-reply Slack helper. Override with
# WEEKEND_WINDOW_MODEL (e.g. claude-sonnet-5 / claude-opus-4-8) for more headroom.
DEFAULT_MODEL = os.environ.get("WEEKEND_WINDOW_MODEL", "claude-haiku-4-5")

SYSTEM = (
    "You are weekend-window, a Slack teammate for a small group of friends who ride "
    "bikes together. You share one channel with them: anyone in the channel can talk "
    "to you, and messages are labelled with who is speaking, so remember what each "
    "person said (e.g. one has time Saturday afternoon, another is free both days).\n\n"
    "Your specialty is the weekend weather for the places they might ride. When someone "
    "asks what the weather is or will be like somewhere, call get_forecast for that place "
    "and answer from the result. When someone asks you to watch / keep an eye on / monitor "
    "a place, call schedule_monitor with the place name — after that you ping the channel "
    "on your OWN if the outlook changes (e.g. clear turns to thunderstorm), so they don't "
    "have to keep asking. When someone says stop / cancel / never mind, call cancel_monitor — "
    "pass place if they mean one spot, omit it only when they clearly mean everything. "
    "When someone asks what you're watching or whether a watch is still on, call list_monitors "
    "first — it's the ground truth (a watch may have been cancelled by someone else or expired; "
    "don't trust conversation memory alone).\n\n"
    "You can also just chat: help them weigh Saturday vs Sunday, suggest what to pack, "
    "answer questions. Keep replies short and warm — this is Slack, a sentence or two. "
    "Place names are enough; never ask for coordinates or exact times. If you genuinely "
    "can't tell which place they mean, ask.\n\n"
    "Dates: ALWAYS name the concrete date when you report a forecast, window, or watch "
    "(say 'Saturday Jul 4', never a bare 'Saturday' — riders shouldn't have to ask which "
    "one). Forecast and watch tools default to the upcoming Saturday, but take a date "
    "parameter — when someone asks about a different day or next weekend, pass that date "
    "(forecasts are reliable ~15 days out; only beyond that suggest a watch instead)."
)

# The custom tools. The model decides when to call them; the broker (slack_app)
# supplies the handlers that actually touch weather/geocoding/the spine. Prescriptive
# "call this when…" descriptions matter — they drive the model's should-call rate.
TOOLS = [
    {
        "name": "get_forecast",
        "description": (
            "Look up a place's riding outlook RIGHT NOW (real forecast, 12:00–18:00 window). "
            "Call this whenever someone asks what the weather is or will be like somewhere — "
            "'what's the weather in Central Park?', 'will it rain Saturday?', 'how about next "
            "weekend?'. Defaults to the upcoming Saturday; pass date for any other day "
            "(reliable up to ~15 days out)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": "The place to look up, as a plain name, e.g. 'Central Park'.",
                },
                "date": {
                    "type": "string",
                    "description": "Optional YYYY-MM-DD for a specific day (e.g. next weekend). "
                                   "Omit for the upcoming Saturday.",
                },
            },
            "required": ["place"],
        },
    },
    {
        "name": "schedule_monitor",
        "description": (
            "Start watching a place's ride-day weather and ping the channel ONLY when "
            "the outlook changes. Call this whenever someone asks you to watch, monitor, "
            "or keep an eye on a place. Pass the place name people used (e.g. 'Central "
            "Park') — no coordinates needed. Defaults to the upcoming Saturday; pass "
            "date for a different ride day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": "The place to watch, as a plain name, e.g. 'Prospect Park'.",
                },
                "date": {
                    "type": "string",
                    "description": "Optional YYYY-MM-DD ride day to watch. "
                                   "Omit for the upcoming Saturday.",
                },
            },
            "required": ["place"],
        },
    },
    {
        "name": "cancel_monitor",
        "description": (
            "Stop weather watch(es) in this channel. Call when someone says stop, cancel, "
            "never mind, or that's enough. If they mean ONE spot ('stop watching Central "
            "Park'), pass place (and date if they name a ride day); with no arguments it "
            "stops EVERY watch in the channel — only do that when they clearly mean all."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": "Stop only the watch(es) for this place, e.g. 'Central Park'. "
                                   "Omit to stop all watches in the channel.",
                },
                "date": {
                    "type": "string",
                    "description": "Optional YYYY-MM-DD to narrow to one ride day's watch.",
                },
            },
        },
    },
    {
        "name": "list_monitors",
        "description": (
            "Ground truth about which weather watches are ACTUALLY running right now "
            "(place, cadence, checks so far, last observed state). Call this BEFORE "
            "answering 'what are you watching?' / 'is the watch still on?' — never "
            "answer from conversation memory alone: a watch you remember may have been "
            "cancelled by another rider or expired after its ride day. If it's gone, "
            "say so and offer to create a new one."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

_MAX_HISTORY = 40      # cap the per-channel transcript so it can't grow unbounded
_MAX_TOOL_ROUNDS = 4   # guard against a runaway tool loop


class Agent:
    """A conversational Claude agent, one running transcript per Slack channel.

    The per-channel history IS the memory — no predefined schema; the model keeps
    and uses what matters. (In-process, so it resets on restart; a CMA memory_store
    is the durable version.)
    """

    def __init__(self, model: str = DEFAULT_MODEL):
        from anthropic import AsyncAnthropic   # imported here so the spine/tests need no anthropic
        self._client = AsyncAnthropic()        # reads ANTHROPIC_API_KEY from the environment
        self._model = model
        self._history: dict[str, list] = {}

    async def respond(self, channel_id: str, speaker: str, text: str, handlers: dict) -> str:
        """Feed one @mention through the model; run any tools it calls; return its reply.

        `speaker` labels the turn so the model can attribute memory per person.
        `handlers` maps tool name -> callable (sync or async); they close over this
        channel/thread so the model's tool calls land in the right Slack place.
        """
        msgs = self._history.setdefault(channel_id, [])
        msgs.append({"role": "user", "content": f"{speaker}: {text}" if speaker else text})

        try:
            for _ in range(_MAX_TOOL_ROUNDS):
                resp = await self._client.messages.create(
                    model=self._model,
                    max_tokens=1024,
                    system=SYSTEM,
                    tools=TOOLS,
                    messages=msgs,
                )
                msgs.append({"role": "assistant", "content": resp.content})

                if resp.stop_reason == "tool_use":
                    results = []
                    for block in resp.content:
                        if block.type == "tool_use":
                            out = await self._run_tool(block.name, block.input, handlers)
                            results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": out,
                            })
                    msgs.append({"role": "user", "content": results})
                    continue

                return self._text_of(resp) or "…"
            # Ran out of tool rounds — return whatever text we have.
            return self._text_of(resp) or "…"
        finally:
            # Trim oldest turns, but never start the window on a dangling tool_result
            # (a tool_result user-turn must follow its assistant tool_use).
            if len(msgs) > _MAX_HISTORY:
                del msgs[:len(msgs) - _MAX_HISTORY]
                while msgs and _starts_with_tool_result(msgs[0]):
                    del msgs[0]

    async def _run_tool(self, name: str, args: dict, handlers: dict) -> str:
        fn = handlers.get(name)
        if fn is None:
            return f"(no handler for {name})"
        try:
            res = fn(**args)
            if asyncio.iscoroutine(res):
                res = await res
            return str(res)
        except Exception as e:   # a tool failure is reported to the model, not fatal
            return f"tool error: {e}"

    @staticmethod
    def _text_of(resp) -> str:
        return "".join(b.text for b in resp.content if b.type == "text").strip()


def _starts_with_tool_result(msg) -> bool:
    c = msg.get("content")
    return (msg.get("role") == "user" and isinstance(c, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c))
