from __future__ import annotations

import pytest

from conftest import FAKE_DB_PASSWORD, FAKE_PASSPHRASE, FAKE_SECRET_KEY, base_env, make_settings
from db_backup_s3.config import ConfigError, load_settings

AGE_KEY = "age1" + "q" * 58


def problems(env: dict[str, str], purpose: str = "backup") -> list[str]:
    with pytest.raises(ConfigError) as info:
        load_settings(env, purpose=purpose)
    return info.value.problems


def test_minimal_postgres_defaults():
    s = make_settings()
    assert s.engine == "postgres"
    assert s.port == 5432
    assert s.databases == ("app",)
    assert s.prefix == "backups"
    assert s.encryption == "none"
    assert s.schedule is None
    assert not s.retention.enabled


@pytest.mark.parametrize(
    ("engine", "normalised", "port", "user"),
    [
        ("mysql", "mysql", 3306, "root"),
        ("MariaDB", "mysql", 3306, "root"),
        ("postgresql", "postgres", 5432, "postgres"),
    ],
)
def test_engines_and_default_ports(engine, normalised, port, user):
    s = make_settings(DB_ENGINE=engine, DB_USER="")
    assert (s.engine, s.port, s.user) == (normalised, port, user)


def test_all_problems_reported_together():
    found = problems({"DB_ENGINE": "oracle", "DB_PORT": "abc", "COMPRESSION_LEVEL": "12"})
    text = "\n".join(found)
    assert "DB_ENGINE must be one of" in text
    assert "DB_HOST is required" in text
    assert "DB_NAMES is required" in text
    assert "S3_BUCKET is required" in text
    assert "DB_PORT must be a whole number" in text
    assert "COMPRESSION_LEVEL must be between 1 and 9" in text


def test_error_messages_never_contain_secrets():
    env = base_env(ENCRYPTION="aes", ENCRYPTION_PASSPHRASE="short", S3_ACCESS_KEY_ID="")
    found = "\n".join(problems(env))
    assert "at least 12 characters" in found
    assert "must be set together" in found
    for secret in (FAKE_DB_PASSWORD, FAKE_SECRET_KEY, "short"):
        assert secret not in found


def test_secret_from_file(tmp_path):
    secret_file = tmp_path / "pw"
    secret_file.write_text(FAKE_DB_PASSWORD + "\n")
    s = load_settings(base_env(DB_PASSWORD=None, DB_PASSWORD_FILE=str(secret_file)))
    assert s.password == FAKE_DB_PASSWORD


def test_secret_file_problems(tmp_path):
    assert any("cannot read" in p for p in problems(base_env(DB_PASSWORD=None, DB_PASSWORD_FILE=str(tmp_path / "x"))))
    assert any("not both" in p for p in problems(base_env(DB_PASSWORD_FILE=str(tmp_path / "x"))))


def test_aws_credential_aliases():
    env = base_env(
        S3_ACCESS_KEY_ID=None, S3_SECRET_ACCESS_KEY=None, AWS_ACCESS_KEY_ID="AKIAX", AWS_SECRET_ACCESS_KEY="y" * 8
    )
    s = load_settings(env)
    assert s.access_key_id == "AKIAX"


def test_default_credential_chain_when_no_keys():
    s = make_settings(S3_ACCESS_KEY_ID=None, S3_SECRET_ACCESS_KEY=None)
    assert s.access_key_id is None
    assert s.summary()["s3_credentials"] == "default AWS credential chain"


def test_database_names():
    assert make_settings(DB_NAMES="a, b ,c").databases == ("a", "b", "c")
    assert make_settings(DB_NAMES="all").all_databases
    assert any("alone" in p for p in problems(base_env(DB_NAMES="all,app")))
    assert any("not allowed" in p for p in problems(base_env(DB_NAMES="app;drop")))


def test_schedule_and_timezone():
    s = make_settings(SCHEDULE="0 3 * * *", SCHEDULE_TZ="Europe/Berlin")
    assert s.schedule is not None and s.schedule.expression == "0 3 * * *"
    assert s.timezone == "Europe/Berlin"
    found = problems(base_env(SCHEDULE="61 * * * *", SCHEDULE_TZ="Mars/Base"))
    assert any(p.startswith("SCHEDULE:") for p in found)
    assert any("unknown time zone" in p for p in found)


def test_encryption_validation():
    assert any("AGE_RECIPIENTS is required" in p for p in problems(base_env(ENCRYPTION="age")))
    assert any("not an age" in p for p in problems(base_env(ENCRYPTION="age", AGE_RECIPIENTS="nope")))
    assert make_settings(ENCRYPTION="age", AGE_RECIPIENTS=AGE_KEY).age_recipients == (AGE_KEY,)
    assert make_settings(ENCRYPTION="aes", ENCRYPTION_PASSPHRASE=FAKE_PASSPHRASE).passphrase == FAKE_PASSPHRASE
    assert any("ENCRYPTION must be one of" in p for p in problems(base_env(ENCRYPTION="rot13")))


def test_booleans_and_endpoint():
    s = make_settings(S3_FORCE_PATH_STYLE="true", S3_ENDPOINT_URL="http://minio:9000", RUN_ON_STARTUP="yes")
    assert s.force_path_style and s.run_on_startup
    found = problems(base_env(S3_FORCE_PATH_STYLE="maybe", S3_ENDPOINT_URL="minio:9000"))
    assert any("true or false" in p for p in found)
    assert any("http:// or https://" in p for p in found)


def test_retention():
    s = make_settings(RETENTION_KEEP_DAILY="7", RETENTION_KEEP_WEEKLY="4", RETENTION_KEEP_MONTHLY="12")
    assert s.retention.enabled
    assert (s.retention.keep_daily, s.retention.keep_weekly, s.retention.keep_monthly) == (7, 4, 12)
    assert any("at least 0" in p for p in problems(base_env(RETENTION_KEEP_LAST="-1")))


def test_purpose_storage_needs_only_bucket():
    s = load_settings({"S3_BUCKET": "b"}, purpose="storage")
    assert s.bucket == "b"
    assert any("DB_HOST" in p for p in problems({"S3_BUCKET": "b"}, purpose="restore"))
    # restore does not need DB_NAMES or AGE_RECIPIENTS
    load_settings({"S3_BUCKET": "b", "DB_ENGINE": "mysql", "DB_HOST": "h", "ENCRYPTION": "age"}, purpose="restore")


def test_summary_and_repr_mask_secrets():
    s = make_settings(
        ENCRYPTION="aes", ENCRYPTION_PASSPHRASE=FAKE_PASSPHRASE, HEALTHCHECK_URL="https://hc-ping.com/uuid-123"
    )
    rendered = repr(s) + str(s.summary())
    for secret in (FAKE_DB_PASSWORD, FAKE_SECRET_KEY, FAKE_PASSPHRASE, "uuid-123"):
        assert secret not in rendered
    assert set(s.secrets()) == {FAKE_DB_PASSWORD, FAKE_SECRET_KEY, FAKE_PASSPHRASE, "https://hc-ping.com/uuid-123"}


def test_extra_args():
    s = make_settings(
        DB_DUMP_EXTRA_ARGS="--no-owner --exclude-table='audit log'", DB_RESTORE_EXTRA_ARGS="--single-transaction"
    )
    assert s.dump_extra_args == ("--no-owner", "--exclude-table=audit log")
    assert s.restore_extra_args == ("--single-transaction",)
    assert any("cannot be parsed" in p for p in problems(base_env(DB_DUMP_EXTRA_ARGS="'unclosed")))
