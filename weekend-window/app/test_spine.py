"""
test_spine.py — verify the essence with SEEDED data. No Slack, no CMA, no human role-play.

    python test_spine.py            # plain asserts; prints ✓/✗ per check, exits non-zero on any fail

Covers the deterministic core of the passing criteria (the parts that don't need a live model):
  · classify(): frozen forecast codes → weather-state category + hazard hours    (correctness — SPEC M3/M4)
  · classify() is reproducible: same input → identical state                      (SPEC M2)
  · THE ESSENCE: a watch posts EXACTLY on weather-state changes, never repeats     (SPEC M6/M7)
  · fault tolerance: one failing check doesn't kill the watch                      (SPEC M10)
  · interpret(): natural asks → intent + place                                     (parsing)

None of this depends on a human typing in Slack — it's fixtures fed through the real spine code.
"""
import asyncio

from weather import classify, WeatherState, ScriptedSource
from spine import Monitor, MonitorManager
import slack_app

FAILS = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗'} {name}")
    if not cond:
        FAILS.append(name)


# ── seeded fixtures: frozen hourly forecasts (WMO weathercode) over 12:00–17:00 ──
def hourly(codes_by_hour):
    return {"time": [f"2026-07-04T{h:02d}:00" for h in range(12, 18)],
            "weathercode": [codes_by_hour.get(h, 0) for h in range(12, 18)]}

CLEAR = hourly({})                    # all 0 → clear
RAIN  = hourly({13: 61, 14: 63})      # rain
STORM = hourly({15: 95, 16: 95})      # thunderstorm during the 3pm & 4pm hours
SNOW  = hourly({12: 71})              # snow → slippery
MIXED = hourly({13: 61, 15: 95})      # rain + storm → worst-wins = hazard
W = ("2026-07-04", 12, 18)


def test_classify():
    print("classify() correctness (SPEC M3/M4):")
    check("clear → good, no hazards", (lambda s: s.category == "good" and s.hazards == ())(classify(CLEAR, *W)))
    check("rain → rain", classify(RAIN, *W).category == "rain")
    s = classify(STORM, *W)
    check("storm → hazard", s.category == "hazard")
    check("hazard hours pinpointed = (thunderstorm,15,17)", s.hazards == (("thunderstorm", 15, 17),))
    check("snow → slippery", classify(SNOW, *W).category == "slippery")
    check("rain+storm → hazard (worst wins)", classify(MIXED, *W).category == "hazard")


def test_reproducible():
    print("classify() reproducibility (SPEC M2):")
    check("same input → identical state", classify(STORM, *W) == classify(STORM, *W))


async def _watch(seq):
    posts = []
    mgr = MonitorManager()
    mgr.schedule_monitor(Monitor("t", "t", ScriptedSource(seq), W, 0.001,
                                 on_update=lambda st, first: posts.append(st.category)))
    await mgr.join()
    return posts


def test_change_detection():
    print("post-on-change, never-repeat — THE ESSENCE (SPEC M6/M7):")
    good = WeatherState("good", (), "")
    storm = WeatherState("hazard", (("thunderstorm", 15, 16),), "")
    check("good,good,storm,storm,good → [good,hazard,good]",
          asyncio.run(_watch([good, good, storm, storm, good])) == ["good", "hazard", "good"])
    check("all-same → 1 post", asyncio.run(_watch([good, good, good])) == ["good"])
    check("alternating → 4 posts",
          asyncio.run(_watch([good, storm, good, storm])) == ["good", "hazard", "good", "hazard"])


def test_fault_tolerance():
    print("fault tolerance — a failing check doesn't kill the watch (SPEC M10):")
    good, storm = WeatherState("good", (), ""), WeatherState("hazard", (), "")

    class Flaky:
        seq = [good, "BOOM", storm]
        i = 0
        def state(self, w):
            v = self.seq[self.i]; self.i += 1
            if v == "BOOM":
                raise RuntimeError("simulated network error")
            return v
        @property
        def exhausted(self):
            return self.i >= len(self.seq)

    posts = []
    async def run():
        mgr = MonitorManager()
        mgr.schedule_monitor(Monitor("f", "f", Flaky(), W, 0.001,
                                     on_update=lambda st, first: posts.append(st.category)))
        await mgr.join()
    asyncio.run(run())
    check("survived the error; posted good then hazard", posts == ["good", "hazard"])


def test_cancellation():
    print("cancellation — no post after cancel_monitor (SPEC M15):")
    good = WeatherState("good", (), "")
    storm = WeatherState("hazard", (), "")
    posts = []

    async def run():
        mgr = MonitorManager()
        # long cadence: after the first tick the monitor sleeps, giving us time to cancel
        mid = mgr.schedule_monitor(Monitor("c", "c", ScriptedSource([good, storm, storm]), W, 1.0,
                                           on_update=lambda st, first: posts.append(st.category)))
        await asyncio.sleep(0.05)          # let the first tick post
        cancelled = mgr.cancel_monitor(mid)
        await mgr.join()
        return cancelled

    cancelled = asyncio.run(run())
    check("cancel returned True", cancelled)
    check("exactly the pre-cancel post, nothing after", posts == ["good"])


def test_describe():
    print("describe() — ground truth for list_monitors:")
    good = WeatherState("good", (), "")

    async def run():
        mgr = MonitorManager()
        mid = mgr.schedule_monitor(Monitor("d", "GWB · Sat", ScriptedSource([good, good]), W, 1.0,
                                           on_update=lambda st, first: None))
        await asyncio.sleep(0.05)          # first tick happened
        desc = mgr.describe()
        mgr.cancel_monitor(mid)
        return desc, mgr.describe()

    desc, after = asyncio.run(run())
    check("one active watch described with label + state",
          len(desc) == 1 and desc[0]["label"] == "GWB · Sat"
          and desc[0]["checks"] == 1 and desc[0]["last_state"] == "good")
    check("empty after cancel", after == [])


def test_seed():
    print("rehydration seed — a restarted watch doesn't re-announce an unchanged state (SPEC M16):")
    good = WeatherState("good", (), "")
    storm = WeatherState("hazard", (("thunderstorm", 15, 17),), "")

    def run(seed, states):
        posts = []

        async def go():
            mgr = MonitorManager()
            mid = mgr.schedule_monitor(Monitor("s", "s", ScriptedSource(states), W, 0.001,
                                               on_update=lambda st, first: posts.append(st.category)))
            mgr.seed_last(mid, seed)
            await mgr.join()
        asyncio.run(go())
        return posts

    check("seeded good + still good → SILENT", run(good, [good, good]) == [])
    check("seeded good + turns to storm → posts the change", run(good, [storm]) == ["hazard"])


def test_cancel_scope():
    print("scoped cancel — stop one place/date without killing the others (SPEC M15):")
    good = WeatherState("good", (), "")
    real_geocode = slack_app.geocode
    slack_app.geocode = lambda name: (40.0, -73.0, name)   # offline: name is its own label

    class FakeSlack:
        async def chat_postMessage(self, **kw): pass

    async def go():
        slack_app._unpersist_watches("C_CX")
        loop = asyncio.get_running_loop()
        h = slack_app._make_handlers("C_CX", "T", FakeSlack(), loop, "rules", None,
                                     source_factory=lambda a, b: ScriptedSource([good] * 999),
                                     cadence_s=5.0)
        await h["schedule_monitor"](place="Central Park")                      # Jul 4 default
        await h["schedule_monitor"](place="Central Park", date="2026-07-11")   # same place, other Sat
        await h["schedule_monitor"](place="GW Bridge")
        results = []
        results.append((h["cancel_monitor"](place="Central Park", date="2026-07-11"),
                        sorted(slack_app.MGR.active())))
        results.append((h["cancel_monitor"](place="Central Park"),
                        sorted(slack_app.MGR.active())))
        results.append((h["cancel_monitor"](), sorted(slack_app.MGR.active())))
        slack_app._unpersist_watches("C_CX")
        await slack_app.MGR.join()
        return results

    (r1, a1), (r2, a2), (r3, a3) = asyncio.run(go())
    check("place+date cancels exactly that watch",
          "Stopped 1" in r1 and len(a1) == 2 and not any("2026-07-11" in m for m in a1))
    check("place cancels the remaining CP watch, GW Bridge survives",
          "Stopped 1" in r2 and a2 == ["C_CX:GW Bridge:" + a2[0].split(":")[-1]])
    check("bare cancel stops everything", "Stopped 1" in r3 and a3 == [])
    slack_app.geocode = real_geocode


def test_interpret():
    print("interpret() parsing (natural asks → intent + place):")
    check("watch <place>", slack_app.interpret("<@U1> watch Central Park and ping us")
          == {"intent": "watch", "place": "Central Park"})
    check("keep an eye on <place>", slack_app.interpret("keep an eye on Prospect Park")["place"] == "Prospect Park")
    check("a day is not a place", slack_app.interpret("watch Saturday")["place"] is None)
    check("stop → cancel", slack_app.interpret("stop watching")["intent"] == "cancel")
    check("small talk → chat", slack_app.interpret("hey how's it going")["intent"] == "chat")


if __name__ == "__main__":
    for t in (test_classify, test_reproducible, test_change_detection, test_fault_tolerance,
              test_cancellation, test_describe, test_seed, test_cancel_scope, test_interpret):
        t()
    print("-" * 48)
    print("  ALL PASS ✓" if not FAILS else f"  {len(FAILS)} FAILED: " + ", ".join(FAILS))
    raise SystemExit(1 if FAILS else 0)
