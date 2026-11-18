# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - Unreleased

### Added

- `db-backup-s3` CLI with the commands `run`, `backup`, `list`, `restore`,
  `prune` and `check-config`.
- MySQL and MariaDB backups with `mariadb-dump`/`mysqldump`
  (`--single-transaction`, routines, triggers, events) and PostgreSQL backups
  with `pg_dump` or `pg_dumpall` (`DB_NAMES=all`).
- Streaming gzip compression (`COMPRESSION_LEVEL`) with no full copy in memory.
- Optional encryption: `age` public keys (`ENCRYPTION=age`) or a passphrase with
  AES-256-GCM in authenticated 64 KiB chunks and scrypt (`ENCRYPTION=aes`).
- Upload to AWS S3, Cloudflare R2, Backblaze B2, MinIO and other S3-compatible
  storage (`S3_ENDPOINT_URL`, `S3_FORCE_PATH_STYLE`, `S3_STORAGE_CLASS`).
- Built-in cron scheduler (5 fields, names, steps and `@daily`-style aliases)
  with time zones (`SCHEDULE_TZ`), `RUN_ON_STARTUP` and graceful shutdown.
- Grandfather-father-son retention (`RETENTION_KEEP_LAST/DAILY/WEEKLY/MONTHLY`)
  that only touches objects created by db-backup-s3, with `prune --dry-run`.
- Healthcheck pings for healthchecks.io and Uptime Kuma push monitors, with
  start, success and failure signals and a summary of the run.
- `restore` of the latest backup or a specific one, into the original or
  another database, or as plain SQL to a file or stdout.
- Configuration validation that reports every problem at once, `*_FILE`
  variants for all secrets, and secret redaction in all logs and errors.
- Multi-arch container image (`linux/amd64`, `linux/arm64`) on
  `ghcr.io/saizul/db-backup-s3` with PostgreSQL 17 and MariaDB clients and age.

[Unreleased]: https://github.com/saizul/db-backup-s3/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saizul/db-backup-s3/releases/tag/v0.1.0
