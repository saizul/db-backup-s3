from __future__ import annotations

import sys
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from db_backup_s3 import logs
from db_backup_s3.config import Settings, load_settings
from db_backup_s3.dump import Command
from db_backup_s3.storage import Storage

BUCKET = "test-backups"

# Obviously fake values, assembled at runtime so secret scanners stay quiet.
FAKE_DB_PASSWORD = "pw-" + "not-a-real-password"
FAKE_SECRET_KEY = "fake" + "-secret-" + "access-key"
FAKE_PASSPHRASE = "correct horse " + "battery staple"


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "DB_ENGINE": "postgres",
        "DB_HOST": "db",
        "DB_USER": "app",
        "DB_PASSWORD": FAKE_DB_PASSWORD,
        "DB_NAMES": "app",
        "S3_BUCKET": BUCKET,
        "S3_ACCESS_KEY_ID": "AKIATESTFAKE",
        "S3_SECRET_ACCESS_KEY": FAKE_SECRET_KEY,
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def make_settings(**overrides: str) -> Settings:
    return load_settings(base_env(**overrides))


def python_command(code: str) -> Command:
    """A Command that runs a small Python snippet instead of a real database client."""
    return Command([sys.executable, "-c", code], {})


@pytest.fixture(autouse=True)
def _clean_secrets() -> Iterator[None]:
    logs.clear_secrets()
    yield
    logs.clear_secrets()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield


@pytest.fixture
def s3(aws: None):
    return boto3.client("s3", region_name="us-east-1")


@pytest.fixture
def storage_for(aws: None):
    def factory(settings: Settings) -> Storage:
        return Storage(settings)

    return factory
