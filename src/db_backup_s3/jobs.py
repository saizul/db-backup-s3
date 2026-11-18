"""The backup, prune and restore jobs."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from db_backup_s3.config import Settings
from db_backup_s3.crypto import (
    EXTENSIONS,
    AesEncryptSink,
    AgeEncryptSink,
    CryptoError,
    FileSink,
    GzipSink,
    Sink,
    aes_decrypt,
    age_decrypt,
    gunzip,
    read_chunks,
)
from db_backup_s3.dump import DumpError, dump_command, restore_command, run_dump, run_restore, secrets_directory
from db_backup_s3.healthcheck import Healthcheck
from db_backup_s3.logs import get_logger, redact
from db_backup_s3.retention import plan_retention
from db_backup_s3.storage import BackupObject, Storage, StorageError, build_key, label_for

Clock = Callable[[], datetime]
KNOWN_ERRORS = (DumpError, StorageError, CryptoError, OSError)


class JobError(Exception):
    """A job failed with a message that is safe to show."""


@dataclass
class DatabaseResult:
    database: str
    ok: bool
    key: str = ""
    dump_bytes: int = 0
    stored_bytes: int = 0
    seconds: float = 0.0
    error: str = ""


@dataclass
class BackupReport:
    results: list[DatabaseResult] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    retention_error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results) and not self.retention_error

    def summary(self) -> str:
        lines = []
        for r in self.results:
            if r.ok:
                lines.append(
                    f"OK   {label_for(r.database)}: {human_size(r.stored_bytes)} "
                    f"(raw {human_size(r.dump_bytes)}) in {r.seconds:.1f}s -> {r.key}"
                )
            else:
                lines.append(f"FAIL {label_for(r.database)}: {r.error}")
        if self.deleted:
            lines.append(f"Retention removed {len(self.deleted)} old backup(s)")
        if self.retention_error:
            lines.append(f"Retention failed: {self.retention_error}")
        return "\n".join(lines)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"  # pragma: no cover


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _temp_path(settings: Settings, suffix: str) -> str:
    directory = settings.work_dir if os.path.isdir(settings.work_dir) else None
    fd, path = tempfile.mkstemp(prefix="dbs3-", suffix=suffix, dir=directory)
    os.close(fd)
    return path


@contextlib.contextmanager
def _encrypted_sink(settings: Settings, path: str) -> Iterator[Sink]:
    """File sink chain for ``path``: (encryptor ->) file."""
    if settings.encryption == "age":
        age = AgeEncryptSink(path, settings.age_recipients)
        try:
            yield age
        except BaseException:
            age.abort()
            raise
        return
    with open(path, "wb") as fh:
        sink: Sink = FileSink(fh)
        if settings.encryption == "aes":
            assert settings.passphrase
            sink = AesEncryptSink(sink, settings.passphrase)
        yield sink


def backup_database(settings: Settings, storage: Storage, database: str, now: datetime) -> DatabaseResult:
    log = get_logger()
    started = time.monotonic()
    key = build_key(settings.prefix, database, now, settings.encryption)
    path = _temp_path(settings, ".sql.gz" + EXTENSIONS[settings.encryption])
    log.info("Backing up %s", label_for(database))
    try:
        with secrets_directory(settings.work_dir) as secrets_dir, _encrypted_sink(settings, path) as inner:
            command = dump_command(settings, database, secrets_dir)
            gz = GzipSink(inner, settings.compression_level)
            dumped = run_dump(command, gz)
            gz.close()
        stored = os.path.getsize(path)
        storage.upload(path, key)
    except KNOWN_ERRORS as exc:
        message = redact(str(exc))
        log.error("Backup of %s failed: %s", label_for(database), message)
        return DatabaseResult(database, ok=False, error=message, seconds=time.monotonic() - started)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    seconds = time.monotonic() - started
    log.info("Uploaded %s (%s) in %.1fs", key, human_size(stored), seconds)
    return DatabaseResult(database, ok=True, key=key, dump_bytes=dumped, stored_bytes=stored, seconds=seconds)


def prune(settings: Settings, storage: Storage, databases: list[str] | None = None, dry_run: bool = False) -> list[str]:
    """Apply the retention policy. Returns the deleted (or, with ``dry_run``, deletable) keys."""
    log = get_logger()
    if not settings.retention.enabled:
        log.info("Retention disabled; keeping every backup")
        return []
    backups: list[BackupObject] = []
    for database in databases or list(settings.databases):
        backups.extend(storage.list_backups(database))
    plan = plan_retention(backups, settings.retention, settings.tz)
    keys = [b.key for b in plan.delete]
    for key in keys:
        log.info("%s %s", "Would delete" if dry_run else "Deleting", key)
    if keys and not dry_run:
        storage.delete(keys)
    log.info("Retention: keeping %d, %s %d", len(plan.keep), "would delete" if dry_run else "deleted", len(keys))
    return keys


def run_backup(
    settings: Settings,
    storage: Storage,
    healthcheck: Healthcheck | None = None,
    clock: Clock = _utcnow,
) -> BackupReport:
    """Back up every configured database, apply retention and ping the healthcheck."""
    hc = healthcheck or Healthcheck(None)
    started = time.monotonic()
    hc.ping("start")
    report = BackupReport()
    now = clock()
    for database in settings.databases:
        report.results.append(backup_database(settings, storage, database, now))
    succeeded = [r.database for r in report.results if r.ok]
    if succeeded:
        try:
            report.deleted = prune(settings, storage, succeeded)
        except StorageError as exc:
            report.retention_error = redact(str(exc))
            get_logger().error("Retention failed: %s", report.retention_error)
    duration = time.monotonic() - started
    summary = report.summary()
    if report.ok:
        get_logger().info("Backup finished in %.1fs", duration)
        hc.ping("success", summary, duration)
    else:
        get_logger().error("Backup finished with errors in %.1fs", duration)
        hc.ping("fail", summary, duration)
    return report


def resolve_backup(storage: Storage, reference: str, database: str | None) -> BackupObject:
    """Find a backup by key, file name or ``latest`` (needs ``database``)."""
    if reference == "latest":
        if not database:
            raise JobError("restore latest needs --database NAME (or 'all' for an all-databases backup)")
        backups = storage.list_backups(database)
        if not backups:
            raise JobError(f"no backups found for {label_for(database)!r} under s3://{storage.bucket}/{storage.prefix}")
        return backups[0]
    candidates = storage.list_backups(database)
    for backup in candidates:
        if reference in (backup.key, backup.filename):
            return backup
    raise JobError(f"backup {reference!r} not found; run 'db-backup-s3 list' to see available backups")


def _plaintext(settings: Settings, backup: BackupObject, path: str) -> Iterator[bytes]:
    if backup.encryption == "age":
        if not settings.age_identity_file:
            raise JobError("this backup is age-encrypted; set AGE_IDENTITY_FILE to the private key file")
        yield from gunzip(age_decrypt(path, settings.age_identity_file))
    elif backup.encryption == "aes":
        if not settings.passphrase:
            raise JobError("this backup is AES-encrypted; set ENCRYPTION_PASSPHRASE or ENCRYPTION_PASSPHRASE_FILE")
        with open(path, "rb") as fh:
            yield from gunzip(aes_decrypt(fh, settings.passphrase))
    else:
        with open(path, "rb") as fh:
            yield from gunzip(read_chunks(fh))


def run_restore_job(
    settings: Settings,
    storage: Storage,
    reference: str,
    database: str | None = None,
    target: str | None = None,
    output: str | None = None,
) -> BackupObject:
    """Download, decrypt and decompress a backup, then load it (or write it to ``output``)."""
    log = get_logger()
    backup = resolve_backup(storage, reference, database)
    log.info("Restoring %s (%s, %s)", backup.key, human_size(backup.size), backup.created.isoformat())
    path = _temp_path(settings, ".download")
    try:
        storage.download(backup.key, path)
        chunks = _plaintext(settings, backup, path)
        if output:
            written = _write_output(chunks, output)
            log.info("Wrote %s of SQL to %s", human_size(written), "stdout" if output == "-" else output)
            return backup
        dbname = target or (None if backup.database == "all-databases" else backup.database)
        with secrets_directory(settings.work_dir) as secrets_dir:
            command = restore_command(settings, dbname, secrets_dir)
            restored = run_restore(command, chunks)
        log.info("Restored %s of SQL into %s", human_size(restored), dbname or "the server")
        return backup
    except KNOWN_ERRORS as exc:
        raise JobError(redact(str(exc))) from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _write_output(chunks: Iterator[bytes], output: str) -> int:
    total = 0
    if output == "-":
        for chunk in chunks:
            sys.stdout.buffer.write(chunk)
            total += len(chunk)
        sys.stdout.buffer.flush()
        return total
    partial = output + ".partial"
    try:
        with open(partial, "wb") as fh:
            for chunk in chunks:
                fh.write(chunk)
                total += len(chunk)
        os.replace(partial, output)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(partial)
        raise
    return total
