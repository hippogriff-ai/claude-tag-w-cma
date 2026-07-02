"""
run_demo.py — the async spine, running, with no credentials.

    python app/run_demo.py

It shows the essence: Alice & Bob asked the agent to watch a place; the agent
stands up a recurring check and posts into the channel ONLY when the weather-state
changes — staying quiet otherwise. A scripted source makes a change happen in
seconds; then we do one real Open-Meteo pull to prove the live path.

Slack (Bolt) and CMA are pluggable behind the same `on_update` sink — see slack_app.py.
"""
import asyncio
from datetime import date, timedelta

from weather import WeatherState, OpenMeteoSource, ScriptedSource
from spine import Monitor, MonitorManager

CENTRAL_PARK = (40.7829, -73.9654)


def phrase(label: str, st: WeatherState, first: bool) -> str:
    lead = "Heads up —" if first else "Update —"
    if st.category == "good":
        tail = "looks good — clear/pleasant. 🚴" if first else f"{label.split(' · ')[0]} cleared up — looks good now. 🚴"
        return f"{lead} {label}: {tail}" if first else f"{lead} {tail}"
    if st.category == "hazard":
        return f"{lead} {label}: {st.summary}. You'd want to wrap up before it hits."
    return f"{lead} {label}: {st.summary}."


def channel_sink(label):
    """A console 'sink' that looks like a Slack channel post."""
    def on_update(st: WeatherState, first: bool):
        print(f"      #weekend-ride  ✳ @weekend-window:  {phrase(label, st, first)}")
    return on_update


class _Tracing:
    """Wrap a source to show every check (so the 'quiet unless changed' behavior is visible)."""
    def __init__(self, src):
        self.src = src
        self.n = 0

    def state(self, window):
        st = self.src.state(window)
        self.n += 1
        print(f"   · check #{self.n}: {st.category:8s} ({st.summary})")
        return st

    @property
    def exhausted(self):
        return getattr(self.src, "exhausted", False)


async def scripted_demo():
    print("=" * 74)
    print("SCRIPTED DEMO — the agent watches Central Park for Sat afternoon.")
    print("(fast cadence so a change happens in seconds; real cadence would be hourly)")
    print("=" * 74)
    print("  Alice: @weekend-window we're riding Sat afternoon, Central Park")
    print("  Bob:   @weekend-window keep an eye on the weather and ping us if it changes")
    print("  ✳ @weekend-window: on it — I'll watch Central Park for Sat afternoon and")
    print("     only ping if something changes.  (schedule_monitor: hourly, until Sat)\n")

    good = WeatherState("good", (), "looks good — clear/pleasant")
    storm = WeatherState("hazard", (("thunderstorm", 15, 16),), "thunderstorm around 3pm–4pm")
    # forecast evolves: good, good, STORM appears, storm, clears again
    seq = [good, good, storm, storm, good]

    mgr = MonitorManager()
    mgr.schedule_monitor(Monitor(
        id="cp",
        label="Central Park · Sat afternoon",
        source=_Tracing(ScriptedSource(seq)),
        window=("2026-07-04", 12, 18),
        cadence_s=0.4,
        on_update=channel_sink("Central Park · Sat afternoon"),
    ))
    await mgr.join()
    print("\n  → 5 checks, 3 posts. It posted the outlook, the storm appearing, and the")
    print("    clear-up — and stayed silent on the two unchanged checks. Never spammed.\n")


def real_pull():
    print("=" * 74)
    print("LIVE PULL — same classifier against real Open-Meteo (Central Park).")
    print("=" * 74)
    sat = date(2026, 7, 1) + timedelta((5 - date(2026, 7, 1).weekday()) % 7)  # next Saturday
    try:
        src = OpenMeteoSource(*CENTRAL_PARK)
        st = src.state((sat.isoformat(), 12, 18))
        print(f"  {sat} 12:00–18:00 → category={st.category!r}  ({st.summary})")
        print("  (real data — whatever the sky is doing today; the spine treats it the same)\n")
    except Exception as e:
        print(f"  (skipped — no network: {e})\n")


async def main():
    await scripted_demo()
    real_pull()


if __name__ == "__main__":
    asyncio.run(main())
