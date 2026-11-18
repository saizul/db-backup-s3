"""S3-compatible storage (AWS S3, Cloudflare R2, Backblaze B2, MinIO, ...) and the backup naming scheme.

Objects are stored as::

    <prefix>/<database>/<database>-<YYYYMMDDTHHMMSSZ>.sql.gz[.age|.enc]

Only keys that follow this pattern are listed or touched by retention, so the
tool never deletes files it did not create.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from db_backup_s3.config import ALL_DATABASES, Settings
from db_backup_s3.crypto import EXTENSIONS

TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
ALL_LABEL = "all-databases"
_KEY_RE = re.compile(r"^(?:(?P<prefix>.*)/)?(?P<db>[^/]+)/(?P=db)-(?P<ts>\d{8}T\d{6}Z)\.sql\.gz(?P<ext>\.age|\.enc)?$")


class StorageError(Exception):
    """Talking to the object storage failed."""


@dataclass(frozen=True)
class BackupObject:
    key: str
    database: str
    created: datetime
    size: int
    encryption: str  # none, age or aes

    @property
    def filename(self) -> str:
        return self.key.rsplit("/", 1)[-1]


def label_for(database: str) -> str:
    return ALL_LABEL if database == ALL_DATABASES else database


def build_key(prefix: str, database: str, created: datetime, encryption: str) -> str:
    label = label_for(database)
    stamp = created.astimezone(UTC).strftime(TIMESTAMP_FORMAT)
    name = f"{label}/{label}-{stamp}.sql.gz{EXTENSIONS[encryption]}"
    return f"{prefix}/{name}" if prefix else name


def parse_key(key: str, size: int = 0) -> BackupObject | None:
    match = _KEY_RE.match(key)
    if not match:
        return None
    created = datetime.strptime(match["ts"], TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    encryption = {".age": "age", ".enc": "aes"}.get(match["ext"] or "", "none")
    return BackupObject(key=key, database=match["db"], created=created, size=size, encryption=encryption)


def _client(settings: Settings) -> Any:
    import boto3
    from botocore.config import Config

    config = Config(
        retries={"max_attempts": 5, "mode": "standard"},
        s3={"addressing_style": "path" if settings.force_path_style else "auto"},
        # R2, B2 and MinIO reject the newer default checksum headers in some versions.
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    kwargs: dict[str, Any] = {"region_name": settings.region, "config": config}
    if settings.endpoint_url:
        kwargs["endpoint_url"] = settings.endpoint_url
    if settings.access_key_id and settings.secret_access_key:
        kwargs["aws_access_key_id"] = settings.access_key_id
        kwargs["aws_secret_access_key"] = settings.secret_access_key
    return boto3.client("s3", **kwargs)


def _describe(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        code, message = error.get("Code"), error.get("Message")
        if code:
            return f"{code}: {message}" if message else str(code)
    return str(exc) or type(exc).__name__


class Storage:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.bucket = settings.bucket
        self.prefix = settings.prefix
        self.client = client or _client(settings)

    def upload(self, path: str, key: str) -> None:
        extra: dict[str, str] = {"ContentType": "application/octet-stream"}
        if self.settings.storage_class:
            extra["StorageClass"] = self.settings.storage_class
        try:
            self.client.upload_file(path, self.bucket, key, ExtraArgs=extra)
        except Exception as exc:
            raise StorageError(f"upload to s3://{self.bucket}/{key} failed ({_describe(exc)})") from None

    def download(self, key: str, path: str) -> None:
        try:
            self.client.download_file(self.bucket, key, path)
        except Exception as exc:
            raise StorageError(f"download of s3://{self.bucket}/{key} failed ({_describe(exc)})") from None

    def delete(self, keys: list[str]) -> None:
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            try:
                response = self.client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True}
                )
            except Exception as exc:
                raise StorageError(f"deleting {len(batch)} object(s) failed ({_describe(exc)})") from None
            errors = response.get("Errors") or []
            if errors:
                first = errors[0]
                raise StorageError(f"could not delete {first.get('Key')}: {first.get('Code')} {first.get('Message')}")

    def list_backups(self, database: str | None = None) -> list[BackupObject]:
        """All backups (newest first), optionally only for one database."""
        base = f"{self.prefix}/" if self.prefix else ""
        if database:
            base += f"{label_for(database)}/"
        found: list[BackupObject] = []
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=base):
                for item in page.get("Contents", []):
                    backup = parse_key(item["Key"], int(item.get("Size", 0)))
                    if backup is None:
                        continue
                    parent = backup.key[: -len(f"{backup.database}/{backup.filename}")].rstrip("/")
                    if parent != self.prefix:
                        continue  # a deeper "folder" that only looks like ours
                    found.append(backup)
        except Exception as exc:
            raise StorageError(f"listing s3://{self.bucket}/{base} failed ({_describe(exc)})") from None
        found.sort(key=lambda b: (b.created, b.key), reverse=True)
        return found

    def check_access(self) -> None:
        """Fail early with a readable error if the bucket is missing or credentials are wrong."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            raise StorageError(f"cannot access bucket {self.bucket!r} ({_describe(exc)})") from None
