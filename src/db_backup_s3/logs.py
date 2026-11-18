"""Logging setup with secret redaction.

Every log record passes through :class:`RedactingFilter`, which replaces any
registered secret value (database password, S3 secret key, passphrase,
healthcheck URL) with ``***`` before it is formatted. Output from ``mysqldump``
and ``pg_dump`` is redacted the same way before it is logged or raised.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime

LOGGER_NAME = "db_backup_s3"
MASK = "***"

_lock = threading.Lock()
_secrets: set[str] = set()


def register_secrets(*values: str | None) -> None:
    """Remember values that must never be logged. Short values are ignored to avoid masking noise."""
    with _lock:
        for value in values:
            if value and len(value) >= 4:
                _secrets.add(value)


def clear_secrets() -> None:
    with _lock:
        _secrets.clear()


def redact(text: str) -> str:
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message = message + "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message)
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    # boto3 debug output can contain signed headers; keep it quiet.
    for noisy in ("boto3", "botocore", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
