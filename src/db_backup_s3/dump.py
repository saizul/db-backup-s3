"""Build and run ``mysqldump``/``mariadb-dump``, ``pg_dump`` and the matching restore clients.

Passwords are never put on the command line (where ``ps`` would show them):
MySQL/MariaDB get a temporary ``--defaults-extra-file`` with mode 0600 and
PostgreSQL gets ``PGPASSWORD`` in the child environment only.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from db_backup_s3.config import Settings
from db_backup_s3.crypto import Sink
from db_backup_s3.logs import redact

READ_SIZE = 256 * 1024
STDERR_TAIL = 2000


class DumpError(Exception):
    """The dump or restore client failed."""


@dataclass
class Command:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


def _which(*names: str) -> str:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    wanted = names[0] if len(names) == 1 else "neither " + " nor ".join(names)
    verb = "is not" if len(names) == 1 else "is"
    raise DumpError(f"{wanted} {verb} installed (the Docker image includes all database clients)")


def _quote(value: str) -> str:
    """Quote a value for a MySQL option file."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mysql_defaults(settings: Settings, directory: str) -> str:
    path = os.path.join(directory, "client.cnf")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"[client]\nuser={_quote(settings.user)}\npassword={_quote(settings.password)}\n")
    return path


def _base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("DB_", "S3_", "AWS_", "ENCRYPTION_", "HEALTHCHECK"))
    }
    env.update(extra or {})
    return env


def dump_command(settings: Settings, database: str, secrets_dir: str) -> Command:
    """The command that writes an SQL dump of ``database`` (or all databases) to stdout."""
    if settings.engine == "mysql":
        binary = _which("mariadb-dump", "mysqldump")
        argv = [
            binary,
            f"--defaults-extra-file={_mysql_defaults(settings, secrets_dir)}",
            f"--host={settings.host}",
            f"--port={settings.port}",
            "--protocol=tcp",
            "--single-transaction",
            "--quick",
            "--routines",
            "--triggers",
            "--events",
            "--hex-blob",
            "--default-character-set=utf8mb4",
            *settings.dump_extra_args,
        ]
        # A single database is dumped without CREATE DATABASE/USE, so it can be restored under another name.
        argv += ["--all-databases"] if settings.all_databases else [database]
        return Command(argv, _base_env())

    env = _base_env({"PGPASSWORD": settings.password} if settings.password else {})
    conn = ["--host", settings.host, "--port", str(settings.port), "--username", settings.user, "--no-password"]
    if settings.all_databases:
        return Command([_which("pg_dumpall"), *conn, *settings.dump_extra_args], env)
    return Command([_which("pg_dump"), *conn, "--format=plain", *settings.dump_extra_args, database], env)


def restore_command(settings: Settings, database: str | None, secrets_dir: str) -> Command:
    """The client command that reads SQL from stdin into ``database`` (``None`` = dump decides)."""
    if settings.engine == "mysql":
        binary = _which("mariadb", "mysql")
        argv = [
            binary,
            f"--defaults-extra-file={_mysql_defaults(settings, secrets_dir)}",
            f"--host={settings.host}",
            f"--port={settings.port}",
            "--protocol=tcp",
            "--default-character-set=utf8mb4",
            *settings.restore_extra_args,
        ]
        if database:
            argv.append(database)
        return Command(argv, _base_env())

    env = _base_env({"PGPASSWORD": settings.password} if settings.password else {})
    argv = [
        _which("psql"),
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--username",
        settings.user,
        "--no-password",
        "--quiet",
        "--set",
        "ON_ERROR_STOP=1",
        *settings.restore_extra_args,
        "--dbname",
        database or "postgres",
    ]
    return Command(argv, env)


@contextlib.contextmanager
def secrets_directory(work_dir: str) -> Iterator[str]:
    """A private temporary directory for client config files, removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="dbs3-", dir=work_dir if os.path.isdir(work_dir) else None) as path:
        os.chmod(path, 0o700)
        yield path


def _tail(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    return redact(text[-STDERR_TAIL:])


def run_dump(command: Command, sink: Sink) -> int:
    """Run ``command`` and stream its stdout into ``sink``. Returns the number of bytes dumped."""
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(command.argv, stdout=subprocess.PIPE, stderr=err, env=command.env)
        except OSError as exc:
            raise DumpError(f"cannot start {os.path.basename(command.argv[0])}: {exc}") from None
        assert proc.stdout is not None
        total = 0
        try:
            while chunk := proc.stdout.read(READ_SIZE):
                total += len(chunk)
                sink.write(chunk)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            proc.stdout.close()
        code = proc.wait()
        err.seek(0)
        stderr = err.read()
    if code != 0:
        name = os.path.basename(command.argv[0])
        raise DumpError(f"{name} exited with code {code}: {_tail(stderr) or 'no error output'}")
    if total == 0:
        raise DumpError(f"{os.path.basename(command.argv[0])} produced no output")
    return total


def run_restore(command: Command, chunks: Iterable[bytes]) -> int:
    """Feed ``chunks`` into the restore client's stdin. Returns the number of bytes restored."""
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(command.argv, stdin=subprocess.PIPE, stdout=err, stderr=err, env=command.env)
        except OSError as exc:
            raise DumpError(f"cannot start {os.path.basename(command.argv[0])}: {exc}") from None
        assert proc.stdin is not None
        total = 0
        broken = False
        try:
            for chunk in chunks:
                try:
                    proc.stdin.write(chunk)
                except BrokenPipeError:
                    broken = True
                    break
                total += len(chunk)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        with contextlib.suppress(BrokenPipeError):
            proc.stdin.close()
        code = proc.wait()
        err.seek(0)
        output = err.read()
    if code != 0 or broken:
        name = os.path.basename(command.argv[0])
        raise DumpError(f"{name} exited with code {code}: {_tail(output) or 'no error output'}")
    return total
