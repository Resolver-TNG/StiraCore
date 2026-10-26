"""Schedules for operator-defined jobs (#239, ADR 0008 §1): cron, interval, once.

All functions are pure: they take epoch seconds and return epoch seconds, so the
scheduler can drive them with an injected (fake) clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["CronExpression", "Schedule", "ScheduleError", "parse_schedule"]

MIN_INTERVAL_SEC = 60
# A cron expression that matches nothing within this many days is refused.
_SEARCH_DAYS = 366 * 5
_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),  # 0 and 7 are Sunday
)


class ScheduleError(ValueError):
    """The schedule definition is not valid."""


def _is_number(text: str) -> bool:
    """ASCII digits only: ``"²".isdigit()`` is true but ``int("²")`` fails (#601 B12)."""
    return text.isascii() and text.isdigit()


def _parse_field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ScheduleError(f"empty {name} list item")
        base, sep, step_text = part.partition("/")
        step = 1
        if sep:
            if not _is_number(step_text) or int(step_text) < 1:
                raise ScheduleError(f"invalid {name} step: {step_text!r}")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            if not (_is_number(start_text) and _is_number(end_text)):
                raise ScheduleError(f"invalid {name} range: {base!r}")
            start, end = int(start_text), int(end_text)
        elif _is_number(base):
            start = int(base)
            end = high if sep else start
        else:
            raise ScheduleError(f"invalid {name} value: {base!r}")
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise ScheduleError(f"{name} out of range: {base!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class CronExpression:
    """Five-field cron (minute hour day-of-month month day-of-week), numeric only."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, text: Any) -> CronExpression:
        if not isinstance(text, str) or len(text) > 200:
            raise ScheduleError("cron expression must be a string of at most 200 characters")
        parts = text.split()
        if len(parts) != 5:
            raise ScheduleError("cron expression needs exactly 5 fields")
        parsed = [_parse_field(p, n, lo, hi) for p, (n, lo, hi) in zip(parts, _FIELDS)]
        weekdays = frozenset(0 if d == 7 else d for d in parsed[4])
        return cls(
            text=" ".join(parts),
            minutes=parsed[0],
            hours=parsed[1],
            days=parsed[2],
            months=parsed[3],
            weekdays=weekdays,
            # Vixie cron: a field whose text starts with "*" (also "*/n") is a star field.
            dom_restricted=not parts[2].startswith("*"),
            dow_restricted=not parts[4].startswith("*"),
        )

    def _day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = (day.isoweekday() % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom or dow  # Vixie cron: either restriction
        return dom and dow

    def next_after(self, after: float, tz: Any) -> float | None:
        """Smallest fire time strictly greater than ``after`` (epoch seconds)."""
        start = datetime.fromtimestamp(math.floor(after), tz)
        day = start.date()
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        for index in range(_SEARCH_DAYS):
            if self._day_matches(day):
                for hour in hours:
                    # Prune the first day (one hour of margin for a DST fold); the
                    # epoch comparison below stays the authority.
                    if index == 0 and hour < start.hour - 1:
                        continue
                    for minute in minutes:
                        if index == 0 and hour == start.hour and minute < start.minute:
                            continue
                        wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                        epoch = wall.timestamp()
                        if epoch <= after:
                            continue
                        # A wall time inside a DST gap does not exist: skip it.
                        back = datetime.fromtimestamp(epoch, tz)
                        if (back.hour, back.minute, back.day) != (hour, minute, day.day):
                            continue
                        return float(epoch)
            day += timedelta(days=1)
        return None


def _zone(name: Any):
    if name in (None, "UTC"):
        return UTC
    if not isinstance(name, str) or len(name) > 64:
        raise ScheduleError("timezone must be an IANA name")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleError(f"unknown timezone: {name!r}") from error


def _epoch(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScheduleError(f"{name} must be a finite number (epoch seconds)")
    try:
        finite = math.isfinite(value)  # an int beyond float range overflows (#601 B13)
    except OverflowError:
        finite = False
    if not finite:
        raise ScheduleError(f"{name} must be a finite number (epoch seconds)")
    if value < 0:
        raise ScheduleError(f"{name} must not be negative")
    return float(value)


@dataclass(frozen=True)
class Schedule:
    type: str
    cron: CronExpression | None = None
    timezone: str = "UTC"
    every_sec: int | None = None
    anchor: float | None = None
    at: float | None = None

    def next_after(self, after: float) -> float | None:
        """Next fire strictly after ``after``; ``None`` when the schedule is exhausted."""
        if self.type == "cron":
            assert self.cron is not None
            return self.cron.next_after(after, _zone(self.timezone))
        if self.type == "interval":
            assert self.every_sec is not None and self.anchor is not None
            if after < self.anchor:
                return self.anchor
            steps = math.floor((after - self.anchor) / self.every_sec) + 1
            return self.anchor + steps * self.every_sec
        assert self.at is not None
        return self.at if self.at > after else None

    def fires_between(self, after: float, until: float, limit: int) -> list[float]:
        """Fire times in ``(after, until]``, at most ``limit`` (the newest are kept)."""
        fires: list[float] = []
        if self.type == "interval" and self.every_sec is not None and self.anchor is not None:
            # Closed form: jump straight to the window instead of stepping through it.
            first = self.next_after(after)
            if first is None or first > until:
                return []
            count = math.floor((until - first) / self.every_sec) + 1
            skip = max(0, count - limit)
            return [first + (skip + i) * self.every_sec for i in range(count - skip)]
        cursor = after
        while True:
            fire = self.next_after(cursor)
            if fire is None or fire > until:
                return fires
            fires.append(fire)
            if len(fires) > limit:
                fires.pop(0)
            cursor = fire

    def to_dict(self) -> dict[str, Any]:
        if self.type == "cron":
            assert self.cron is not None
            return {"type": "cron", "expr": self.cron.text, "timezone": self.timezone}
        if self.type == "interval":
            return {"type": "interval", "every_sec": self.every_sec, "anchor": self.anchor}
        return {"type": "once", "at": self.at}


def parse_schedule(value: Any, *, default_anchor: float) -> Schedule:
    try:
        return _parse_schedule(value, default_anchor=default_anchor)
    except (TypeError, AttributeError) as error:
        # e.g. {"type": []}: a wrong type is an invalid definition, never a 500.
        raise ScheduleError(f"invalid schedule field type ({type(error).__name__})") from None


def _parse_schedule(value: Any, *, default_anchor: float) -> Schedule:
    if not isinstance(value, dict):
        raise ScheduleError("schedule must be an object")
    kind = value.get("type")
    if not isinstance(kind, str):
        raise ScheduleError("schedule.type must be cron, interval or once")
    allowed = {
        "cron": {"type", "expr", "timezone"},
        "interval": {"type", "every_sec", "anchor"},
        "once": {"type", "at"},
    }
    if kind not in allowed:
        raise ScheduleError("schedule.type must be cron, interval or once")
    unknown = set(value) - allowed[kind]
    if unknown:
        raise ScheduleError(f"unknown schedule field(s): {', '.join(sorted(unknown))}")
    if kind == "cron":
        expression = CronExpression.parse(value.get("expr"))
        tz_name = value.get("timezone", "UTC")
        _zone(tz_name)
        schedule = Schedule("cron", cron=expression, timezone=tz_name)
        if schedule.next_after(default_anchor) is None:
            raise ScheduleError("cron expression never fires")
        return schedule
    if kind == "interval":
        every = value.get("every_sec")
        if isinstance(every, bool) or not isinstance(every, int):
            raise ScheduleError("every_sec must be an integer")
        if every < MIN_INTERVAL_SEC or every > 366 * 86400:
            raise ScheduleError(f"every_sec must be between {MIN_INTERVAL_SEC} and 31622400")
        anchor = value.get("anchor")
        anchor = default_anchor if anchor is None else _epoch(anchor, "anchor")
        return Schedule("interval", every_sec=every, anchor=anchor)
    return Schedule("once", at=_epoch(value.get("at"), "at"))
