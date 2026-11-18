# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## What counts as a vulnerability

- A password, access key, passphrase or healthcheck URL appearing in logs,
  error messages, `check-config` output or process arguments
- Encrypted backups that can be read or modified without the key or passphrase
  (for example a weakness in the AES stream format)
- Retention deleting objects that were not created by db-backup-s3
- Command or SQL injection through environment variables or object names
- Temporary files or client configuration readable by other users

## Reporting

Please **do not** open a public issue. Use
[GitHub private vulnerability reporting](https://github.com/saizul/db-backup-s3/security/advisories/new)
with the version, the impact and steps to reproduce.

You will get an answer within 5 working days. Fixes are released as soon as
possible and credited in the changelog unless you prefer otherwise.

## Hardening tips

- Prefer `ENCRYPTION=age`: the backup host only needs the public key, so a
  compromised server cannot read old backups.
- Give the S3 credentials only `s3:PutObject`, `s3:GetObject`, `s3:ListBucket`
  and `s3:DeleteObject` on the backup bucket (drop `DeleteObject` if you do not
  use retention, and use bucket object lock or versioning for immutability).
- Use a read-only database user for backups (see the README FAQ).
- Pass secrets with the `*_FILE` variables and Docker or Kubernetes secrets.
