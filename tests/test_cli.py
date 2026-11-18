from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from conftest import BUCKET, FAKE_DB_PASSWORD, FAKE_SECRET_KEY, base_env, make_settings, python_command
from db_backup_s3 import cli, jobs
from db_backup_s3.storage import Storage, build_key


@pytest.fixture
def env(monkeypatch):
    def apply(**overrides):
        for key, value in base_env(**overrides).items():
            monkeypatch.setenv(key, value)

    apply()
    return apply


@pytest.fixture(autouse=True)
def _dump(monkeypatch):
    monkeypatch.setattr(jobs, "dump_command", lambda s, d, x: python_command("print('SELECT 1;')"))


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert "db-backup-s3 0.1.0" in capsys.readouterr().out


def test_config_error_exit_code(monkeypatch, capsys):
    for key in ("DB_ENGINE", "DB_HOST", "DB_NAMES", "S3_BUCKET"):
        monkeypatch.delenv(key, raising=False)
    assert cli.main(["backup"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "DB_ENGINE is required" in err
    assert "S3_BUCKET is required" in err


def test_check_config_masks_secrets(env, capsys):
    env(SCHEDULE="@daily", HEALTHCHECK_URL="https://hc-ping.com/secret-uuid")
    assert cli.main(["check-config"]) == 0
    out = capsys.readouterr().out
    assert "Configuration is valid" in out
    assert "next runs:" in out
    for secret in (FAKE_DB_PASSWORD, FAKE_SECRET_KEY, "secret-uuid"):
        assert secret not in out


def test_check_config_connect(env, aws, capsys):
    assert cli.main(["check-config", "--connect"]) == 0
    assert "is reachable" in capsys.readouterr().out
    env(S3_BUCKET="nope")
    assert cli.main(["check-config", "--connect"]) == cli.EXIT_FAILED


def test_backup_then_list(env, aws, capsys):
    assert cli.main(["backup"]) == 0
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "backups/app/app-" in out
    assert "1 backup(s)" in out
    assert cli.main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["database"] == "app" and rows[0]["encryption"] == "none"


def test_list_needs_only_bucket(monkeypatch, aws, capsys):
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    for key in ("DB_ENGINE", "DB_HOST", "DB_NAMES"):
        monkeypatch.delenv(key, raising=False)
    assert cli.main(["list"]) == 0
    assert "No backups found" in capsys.readouterr().out


def test_backup_failure_exit_code(env, aws, monkeypatch):
    monkeypatch.setattr(jobs, "dump_command", lambda s, d, x: python_command("import sys; sys.exit(1)"))
    assert cli.main(["backup"]) == cli.EXIT_FAILED


def test_restore_requires_confirmation(env, aws, monkeypatch, capsys):
    cli.main(["backup"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["restore", "latest", "-d", "app"]) == cli.EXIT_FAILED
    assert "pass --yes" in capsys.readouterr().err


def test_restore_to_stdout(env, aws, capsysbinary):
    cli.main(["backup"])
    assert cli.main(["restore", "latest", "-d", "app", "-o", "-"]) == 0
    assert capsysbinary.readouterr().out == b"SELECT 1;\n"


def test_restore_missing_backup(env, aws):
    assert cli.main(["restore", "latest", "-d", "ghost", "--yes"]) == cli.EXIT_FAILED


def test_prune_command(env, aws, s3):
    env(RETENTION_KEEP_LAST="1")
    for day in (1, 2, 3):
        key = build_key("backups", "app", datetime(2026, 11, day, tzinfo=UTC), "none")
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")
    assert cli.main(["prune", "--dry-run"]) == 0
    assert len(s3.list_objects_v2(Bucket=BUCKET)["Contents"]) == 3
    assert cli.main(["prune"]) == 0
    assert len(s3.list_objects_v2(Bucket=BUCKET)["Contents"]) == 1


def test_scheduler_runs_on_time_and_stops(aws, monkeypatch):
    settings = make_settings(SCHEDULE="*/5 * * * *", RUN_ON_STARTUP="true")
    storage = Storage(settings)
    stop = threading.Event()
    calls = []
    fake_now = [datetime(2026, 11, 18, 3, 3, tzinfo=UTC)]

    def fake_backup(s, st):
        calls.append(fake_now[0])
        if len(calls) == 3:
            stop.set()
        return 0

    def fake_wait(timeout):
        fake_now[0] += timedelta(seconds=timeout)
        return stop.is_set()

    monkeypatch.setattr(cli, "_backup_once", fake_backup)
    monkeypatch.setattr(stop, "wait", fake_wait)
    assert cli.run_scheduler(settings, storage, stop, now=lambda: fake_now[0]) == 0
    assert calls[0] == datetime(2026, 11, 18, 3, 3, tzinfo=UTC)  # RUN_ON_STARTUP
    assert calls[1] == datetime(2026, 11, 18, 3, 5, tzinfo=UTC)
    assert calls[2] == datetime(2026, 11, 18, 3, 10, tzinfo=UTC)


def test_unexpected_crash_is_reported(env, aws, monkeypatch):
    pings = []
    monkeypatch.setattr(cli.Healthcheck, "ping", lambda self, event, message="", duration=None: pings.append(event))

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "run_backup", boom)
    assert cli.main(["backup"]) == cli.EXIT_FAILED
    assert pings == ["fail"]
