"""Command line interface: ``db-backup-s3 run|backup|list|restore|prune|check-config``."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from db_backup_s3._version import __version__
from db_backup_s3.config import ConfigError, Settings, load_settings
from db_backup_s3.healthcheck import Healthcheck
from db_backup_s3.jobs import JobError, human_size, prune, run_backup, run_restore_job
from db_backup_s3.logs import get_logger, redact, register_secrets, setup_logging
from db_backup_s3.storage import Storage, StorageError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

StorageFactory = Callable[[Settings], Storage]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="db-backup-s3",
        description="Back up MySQL/MariaDB and PostgreSQL to S3-compatible storage. Configured with environment "
        "variables; see the README for the full list.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("run", help="run on SCHEDULE (cron), or once if SCHEDULE is empty (default)")
    sub.add_parser("backup", help="back up all configured databases once and exit")

    p_list = sub.add_parser("list", help="list backups in the bucket")
    p_list.add_argument("--database", "-d", help="only this database ('all' for all-databases backups)")
    p_list.add_argument("--json", action="store_true", help="print JSON instead of a table")

    p_restore = sub.add_parser("restore", help="restore a backup into the database (or to a file)")
    p_restore.add_argument("backup", help="object key, file name, or 'latest'")
    p_restore.add_argument("--database", "-d", help="database the backup belongs to (required with 'latest')")
    p_restore.add_argument("--target-db", help="restore into this database instead of the original one")
    p_restore.add_argument("--output", "-o", help="write the plain SQL to this file ('-' for stdout) instead")
    p_restore.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")

    p_prune = sub.add_parser("prune", help="apply the retention policy now")
    p_prune.add_argument("--dry-run", action="store_true", help="only show what would be deleted")

    p_check = sub.add_parser("check-config", help="validate the configuration and show it (secrets masked)")
    p_check.add_argument("--connect", action="store_true", help="also check that the bucket is reachable")
    return parser


def _load(purpose: str) -> Settings:
    settings = load_settings(purpose=purpose)
    register_secrets(*settings.secrets())
    setup_logging(settings.log_level, settings.log_format)
    return settings


def _healthcheck(settings: Settings) -> Healthcheck:
    return Healthcheck(settings.healthcheck_url, settings.healthcheck_kind, settings.healthcheck_timeout)


def _backup_once(settings: Settings, storage: Storage) -> int:
    healthcheck = _healthcheck(settings)
    try:
        report = run_backup(settings, storage, healthcheck)
    except Exception as exc:  # an unexpected bug must still alert and must not stop the scheduler
        get_logger().exception("Backup crashed")
        healthcheck.ping("fail", f"Backup crashed: {type(exc).__name__}: {exc}")
        return EXIT_FAILED
    for line in report.summary().splitlines():
        get_logger().info("%s", line)
    return EXIT_OK if report.ok else EXIT_FAILED


def run_scheduler(
    settings: Settings,
    storage: Storage,
    stop: threading.Event,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Run backups on ``settings.schedule`` until ``stop`` is set."""
    log = get_logger()
    assert settings.schedule is not None
    if settings.run_on_startup and not stop.is_set():
        _backup_once(settings, storage)
    while not stop.is_set():
        upcoming = settings.schedule.next_after(now(), settings.tz)
        log.info("Next backup at %s (%s)", upcoming.isoformat(), settings.schedule.expression)
        while not stop.is_set():
            remaining = (upcoming - now()).total_seconds()
            if remaining <= 0:
                break
            stop.wait(min(remaining, 60))
        if stop.is_set():
            break
        _backup_once(settings, storage)
    log.info("Stopped")
    return EXIT_OK


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: object) -> None:
        get_logger().info("Received %s, finishing the current step and stopping", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def _print_list(storage: Storage, database: str | None, as_json: bool) -> int:
    backups = storage.list_backups(database)
    if as_json:
        rows = [
            {
                "key": b.key,
                "database": b.database,
                "created": b.created.isoformat(),
                "size": b.size,
                "encryption": b.encryption,
            }
            for b in backups
        ]
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    if not backups:
        print("No backups found.")
        return EXIT_OK
    width = max(len(b.database) for b in backups)
    print(f"{'CREATED (UTC)':<20}  {'DATABASE':<{width}}  {'SIZE':>10}  {'ENC':<4}  KEY")
    for b in backups:
        created = b.created.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{created:<20}  {b.database:<{width}}  {human_size(b.size):>10}  {b.encryption:<4}  {b.key}")
    total = sum(b.size for b in backups)
    print(f"\n{len(backups)} backup(s), {human_size(total)} in total")
    return EXIT_OK


def _confirm(args: argparse.Namespace, settings: Settings) -> bool:
    if args.yes or args.output:
        return True
    target = args.target_db or args.database or "the original database"
    if not sys.stdin.isatty():
        print("Refusing to restore without confirmation; pass --yes", file=sys.stderr)
        return False
    answer = input(
        f"Restore {args.backup!r} into {target} on {settings.host}:{settings.port}? Existing data may be "
        "overwritten. Type 'yes' to continue: "
    )
    return answer.strip().lower() == "yes"


def main(argv: Sequence[str] | None = None, storage_factory: StorageFactory = Storage) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "run"
    setup_logging()
    log = get_logger()

    purpose = {"list": "storage", "restore": "storage" if getattr(args, "output", None) else "restore"}.get(
        command, "backup"
    )
    try:
        settings = _load(purpose)
    except ConfigError as exc:
        print(f"db-backup-s3: {exc}", file=sys.stderr)
        print("See https://github.com/saizul/db-backup-s3#configuration", file=sys.stderr)
        return EXIT_CONFIG

    if command == "check-config":
        print("Configuration is valid:")
        for name, value in settings.summary().items():
            print(f"  {name:<18} {value}")
        if settings.schedule:
            moment = datetime.now(UTC)
            print("  next runs:")
            for _ in range(3):
                moment = settings.schedule.next_after(moment, settings.tz)
                print(f"    {moment.isoformat()}")
        if not args.connect:
            return EXIT_OK

    try:
        storage = storage_factory(settings)
        if command == "check-config":
            storage.check_access()
            print(f"Bucket {settings.bucket!r} is reachable.")
            return EXIT_OK
        if command == "list":
            return _print_list(storage, args.database, args.json)
        if command == "prune":
            prune(settings, storage, dry_run=args.dry_run)
            return EXIT_OK
        if command == "restore":
            if not _confirm(args, settings):
                return EXIT_FAILED
            run_restore_job(settings, storage, args.backup, args.database, args.target_db, args.output)
            return EXIT_OK
        if command == "backup" or settings.schedule is None:
            return _backup_once(settings, storage)
        stop = threading.Event()
        _install_signal_handlers(stop)
        return run_scheduler(settings, storage, stop)
    except (JobError, StorageError) as exc:
        log.error("%s", redact(str(exc)))
        return EXIT_FAILED
