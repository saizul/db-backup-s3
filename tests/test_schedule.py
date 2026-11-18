from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from db_backup_s3.schedule import CronError, CronSchedule


def nxt(expr: str, start: datetime, tz: str = "UTC") -> datetime:
    return CronSchedule.parse(expr).next_after(start, ZoneInfo(tz))


def test_daily_at_three():
    assert nxt("0 3 * * *", datetime(2026, 1, 1, 2, 59, tzinfo=UTC)) == datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
    assert nxt("0 3 * * *", datetime(2026, 1, 1, 3, 0, tzinfo=UTC)) == datetime(2026, 1, 2, 3, 0, tzinfo=UTC)


def test_steps_lists_ranges():
    s = CronSchedule.parse("*/15 9-17/4 * * 1-5")
    assert s.minutes == {0, 15, 30, 45}
    assert s.hours == {9, 13, 17}
    assert s.weekdays == {1, 2, 3, 4, 5}
    assert CronSchedule.parse("5,10 0 * * *").minutes == {5, 10}
    assert CronSchedule.parse("5/20 * * * *").minutes == {5, 25, 45}


def test_names_and_sunday_seven():
    s = CronSchedule.parse("0 0 * JAN,jul sun")
    assert s.months == {1, 7}
    assert s.weekdays == {0}
    assert CronSchedule.parse("0 0 * * 7").weekdays == {0}
    assert CronSchedule.parse("0 0 * * mon-fri").weekdays == {1, 2, 3, 4, 5}


def test_aliases():
    assert CronSchedule.parse("@daily").minutes == {0}
    assert nxt("@weekly", datetime(2026, 11, 18, 12, 0, tzinfo=UTC)) == datetime(2026, 11, 22, 0, 0, tzinfo=UTC)
    assert nxt("@monthly", datetime(2026, 11, 18, tzinfo=UTC)) == datetime(2026, 12, 1, tzinfo=UTC)
    assert nxt("@hourly", datetime(2026, 11, 18, 10, 30, tzinfo=UTC)) == datetime(2026, 11, 18, 11, 0, tzinfo=UTC)


def test_day_of_month_or_day_of_week():
    # Both fields restricted: "the 13th OR any Friday" (classic cron semantics).
    start = datetime(2026, 11, 1, tzinfo=UTC)  # a Sunday
    assert nxt("0 0 13 * 5", start) == datetime(2026, 11, 6, tzinfo=UTC)  # first Friday wins


def test_leap_day():
    assert nxt("0 0 29 2 *", datetime(2026, 3, 1, tzinfo=UTC)) == datetime(2028, 2, 29, tzinfo=UTC)


def test_timezone_and_dst_gap():
    result = nxt("0 3 * * *", datetime(2026, 7, 1, 0, 0, tzinfo=UTC), "Europe/Berlin")
    assert result.utcoffset().total_seconds() == 7200
    assert result.astimezone(UTC) == datetime(2026, 7, 1, 1, 0, tzinfo=UTC)
    # 02:30 does not exist in Berlin on 2026-03-29 (clocks jump 02:00 -> 03:00), so it is skipped.
    gap = nxt("30 2 * * *", datetime(2026, 3, 28, 12, 0, tzinfo=UTC), "Europe/Berlin")
    assert gap.date().isoformat() == "2026-03-30"


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        CronSchedule.parse("* * * * *").next_after(datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("* * * *", "expected 5 fields"),
        ("60 * * * *", "out of range"),
        ("* 24 * * *", "out of range"),
        ("*/0 * * * *", "positive"),
        ("5-1 * * * *", "backwards"),
        ("a * * * *", "not a number"),
        ("1,,2 * * * *", "empty"),
        ("@sometimes", "unknown alias"),
    ],
)
def test_invalid(expr, message):
    with pytest.raises(CronError, match=message):
        CronSchedule.parse(expr)


def test_impossible_date():
    with pytest.raises(CronError, match="never matches"):
        nxt("0 0 31 2 *", datetime(2026, 1, 1, tzinfo=UTC))
