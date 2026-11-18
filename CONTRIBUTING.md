# Contributing to db-backup-s3

Thank you for helping make database backups boring and reliable! Bug reports,
storage provider guides, new options and documentation fixes are all welcome.

- [Good first issues](#good-first-issues)
- [Development setup](#development-setup)
- [Project layout](#project-layout)
- [Add a configuration option](#add-a-configuration-option)
- [Testing without a database or cloud account](#testing-without-a-database-or-cloud-account)
- [Guidelines](#guidelines)
- [Pull request process](#pull-request-process)

## Good first issues

These are small, well-scoped tasks that are a great way to learn the code base.
Each one touches one or two modules and comes with an obvious test. Comment on
the matching issue (labelled `good first issue`) so nobody else picks the same one.

| # | Task | Where to start |
| --- | --- | --- |
| 1 | **`list --since 7d` / `--limit N`**: filter the `list` output by age or count. | `cli.py` (`_print_list`), `tests/test_cli.py` |
| 2 | **`DB_EXCLUDE`**: with `DB_NAMES=all`, skip some databases (MySQL `--ignore-database`, `pg_dumpall --exclude-database`). | `config.py`, `dump.py`, `tests/test_dump.py` |
| 3 | **zstd compression**: `COMPRESSION=gzip\|zstd` using the `zstandard` package, with `.sql.zst` object names. | `crypto.py` (`GzipSink`, `gunzip`), `storage.py` (`_KEY_RE`) |
| 4 | **Retention `keep_yearly`**: one more rule next to daily/weekly/monthly. | `retention.py`, `config.py`, `tests/test_retention.py` |
| 5 | **`HEALTHCHECK_KIND=generic`**: plain GET/POST to separate success and failure URLs (`HEALTHCHECK_FAIL_URL`) for tools like Gatus or Better Stack. | `healthcheck.py`, `tests/test_healthcheck.py` |
| 6 | **`restore --dry-run`**: download, decrypt and decompress to `/dev/null` to prove a backup is restorable, and print its size and SHA-256. | `jobs.py` (`run_restore_job`), `cli.py` |
| 7 | **Wasabi, DigitalOcean Spaces, Scaleway and Hetzner examples**: an `.env.example` file and a README row for each provider you can test. | `examples/`, `README.md` |
| 8 | **Upload metadata**: store the db-backup-s3 version, engine and raw size as S3 object metadata and show them in `list --json`. | `storage.py` (`upload`), `jobs.py` |
| 9 | **Webhook notifications**: `NOTIFY_WEBHOOK_URL` that posts a JSON summary (Slack, Discord, ntfy) on failure. | new `notify.py`, `jobs.py` (`run_backup`) |
| 10 | **`MAX_BACKUP_SECONDS`**: kill a dump that hangs and report a clear failure. | `dump.py` (`run_dump`) |

## Development setup

```bash
git clone https://github.com/saizul/db-backup-s3
cd db-backup-s3
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .[dev]
pytest
ruff check . && ruff format --check .
```

Python 3.11+ is needed. `age` is optional locally (the age round-trip test is
skipped without it). Docker is only needed to build the image or try the
[quick start](README.md#60-second-quick-start).

## Project layout

| Path | Responsibility |
| --- | --- |
| `src/db_backup_s3/config.py` | Environment variables to a validated, immutable `Settings` object (`load_settings`) |
| `src/db_backup_s3/schedule.py` | Dependency-free cron parser and next-run calculation |
| `src/db_backup_s3/dump.py` | Builds and runs `mariadb-dump`/`mysqldump`, `pg_dump`, `pg_dumpall`, `mariadb`/`psql` |
| `src/db_backup_s3/crypto.py` | Streaming gzip, AES-256-GCM chunked format, age wrapper |
| `src/db_backup_s3/storage.py` | boto3 wrapper and the object naming scheme (`build_key`, `parse_key`) |
| `src/db_backup_s3/retention.py` | Pure retention planner (no I/O, easy to test) |
| `src/db_backup_s3/healthcheck.py` | healthchecks.io and Uptime Kuma pings |
| `src/db_backup_s3/jobs.py` | The backup, prune and restore jobs that tie everything together |
| `src/db_backup_s3/cli.py` | Commands, the scheduler loop, exit codes |
| `src/db_backup_s3/logs.py` | Logging setup and secret redaction |
| `docs/aes-format.md` | The AES file format, with a stand-alone decrypt script |

## Add a configuration option

1. Read and validate it in `load_settings` in `config.py` using the `_Reader`
   helpers (`integer`, `boolean`, `choice`, `secret`, ...). Invalid values must
   add a clear message to `r.problems` (never include secret values).
2. Add the field to `Settings` (with `repr=False` if it is secret, and add it to
   `Settings.secrets()` so it is redacted from logs).
3. Use it, and add tests for the valid and invalid cases.
4. Document it in the README configuration table and in `CHANGELOG.md`.

## Testing without a database or cloud account

- **Database clients** are replaced by small Python commands: `python_command(code)`
  in `tests/conftest.py` returns a `Command` you can monkeypatch into
  `jobs.dump_command` / `jobs.restore_command`.
- **S3** is emulated in memory with [moto](https://github.com/getmoto/moto):
  use the `s3` fixture (a boto3 client for the pre-created `test-backups` bucket).
- **Healthchecks** take an `opener` argument, so tests never use the network.
- The CI `integration` job runs the real image against PostgreSQL, MariaDB and
  MinIO containers. You can run the same steps locally with Docker.

Never put a real-looking secret in a test as one string literal; build it from
pieces at runtime (see `conftest.py`).

## Guidelines

- **Safety first.** Backups must be complete or fail loudly. Never upload a
  partial dump, never delete objects that do not match the naming scheme.
- **Secrets stay secret.** No secret in logs, errors, process arguments or
  `check-config` output.
- **Backwards compatible.** Old backups must stay restorable; object names and
  encryption formats are versioned.
- **Few dependencies.** Runtime dependencies are boto3 and cryptography; discuss
  new ones in an issue first.
- Type hints everywhere, `ruff check` and `ruff format` clean.

## Pull request process

1. Fork and create a branch (`feat/db-exclude`, `fix/r2-checksum`).
2. Keep pull requests focused; fill in the checklist in the template.
3. Add an entry to `CHANGELOG.md` under "Unreleased".
4. A maintainer reviews within a few days.

By contributing you agree that your work is released under the MIT License and
that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).
