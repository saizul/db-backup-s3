## What does this pull request do?

<!-- One or two sentences. Link the issue: "Closes #123". -->

## Type

- [ ] Bug fix
- [ ] New feature or option
- [ ] New storage provider / database example
- [ ] Documentation

## Checklist

- [ ] Tests added or updated (`pytest` passes; no network, no real database: use the fake commands and moto)
- [ ] `ruff check . && ruff format --check .` passes
- [ ] New environment variables are validated in `config.py` and documented in the README table
- [ ] Secrets are never logged, printed or put on a command line
- [ ] Existing backups stay restorable (object names and encryption formats are unchanged or versioned)
- [ ] CHANGELOG.md updated under "Unreleased"
