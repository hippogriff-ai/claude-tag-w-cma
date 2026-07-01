"""
slack_app.py — real Slack adapter (Bolt + Socket Mode).

    python async_spine/slack_app.py     # needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN

Same spine as the demo; the only difference is the sink posts to a real Slack
channel and the trigger is a real @mention. Socket Mode = no public URL / ngrok.

The conversational brain lives in agent.py (a real Claude agent: Messages API +
tool use). It reads the channel, decides whether to call schedule_monitor /
cancel_monitor, and phrases its own replies. This adapter just wires that brain to
Slack and supplies the tool handlers that touch the spine. If ANTHROPIC_API_KEY
isn't set (or the anthropic package is missing), it falls back to the rule-based
`interpret()` below so the bot still runs offline.

Setup (one-time, ~15 min): create a Slack app, enable Socket Mode (app-level token,
scope connections:write), bot scopes app_mentions:read + chat:write + groups:history,
subscribe to app_mention, install to the workspace, invite the bot to a private
channel. Two riders = two accounts via you+alice@ / you+bob@ email aliases.
"""
import asyncio
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from weather import WeatherState, OpenMeteoSource
from spine import Monitor, MonitorManager

MGR = MonitorManager()


def _load_dotenv():
    """Load .env.local (next to this file) into os.environ so `python slack_app.py` just works."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.local")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v and not v.startswith("#"):
            os.environ.setdefault(k.strip(), v)


# ── geocode a place name → coords (Open-Meteo geocoding, free, no key) ──────────
def geocode(name: str):
    """Name → (lat, lon, rich_label) via Nominatim (OpenStreetMap): free, no key,
    handles qualified names ('Central Park, New York') and ranks by importance
    (plain 'Central Park' → NYC). The label carries the region so the agent can
    surface/confirm what it resolved. (Nominatim policy: set a User-Agent, ≤1 req/s.)"""
    q = urllib.parse.urlencode({"q": name, "format": "jsonv2", "limit": 1})
    url = f"https://nominatim.openstreetmap.org/search?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "weekend-window-demo/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        hits = json.loads(r.read().decode())
    if not hits:
        return None
    h = hits[0]
    parts = [p.strip() for p in h["display_name"].split(",")]
    label = ", ".join(parts[:1] + parts[-2:]) if len(parts) > 3 else h["display_name"]
    return float(h["lat"]), float(h["lon"]), label


# ── rule-based fallback (offline path) ───────────────────────────────────────
# The real conversational brain is agent.py. This regex `interpret()` is only used
# when ANTHROPIC_API_KEY / the anthropic package is unavailable — a graceful
# degrade so the bot still runs, and the deterministic anchor test_spine.py checks.
_STOP = re.compile(r"\b(stop|cancel|never ?mind|that'?s enough|enough)\b", re.I)
_WATCH = re.compile(r"\b(watch|monitor|keep an eye|keep watching|track)\b", re.I)
# a place = a Capitalized word run right after a watch verb or a preposition.
# The (?i:...) group makes the verb/preposition case-insensitive while the
# capture stays case-sensitive so it only grabs real proper nouns.
_PLACE_AFTER = re.compile(
    r"(?i:watch|monitor|eye on|track|in|at|for)\s+(?:the\s+)?"
    r"([A-Z][\w’'.-]*(?:\s+(?:of\s+)?[A-Z][\w’'.-]*){0,3})")
_DAYS = {"sat", "saturday", "sun", "sunday", "mon", "monday", "tue", "tuesday",
         "wed", "wednesday", "thu", "thursday", "fri", "friday",
         "today", "tomorrow", "weekend"}


def _extract_place(text: str):
    for m in _PLACE_AFTER.finditer(text):
        cand = m.group(1).strip()
        if cand.split()[0].lower() not in _DAYS:   # don't mistake "Saturday" for a place
            return cand
    return None


def interpret(text: str):
    if _STOP.search(text):
        return {"intent": "cancel"}
    if _WATCH.search(text):
        return {"intent": "watch", "place": _extract_place(text)}
    return {"intent": "chat"}


def phrase_update(place: str, st: WeatherState, first: bool) -> str:
    lead = "Heads up —" if first else "Update —"
    if st.category == "good":
        return f"{lead} {place} looks good for your window — clear/pleasant. 🚴"
    if st.category == "hazard":
        return f"{lead} {place}: {st.summary}. You'd want to wrap up before it hits."
    return f"{lead} {place}: {st.summary}."


def _next_saturday_window():
    today = date.today()
    sat = today + timedelta((5 - today.weekday()) % 7)
    return (sat.isoformat(), 12, 18)   # Sat 12:00–18:00; a real agent would infer this


def _pick_brain():
    """Mode selection: CMA broker (the real thing) → Messages-API agent → rule-based.

    Returns ("cma", broker) | ("messages", agent) | ("rules", None).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("   (no ANTHROPIC_API_KEY → rule-based mode)")
        return "rules", None
    try:
        import cma_broker
        if cma_broker.load_config().get("agent_id"):
            from anthropic import AsyncAnthropic
            print(f"   CMA broker ready (agent model: {cma_broker.CMA_MODEL}) — session per channel")
            return "cma", cma_broker.Broker(AsyncAnthropic())
        print("   (no cma_config.json — run `python provision.py` for the CMA agent; "
              "using the Messages-API brain meanwhile)")
    except Exception as e:
        print(f"   (CMA broker unavailable: {e})")
    try:
        from agent import Agent, DEFAULT_MODEL
        print(f"   Messages-API agent ready (model: {DEFAULT_MODEL})")
        return "messages", Agent()
    except Exception as e:
        print(f"   (couldn't start Claude agent: {e}; using rule-based fallback)")
        return "rules", None


def _make_handlers(channel, thread_ts, client, loop, brain_mode, broker=None,
                   source_factory=None, cadence_s=3600):
    """The two custom tools, closed over THIS channel/thread. In CMA mode the monitor's
    updates are fed back INTO the session (the model phrases the ping — SPEC C5);
    in fallback modes the sink posts a templated line directly.

    `source_factory(lat, lon)` and `cadence_s` are injectable so the scenario battery
    (scenarios.py) can script the weather; production uses OpenMeteoSource hourly."""
    if source_factory is None:
        source_factory = OpenMeteoSource

    async def forecast_tool(place: str) -> str:
        geo = await loop.run_in_executor(None, geocode, place)
        if not geo:
            return (f"Couldn't find a place called {place!r} — ask them for a fuller "
                    f"name (e.g. add the city).")
        lat, lon, resolved = geo
        window = _next_saturday_window()
        try:
            st = await loop.run_in_executor(None, OpenMeteoSource(lat, lon).state, window)
        except Exception as e:
            return f"tool error: live forecast fetch failed ({e}) — try again in a minute."
        return (f"{resolved} — Saturday {window[0]}, {window[1]}:00–{window[2]}:00: "
                f"state={st.category}; {st.summary}")

    async def schedule_tool(place: str) -> str:
        geo = await loop.run_in_executor(None, geocode, place)   # don't block the loop
        if not geo:
            return (f"Couldn't find a place called {place!r} — ask them for a fuller "
                    f"name (e.g. add the city).")
        lat, lon, resolved = geo
        window = _next_saturday_window()

        async def sink_post(st: WeatherState, first: bool):
            if brain_mode == "cma":
                state_line = f"state={st.category}; {st.summary}; watch window Sat {window[1]}:00–{window[2]}:00"
                ping = await broker.proactive_update(
                    channel, resolved, state_line, first,
                    _make_handlers(channel, thread_ts, client, loop, brain_mode, broker,
                                   source_factory, cadence_s))
                await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=ping)
            else:
                await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                              text=phrase_update(resolved, st, first))

        def on_update(st, first):   # the spine's callback is sync; bridge onto the loop
            loop.create_task(sink_post(st, first))

        MGR.schedule_monitor(Monitor(
            id=f"{channel}:{resolved}",
            label=f"{resolved} · Sat afternoon",
            source=source_factory(lat, lon),
            window=window,
            cadence_s=cadence_s,                 # hourly in production
            on_update=on_update,
            max_ticks=48,                        # bound it (safety)
        ))
        return (f"Watch created for {resolved}, Saturday {window[1]}:00–{window[2]}:00, "
                f"checked hourly; the channel is pinged only when the outlook changes.")

    def cancel_tool() -> str:
        n = sum(MGR.cancel_monitor(mid) for mid in MGR.active()
                if mid.startswith(channel + ":"))
        return (f"Stopped {n} watch(es) for this channel." if n
                else "There were no active watches here to stop.")

    return {"get_forecast": forecast_tool, "schedule_monitor": schedule_tool,
            "cancel_monitor": cancel_tool}


_NAMES: dict[str, str] = {}
_BOT_UID: list = [None]
_MENTION = re.compile(r"<@([UW][A-Z0-9]+)>")


async def _speaker(client, user_id: str) -> str:
    """Display name for per-rider attribution; falls back to the Slack user id."""
    if user_id in _NAMES:
        return _NAMES[user_id]
    try:
        info = await client.users_info(user=user_id)
        p = info["user"].get("profile", {})
        name = p.get("display_name") or p.get("real_name") or user_id
        _NAMES[user_id] = name          # cache successes only, so adding users:read
        return name                     # later doesn't leave stale raw ids behind
    except Exception:
        return user_id                  # missing users:read scope — still a stable label


async def _bot_uid(client) -> str:
    if _BOT_UID[0] is None:
        _BOT_UID[0] = (await client.auth_test())["user_id"]
    return _BOT_UID[0]


async def _render(client, text: str, bot_uid: str) -> str:
    """Slack markup → plain text: drop the bot's own mention, name other people's."""
    out = text
    for uid in set(_MENTION.findall(text)):
        rep = "" if uid == bot_uid else "@" + await _speaker(client, uid)
        out = out.replace(f"<@{uid}>", rep)
    return " ".join(out.split())


async def _gather_context(client, channel: str, thread_ts: str, event_ts: str):
    """Conversation catch-up for the session: the unseen MAIN-CHANNEL messages (last 20,
    like Claude Tag's channel pull) PLUS — when tagged in a thread — the unseen THREAD
    messages (last 50). Richer than stateless Tag (thread-only in threads) on purpose:
    availability lives in the main chat while logistics live in threads, and the stateful
    session must see both. Per-channel/thread cursors prevent resending history."""
    import cma_broker
    bot = await _bot_uid(client)
    cfg = cma_broker.load_config()
    cursors = cfg.setdefault("context_cursors", {})
    in_thread = thread_ts != event_ts

    async def unseen(msgs, key, root_only):
        seen = float(cursors.get(key, 0))
        out = []
        for m in msgs:
            ts = m.get("ts", "0")
            if float(ts) <= seen or ts == event_ts:           # already relayed / the mention itself
                continue
            if m.get("user") == bot or m.get("bot_id"):       # our own posts are in the session already
                continue
            if root_only and m.get("thread_ts") not in (None, ts):
                continue                                      # root view: skip thread replies
            who = await _speaker(client, m.get("user", "someone"))
            txt = await _render(client, m.get("text", ""), bot)
            if txt:
                out.append(f"{who}: {txt}")
        cursors[key] = event_ts
        return out

    root_lines: list[str] = []
    thread_lines: list[str] = []
    try:
        r = await client.conversations_history(channel=channel, limit=20)
        root_lines = await unseen(list(reversed(r.get("messages", []))), channel, root_only=True)
        if in_thread:
            r = await client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
            thread_lines = await unseen(r.get("messages", []), f"{channel}:{thread_ts}", root_only=False)
        cma_broker.save_config(cfg)
    except Exception as e:
        print(f"   (couldn't fetch conversation history: {e} — bot in channel? "
              f"scopes groups:history / channels:history granted?)")
    return root_lines, thread_lines


async def _compose_turn(client, event) -> str:
    """The full user turn for the agent: catch-up context + the tagged message."""
    channel, event_ts = event["channel"], event["ts"]
    thread_ts = event.get("thread_ts") or event_ts
    bot = await _bot_uid(client)
    speaker = await _speaker(client, event.get("user", "someone"))
    clean = await _render(client, event.get("text", ""), bot)
    root_lines, thread_lines = await _gather_context(client, channel, thread_ts, event_ts)
    parts = []
    if root_lines:
        parts.append("[in the main channel since your last look]\n" + "\n".join(root_lines))
    if thread_lines:
        parts.append("[in this thread]\n" + "\n".join(thread_lines))
    parts.append(f"[tagging you] {speaker}: {clean}" if parts else f"{speaker}: {clean}")
    return "\n".join(parts)


def build_app():
    _load_dotenv()
    from slack_bolt.async_app import AsyncApp   # imported here so run_demo needs no slack_bolt

    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    mode, brain = _pick_brain()

    @app.event("app_mention")
    async def on_mention(event, say, client):
        text = event.get("text", "")
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        loop = asyncio.get_running_loop()
        handlers = _make_handlers(channel, thread_ts, client, loop, mode,
                                  brain if mode == "cma" else None)

        if mode == "cma":
            try:
                turn = await _compose_turn(client, event)
                reply = await brain.run_turn(channel, turn, handlers)
                await say(text=reply, thread_ts=thread_ts)
                return
            except Exception as e:
                print(f"   (CMA turn failed: {e}; falling back to rule-based)")
        elif mode == "messages":
            try:
                turn = await _compose_turn(client, event)
                reply = await brain.respond(channel, "", turn, handlers)
                await say(text=reply, thread_ts=thread_ts)
                return
            except Exception as e:
                print(f"   (agent error: {e}; falling back to rule-based)")

        await _handle_rule_based(text, handlers, say, thread_ts)

    return app


async def _handle_rule_based(text, handlers, say, thread_ts):
    """Offline path: regex intent → the same tool handlers the agent would call."""
    decision = interpret(text)
    if decision["intent"] == "cancel":
        await say(text=handlers["cancel_monitor"](), thread_ts=thread_ts)
        return
    if decision["intent"] == "watch":
        place = decision.get("place")
        if not place:
            await say(text="Which place should I watch? (name it and I'll keep an eye on it)",
                      thread_ts=thread_ts)
            return
        await say(text=await handlers["schedule_monitor"](place), thread_ts=thread_ts)
        return
    await say(text="Hi! Ask me to watch a place's weekend weather — e.g. “watch Central "
                   "Park” — and I'll ping you only when the outlook changes.", thread_ts=thread_ts)


async def main():
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    app = build_app()
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("weekend-window is live on Slack (Socket Mode). @-mention it in your channel.")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
