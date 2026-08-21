from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile


class BlobError(ValueError):
    pass


class BlobStore:
    """Content-addressed immutable bytes, addressed by lowercase SHA-256."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> tuple[str, Path]:
        digest = hashlib.sha256(content).hexdigest()
        target = self.path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self.verify(digest, len(content))
            return digest, target
        fd, raw = tempfile.mkstemp(prefix=".blob-", dir=target.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return digest, target

    def path(self, digest: str) -> Path:
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise BlobError("blob digest must be 64 lowercase hexadecimal characters")
        return self.root / digest[:2] / digest[2:]

    def verify(self, digest: str, expected_bytes: int | None = None) -> Path:
        path = self.path(digest)
        if not path.is_file():
            raise BlobError(f"blob is missing: {digest}")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise BlobError(f"blob checksum mismatch: {digest}")
        if expected_bytes is not None and len(content) != expected_bytes:
            raise BlobError(f"blob size mismatch: {digest}")
        return path
