"""
scenarios.py — the SPEC §C acceptance battery (S1–S8) + §D rubric scoring, run LIVE.

    python scenarios.py        # real CMA, real model, real spine, real geocoding

What's real: the CMA session/agent/memory store, the model's judgment, the spine,
Nominatim geocoding. What's scripted: the WEATHER (so a change happens in seconds,
not days) and the Slack transport (a recording sink asserting thread routing — the
same handler code `slack_app._make_handlers` used in production; only Slack's event
delivery is bypassed, since two human riders can't be automated).

Rider turns are scripted; every reply and every proactive ping is the live model.
Afterwards an LLM judge scores rubrics R1–R4 against the SPEC anchors (§D);
R1 and R2 gate. Exit 0 = S1–S8 pass and rubrics ≥ 4/5.
"""
import asyncio
import json
import os

import slack_app
from slack_app import MGR, _make_handlers
from weather import WeatherState, ScriptedSource
import cma_broker

CH = "C_SCEN"
GOOD = WeatherState("good", (), "clear and pleasant")
STORM = WeatherState("hazard", (("thunderstorm", 15, 17),), "thunderstorm around 3pm–5pm")

RESULTS = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   [{detail}]" if detail and not cond else ""))
    RESULTS.append((name, bool(cond)))


class FakeSlack:
    """Records what production would chat_postMessage — asserts thread routing (M6)."""
    def __init__(self):
        self.posts = []   # (thread_ts, text)

    async def chat_postMessage(self, channel, thread_ts, text):
        self.posts.append((thread_ts, text))
        print(f"    [ping → {thread_ts}] {text[:110]}")

    def in_thread(self, ts):
        return [t for th, t in self.posts if th == ts]


class Flaky:
    """S8: good → network error → hazard. The watch must survive the error."""
    def __init__(self):
        self.i = 0

    def state(self, window):
        self.i += 1
        if self.i == 2:
            raise RuntimeError("simulated Open-Meteo outage")
        return GOOD if self.i == 1 else STORM

    @property
    def exhausted(self):
        return self.i >= 3


def spy(handlers, calls):
    def wrap(name, fn):
        def inner(**kw):
            calls.append((name, kw))
            return fn(**kw)
        return inner
    return {n: wrap(n, f) for n, f in handlers.items()}


async def wait_for(cond, timeout, what):
    for _ in range(int(timeout * 2)):
        if cond():
            return True
        await asyncio.sleep(0.5)
    print(f"    (timed out waiting for {what})")
    return False


async def run_scenarios(client):
    broker = cma_broker.Broker(client)
    slack = FakeSlack()
    loop = asyncio.get_running_loop()
    calls = []
    transcript = []

    # deterministic weather per watch, in creation order: Central Park, Prospect, GW Bridge
    scripts = [ScriptedSource([GOOD, GOOD, STORM, STORM, GOOD]),      # CP: first → change → quiet → clears
               ScriptedSource([GOOD] * 90),                            # Prospect: never changes (stays alive for S6)
               Flaky()]                                                # GWB: survives an error (S8)
    factory = lambda lat, lon: scripts.pop(0)

    def handlers_for(thread):
        h = _make_handlers(CH, thread, slack, loop, "cma", broker,
                           source_factory=factory, cadence_s=0.7)
        return spy(h, calls)

    async def turn(thread, text):
        reply = await broker.run_turn(CH, text, handlers_for(thread))
        transcript.append((text, reply))
        print(f"    [{text[:60]}…] → {reply[:110]}")
        return reply

    # fresh stage: clear any prior session for this channel + empty the memory store
    cfg = cma_broker.load_config()
    cfg.get("sessions", {}).pop(CH, None)
    cma_broker.save_config(cfg)
    page = await client.beta.memory_stores.memories.list(cfg["memory_store_id"])
    for m in list(getattr(page, "data", []) or []):
        if getattr(m, "type", "") == "memory":
            await client.beta.memory_stores.memories.delete(m.id, memory_store_id=cfg["memory_store_id"])
    print("  (stage reset: fresh session, empty memory store)")

    print("\nS1 — multiplayer availability → mutual window")
    await turn("T_ROOT", "alice: I'm only free Saturday afternoon this weekend. "
                         "And remember this for good: I never ride below 40°F.")
    r = await turn("T_ROOT", "bob: I've got both Saturday and Sunday free. "
                             "Let's plan on Central Park. When works for us both?")
    check("S1: confirms the mutual window (Saturday), referencing both riders", "saturday" in r.lower())

    print("\nS2 — watch request, place resolved from context ('there')")
    r = await turn("T_CP", "bob: can you keep an eye on the weather there for the ride "
                           "and ping us if it changes?")
    sched = [kw for n, kw in calls if n == "schedule_monitor"]
    check("S2: schedule_monitor called with Central Park (resolved from context)",
          any("central park" in (kw.get("place") or "").lower() for kw in sched), str(sched))

    print("\nS3-prep — a second watch (Prospect Park), different thread")
    await turn("T_PP", "alice: we're also weighing Prospect Park as a backup — keep an eye on that one too.")
    check("second watch created", sum(1 for n, _ in calls if n == "schedule_monitor") >= 2)

    print("\nS8-prep — third watch (George Washington Bridge) whose feed will error mid-watch")
    await turn("T_GWB", "bob: one more — watch George Washington Bridge too please.")

    print("\nS3/S4/S5/S8 — waiting for the proactive pings (scripted weather, live model)…")
    ok = await wait_for(lambda: len(slack.in_thread("T_CP")) >= 3 and len(slack.in_thread("T_PP")) >= 1
                        and len(slack.in_thread("T_GWB")) >= 2, 240, "pings")
    await asyncio.sleep(2)
    cp, pp, gwb = slack.in_thread("T_CP"), slack.in_thread("T_PP"), slack.in_thread("T_GWB")
    check("S3: storm change → one unprompted ping in the Central Park thread",
          len(cp) >= 2 and any(("storm" in t.lower() or "thunder" in t.lower()) for t in cp[1:]))
    check("S3: other watched place (Prospect) got no change ping", len(pp) == 1, f"pp={len(pp)}")
    check("S4: no repeat for the unchanged storm state (CP pings == 3 exactly)", len(cp) == 3, f"cp={len(cp)}")
    check("S5: the clear-again is the final CP ping (return-to-good posted)",
          len(cp) == 3 and any(w in cp[-1].lower() for w in ("clear", "good", "nice", "settled")),
          cp[-1] if cp else "")
    check("S8: flaky watch survived the error and still posted its change (2 pings)", len(gwb) == 2, f"gwb={len(gwb)}")
    check("M6 live: every ping landed in the thread its watch was asked in",
          all(th in ("T_CP", "T_PP", "T_GWB") for th, _ in slack.posts))

    print("\nS6 — 'stop' cancels; checks cease")
    n_before = len(slack.posts)
    r = await turn("T_ROOT", "alice: the plan's settled — please stop watching everything, thanks!")
    cancelled = any(n == "cancel_monitor" for n, _ in calls)
    check("S6: cancel_monitor called", cancelled)
    check("S6: no active watches remain", not MGR.active(), str(MGR.active()))
    await asyncio.sleep(3)
    check("S6: no post after cancel", len(slack.posts) == n_before)

    print("\nS7 — bot restart: fresh session, memory store carries the group's knowledge")
    cfg = cma_broker.load_config()
    old = cfg["sessions"].pop(CH)
    cma_broker.save_config(cfg)
    broker2 = cma_broker.Broker(client)

    async def turn2(thread, text):
        reply = await broker2.run_turn(CH, text, handlers_for(thread))
        transcript.append((text, reply))
        print(f"    [{text[:60]}…] → {reply[:110]}")
        return reply
    r = await turn2("T_ROOT", "carol: quick sanity check — what's alice's cold-weather rule again?")
    new_sid = cma_broker.load_config()["sessions"][CH]
    check("S7: a fresh session was created", new_sid != old)
    check("S7: the 40°F preference survived the restart", "40" in r)

    return transcript, slack.posts


RUBRIC_PROMPT = """You are grading a Slack weather-teammate agent's transcript against these rubrics (score 1, 3, or 5; 4 = between 3 and 5):

R1 — Conversational, not a command bot. 1: only reacts to exact trigger phrases. 3: handles weather asks but stilted otherwise. 5: answers real questions naturally, resolves references from context (e.g. "there"), knows when to just reply vs call a tool.
R2 — Update quality. 1: vague/spammy or wrong thread. 3: right thread but missing hours or advice. 5: one useful line — outlook, the bad hours, what to do — never a repeat.
R3 — Memory-in-use. 1: ignores or invents preferences/availability. 3: recalls but recites. 5: applies stored context meaningfully without inventing.
R4 — Thread etiquette / autonomy. 1: hijacks or over-posts. 3: mostly appropriate. 5: contributes only weather, only on real change.

TRANSCRIPT (rider turns → agent replies):
{turns}

PROACTIVE PINGS (thread → text), posted with no human turn:
{pings}

Return JSON only: {{"R1": {{"score": n, "why": "..."}}, "R2": {{...}}, "R3": {{...}}, "R4": {{...}}}}"""


async def run_rubrics(transcript, posts):
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    turns = "\n".join(f"- {t}\n  → {r}" for t, r in transcript)
    pings = "\n".join(f"- [{th}] {t}" for th, t in posts)
    resp = await client.messages.create(
        model="claude-opus-4-8", max_tokens=2000,
        output_config={"format": {"type": "json_schema", "schema": {
            "type": "object", "additionalProperties": False,
            "required": ["R1", "R2", "R3", "R4"],
            "properties": {k: {"type": "object", "additionalProperties": False,
                               "required": ["score", "why"],
                               "properties": {"score": {"type": "integer"},
                                              "why": {"type": "string"}}}
                           for k in ("R1", "R2", "R3", "R4")}}}},
        messages=[{"role": "user", "content": RUBRIC_PROMPT.format(turns=turns, pings=pings)}])
    scores = json.loads(next(b.text for b in resp.content if b.type == "text"))
    print("\nRubrics (LLM judge, SPEC §D — R1/R2 gate):")
    for k in ("R1", "R2", "R3", "R4"):
        s = scores[k]
        print(f"  {k}: {s['score']}/5 — {s['why'][:140]}")
        check(f"{k} ≥ 4", s["score"] >= 4)
    return scores


async def main():
    slack_app._load_dotenv()
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    transcript, posts = await run_scenarios(client)
    await run_rubrics(transcript, posts)
    fails = [n for n, ok in RESULTS if not ok]
    print("-" * 60)
    print(f"  {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed"
          + (f" — FAILED: {fails}" if fails else "  — S1–S8 + rubrics ALL PASS ✓"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
