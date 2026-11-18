# AES backup file format

Backups made with `ENCRYPTION=aes` end in `.sql.gz.enc`. The format is small and
documented so that you can always decrypt your data, even without this tool.

```text
offset  size  content
0       8     magic "DBS3AES" followed by the version byte 0x01
8       16    salt for scrypt
24      7     random nonce prefix
31      ...   chunks
```

- **Key**: `scrypt(passphrase (UTF-8), salt, n=2**15, r=8, p=1, length=32)`.
- **Chunks**: the gzip stream is split into 64 KiB pieces (the last one may be
  shorter, or empty). Each piece is encrypted with AES-256-GCM and stored as
  `ciphertext || 16-byte tag`, so every chunk except the last is exactly
  65 552 bytes.
- **Nonce** (12 bytes): `prefix (7) || chunk counter (4, big endian, from 0) || last flag (1)`,
  where the last flag is `0x01` for the final chunk and `0x00` otherwise.
- **Associated data**: the 31-byte header, for every chunk.

Because the counter and the last flag are part of the nonce, reordered,
dropped, truncated or appended chunks fail authentication.

## Decrypting with a few lines of Python

```python
import sys, zlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

data = open(sys.argv[1], "rb").read()
passphrase = sys.argv[2].encode()
header, body = data[:31], data[31:]
assert header[:8] == b"DBS3AES\x01"
key = Scrypt(salt=header[8:24], length=32, n=2**15, r=8, p=1).derive(passphrase)
aead, block = AESGCM(key), 65536 + 16
chunks = [body[i:i + block] for i in range(0, len(body), block)] or [b""]
gz = b"".join(
    aead.decrypt(header[24:31] + n.to_bytes(4, "big") + bytes([n == len(chunks) - 1]), c, header)
    for n, c in enumerate(chunks)
)
sys.stdout.buffer.write(zlib.decompress(gz, 31))
```

(This reads the whole file into memory; `db-backup-s3 restore --output` streams.)
