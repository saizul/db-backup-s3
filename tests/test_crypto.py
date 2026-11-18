from __future__ import annotations

import gzip
import io
import os

import pytest

from conftest import FAKE_PASSPHRASE
from db_backup_s3 import crypto
from db_backup_s3.crypto import CHUNK_SIZE, AesEncryptSink, CryptoError, FileSink, GzipSink, aes_decrypt, gunzip


def encrypt(data: bytes, passphrase: str = FAKE_PASSPHRASE, pieces: int = 7) -> bytes:
    out = io.BytesIO()
    sink = AesEncryptSink(FileSink(out), passphrase)
    step = max(1, len(data) // pieces)
    for i in range(0, len(data), step):
        sink.write(data[i : i + step])
    sink.close()
    return out.getvalue()


def decrypt(blob: bytes, passphrase: str = FAKE_PASSPHRASE) -> bytes:
    return b"".join(aes_decrypt(io.BytesIO(blob), passphrase))


@pytest.mark.parametrize("size", [0, 1, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 3 * CHUNK_SIZE + 17])
def test_aes_roundtrip(size):
    data = os.urandom(size)
    blob = encrypt(data)
    assert blob.startswith(crypto.AES_MAGIC)
    assert decrypt(blob) == data


def test_aes_is_randomised():
    assert encrypt(b"same input") != encrypt(b"same input")


def test_aes_wrong_passphrase():
    with pytest.raises(CryptoError, match="wrong passphrase"):
        decrypt(encrypt(b"secret data"), "another passphrase!")


@pytest.mark.parametrize(
    "tamper",
    [
        lambda b: b[:-1],  # cut inside the last chunk
        lambda b: b[: crypto.HEADER_SIZE + CHUNK_SIZE + crypto.TAG_SIZE],  # cut at a chunk boundary
        lambda b: b + b"x",  # appended data
        lambda b: b[:40] + bytes([b[40] ^ 1]) + b[41:],  # flipped bit
    ],
)
def test_aes_detects_tampering(tamper):
    blob = encrypt(os.urandom(2 * CHUNK_SIZE + 100))
    with pytest.raises(CryptoError):
        decrypt(tamper(blob))


def test_aes_bad_header():
    with pytest.raises(CryptoError, match="bad header"):
        decrypt(b"age-encryption.org/v1\n...")


def test_gzip_sink_and_gunzip():
    out = io.BytesIO()
    sink = GzipSink(FileSink(out), level=9)
    for _ in range(100):
        sink.write(b"INSERT INTO t VALUES (1);\n")
    sink.close()
    assert sink.bytes_in == 2600
    assert gzip.decompress(out.getvalue()) == b"INSERT INTO t VALUES (1);\n" * 100
    raw = out.getvalue()
    assert b"".join(gunzip([raw[:10], raw[10:]])) == b"INSERT INTO t VALUES (1);\n" * 100


def test_gunzip_errors():
    raw = gzip.compress(b"x" * 1000)
    with pytest.raises(CryptoError, match="truncated"):
        b"".join(gunzip([raw[:-12]]))
    with pytest.raises(CryptoError, match="not valid gzip"):
        b"".join(gunzip([b"definitely not gzip"]))


def test_age_missing_binary(monkeypatch):
    monkeypatch.setattr(crypto.shutil, "which", lambda name: None)
    with pytest.raises(CryptoError, match="not installed"):
        crypto.age_binary()
