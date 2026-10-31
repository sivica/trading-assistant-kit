"""Sports "live game" detection from Gamma market data. Pure functions: easy to test."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import os

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "UTC"))   # your timezone, e.g. Europe/Berlin
except Exception:  # pragma: no cover
    LOCAL_TZ = timezone.utc


def parse_ts(s) -> Optional[datetime]:
    """'2026-10-31 17:30:00+00' / '2026-10-31T17:30:00Z' -> aware UTC datetime (None if unparseable)."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    if len(t) >= 3 and t[-3] in "+-" and t[-3:-2] != ":" and t[-6:-5] not in "+-":
        t = t + ":00"                       # '+00' -> '+00:00'
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def game_start(m: Optional[dict]) -> tuple[Optional[datetime], str, bool]:
    """(start time, where it came from, looks like a sports market)."""
    if not m:
        return None, "", False
    events = m.get("events") or []
    sports = bool(m.get("sportsMarketType") or m.get("gameStartTime")
                  or any(e.get("gameId") for e in events if isinstance(e, dict)))
    st = parse_ts(m.get("gameStartTime"))
    if st:
        return st, "gameStartTime", True
    if sports:   # only trust an event startTime for sports events (politics events have one too)
        for e in events:
            if isinstance(e, dict):
                st = parse_ts(e.get("startTime"))
                if st:
                    return st, "event startTime", True
    return None, "", sports


def local(d: datetime) -> str:
    return d.astimezone(LOCAL_TZ).strftime("%d %b %H:%M")


@dataclass
class GameInfo:
    phase: str                  # "none" (not sports) | "unknown" | "pre" | "live" | "after" | "closed"
    start: Optional[datetime] = None
    source: str = ""
    note: str = ""

    @property
    def live(self) -> bool:
        return self.phase == "live"

    def label(self) -> str:
        if self.phase == "none":
            return ""
        if self.phase == "unknown":
            return "game time ?"
        s = self.start.astimezone(LOCAL_TZ).strftime("%H:%M") if self.start else "?"
        return {"pre": f"starts {s}", "live": f"LIVE since {s}", "after": f"ended (start {s})",
                "closed": "market closed"}.get(self.phase, self.phase)


def classify(m: Optional[dict], now: datetime, window_hours: float, fetch_failed: bool = False) -> GameInfo:
    if m is None:
        return GameInfo("unknown", note="market data unavailable" if fetch_failed else "market not found")
    start, src, sports = game_start(m)
    if m.get("closed"):
        return GameInfo("closed", start, src)
    if start is None:
        return GameInfo("unknown" if sports else "none", note="sports market without a start time" if sports else "")
    if now < start:
        return GameInfo("pre", start, src)
    if now < start + timedelta(hours=window_hours):
        return GameInfo("live", start, src)
    return GameInfo("after", start, src)
