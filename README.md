# db-backup-s3

[![CI](https://github.com/saizul/db-backup-s3/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/db-backup-s3/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/saizul/db-backup-s3?include_prereleases&sort=semver)](https://github.com/saizul/db-backup-s3/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Container image](https://img.shields.io/badge/ghcr.io-saizul%2Fdb--backup--s3-2496ED?logo=docker&logoColor=white)](https://github.com/saizul/db-backup-s3/pkgs/container/db-backup-s3)
![Python](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**Set-and-forget database backups to any S3-compatible storage.** A small
container (and CLI) that dumps **MySQL / MariaDB** and **PostgreSQL** on a cron
schedule, compresses and optionally encrypts the dump, uploads it to **AWS S3,
Cloudflare R2, Backblaze B2, MinIO** or anything else that speaks S3, prunes old
backups, pings your **healthchecks.io / Uptime Kuma** monitor, and restores
with one command.

```text
$ docker run --rm --env-file backup.env ghcr.io/saizul/db-backup-s3 backup
2026-11-18T03:00:00+0000 INFO    Backing up app
2026-11-18T03:00:04+0000 INFO    Uploaded backups/app/app-20261118T030000Z.sql.gz.age (18.2 MiB) in 4.1s
2026-11-18T03:00:05+0000 INFO    Deleting backups/app/app-20261021T030000Z.sql.gz.age
2026-11-18T03:00:05+0000 INFO    Retention: keeping 20, deleted 1
2026-11-18T03:00:05+0000 INFO    Backup finished in 4.9s
2026-11-18T03:00:05+0000 INFO    OK   app: 18.2 MiB (raw 141.7 MiB) in 4.1s -> backups/app/app-20261118T030000Z.sql.gz.age
2026-11-18T03:00:05+0000 INFO    Retention removed 1 old backup(s)

$ docker run --rm --env-file backup.env -it ghcr.io/saizul/db-backup-s3 restore latest -d app --target-db app_copy
```

## Contents

- [Features](#features)
- [60-second quick start](#60-second-quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [Storage providers](#storage-providers) (AWS S3, Cloudflare R2, Backblaze B2, MinIO)
- [Encryption](#encryption)
- [Retention](#retention)
- [Healthchecks](#healthchecks)
- [Restore guide](#restore-guide)
- [Running without Docker](#running-without-docker)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

## Features

- **MySQL, MariaDB and PostgreSQL**: consistent dumps (`--single-transaction` /
  `pg_dump` snapshots) of one, several or all databases (`pg_dumpall`).
- **Any S3-compatible storage** with boto3: AWS S3, Cloudflare R2, Backblaze B2,
  MinIO, Wasabi, DigitalOcean Spaces, Scaleway, ... Multipart uploads for large dumps.
- **Streaming gzip**: the dump is compressed while it is produced; memory use stays flat.
- **Encryption**: [age](https://age-encryption.org) public keys (the server
  cannot read its own backups) or a passphrase with AES-256-GCM.
- **Built-in scheduler**: standard cron expressions, time zones, run on start,
  graceful shutdown. Or run once and exit for Kubernetes CronJobs and systemd timers.
- **Retention**: keep the last N, plus daily, weekly and monthly backups. Never
  touches files it did not create; `prune --dry-run` shows the plan.
- **Healthchecks**: start, success and failure pings for healthchecks.io and
  Uptime Kuma push monitors, with a summary of the run.
- **One-command restore**: `restore latest` into the same or another database,
  or decrypt and decompress to a plain `.sql` file.
- **Safe by default**: every configuration problem reported at once, secrets
  from `*_FILE` (Docker / Kubernetes secrets), passwords never on the command
  line, secrets masked in all logs, non-root container.

## 60-second quick start

Try the whole thing locally with PostgreSQL and MinIO (needs Docker):

```bash
git clone https://github.com/saizul/db-backup-s3 && cd db-backup-s3
docker compose -f examples/docker-compose.quickstart.yml up -d       # postgres + minio + db-backup-s3
docker compose -f examples/docker-compose.quickstart.yml exec backup db-backup-s3 list
docker compose -f examples/docker-compose.quickstart.yml exec backup db-backup-s3 backup
docker compose -f examples/docker-compose.quickstart.yml exec -T backup db-backup-s3 restore latest -d app -o - | head
```

The container backs up on start and then every 5 minutes. Open the MinIO console
at <http://localhost:9001> (`demo-user` / `demo-password`) to see the files. Clean
up with `docker compose -f examples/docker-compose.quickstart.yml down -v`.

To back up your own database, create `backup.env`:

```bash
DB_ENGINE=postgres            # or mysql / mariadb
DB_HOST=db.internal
DB_USER=backup
DB_PASSWORD=...
DB_NAMES=app,analytics        # or "all"
S3_BUCKET=my-db-backups
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
SCHEDULE=0 3 * * *            # every night at 03:00
RETENTION_KEEP_DAILY=7
RETENTION_KEEP_WEEKLY=4
RETENTION_KEEP_MONTHLY=12
```

```bash
docker run --rm --env-file backup.env ghcr.io/saizul/db-backup-s3 check-config --connect
docker run -d --name db-backup --restart unless-stopped --env-file backup.env ghcr.io/saizul/db-backup-s3
```

For Docker Compose, see [`examples/docker-compose.yml`](examples/docker-compose.yml)
(with Docker secrets); for Kubernetes, [`examples/kubernetes-cronjob.yaml`](examples/kubernetes-cronjob.yaml).

## Usage

```text
db-backup-s3 [run]                 run on SCHEDULE; without SCHEDULE back up once and exit (default)
db-backup-s3 backup                back up all DB_NAMES once and exit
db-backup-s3 list [-d DB] [--json] list backups, newest first
db-backup-s3 restore REF [-d DB] [--target-db NAME] [-o FILE|-] [--yes]
                                   REF is "latest", an object key or a file name
db-backup-s3 prune [--dry-run]     apply the retention policy now
db-backup-s3 check-config [--connect]
                                   validate the configuration, show it with secrets masked,
                                   print the next run times and (optionally) test the bucket
```

Exit codes: `0` success, `1` a backup, upload, restore or retention step failed,
`2` invalid configuration.

Backups are stored as
`<S3_PREFIX>/<database>/<database>-<YYYYMMDDTHHMMSSZ>.sql.gz[.age|.enc]`
(`all-databases` when `DB_NAMES=all`).

## Configuration

Everything is configured with environment variables. Every secret can also be
read from a file by adding `_FILE` (for example `DB_PASSWORD_FILE=/run/secrets/db_password`).

| Variable | Default | Description |
| --- | --- | --- |
| `DB_ENGINE` | **required** | `postgres`, `mysql` or `mariadb` |
| `DB_HOST` | **required** | Database host name |
| `DB_PORT` | `5432` / `3306` | Database port |
| `DB_USER` | `postgres` / `root` | Database user |
| `DB_PASSWORD` | | Database password (`DB_PASSWORD_FILE` supported) |
| `DB_NAMES` | **required** | Comma-separated database names, or `all` (`pg_dumpall` / `--all-databases`) |
| `DB_DUMP_EXTRA_ARGS` | | Extra arguments for the dump tool, shell-quoted (e.g. `--no-owner --exclude-table=logs`) |
| `DB_RESTORE_EXTRA_ARGS` | | Extra arguments for `psql` / `mariadb` during restore |
| `S3_BUCKET` | **required** | Bucket name |
| `S3_PREFIX` | `backups` | Folder inside the bucket |
| `S3_ENDPOINT_URL` | AWS | Endpoint for non-AWS storage, see [Storage providers](#storage-providers) |
| `S3_REGION` | `us-east-1` | Region (`AWS_REGION` also works; `auto` for R2) |
| `S3_ACCESS_KEY_ID` | | Access key (`AWS_ACCESS_KEY_ID` also works). Empty = default AWS credential chain (IAM role, profile) |
| `S3_SECRET_ACCESS_KEY` | | Secret key (`AWS_SECRET_ACCESS_KEY` and `_FILE` supported) |
| `S3_FORCE_PATH_STYLE` | `false` | Path-style URLs (needed for MinIO and some self-hosted storage) |
| `S3_STORAGE_CLASS` | | e.g. `STANDARD_IA`, `GLACIER_IR` (AWS) |
| `SCHEDULE` | | Cron expression (`0 3 * * *`, `*/30 * * * *`, `@daily`). Empty = run once and exit |
| `SCHEDULE_TZ` | `UTC` | Time zone for `SCHEDULE` and retention days (`TZ` also works), e.g. `Europe/Berlin` |
| `RUN_ON_STARTUP` | `false` | Also back up immediately when the container starts |
| `ENCRYPTION` | `none` | `none`, `age` or `aes`, see [Encryption](#encryption) |
| `AGE_RECIPIENTS` | | Comma-separated age (`age1...`) or SSH public keys, for `ENCRYPTION=age` |
| `AGE_IDENTITY_FILE` | | Private key file, only needed to restore age backups |
| `ENCRYPTION_PASSPHRASE` | | Passphrase for `ENCRYPTION=aes`, 12+ characters (`_FILE` supported) |
| `RETENTION_KEEP_LAST` | `0` | Keep the N most recent backups |
| `RETENTION_KEEP_DAILY` | `0` | Keep the newest backup of each of the last N days |
| `RETENTION_KEEP_WEEKLY` | `0` | ... of each of the last N weeks |
| `RETENTION_KEEP_MONTHLY` | `0` | ... of each of the last N months. All `0` = keep everything |
| `HEALTHCHECK_URL` | | healthchecks.io ping URL or Uptime Kuma push URL (`_FILE` supported) |
| `HEALTHCHECK_KIND` | `auto` | `auto`, `healthchecks` or `uptime-kuma` |
| `HEALTHCHECK_TIMEOUT` | `10` | Seconds per ping attempt (3 attempts) |
| `COMPRESSION_LEVEL` | `6` | gzip level 1 (fast) to 9 (small) |
| `WORK_DIR` | `/tmp` | Where the compressed file is staged before upload (needs space for one backup) |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `LOG_FORMAT` | `text` | `text` or `json` (one JSON object per line) |

Invalid configuration is reported in one go:

```text
$ db-backup-s3 backup
db-backup-s3: Invalid configuration:
  - DB_HOST is required (host name of the database server)
  - SCHEDULE: hour: 24 is out of range 0-23
  - ENCRYPTION_PASSPHRASE must be at least 12 characters long
```

## Storage providers

Full files are in [`examples/`](examples/).

| Provider | `S3_ENDPOINT_URL` | `S3_REGION` | Notes |
| --- | --- | --- | --- |
| AWS S3 | *(empty)* | your region | Works with IAM roles; least-privilege policy in [`examples/iam-policy.json`](examples/iam-policy.json) |
| Cloudflare R2 | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` | `auto` | API token with Object Read & Write; no egress fees |
| Backblaze B2 | `https://s3.<region>.backblazeb2.com` | e.g. `us-west-004` | Application key limited to the bucket |
| MinIO | `http://minio:9000` | `us-east-1` | Set `S3_FORCE_PATH_STYLE=true` |
| Wasabi | `https://s3.<region>.wasabisys.com` | your region | |
| DigitalOcean Spaces | `https://<region>.digitaloceanspaces.com` | your region | |

**Cloudflare R2**

```bash
S3_BUCKET=my-db-backups
S3_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
S3_REGION=auto
S3_ACCESS_KEY_ID=<R2 access key id>
S3_SECRET_ACCESS_KEY=<R2 secret access key>
```

**Backblaze B2**

```bash
S3_BUCKET=my-db-backups
S3_ENDPOINT_URL=https://s3.us-west-004.backblazeb2.com
S3_REGION=us-west-004
S3_ACCESS_KEY_ID=<keyID>
S3_SECRET_ACCESS_KEY=<applicationKey>
```

B2 keeps old file versions by default; set the bucket lifecycle to "keep only
the last version" so retention actually frees space.

**MinIO**

```bash
S3_BUCKET=db-backups
S3_ENDPOINT_URL=http://minio:9000
S3_FORCE_PATH_STYLE=true
S3_ACCESS_KEY_ID=<access key>
S3_SECRET_ACCESS_KEY=<secret key>
```

## Encryption

**age (recommended).** Generate a key pair on your own machine and give the
container only the public key. A compromised server cannot read old backups.

```bash
age-keygen -o age-key.txt          # keep this file offline / in your password manager
# Public key: age1...
ENCRYPTION=age
AGE_RECIPIENTS=age1...,age1...     # several recipients = several people/keys can restore
```

**AES passphrase.** Simpler, but the passphrase lives on the server:

```bash
ENCRYPTION=aes
ENCRYPTION_PASSPHRASE_FILE=/run/secrets/backup_passphrase
```

AES-256-GCM with a scrypt-derived key, in authenticated 64 KiB chunks:
tampered or truncated files are detected. The format is documented in
[`docs/aes-format.md`](docs/aes-format.md), including a short script to
decrypt without this tool.

## Retention

Retention runs after each successful backup, per database. A backup is kept if
any rule selects it:

```bash
RETENTION_KEEP_LAST=3       # the 3 newest
RETENTION_KEEP_DAILY=7      # + newest of each of the last 7 days
RETENTION_KEEP_WEEKLY=4     # + newest of each of the last 4 ISO weeks
RETENTION_KEEP_MONTHLY=12   # + newest of each of the last 12 months
```

Only objects that match the naming scheme are considered, the newest backup is
always kept, and nothing is deleted when all rules are `0`. Preview with
`db-backup-s3 prune --dry-run`.

## Healthchecks

Set `HEALTHCHECK_URL` and get alerted when backups stop or fail:

- **[healthchecks.io](https://healthchecks.io)** (or self-hosted Healthchecks):
  use the ping URL, e.g. `https://hc-ping.com/<uuid>`. db-backup-s3 sends
  `/start` before, the URL itself on success and `/fail` on failure, with the
  run summary in the body. Set the period to match `SCHEDULE`.
- **[Uptime Kuma](https://github.com/louislam/uptime-kuma)**: create a *Push*
  monitor and use its URL (`https://kuma.example.com/api/push/<token>?...`).
  db-backup-s3 sends `status=up` or `status=down` with a message and the duration.

A failing ping never fails the backup. The URL is treated as a secret.

## Restore guide

1. **Find the backup.**

   ```bash
   docker run --rm --env-file backup.env ghcr.io/saizul/db-backup-s3 list -d app
   ```

2. **Create the target database** (a dump of a single database does not contain
   `CREATE DATABASE`, so you can restore it under any name):

   ```bash
   createdb -h db.internal -U postgres app_restore          # PostgreSQL
   mariadb -h db.internal -u root -p -e "CREATE DATABASE app_restore"   # MySQL / MariaDB
   ```

3. **Restore.** `latest` picks the newest backup of the database; you can also pass
   an object key or file name from `list`.

   ```bash
   docker run --rm -it --env-file backup.env ghcr.io/saizul/db-backup-s3 \
     restore latest -d app --target-db app_restore
   ```

   Without `--target-db` the backup goes into the database it came from. The
   command asks for confirmation; add `--yes` in scripts.

   For **age** backups, mount the private key and point `AGE_IDENTITY_FILE` at it:

   ```bash
   docker run --rm -it --env-file backup.env -v "$PWD/age-key.txt:/keys/age.txt:ro" \
     -e AGE_IDENTITY_FILE=/keys/age.txt ghcr.io/saizul/db-backup-s3 restore latest -d app
   ```

4. **Or just get the SQL.** `--output` decrypts and decompresses to a file (or
   `-` for stdout) and only needs the bucket settings:

   ```bash
   docker run --rm --env-file backup.env ghcr.io/saizul/db-backup-s3 \
     restore latest -d app -o - > app.sql
   ```

`DB_NAMES=all` backups are listed as `all-databases` and restored with
`restore latest -d all` (PostgreSQL connects to `postgres` first; the dump
recreates every database).

**Test your restores.** A backup you have never restored is a hope, not a
backup. A weekly job that runs `restore latest --target-db restore_test --yes`
and a sanity query is cheap insurance.

## Running without Docker

```bash
pipx install git+https://github.com/saizul/db-backup-s3
export DB_ENGINE=postgres DB_HOST=localhost DB_NAMES=app S3_BUCKET=my-db-backups
db-backup-s3 check-config
db-backup-s3 backup
```

You need the client tools on your `PATH`: `pg_dump`/`pg_dumpall`/`psql` for
PostgreSQL, `mariadb-dump`/`mariadb` (or `mysqldump`/`mysql`) for MySQL and
MariaDB, and `age` for age encryption. Run it from cron or a systemd timer
with `backup`, or let it schedule itself with `SCHEDULE` and `run`.

## FAQ

**Which database versions are supported?**
The image ships `pg_dump` 17, which can dump PostgreSQL 9.2 to 17, and the
MariaDB 11.4 client, which works with MariaDB 10.x/11.x and MySQL 5.7/8.x.

**MySQL 8 says `TLS/SSL error: self-signed certificate`.**
Recent MariaDB clients verify the server certificate. Either give the server a
proper certificate, or (on a trusted private network) set
`DB_DUMP_EXTRA_ARGS=--skip-ssl-verify-server-cert` and the same for `DB_RESTORE_EXTRA_ARGS`.

**What permissions does the database user need?**
PostgreSQL: `CONNECT` on the database and `SELECT` on everything to dump
(`GRANT pg_read_all_data TO backup;` on PostgreSQL 14+). MySQL/MariaDB:
`SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, PROCESS` (plus `RELOAD` for `all`).

**How much disk space is needed?**
One compressed (and encrypted) backup in `WORK_DIR`; the uncompressed dump is
never written to disk. Mount a volume on `/tmp` for very large databases.

**Is a dump consistent while the application is writing?**
Yes for InnoDB (`--single-transaction`) and for PostgreSQL (`pg_dump` uses a
snapshot). MyISAM tables are not transactional; add
`DB_DUMP_EXTRA_ARGS=--lock-tables` if you still use them.

**Can I keep backups immutable?**
Enable S3 Object Lock or bucket versioning and give db-backup-s3 a key without
`DeleteObject`, then let the bucket lifecycle expire old objects instead of
using `RETENTION_*`.

**Does it support MongoDB / Redis / SQLite?**
Not yet; see the roadmap. The engine code is small (`dump.py`), so new engines
are welcome contributions.

**Why not just a shell script?**
You can. This is the shell script after a few years of production lessons:
validated configuration, no partial uploads, no secrets in `ps` output or logs,
retention that cannot delete the wrong files, alerting, tested restores and a
documented encryption format.

## Roadmap

Ideas marked *good first issue* are small and well-scoped; see
[CONTRIBUTING.md](CONTRIBUTING.md#good-first-issues) for pointers.

- [ ] `list --since 7d` and `--limit` (*good first issue*)
- [ ] `DB_EXCLUDE` for `DB_NAMES=all` (*good first issue*)
- [ ] `RETENTION_KEEP_YEARLY` (*good first issue*)
- [ ] `restore --dry-run` to verify that a backup decrypts and decompresses (*good first issue*)
- [ ] Generic webhook / Slack / Discord / ntfy notifications (*good first issue*)
- [ ] More provider examples: Wasabi, DigitalOcean Spaces, Scaleway, Hetzner (*good first issue*)
- [ ] zstd compression
- [ ] Prometheus metrics endpoint (last success time, size, duration)
- [ ] PostgreSQL custom format (`pg_dump -Fc`) with parallel `pg_restore`
- [ ] MongoDB (`mongodump`) and Redis (`BGSAVE` + RDB) engines
- [ ] Streaming upload without a temporary file
- [ ] Automatic restore test job (`verify` command against a scratch database)

## Contributing

Contributions of all sizes are welcome: bug reports, provider guides, new
options, docs. Start with [CONTRIBUTING.md](CONTRIBUTING.md), which explains the
project layout, how to test without a database or cloud account, and lists
beginner-friendly issues. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md)
and report security problems privately as described in [SECURITY.md](SECURITY.md).

```bash
pip install -e .[dev]
pytest
ruff check . && ruff format --check .
```

## License

[MIT](LICENSE) © Saizul Amin
