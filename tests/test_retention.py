from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from db_backup_s3.config import Retention
from db_backup_s3.retention import plan_retention
from db_backup_s3.storage import BackupObject, build_key

START = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)


def daily(days: int, database: str = "app", per_day: int = 1) -> list[BackupObject]:
    items = []
    for d in range(days):
        for h in range(per_day):
            created = START + timedelta(days=d, hours=h)
            items.append(BackupObject(build_key("b", database, created, "none"), database, created, 1, "none"))
    return items


def kept_dates(plan) -> list[str]:
    return sorted({b.created.date().isoformat() for b in plan.keep})


def test_disabled_keeps_everything():
    backups = daily(10)
    plan = plan_retention(backups, Retention())
    assert len(plan.keep) == 10 and plan.delete == []


def test_keep_last():
    plan = plan_retention(daily(10), Retention(keep_last=3))
    assert [b.created.day for b in plan.keep] == [10, 9, 8]
    assert len(plan.delete) == 7


def test_keep_daily_picks_newest_per_day():
    plan = plan_retention(daily(5, per_day=3), Retention(keep_daily=2))
    assert len(plan.keep) == 2
    assert all(b.created.hour == 5 for b in plan.keep)  # 03:00 + 2h, the last of each day
    assert kept_dates(plan) == ["2026-01-04", "2026-01-05"]


def test_gfs_combination():
    backups = daily(120)  # 2026-01-01 .. 2026-04-30
    plan = plan_retention(backups, Retention(keep_daily=7, keep_weekly=4, keep_monthly=3))
    dates = kept_dates(plan)
    # 7 dailies (Apr 24-30); weekly adds the newest of 3 earlier ISO weeks (Sundays Apr 26 is
    # already daily, then Apr 19 and Apr 12); monthly adds the month ends of March and February.
    assert dates[-7:] == [f"2026-04-{d:02d}" for d in range(24, 31)]
    assert "2026-04-19" in dates and "2026-04-12" in dates and "2026-04-05" not in dates
    assert "2026-03-31" in dates and "2026-02-28" in dates
    assert "2026-01-31" not in dates
    assert len(dates) == 7 + 2 + 2
    assert len(plan.keep) + len(plan.delete) == 120
    assert "monthly" in plan.reasons[next(b.key for b in plan.keep if b.created.date().isoformat() == "2026-03-31")]


def test_newest_always_kept_and_databases_separate():
    backups = daily(5, "app") + daily(2, "crm")
    plan = plan_retention(backups, Retention(keep_monthly=1))
    kept = {(b.database, b.created.day) for b in plan.keep}
    assert kept == {("app", 5), ("crm", 2)}


def test_timezone_changes_day_boundaries():
    # 23:30 UTC on Jan 1 is already Jan 2 in Tokyo.
    a = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
    b = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    items = [BackupObject(build_key("p", "app", t, "none"), "app", t, 1, "none") for t in (a, b)]
    assert len(plan_retention(items, Retention(keep_daily=5)).keep) == 2
    assert len(plan_retention(items, Retention(keep_daily=5), ZoneInfo("Asia/Tokyo")).keep) == 1
