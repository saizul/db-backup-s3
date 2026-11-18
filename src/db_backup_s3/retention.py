"""Grandfather-father-son retention.

A backup is kept if it is selected by *any* rule:

- ``keep_last``: the N most recent backups;
- ``keep_daily``: the newest backup of each of the N most recent days that have backups;
- ``keep_weekly``: the same per ISO week;
- ``keep_monthly``: the same per calendar month.

Days, weeks and months are evaluated in the configured time zone. When every
rule is 0, retention is disabled and nothing is deleted. Rules are applied per
database, and the newest backup is always kept.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from db_backup_s3.config import Retention
from db_backup_s3.storage import BackupObject


@dataclass(frozen=True)
class Plan:
    keep: list[BackupObject]
    delete: list[BackupObject]
    reasons: dict[str, list[str]]


def _buckets(
    backups: list[BackupObject], count: int, period: Callable[[datetime], Hashable], tz: ZoneInfo
) -> list[BackupObject]:
    chosen: list[BackupObject] = []
    seen: set[Hashable] = set()
    for backup in backups:  # newest first
        if len(seen) >= count:
            break
        key = period(backup.created.astimezone(tz))
        if key not in seen:
            seen.add(key)
            chosen.append(backup)
    return chosen


def plan_retention(backups: Iterable[BackupObject], policy: Retention, tz: ZoneInfo | None = None) -> Plan:
    zone = tz or ZoneInfo("UTC")
    items = list(backups)
    if not policy.enabled:
        return Plan(keep=sorted(items, key=lambda b: b.created, reverse=True), delete=[], reasons={})

    by_db: dict[str, list[BackupObject]] = defaultdict(list)
    for backup in items:
        by_db[backup.database].append(backup)

    keep: list[BackupObject] = []
    delete: list[BackupObject] = []
    reasons: dict[str, list[str]] = defaultdict(list)
    rules: list[tuple[str, int, Callable[[datetime], Hashable]]] = [
        ("daily", policy.keep_daily, lambda d: d.date()),
        ("weekly", policy.keep_weekly, lambda d: d.isocalendar()[:2]),
        ("monthly", policy.keep_monthly, lambda d: (d.year, d.month)),
    ]
    for group in by_db.values():
        group.sort(key=lambda b: (b.created, b.key), reverse=True)
        selected: set[str] = set()
        for backup in group[: max(policy.keep_last, 1)]:
            selected.add(backup.key)
            reasons[backup.key].append("last" if policy.keep_last else "newest")
        for name, count, period in rules:
            if count:
                for backup in _buckets(group, count, period, zone):
                    selected.add(backup.key)
                    reasons[backup.key].append(name)
        for backup in group:
            (keep if backup.key in selected else delete).append(backup)

    keep.sort(key=lambda b: b.created, reverse=True)
    delete.sort(key=lambda b: b.created, reverse=True)
    return Plan(keep=keep, delete=delete, reasons=dict(reasons))
