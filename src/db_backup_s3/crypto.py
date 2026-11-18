"""Streaming compression and encryption.

Backups are written through a chain of *sinks*: the dump output goes into a
:class:`GzipSink`, whose compressed bytes go into an encryptor (optional) and
finally into a file. Nothing is held in memory beyond one chunk.

Encryption formats
------------------

``age``  Uses the `age <https://age-encryption.org>`_ binary with one or more
         public keys. Restoring needs the matching identity file. Recommended:
         the backup host never has the key that can decrypt.

``aes``  AES-256-GCM in 64 KiB chunks (the STREAM construction), with the key
         derived from a passphrase by scrypt. File layout::

             magic "DBS3AES" 0x01 | salt (16) | nonce prefix (7) | chunks...

         Each chunk is ``ciphertext || tag`` of up to 64 KiB of plaintext. The
         nonce is ``prefix || counter (4, big endian) || last-flag (1)`` and the
         header is authenticated as associated data, so reordering, truncating or
         appending chunks is detected.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import zlib
from collections.abc import Iterable, Iterator
from typing import BinaryIO, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

AES_MAGIC = b"DBS3AES\x01"
SALT_SIZE = 16
PREFIX_SIZE = 7
CHUNK_SIZE = 64 * 1024
TAG_SIZE = 16
HEADER_SIZE = len(AES_MAGIC) + SALT_SIZE + PREFIX_SIZE
SCRYPT_N = 2**15
READ_SIZE = 256 * 1024

EXTENSIONS = {"none": "", "age": ".age", "aes": ".enc"}


class CryptoError(Exception):
    """Encryption or decryption failed (wrong key, corrupted or truncated file, missing tool)."""


class Sink(Protocol):
    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class FileSink:
    def __init__(self, fileobj: BinaryIO) -> None:
        self._f = fileobj

    def write(self, data: bytes) -> None:
        self._f.write(data)

    def close(self) -> None:
        self._f.flush()


class GzipSink:
    """Gzip-compresses everything written to it into ``inner``."""

    def __init__(self, inner: Sink, level: int = 6) -> None:
        self._inner = inner
        self._z = zlib.compressobj(level, zlib.DEFLATED, 31)
        self.bytes_in = 0

    def write(self, data: bytes) -> None:
        self.bytes_in += len(data)
        out = self._z.compress(data)
        if out:
            self._inner.write(out)

    def close(self) -> None:
        self._inner.write(self._z.flush())
        self._inner.close()


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=8, p=1).derive(passphrase.encode("utf-8"))


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    if counter >= 2**32:
        raise CryptoError("file too large for one AES stream")
    return prefix + counter.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


class AesEncryptSink:
    """Encrypts with AES-256-GCM (chunked) into ``inner``."""

    def __init__(self, inner: Sink, passphrase: str) -> None:
        self._inner = inner
        salt = os.urandom(SALT_SIZE)
        self._prefix = os.urandom(PREFIX_SIZE)
        self._header = AES_MAGIC + salt + self._prefix
        self._aead = AESGCM(_derive_key(passphrase, salt))
        self._buffer = bytearray()
        self._counter = 0
        inner.write(self._header)

    def _emit(self, chunk: bytes, last: bool) -> None:
        nonce = _nonce(self._prefix, self._counter, last)
        self._inner.write(self._aead.encrypt(nonce, chunk, self._header))
        self._counter += 1

    def write(self, data: bytes) -> None:
        self._buffer += data
        # Keep at least one byte back so the final chunk is never a full chunk followed by nothing.
        while len(self._buffer) > CHUNK_SIZE:
            self._emit(bytes(self._buffer[:CHUNK_SIZE]), last=False)
            del self._buffer[:CHUNK_SIZE]

    def close(self) -> None:
        self._emit(bytes(self._buffer), last=True)
        self._buffer.clear()
        self._inner.close()


def aes_decrypt(stream: BinaryIO, passphrase: str) -> Iterator[bytes]:
    """Yield plaintext chunks from an AES stream written by :class:`AesEncryptSink`."""
    header = stream.read(HEADER_SIZE)
    if len(header) < HEADER_SIZE or not header.startswith(AES_MAGIC):
        raise CryptoError("not an AES-encrypted backup (bad header); was it encrypted with age?")
    salt = header[len(AES_MAGIC) : len(AES_MAGIC) + SALT_SIZE]
    prefix = header[len(AES_MAGIC) + SALT_SIZE :]
    aead = AESGCM(_derive_key(passphrase, salt))
    block = CHUNK_SIZE + TAG_SIZE
    counter = 0
    current = stream.read(block)
    while True:
        following = stream.read(block)
        last = not following
        try:
            yield aead.decrypt(_nonce(prefix, counter, last), current, header)
        except InvalidTag:
            raise CryptoError("decryption failed: wrong passphrase, or the file is corrupted or truncated") from None
        if last:
            return
        counter += 1
        current = following


def age_binary() -> str:
    path = shutil.which("age")
    if not path:
        raise CryptoError("the 'age' binary is not installed (it is included in the Docker image)")
    return path


class AgeEncryptSink:
    """Pipes data through ``age -r ...`` into a file."""

    def __init__(self, out_path: str, recipients: Iterable[str]) -> None:
        args = [age_binary()]
        for recipient in recipients:
            args += ["-r", recipient]
        args += ["-o", out_path]
        self._proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, data: bytes) -> None:
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(data)
        except BrokenPipeError:
            self._fail()

    def _fail(self) -> None:
        stderr = self._proc.stderr.read().decode(errors="replace").strip() if self._proc.stderr else ""
        self._proc.wait()
        raise CryptoError(f"age failed: {stderr or 'exit code ' + str(self._proc.returncode)}")

    def abort(self) -> None:
        self._proc.kill()
        self._proc.wait()

    def close(self) -> None:
        assert self._proc.stdin is not None
        with contextlib.suppress(BrokenPipeError):
            self._proc.stdin.close()
        if self._proc.wait() != 0:
            self._fail()


def age_decrypt(path: str, identity_file: str) -> Iterator[bytes]:
    """Yield plaintext chunks from an age-encrypted file."""
    proc = subprocess.Popen(
        [age_binary(), "-d", "-i", identity_file, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert proc.stdout is not None
    while chunk := proc.stdout.read(READ_SIZE):
        yield chunk
    stderr = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
    if proc.wait() != 0:
        raise CryptoError(f"age decryption failed: {stderr or 'exit code ' + str(proc.returncode)}")


def gunzip(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Decompress a gzip stream given as chunks."""
    z = zlib.decompressobj(31)
    try:
        for chunk in chunks:
            out = z.decompress(chunk)
            if out:
                yield out
        tail = z.flush()
    except zlib.error as exc:
        raise CryptoError(f"backup is not valid gzip data ({exc})") from None
    if tail:
        yield tail
    if not z.eof:
        raise CryptoError("backup is truncated (gzip stream ended early)")


def read_chunks(stream: BinaryIO, size: int = READ_SIZE) -> Iterator[bytes]:
    while chunk := stream.read(size):
        yield chunk
