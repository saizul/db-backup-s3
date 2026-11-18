from __future__ import annotations

import urllib.error
from urllib.parse import parse_qs, urlsplit

from db_backup_s3 import logs
from db_backup_s3.healthcheck import Healthcheck, build_request, detect_kind

HC = "https://hc-ping.com/1111-2222"
KUMA = "https://status.example.com/api/push/AbCdEf?status=up&msg=OK&ping="


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Recorder:
    def __init__(self, fail_times: int = 0, status: int = 200) -> None:
        self.requests = []
        self.fail_times = fail_times
        self.status = status

    def __call__(self, request, timeout):
        self.requests.append(request)
        if len(self.requests) <= self.fail_times:
            raise urllib.error.URLError("boom")
        return FakeResponse(self.status)


def test_detect_kind():
    assert detect_kind(HC) == "healthchecks"
    assert detect_kind(KUMA) == "uptime-kuma"
    assert detect_kind(HC, "uptime-kuma") == "uptime-kuma"


def test_healthchecks_urls():
    assert build_request(HC, "auto", "start") == (HC + "/start", b"")
    assert build_request(HC + "/", "auto", "success", "ok") == (HC, b"ok")
    assert build_request(HC, "auto", "fail", "bad") == (HC + "/fail", b"bad")


def test_uptime_kuma_url():
    url, body = build_request(KUMA, "auto", "fail", "dump failed", 1.5)
    query = parse_qs(urlsplit(url).query)
    assert body is None
    assert query["status"] == ["down"]
    assert query["msg"] == ["dump failed"]
    assert query["ping"] == ["1500"]
    assert parse_qs(urlsplit(build_request(KUMA, "auto", "success")[0]).query)["status"] == ["up"]


def test_ping_sends_redacted_body():
    logs.register_secrets("hunter2-secret")
    rec = Recorder()
    hc = Healthcheck(HC, opener=rec)
    assert hc.ping("fail", "error with hunter2-secret inside")
    assert rec.requests[0].full_url == HC + "/fail"
    assert rec.requests[0].data == b"error with *** inside"
    assert rec.requests[0].get_method() == "POST"


def test_ping_retries_then_gives_up_without_raising(caplog):
    rec = Recorder(fail_times=10)
    sleeps = []
    hc = Healthcheck(HC, retries=3, opener=rec, sleep=sleeps.append)
    assert hc.ping("success") is False
    assert len(rec.requests) == 3
    assert sleeps == [1, 2]


def test_ping_recovers_after_one_failure():
    rec = Recorder(fail_times=1)
    assert Healthcheck(HC, opener=rec, sleep=lambda s: None).ping("success")
    assert len(rec.requests) == 2


def test_disabled_and_kuma_start_are_noops():
    rec = Recorder()
    assert Healthcheck(None, opener=rec).ping("success") is False
    assert Healthcheck(KUMA, opener=rec).ping("start") is False
    assert rec.requests == []


def test_failed_ping_log_hides_token(caplog):
    logger = logs.setup_logging("DEBUG")
    logger.propagate = True
    try:
        Healthcheck(HC, retries=1, opener=Recorder(fail_times=5)).ping("success")
    finally:
        logger.propagate = False
    assert "hc-ping.com" in caplog.text
    assert "1111-2222" not in caplog.text
