"""Small cross-platform advisory lock used by crash-safe artifact writers."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - Unix only
    msvcrt = None


@contextmanager
def exclusive_file_lock(file_descriptor: int) -> Iterator[None]:
    """Lock one open regular file until the context exits."""
    if fcntl is not None:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        return

    if msvcrt is None:  # Defensive: all supported runtimes provide one backend.
        raise OSError("No supported file-lock backend is available")
    if os.fstat(file_descriptor).st_size == 0:
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        os.write(file_descriptor, b"\0")
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    msvcrt.locking(file_descriptor, msvcrt.LK_LOCK, 1)
    try:
        yield
    finally:
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        msvcrt.locking(file_descriptor, msvcrt.LK_UNLCK, 1)
