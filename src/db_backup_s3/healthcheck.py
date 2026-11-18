"""Healthcheck pings for healthchecks.io (and self-hosted Healthchecks) and Uptime Kuma push monitors.

healthchecks.io: ``<url>/start`` when a run begins, ``<url>`` on success and
``<url>/fail`` on failure, with a short summary as the request body.

Uptime Kuma: ``<push url>?status=up|down&msg=...&ping=<ms>``.

A failing ping is logged as a warning and never fails the backup. The URL
contains a secret token, so only its host name is ever logged.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from db_backup_s3.logs import get_logger, redact

Opener = Callable[..., Any]
MAX_BODY = 10_000


def detect_kind(url: str, kind: str = "auto") -> str:
    if kind != "auto":
        return kind
    return "uptime-kuma" if "/api/push/" in urllib.parse.urlsplit(url).path else "healthchecks"


def build_request(
    url: str, kind: str, event: str, message: str = "", duration: float | None = None
) -> tuple[str, bytes | None]:
    """Return ``(url, body)`` for ``event`` in start, success or fail."""
    if event not in ("start", "success", "fail"):
        raise ValueError(f"unknown event {event!r}")
    kind = detect_kind(url, kind)
    if kind == "uptime-kuma":
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        query["status"] = "down" if event == "fail" else "up"
        query["msg"] = (message or event)[:250]
        if duration is not None:
            query["ping"] = str(round(duration * 1000))
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query))), None
    base = url.rstrip("/")
    suffix = {"start": "/start", "success": "", "fail": "/fail"}[event]
    return base + suffix, message.encode("utf-8")[:MAX_BODY]


class Healthcheck:
    def __init__(
        self,
        url: str | None,
        kind: str = "auto",
        timeout: float = 10.0,
        retries: int = 3,
        opener: Opener | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = url
        self.kind = kind
        self.timeout = timeout
        self.retries = retries
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def ping(self, event: str, message: str = "", duration: float | None = None) -> bool:
        if not self.url:
            return False
        if event == "start" and detect_kind(self.url, self.kind) == "uptime-kuma":
            return False  # Uptime Kuma has no "start" signal; an "up" here would hide a hung run.
        target, body = build_request(self.url, self.kind, event, redact(message), duration)
        host = urllib.parse.urlsplit(target).hostname or "healthcheck"
        request = urllib.request.Request(target, data=body, method="POST" if body is not None else "GET")
        request.add_header("User-Agent", "db-backup-s3")
        for attempt in range(1, self.retries + 1):
            try:
                with self._open(request, timeout=self.timeout) as response:
                    status = getattr(response, "status", 200)
                if 200 <= status < 300:
                    get_logger().debug("Healthcheck %s ping sent to %s", event, host)
                    return True
                problem = f"HTTP {status}"
            except Exception as exc:
                problem = type(exc).__name__
            if attempt < self.retries:
                self._sleep(2 ** (attempt - 1))
        get_logger().warning(
            "Healthcheck %s ping to %s failed after %d attempts (%s)", event, host, self.retries, problem
        )
        return False
