from __future__ import annotations

import io
import os
import stat

import pytest

from conftest import FAKE_DB_PASSWORD, make_settings, python_command
from db_backup_s3 import dump, logs
from db_backup_s3.crypto import FileSink
from db_backup_s3.dump import DumpError, dump_command, restore_command, run_dump, run_restore


@pytest.fixture
def fake_binaries(monkeypatch):
    monkeypatch.setattr(dump.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_mysql_dump_command_keeps_password_off_argv(tmp_path, fake_binaries):
    s = make_settings(DB_ENGINE="mariadb", DB_NAMES="shop", DB_PASSWORD='we"ird\\pass')
    cmd = dump_command(s, "shop", str(tmp_path))
    assert cmd.argv[0] == "/usr/bin/mariadb-dump"
    assert cmd.argv[1] == f"--defaults-extra-file={tmp_path / 'client.cnf'}"
    assert "--single-transaction" in cmd.argv
    assert cmd.argv[-1] == "shop"
    assert "--databases" not in cmd.argv  # no USE statement: restorable under another name
    assert not any('we"ird' in a for a in cmd.argv)
    cnf = tmp_path / "client.cnf"
    assert stat.S_IMODE(cnf.stat().st_mode) == 0o600
    assert 'password="we\\"ird\\\\pass"' in cnf.read_text()


def test_mysql_all_databases_and_extra_args(tmp_path, fake_binaries):
    s = make_settings(DB_ENGINE="mysql", DB_NAMES="all", DB_DUMP_EXTRA_ARGS="--column-statistics=0 --skip-lock-tables")
    cmd = dump_command(s, "all", str(tmp_path))
    assert cmd.argv[-1] == "--all-databases"
    assert "--column-statistics=0" in cmd.argv and "--skip-lock-tables" in cmd.argv


def test_postgres_commands(tmp_path, fake_binaries, monkeypatch):
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("LANG", "C.UTF-8")
    s = make_settings(DB_PORT="6543")
    cmd = dump_command(s, "app", str(tmp_path))
    assert cmd.argv[0] == "/usr/bin/pg_dump"
    assert cmd.argv[-1] == "app"
    assert cmd.argv[3:5] == ["--port", "6543"]
    assert cmd.env["PGPASSWORD"] == FAKE_DB_PASSWORD
    assert FAKE_DB_PASSWORD not in " ".join(cmd.argv)
    assert "S3_SECRET_ACCESS_KEY" not in cmd.env  # our own settings are not passed to clients
    assert cmd.env["LANG"] == "C.UTF-8"
    assert dump_command(make_settings(DB_NAMES="all"), "all", str(tmp_path)).argv[0] == "/usr/bin/pg_dumpall"


def test_restore_commands(tmp_path, fake_binaries):
    pg = restore_command(make_settings(), "copy", str(tmp_path))
    assert pg.argv[0] == "/usr/bin/psql"
    assert pg.argv[-2:] == ["--dbname", "copy"]
    assert "ON_ERROR_STOP=1" in pg.argv
    my = restore_command(make_settings(DB_ENGINE="mysql", DB_RESTORE_EXTRA_ARGS="--force"), None, str(tmp_path))
    assert my.argv[0] == "/usr/bin/mariadb"
    assert my.argv[-1] == "--force"
    assert restore_command(make_settings(DB_ENGINE="mysql"), "shop", str(tmp_path)).argv[-1] == "shop"


def test_missing_client(monkeypatch, tmp_path):
    monkeypatch.setattr(dump.shutil, "which", lambda name: None)
    with pytest.raises(DumpError, match="pg_dump is not installed"):
        dump_command(make_settings(), "app", str(tmp_path))
    with pytest.raises(DumpError, match="neither mariadb-dump nor mysqldump is installed"):
        dump_command(make_settings(DB_ENGINE="mysql"), "app", str(tmp_path))


def test_run_dump_streams_output():
    out = io.BytesIO()
    code = "import sys\nfor i in range(50000): sys.stdout.write(f'INSERT {i};\\n')"
    total = run_dump(python_command(code), FileSink(out))
    assert total == len(out.getvalue())
    assert out.getvalue().startswith(b"INSERT 0;\n")
    assert out.getvalue().endswith(b"INSERT 49999;\n")


def test_run_dump_failure_is_redacted():
    logs.register_secrets(FAKE_DB_PASSWORD)
    code = f"import sys; sys.stderr.write('access denied for password {FAKE_DB_PASSWORD}'); sys.exit(2)"
    with pytest.raises(DumpError) as info:
        run_dump(python_command(code), FileSink(io.BytesIO()))
    assert "exited with code 2" in str(info.value)
    assert "access denied" in str(info.value)
    assert FAKE_DB_PASSWORD not in str(info.value)


def test_run_dump_empty_output():
    with pytest.raises(DumpError, match="no output"):
        run_dump(python_command("pass"), FileSink(io.BytesIO()))


def test_run_dump_cannot_start():
    with pytest.raises(DumpError, match="cannot start"):
        run_dump(dump.Command(["/nonexistent/pg_dump"]), FileSink(io.BytesIO()))


def test_run_restore(tmp_path):
    target = tmp_path / "restored.sql"
    code = f"import sys; open({str(target)!r}, 'wb').write(sys.stdin.buffer.read())"
    total = run_restore(python_command(code), [b"CREATE TABLE t();\n", b"INSERT 1;\n"])
    assert total == 28
    assert target.read_bytes() == b"CREATE TABLE t();\nINSERT 1;\n"


def test_run_restore_failure():
    code = "import sys; sys.stdin.read(); sys.stderr.write('ERROR: relation exists'); sys.exit(3)"
    with pytest.raises(DumpError, match="relation exists"):
        run_restore(python_command(code), [b"x" * 10])


def test_secrets_directory_is_private_and_removed(tmp_path):
    with dump.secrets_directory(str(tmp_path)) as path:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    assert not os.path.exists(path)
