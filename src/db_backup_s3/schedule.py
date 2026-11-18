"""A small, dependency-free cron expression parser.

Supports the classic five fields (minute, hour, day of month, month, day of
week) with ``*``, lists (``1,15``), ranges (``1-5``), steps (``*/15``,
``0-30/10``), month and weekday names (``jan``, ``mon``) and the aliases
``@hourly``, ``@daily``/``@midnight``, ``@weekly``, ``@monthly`` and
``@yearly``/``@annually``. Like Vixie cron, when both day of month and day of
week are restricted a day matches if *either* matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_MONTH_NAMES = "jan feb mar apr may jun jul aug sep oct nov dec"
_MONTHS = {m: i for i, m in enumerate(_MONTH_NAMES.split(" "), start=1)}
_DAYS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
_FIELDS = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _DAYS),
)
_MAX_DAYS = 366 * 5


class CronError(ValueError):
    """The cron expression is invalid."""


def _value(token: str, low: int, high: int, names: dict[str, int], label: str) -> int:
    lowered = token.lower()
    if lowered in names:
        return names[lowered]
    if not token.isdigit():
        raise CronError(f"{label}: {token!r} is not a number")
    number = int(token)
    if not low <= number <= high:
        raise CronError(f"{label}: {number} is out of range {low}-{high}")
    return number


def _parse_field(text: str, low: int, high: int, names: dict[str, int], label: str) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{label}: empty list item in {text!r}")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"{label}: step {step_text!r} must be a positive number")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, low, high, names, label), _value(b, low, high, names, label)
            if start > end:
                raise CronError(f"{label}: range {part!r} goes backwards")
        else:
            start = _value(part, low, high, names, label)
            end = high if step > 1 else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class CronSchedule:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        text = " ".join(expression.split())
        resolved = ALIASES.get(text.lower(), text)
        if resolved.startswith("@"):
            raise CronError(f"unknown alias {text!r}; use one of {', '.join(ALIASES)}")
        parts = resolved.split(" ")
        if len(parts) != 5:
            raise CronError(
                f"expected 5 fields (minute hour day-of-month month day-of-week), got {len(parts)} in {text!r}"
            )
        parsed = [
            _parse_field(p, lo, hi, names, label) for p, (label, lo, hi, names) in zip(parts, _FIELDS, strict=True)
        ]
        weekdays = frozenset(0 if d == 7 else d for d in parsed[4])
        return cls(
            expression=text,
            minutes=parsed[0],
            hours=parsed[1],
            days=parsed[2],
            months=parsed[3],
            weekdays=weekdays,
            dom_restricted=parts[2] != "*",
            dow_restricted=parts[4] != "*",
        )

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = (day.isoweekday() % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom or dow
        return dom and dow

    def next_after(self, moment: datetime, tz: ZoneInfo | None = None) -> datetime:
        """Return the first matching time strictly after ``moment`` (timezone aware)."""
        zone = tz or ZoneInfo("UTC")
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone aware")
        local = moment.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        day = local.replace(hour=0, minute=0)
        for _ in range(_MAX_DAYS):
            if self._day_matches(day):
                for hour in sorted(self.hours):
                    for minute in sorted(self.minutes):
                        candidate = day.replace(hour=hour, minute=minute)
                        if candidate < local:
                            continue
                        aware = candidate.replace(tzinfo=zone)
                        # Skip wall-clock times that do not exist (DST gap).
                        roundtrip = aware.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None)
                        if roundtrip != candidate:
                            continue
                        return aware
            day += timedelta(days=1)
        raise CronError(f"{self.expression!r} never matches a real date")
