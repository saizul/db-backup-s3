from __future__ import annotations

import gzip
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from conftest import BUCKET, FAKE_DB_PASSWORD, FAKE_PASSPHRASE, make_settings, python_command
from db_backup_s3 import jobs, logs
from db_backup_s3.healthcheck import Healthcheck
from db_backup_s3.jobs import JobError, run_backup, run_restore_job
from db_backup_s3.storage import Storage, build_key

SQL = "".join(f"INSERT INTO t VALUES ({i}, 'row {i}');\n" for i in range(2000))


class FakeHealthcheck(Healthcheck):
    def __init__(self) -> None:
        super().__init__("https://hc.example/abc")
        self.events: list[tuple[str, str]] = []

    def ping(self, event, message="", duration=None):
        self.events.append((event, message))
        return True


def fake_dump(sql: str = SQL, fail_for: str | None = None):
    def factory(settings, database, secrets_dir):
        if database == fail_for:
            return python_command(f"import sys; sys.stderr.write('auth failed {FAKE_DB_PASSWORD}'); sys.exit(1)")
        return python_command(f"import sys; sys.stdout.write({sql!r})")

    return factory


def clock(day: int = 18):
    return lambda: datetime(2026, 11, day, 3, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _dump(monkeypatch):
    monkeypatch.setattr(jobs, "dump_command", fake_dump())


def test_backup_uploads_gzip(s3, tmp_path):
    settings = make_settings(DB_NAMES="app,crm", WORK_DIR=str(tmp_path))
    hc = FakeHealthcheck()
    report = run_backup(settings, Storage(settings), hc, clock())
    assert report.ok
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"])
    assert keys == ["backups/app/app-20261118T030000Z.sql.gz", "backups/crm/crm-20261118T030000Z.sql.gz"]
    body = s3.get_object(Bucket=BUCKET, Key=keys[0])["Body"].read()
    assert gzip.decompress(body).decode() == SQL
    assert [e for e, _ in hc.events] == ["start", "success"]
    assert "OK   app" in hc.events[1][1]
    assert list(tmp_path.iterdir()) == []  # temporary files are cleaned up


def test_backup_failure_pings_fail_and_continues(s3, tmp_path, monkeypatch):
    logs.register_secrets(FAKE_DB_PASSWORD)
    monkeypatch.setattr(jobs, "dump_command", fake_dump(fail_for="crm"))
    settings = make_settings(DB_NAMES="app,crm", WORK_DIR=str(tmp_path))
    hc = FakeHealthcheck()
    report = run_backup(settings, Storage(settings), hc, clock())
    assert not report.ok
    assert [r.ok for r in report.results] == [True, False]
    assert hc.events[-1][0] == "fail"
    assert "FAIL crm" in hc.events[-1][1]
    assert FAKE_DB_PASSWORD not in hc.events[-1][1]
    assert FAKE_DB_PASSWORD not in report.results[1].error
    assert len(s3.list_objects_v2(Bucket=BUCKET)["Contents"]) == 1


def test_upload_failure_is_reported(aws, tmp_path):
    settings = make_settings(S3_BUCKET="missing-bucket", WORK_DIR=str(tmp_path))
    report = run_backup(settings, Storage(settings), FakeHealthcheck(), clock())
    assert not report.ok
    assert "upload to s3://missing-bucket" in report.results[0].error


def test_retention_runs_after_backup(s3, tmp_path):
    settings = make_settings(RETENTION_KEEP_LAST="2", WORK_DIR=str(tmp_path))
    storage = Storage(settings)
    for day in range(10, 18):
        s3.put_object(
            Bucket=BUCKET, Key=build_key("backups", "app", datetime(2026, 11, day, tzinfo=UTC), "none"), Body=b"x"
        )
    s3.put_object(Bucket=BUCKET, Key="backups/app/keep-me.txt", Body=b"x")
    report = run_backup(settings, storage, FakeHealthcheck(), clock())
    assert report.ok
    assert len(report.deleted) == 7
    remaining = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"])
    assert remaining == [
        "backups/app/app-20261117T000000Z.sql.gz",
        "backups/app/app-20261118T030000Z.sql.gz",
        "backups/app/keep-me.txt",
    ]


def test_prune_dry_run(s3, tmp_path):
    settings = make_settings(RETENTION_KEEP_DAILY="1")
    for hour in range(3):
        when = datetime(2026, 11, 18, tzinfo=UTC) + timedelta(hours=hour)
        s3.put_object(Bucket=BUCKET, Key=build_key("backups", "app", when, "none"), Body=b"x")
    deleted = jobs.prune(settings, Storage(settings), dry_run=True)
    assert len(deleted) == 2
    assert len(s3.list_objects_v2(Bucket=BUCKET)["Contents"]) == 3


def test_aes_backup_and_restore_roundtrip(s3, tmp_path, monkeypatch):
    settings = make_settings(ENCRYPTION="aes", ENCRYPTION_PASSPHRASE=FAKE_PASSPHRASE, WORK_DIR=str(tmp_path))
    storage = Storage(settings)
    report = run_backup(settings, storage, FakeHealthcheck(), clock())
    assert report.ok
    key = report.results[0].key
    assert key.endswith(".sql.gz.enc")
    raw = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert b"INSERT" not in raw

    restored = tmp_path / "restored.sql"
    seen = {}

    def fake_restore(settings, database, secrets_dir):
        seen["database"] = database
        return python_command(f"import sys; open({str(restored)!r}, 'wb').write(sys.stdin.buffer.read())")

    monkeypatch.setattr(jobs, "restore_command", fake_restore)
    backup = run_restore_job(settings, storage, "latest", database="app", target="app_copy")
    assert backup.key == key
    assert seen["database"] == "app_copy"
    assert restored.read_text() == SQL


def test_restore_to_file_by_filename(s3, tmp_path):
    settings = make_settings(WORK_DIR=str(tmp_path))
    storage = Storage(settings)
    key = run_backup(settings, storage, FakeHealthcheck(), clock()).results[0].key
    out = tmp_path / "out.sql"
    run_restore_job(settings, storage, key.rsplit("/", 1)[-1], output=str(out))
    assert out.read_text() == SQL


def test_restore_errors(s3, tmp_path):
    settings = make_settings(WORK_DIR=str(tmp_path))
    storage = Storage(settings)
    with pytest.raises(JobError, match="needs --database"):
        run_restore_job(settings, storage, "latest")
    with pytest.raises(JobError, match="no backups found"):
        run_restore_job(settings, storage, "latest", database="app")
    with pytest.raises(JobError, match="not found"):
        run_restore_job(settings, storage, "backups/app/app-20200101T000000Z.sql.gz")

    enc = make_settings(ENCRYPTION="aes", ENCRYPTION_PASSPHRASE=FAKE_PASSPHRASE, WORK_DIR=str(tmp_path))
    run_backup(enc, Storage(enc), FakeHealthcheck(), clock())
    with pytest.raises(JobError, match="set ENCRYPTION_PASSPHRASE"):
        run_restore_job(settings, storage, "latest", database="app", output=str(tmp_path / "x.sql"))
    wrong = make_settings(ENCRYPTION="aes", ENCRYPTION_PASSPHRASE="a different passphrase", WORK_DIR=str(tmp_path))
    with pytest.raises(JobError, match="wrong passphrase"):
        run_restore_job(wrong, Storage(wrong), "latest", database="app", output=str(tmp_path / "x.sql"))
    assert not (tmp_path / "x.sql").exists()
    assert not (tmp_path / "x.sql.partial").exists()


@pytest.mark.skipif(shutil.which("age") is None or shutil.which("age-keygen") is None, reason="age is not installed")
def test_age_roundtrip(s3, tmp_path):
    identity = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = next(line.split(": ")[1] for line in identity.read_text().splitlines() if "public key" in line)
    work = tmp_path / "work"
    work.mkdir()
    settings = make_settings(
        ENCRYPTION="age", AGE_RECIPIENTS=recipient, AGE_IDENTITY_FILE=str(identity), WORK_DIR=str(work)
    )
    storage = Storage(settings)
    key = run_backup(settings, storage, FakeHealthcheck(), clock()).results[0].key
    assert key.endswith(".sql.gz.age")
    out = tmp_path / "out.sql"
    run_restore_job(settings, storage, "latest", database="app", output=str(out))
    assert out.read_text() == SQL
