"""Feeding plan stored by the feeder in MIoT property 5.1.

The firmware uses a bracketed list that is *not* valid JSON::

    [1,08000201,18000300]
     │ └┬─┘└┬┘└┬┘
     │  │   │  └─ 01 = active, 00 = skipped for today only
     │  │   └──── portions (1-15, about 10 g each)
     │  └──────── time of day HHMM (device local time)
     └─────────── automatic feeding master switch

The Mi Home plugin also accepts a JSON form
``{"state":1,"schedule":[{"t":seconds,"v":portions,"e":0|1}]}``; it is parsed
here for completeness but always written back in the bracket form.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import pairwise
import json

MAX_ENTRIES = 35
MIN_GAP_MINUTES = 5
MIN_PORTIONS = 1
MAX_PORTIONS = 15
GRAMS_PER_PORTION = 10


class ScheduleError(ValueError):
    """The schedule violates one of the rules enforced by the app."""

    def __init__(self, key: str, **placeholders: str) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


@dataclass(frozen=True, slots=True, order=True)
class ScheduleEntry:
    """One daily feeding."""

    hour: int
    minute: int
    portions: int = field(compare=False)
    enabled: bool = field(default=True, compare=False)

    @property
    def minutes(self) -> int:
        """Minutes since midnight."""
        return self.hour * 60 + self.minute

    @property
    def time_str(self) -> str:
        """HH:MM."""
        return f"{self.hour:02d}:{self.minute:02d}"

    def encode(self) -> str:
        """Firmware representation HHMMPPEE."""
        return f"{self.hour:02d}{self.minute:02d}{self.portions:02d}{1 if self.enabled else 0:02d}"


@dataclass(frozen=True, slots=True)
class FeedingSchedule:
    """Master switch plus the list of daily feedings."""

    auto_feed: bool
    entries: tuple[ScheduleEntry, ...] = ()

    @classmethod
    def parse(cls, raw: str | None) -> FeedingSchedule:
        """Parse the value of property 5.1."""
        if not raw:
            return cls(auto_feed=False)
        raw = str(raw).strip()
        if raw.startswith("{"):
            return cls._parse_json(raw)
        items = [part.strip() for part in raw.strip("[]").split(",") if part.strip()]
        if not items:
            return cls(auto_feed=False)
        auto_feed = items[0] == "1"
        entries = []
        for item in items[1:]:
            if len(item) != 8 or not item.isdigit():
                continue
            entries.append(
                ScheduleEntry(
                    hour=int(item[0:2]),
                    minute=int(item[2:4]),
                    portions=int(item[4:6]),
                    enabled=int(item[6:8]) != 0,
                )
            )
        return cls(auto_feed=auto_feed, entries=tuple(sorted(entries)))

    @classmethod
    def _parse_json(cls, raw: str) -> FeedingSchedule:
        try:
            data = json.loads(raw)
        except ValueError:
            return cls(auto_feed=False)
        entries = []
        for item in data.get("schedule") or []:
            seconds = int(item.get("t", 0))
            entries.append(
                ScheduleEntry(
                    hour=seconds // 3600,
                    minute=(seconds % 3600) // 60,
                    portions=int(item.get("v", 1)),
                    enabled=bool(item.get("e", 1)),
                )
            )
        return cls(auto_feed=bool(data.get("state")), entries=tuple(sorted(entries)))

    def encode(self) -> str:
        """Firmware representation, written with action 5.2."""
        parts = ["1" if self.auto_feed else "0", *(e.encode() for e in sorted(self.entries))]
        return "[" + ",".join(parts) + "]"

    def validate(self) -> None:
        """Apply the same rules as the Mi Home plugin."""
        if len(self.entries) > MAX_ENTRIES:
            raise ScheduleError("schedule_too_many", max=str(MAX_ENTRIES))
        for entry in self.entries:
            if not 0 <= entry.hour <= 23 or not 0 <= entry.minute <= 59:
                raise ScheduleError("schedule_invalid_time", time=entry.time_str)
            if not MIN_PORTIONS <= entry.portions <= MAX_PORTIONS:
                raise ScheduleError(
                    "schedule_invalid_portions",
                    time=entry.time_str,
                    min=str(MIN_PORTIONS),
                    max=str(MAX_PORTIONS),
                )
        ordered = sorted(self.entries)
        for prev, cur in pairwise(ordered):
            if cur.minutes - prev.minutes < MIN_GAP_MINUTES:
                raise ScheduleError("schedule_too_close", first=prev.time_str, second=cur.time_str)
        if len(ordered) > 1 and ordered[0].minutes + 1440 - ordered[-1].minutes < MIN_GAP_MINUTES:
            raise ScheduleError(
                "schedule_too_close", first=ordered[-1].time_str, second=ordered[0].time_str
            )

    def with_entries(
        self, entries: list[ScheduleEntry] | tuple[ScheduleEntry, ...], now_minutes: int
    ) -> FeedingSchedule:
        """Replace entries; like the app, past times are stored as skipped today."""
        fixed = tuple(sorted(replace(e, enabled=e.minutes > now_minutes) for e in entries))
        return replace(self, entries=fixed)

    def with_auto_feed(self, enabled: bool, now_minutes: int) -> FeedingSchedule:
        """Toggle the master switch; turning it on skips entries already past today."""
        if not enabled:
            return replace(self, auto_feed=False)
        entries = tuple(
            replace(e, enabled=e.enabled and e.minutes > now_minutes) for e in self.entries
        )
        return replace(self, auto_feed=True, entries=entries)

    def find(self, hour: int, minute: int) -> ScheduleEntry | None:
        """Entry at a given time."""
        return next((e for e in self.entries if (e.hour, e.minute) == (hour, minute)), None)
