"""
spine.py — the async spine: `schedule_monitor` / `cancel_monitor`.

This is the whole point of the demo: a watch the agent stands up ON REQUEST that
re-checks on a cadence and **notifies only when the weather-state changes** — then
goes quiet again. The "what did I last say" state lives right here (in the running
monitor), mirroring "the model owns its memory".

A monitor is a task with: a source (real or scripted), the window it watches, a
cadence, a stop time, and an `on_update(state, first)` callback (the sink — console
now, Slack later). Nothing runs repetitively without a reason: monitors are created
per request, bounded by `until`, and cancellable.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Monitor:
    id: str
    label: str                       # e.g. "Central Park · Sat afternoon"
    source: object                   # OpenMeteoSource | ScriptedSource
    window: tuple                    # (date, start_hour, end_hour)
    cadence_s: float
    on_update: Callable              # (WeatherState, first: bool) -> None
    max_ticks: Optional[int] = None  # stop after N checks (bounds the demo)
    log: bool = False                # heartbeat line per tick (production observability;
                                     # off in tests to keep their output clean)


class MonitorManager:
    """Holds the live monitors. schedule_monitor / cancel_monitor are what the agent calls."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._last: dict[str, object] = {}     # monitor_id -> last WeatherState we posted
        self._monitors: dict[str, Monitor] = {}
        self._status: dict[str, dict] = {}     # monitor_id -> {ticks, state, at}

    def schedule_monitor(self, m: Monitor) -> str:
        """Stand up a recurring check. Returns the monitor id."""
        if m.id in self._tasks:
            self.cancel_monitor(m.id)
        if m.log:
            print(f"   ▶ watch started: {m.label} — every {m.cadence_s/60:g} min"
                  + (f", ≤{m.max_ticks} checks" if m.max_ticks else ""))
        self._monitors[m.id] = m
        self._tasks[m.id] = asyncio.ensure_future(self._run(m))
        return m.id

    def cancel_monitor(self, monitor_id: str) -> bool:
        t = self._tasks.pop(monitor_id, None)
        self._last.pop(monitor_id, None)
        self._monitors.pop(monitor_id, None)
        self._status.pop(monitor_id, None)
        if t and not t.done():
            t.cancel()
            print(f"   ■ watch cancelled: {monitor_id}")
            return True
        return False

    def active(self):
        return list(self._tasks.keys())

    def describe(self) -> list[dict]:
        """Ground truth about the running watches (for the agent's list_monitors tool):
        label, cadence, checks so far, last observed state, seconds since last check."""
        import time
        out = []
        for mid, m in self._monitors.items():
            st = self._status.get(mid, {})
            out.append({
                "id": mid,
                "label": m.label,
                "cadence_s": m.cadence_s,
                "checks": st.get("ticks", 0),
                "last_state": st.get("state"),
                "seconds_since_check": (time.time() - st["at"]) if "at" in st else None,
            })
        return out

    async def join(self):
        """Wait for all monitors to finish (used by the demo)."""
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, m: Monitor):
        ticks = 0
        try:
            while True:
                try:
                    state = m.source.state(m.window)
                except Exception as e:            # fault tolerance: one bad check doesn't kill the watch
                    print(f"   ⚠ {m.label}: check failed ({e}); will retry")
                    state = None

                if state is not None:
                    import time
                    self._status[m.id] = {"ticks": ticks + 1, "state": state.category,
                                          "at": time.time()}
                    last = self._last.get(m.id)
                    changed = state != last
                    if m.log:
                        print(f"   ⏱ {m.label}: checked → {state.category}"
                              + (" — CHANGED, notifying" if changed else " — no change, staying quiet"))
                    if changed:                   # ← the essence: post ONLY on change
                        first = last is None
                        self._last[m.id] = state
                        m.on_update(state, first)
                    # else: unchanged → stay quiet (no spam)

                ticks += 1
                exhausted = getattr(m.source, "exhausted", False)
                if (m.max_ticks and ticks >= m.max_ticks) or exhausted:
                    break
                await asyncio.sleep(m.cadence_s)
        except asyncio.CancelledError:
            pass
        finally:
            self._tasks.pop(m.id, None)
            self._last.pop(m.id, None)
            self._monitors.pop(m.id, None)
            self._status.pop(m.id, None)
