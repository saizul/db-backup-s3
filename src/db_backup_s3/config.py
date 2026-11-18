"""Configuration from environment variables, with validation and clear errors.

Every secret can be given directly (``DB_PASSWORD``) or as a file path
(``DB_PASSWORD_FILE``), which works with Docker and Kubernetes secrets.
All problems are collected and reported together, so one run shows
everything that needs fixing.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from db_backup_s3.schedule import CronError, CronSchedule

ENGINES = {"mysql": "mysql", "mariadb": "mysql", "postgres": "postgres", "postgresql": "postgres"}
DEFAULT_PORTS = {"mysql": 3306, "postgres": 5432}
ENCRYPTIONS = ("none", "age", "aes")
HEALTHCHECK_KINDS = ("auto", "healthchecks", "uptime-kuma")
ALL_DATABASES = "all"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_$\-.]+$")
_AGE_RECIPIENT_RE = re.compile(r"^(age1[0-9a-z]{58}|ssh-(ed25519|rsa) \S+.*)$")


class ConfigError(Exception):
    """Raised when the configuration is invalid. ``problems`` lists every issue."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class Retention:
    keep_last: int = 0
    keep_daily: int = 0
    keep_weekly: int = 0
    keep_monthly: int = 0

    @property
    def enabled(self) -> bool:
        return any((self.keep_last, self.keep_daily, self.keep_weekly, self.keep_monthly))


@dataclass(frozen=True)
class Settings:
    engine: str  # normalised: "mysql" or "postgres"
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    databases: tuple[str, ...]
    dump_extra_args: tuple[str, ...] = ()
    restore_extra_args: tuple[str, ...] = ()

    bucket: str = ""
    prefix: str = "backups"
    endpoint_url: str | None = None
    region: str = "us-east-1"
    access_key_id: str | None = field(default=None, repr=False)
    secret_access_key: str | None = field(default=None, repr=False)
    force_path_style: bool = False
    storage_class: str | None = None

    schedule: CronSchedule | None = None
    timezone: str = "UTC"
    run_on_startup: bool = False

    encryption: str = "none"
    age_recipients: tuple[str, ...] = ()
    age_identity_file: str | None = None
    passphrase: str | None = field(default=None, repr=False)

    retention: Retention = field(default_factory=Retention)

    healthcheck_url: str | None = field(default=None, repr=False)
    healthcheck_kind: str = "auto"
    healthcheck_timeout: float = 10.0

    compression_level: int = 6
    work_dir: str = "/tmp"
    log_level: str = "INFO"
    log_format: str = "text"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def all_databases(self) -> bool:
        return self.databases == (ALL_DATABASES,)

    def secrets(self) -> list[str]:
        """Values that must never appear in logs or error messages."""
        values = [self.password, self.secret_access_key, self.passphrase, self.healthcheck_url]
        return [v for v in values if v]

    def summary(self) -> dict[str, object]:
        """A safe, human readable view of the configuration (secrets masked)."""

        def mask(value: str | None) -> str:
            return "(set)" if value else "(not set)"

        return {
            "engine": self.engine,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": mask(self.password),
            "databases": ", ".join(self.databases),
            "s3_bucket": self.bucket,
            "s3_prefix": self.prefix,
            "s3_endpoint_url": self.endpoint_url or "(AWS default)",
            "s3_region": self.region,
            "s3_credentials": "explicit" if self.access_key_id else "default AWS credential chain",
            "schedule": self.schedule.expression if self.schedule else "(one-shot)",
            "timezone": self.timezone,
            "encryption": self.encryption,
            "retention": (
                f"last={self.retention.keep_last} daily={self.retention.keep_daily} "
                f"weekly={self.retention.keep_weekly} monthly={self.retention.keep_monthly}"
                if self.retention.enabled
                else "disabled (keep everything)"
            ),
            "healthcheck": mask(self.healthcheck_url),
            "compression_level": self.compression_level,
        }


class _Reader:
    """Reads typed values from an environment mapping and collects problems."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env
        self.problems: list[str] = []

    def raw(self, name: str, *aliases: str) -> str | None:
        for key in (name, *aliases):
            value = self.env.get(key)
            if value is not None and value.strip() != "":
                return value.strip()
        return None

    def secret(self, name: str, *aliases: str) -> str | None:
        """Read ``NAME`` or ``NAME_FILE`` (first alias wins). Never echoes the value."""
        for key in (name, *aliases):
            direct = self.env.get(key)
            path = self.env.get(f"{key}_FILE")
            if direct and path:
                self.problems.append(f"Set either {key} or {key}_FILE, not both")
                return None
            if path:
                try:
                    return Path(path).read_text(encoding="utf-8").strip()
                except OSError as exc:
                    self.problems.append(f"{key}_FILE: cannot read {path!r} ({exc.strerror or exc})")
                    return None
            if direct:
                return direct.strip()
        return None

    def required(self, name: str, hint: str) -> str:
        value = self.raw(name)
        if value is None:
            self.problems.append(f"{name} is required ({hint})")
            return ""
        return value

    def integer(self, name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
        value = self.raw(name)
        if value is None:
            return default
        try:
            number = int(value)
        except ValueError:
            self.problems.append(f"{name} must be a whole number, got {value!r}")
            return default
        if number < minimum or (maximum is not None and number > maximum):
            upper = f" and {maximum}" if maximum is not None else ""
            word = "between" if maximum is not None else "at least"
            self.problems.append(f"{name} must be {word} {minimum}{upper}, got {number}")
            return default
        return number

    def number(self, name: str, default: float) -> float:
        value = self.raw(name)
        if value is None:
            return default
        try:
            number = float(value)
        except ValueError:
            self.problems.append(f"{name} must be a number, got {value!r}")
            return default
        if number <= 0:
            self.problems.append(f"{name} must be greater than 0, got {value!r}")
            return default
        return number

    def boolean(self, name: str, default: bool) -> bool:
        value = self.env.get(name)
        if value is None:
            return default
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        self.problems.append(f"{name} must be true or false, got {value!r}")
        return default

    def arguments(self, name: str) -> tuple[str, ...]:
        value = self.raw(name)
        if not value:
            return ()
        try:
            return tuple(shlex.split(value))
        except ValueError as exc:
            self.problems.append(f"{name} cannot be parsed: {exc}")
            return ()

    def choice(self, name: str, choices: tuple[str, ...] | list[str], default: str) -> str:
        value = (self.raw(name) or default).lower()
        if value not in choices:
            self.problems.append(f"{name} must be one of {', '.join(choices)}; got {value!r}")
            return default
        return value


PURPOSES = ("backup", "restore", "storage")


def load_settings(env: Mapping[str, str] | None = None, purpose: str = "backup") -> Settings:
    """Build :class:`Settings` from ``env`` (default ``os.environ``) or raise :class:`ConfigError`.

    ``purpose`` controls what is required: ``backup`` needs everything, ``restore``
    needs the database connection but not ``DB_NAMES`` or encryption keys, and
    ``storage`` (``list``, ``restore --output``) only needs the bucket.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}")
    r = _Reader(os.environ if env is None else env)
    needs_db = purpose in ("backup", "restore")

    def need(name: str, hint: str) -> str:
        return r.required(name, hint) if needs_db else (r.raw(name) or "")

    engine_raw = need("DB_ENGINE", "mysql, mariadb or postgres").lower()
    engine = ENGINES.get(engine_raw, "")
    if engine_raw and not engine:
        r.problems.append(f"DB_ENGINE must be one of mysql, mariadb, postgres; got {engine_raw!r}")

    host = need("DB_HOST", "host name of the database server")
    port = r.integer("DB_PORT", DEFAULT_PORTS.get(engine, 0), minimum=1, maximum=65535)
    if port == 0:
        port = DEFAULT_PORTS.get(engine, 1)
    user = r.raw("DB_USER") or ("postgres" if engine == "postgres" else "root")
    password = r.secret("DB_PASSWORD") or ""

    databases: tuple[str, ...] = ()
    names_hint = f"comma-separated database names, or {ALL_DATABASES!r}"
    names_raw = r.required("DB_NAMES", names_hint) if purpose == "backup" else r.raw("DB_NAMES")
    if names_raw:
        databases = tuple(n.strip() for n in names_raw.split(",") if n.strip())
        if ALL_DATABASES in databases and len(databases) > 1:
            r.problems.append(f"DB_NAMES: use {ALL_DATABASES!r} alone, not together with other names")
        for name in databases:
            if not _DB_NAME_RE.match(name):
                r.problems.append(f"DB_NAMES: {name!r} contains characters that are not allowed")

    extra = r.arguments("DB_DUMP_EXTRA_ARGS")
    restore_extra = r.arguments("DB_RESTORE_EXTRA_ARGS")

    bucket = r.required("S3_BUCKET", "name of the bucket to upload to")
    prefix = (r.raw("S3_PREFIX") or "backups").strip("/")
    endpoint_url = r.raw("S3_ENDPOINT_URL")
    if endpoint_url and not endpoint_url.startswith(("http://", "https://")):
        r.problems.append(f"S3_ENDPOINT_URL must start with http:// or https://, got {endpoint_url!r}")
    region = r.raw("S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION") or "us-east-1"
    access_key_id = r.secret("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    secret_access_key = r.secret("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    if bool(access_key_id) != bool(secret_access_key):
        r.problems.append("S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set together")

    schedule: CronSchedule | None = None
    expression = r.raw("SCHEDULE")
    if expression:
        try:
            schedule = CronSchedule.parse(expression)
        except CronError as exc:
            r.problems.append(f"SCHEDULE: {exc}")
    timezone = r.raw("SCHEDULE_TZ", "TZ") or "UTC"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        r.problems.append(f"SCHEDULE_TZ: unknown time zone {timezone!r} (use a name like Europe/Berlin)")
        timezone = "UTC"

    encryption = r.choice("ENCRYPTION", ENCRYPTIONS, "none")
    recipients: tuple[str, ...] = ()
    passphrase = r.secret("ENCRYPTION_PASSPHRASE")
    identity = r.raw("AGE_IDENTITY_FILE")
    if purpose != "backup":
        recipients = tuple(x.strip() for x in (r.raw("AGE_RECIPIENTS") or "").split(",") if x.strip())
    elif encryption == "age":
        recipients = tuple(x.strip() for x in (r.raw("AGE_RECIPIENTS") or "").split(",") if x.strip())
        if not recipients:
            r.problems.append("AGE_RECIPIENTS is required when ENCRYPTION=age (one or more age1... public keys)")
        for recipient in recipients:
            if not _AGE_RECIPIENT_RE.match(recipient):
                r.problems.append(f"AGE_RECIPIENTS: {recipient[:12]}... is not an age or SSH public key")
    elif encryption == "aes":
        if not passphrase:
            r.problems.append("ENCRYPTION_PASSPHRASE (or ENCRYPTION_PASSPHRASE_FILE) is required when ENCRYPTION=aes")
        elif len(passphrase) < 12:
            r.problems.append("ENCRYPTION_PASSPHRASE must be at least 12 characters long")

    retention = Retention(
        keep_last=r.integer("RETENTION_KEEP_LAST", 0),
        keep_daily=r.integer("RETENTION_KEEP_DAILY", 0),
        keep_weekly=r.integer("RETENTION_KEEP_WEEKLY", 0),
        keep_monthly=r.integer("RETENTION_KEEP_MONTHLY", 0),
    )

    healthcheck_url = r.secret("HEALTHCHECK_URL")
    if healthcheck_url and not healthcheck_url.startswith(("http://", "https://")):
        r.problems.append("HEALTHCHECK_URL must start with http:// or https://")

    settings = Settings(
        engine=engine or "mysql",
        host=host,
        port=port,
        user=user,
        password=password,
        databases=databases,
        dump_extra_args=extra,
        restore_extra_args=restore_extra,
        bucket=bucket,
        prefix=prefix,
        endpoint_url=endpoint_url,
        region=region,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        force_path_style=r.boolean("S3_FORCE_PATH_STYLE", False),
        storage_class=r.raw("S3_STORAGE_CLASS"),
        schedule=schedule,
        timezone=timezone,
        run_on_startup=r.boolean("RUN_ON_STARTUP", False),
        encryption=encryption,
        age_recipients=recipients,
        age_identity_file=identity,
        passphrase=passphrase,
        retention=retention,
        healthcheck_url=healthcheck_url,
        healthcheck_kind=r.choice("HEALTHCHECK_KIND", HEALTHCHECK_KINDS, "auto"),
        healthcheck_timeout=r.number("HEALTHCHECK_TIMEOUT", 10.0),
        compression_level=r.integer("COMPRESSION_LEVEL", 6, minimum=1, maximum=9),
        work_dir=r.raw("WORK_DIR") or "/tmp",
        log_level=r.choice("LOG_LEVEL", ("debug", "info", "warning", "error"), "info").upper(),
        log_format=r.choice("LOG_FORMAT", ("text", "json"), "text"),
    )
    if r.problems:
        raise ConfigError(r.problems)
    return settings
