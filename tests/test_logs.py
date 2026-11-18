from __future__ import annotations

import json
import logging

from db_backup_s3 import logs


def test_redact_longest_first():
    logs.register_secrets("abcd", "abcdefgh", None, "")
    assert logs.redact("x abcdefgh y abcd") == "x *** y ***"


def test_short_values_are_not_registered():
    logs.register_secrets("ab")
    assert logs.redact("ab") == "ab"


def test_filter_redacts_args_and_tracebacks(capsys):
    logs.register_secrets("s3cr3t-value")
    logger = logs.setup_logging("INFO")
    logger.info("password is %s", "s3cr3t-value")
    try:
        raise RuntimeError("boom s3cr3t-value")
    except RuntimeError:
        logger.exception("failed")
    err = capsys.readouterr().err
    assert "s3cr3t-value" not in err
    assert "password is ***" in err
    assert "RuntimeError: boom ***" in err


def test_json_format(capsys):
    logger = logs.setup_logging("INFO", "json")
    logger.warning("hello %s", "world")
    line = capsys.readouterr().err.strip()
    payload = json.loads(line)
    assert payload["level"] == "warning"
    assert payload["message"] == "hello world"


def test_boto_loggers_quiet():
    logs.setup_logging("DEBUG")
    assert logging.getLogger("botocore").level == logging.WARNING
