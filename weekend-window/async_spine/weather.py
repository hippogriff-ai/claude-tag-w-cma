"""
weather.py — turn a forecast into a small, comparable **weather-state**.

The essence of the demo is "post only when it CHANGES", so everything hinges on a
stable, discretized state we can compare tick-to-tick. Real data is Open-Meteo
(free, no key). A ScriptedSource lets the demo show a change in seconds.

Zero third-party deps (stdlib urllib) so the demo runs anywhere python3 does.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes → our four categories. Worst-wins over the window.
_HAZARD = set([95, 96, 99, 66, 67, 75, 82, 86])          # thunderstorm, freezing rain, heavy snow/showers
_SLIPPERY = set([56, 57, 71, 73, 77, 85])                 # freezing drizzle, snow
_RAIN = set([51, 53, 55, 61, 63, 65, 80, 81])             # drizzle / rain / showers
# everything else (0–3 clear/cloudy, 45/48 fog) => "good"

_SEVERITY = {"good": 0, "rain": 1, "slippery": 2, "hazard": 3}
_LABELS = {95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
           66: "freezing rain", 67: "freezing rain", 75: "heavy snow",
           82: "heavy showers", 86: "snow showers"}


def _cat(code: int) -> str:
    if code in _HAZARD:
        return "hazard"
    if code in _SLIPPERY:
        return "slippery"
    if code in _RAIN:
        return "rain"
    return "good"


def _ampm(h: int) -> str:
    if h == 0:
        return "12am"
    if h == 12:
        return "12pm"
    return f"{h-12}pm" if h > 12 else f"{h}am"


@dataclass(frozen=True)
class WeatherState:
    """A comparable snapshot. Equality (category + hazards) drives change detection."""
    category: str                                   # good | rain | slippery | hazard
    hazards: tuple = ()                              # ((kind, start_hour, end_hour), ...)
    summary: str = ""                               # human line (not part of identity)

    def key(self):
        return (self.category, self.hazards)

    def __eq__(self, other):
        return isinstance(other, WeatherState) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())


def classify(hourly: dict, date: str, start_hour: int, end_hour: int) -> WeatherState:
    """Reduce a window's hourly weather codes to one WeatherState."""
    codes = []
    for i, t in enumerate(hourly.get("time", [])):
        if not t.startswith(date):
            continue
        hh = int(t[11:13])
        if start_hour <= hh < end_hour:
            codes.append((hh, int(hourly["weathercode"][i])))

    if not codes:
        return WeatherState("good", (), "no forecast for this window yet")

    worst = "good"
    for _, c in codes:
        cat = _cat(c)
        if _SEVERITY[cat] > _SEVERITY[worst]:
            worst = cat

    # contiguous hazard/slippery runs → (kind, start, end)
    hazards = []
    run_start = None
    run_kind = None
    prev = None
    for hh, c in codes + [(None, None)]:
        cat = _cat(c) if c is not None else "good"
        bad = cat in ("hazard", "slippery")
        if bad and run_start is None:
            run_start, run_kind = hh, _LABELS.get(c, cat)
        elif not bad and run_start is not None:
            hazards.append((run_kind, run_start, prev + 1))
            run_start, run_kind = None, None
        prev = hh
    hazards = tuple(hazards)

    # human summary
    if worst == "good":
        summary = "looks good — clear/pleasant"
    elif hazards:
        kind, s, e = hazards[0]
        summary = f"{kind} around {_ampm(s)}–{_ampm(e)}"
    elif worst == "rain":
        summary = "rain likely in the window"
    else:
        summary = f"{worst} conditions"

    return WeatherState(worst, hazards, summary)


# ── sources ──────────────────────────────────────────────────────────────────

class OpenMeteoSource:
    """Real forecast for a pinned lat/lon. `.state(window)` fetches + classifies."""
    def __init__(self, lat: float, lon: float):
        self.lat, self.lon = lat, lon

    def state(self, window) -> WeatherState:
        date, start_hour, end_hour = window
        q = urllib.parse.urlencode({
            "latitude": self.lat, "longitude": self.lon,
            "hourly": "weathercode,precipitation_probability,temperature_2m",
            "timezone": "auto", "temperature_unit": "fahrenheit",
            "start_date": date, "end_date": date,
        })
        with urllib.request.urlopen(f"{OPEN_METEO}?{q}", timeout=20) as r:
            hourly = json.loads(r.read().decode())["hourly"]
        return classify(hourly, date, start_hour, end_hour)


class ScriptedSource:
    """Demo source: yields a fixed sequence of states so a change happens in seconds."""
    def __init__(self, states):
        self._states = list(states)
        self._i = 0

    def state(self, window) -> WeatherState:
        s = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return s

    @property
    def exhausted(self) -> bool:
        return self._i >= len(self._states)
