from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import BUCKET, make_settings
from db_backup_s3.storage import Storage, StorageError, build_key, parse_key

WHEN = datetime(2026, 11, 18, 3, 0, 5, tzinfo=UTC)


def test_build_and_parse_key():
    key = build_key("backups", "app", WHEN, "aes")
    assert key == "backups/app/app-20261118T030005Z.sql.gz.enc"
    parsed = parse_key(key, 42)
    assert parsed is not None
    assert (parsed.database, parsed.created, parsed.size, parsed.encryption) == ("app", WHEN, 42, "aes")
    assert build_key("", "all", WHEN, "age") == "all-databases/all-databases-20261118T030005Z.sql.gz.age"


@pytest.mark.parametrize(
    "key",
    [
        "backups/app/other-20261118T030005Z.sql.gz",
        "backups/app/app-2026.sql.gz",
        "backups/app/app-20261118T030005Z.tar",
    ],
)
def test_foreign_keys_are_ignored(key):
    assert parse_key(key) is None


def test_upload_list_download_delete(s3, tmp_path):
    storage = Storage(make_settings())
    src = tmp_path / "dump.sql.gz"
    src.write_bytes(b"\x1f\x8b fake")
    older = build_key("backups", "app", datetime(2026, 11, 17, tzinfo=UTC), "none")
    newer = build_key("backups", "app", WHEN, "none")
    for key in (older, newer, build_key("backups", "crm", WHEN, "none")):
        storage.upload(str(src), key)
    s3.put_object(Bucket=BUCKET, Key="backups/app/notes.txt", Body=b"not a backup")
    s3.put_object(Bucket=BUCKET, Key="backups/nested/app/app-20261118T030005Z.sql.gz", Body=b"x")

    everything = storage.list_backups()
    assert [b.key for b in everything][:2] in (
        [newer, "backups/crm/crm-20261118T030005Z.sql.gz"],
        ["backups/crm/crm-20261118T030005Z.sql.gz", newer],
    )
    assert len(everything) == 3
    app = storage.list_backups("app")
    assert [b.key for b in app] == [newer, older]
    assert app[0].size == len(b"\x1f\x8b fake")

    dest = tmp_path / "back.gz"
    storage.download(newer, str(dest))
    assert dest.read_bytes() == src.read_bytes()

    storage.delete([older])
    assert [b.key for b in storage.list_backups("app")] == [newer]
    storage.check_access()


def test_storage_class_is_sent(s3, tmp_path):
    storage = Storage(make_settings(S3_STORAGE_CLASS="STANDARD_IA"))
    src = tmp_path / "f"
    src.write_bytes(b"x")
    storage.upload(str(src), "backups/app/app-20261118T030005Z.sql.gz")
    head = s3.head_object(Bucket=BUCKET, Key="backups/app/app-20261118T030005Z.sql.gz")
    assert head["StorageClass"] == "STANDARD_IA"


def test_readable_errors(aws, tmp_path):
    storage = Storage(make_settings(S3_BUCKET="does-not-exist"))
    with pytest.raises(StorageError, match="cannot access bucket 'does-not-exist'"):
        storage.check_access()
    with pytest.raises(StorageError, match="NoSuchBucket"):
        storage.list_backups()
    with pytest.raises(StorageError, match="download of s3://does-not-exist/"):
        storage.download("k", str(tmp_path / "x"))


def test_path_style_and_endpoint_config(aws):
    storage = Storage(make_settings(S3_ENDPOINT_URL="http://minio:9000", S3_FORCE_PATH_STYLE="true"))
    assert storage.client.meta.endpoint_url == "http://minio:9000"
    assert storage.client.meta.config.s3["addressing_style"] == "path"
