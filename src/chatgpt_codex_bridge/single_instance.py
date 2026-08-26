"""Per-database interprocess locking for the MCP server runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no cover - exercised only on non-Windows hosts
    import fcntl


class MCPInstanceLockError(RuntimeError):
    """Raised when the per-database lock cannot be opened."""


class MCPInstanceAlreadyRunningError(MCPInstanceLockError):
    """Raised when another process owns the per-database lock."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(
            f"Bridge MCP instance already active for database: {db_path}"
        )
        self.db_path = db_path


def canonical_db_path(db_path: str | os.PathLike[str]) -> Path:
    """Resolve one database path without requiring the database to exist."""

    return Path(db_path).expanduser().resolve(strict=False)


def lock_path_for_db(db_path: str | os.PathLike[str]) -> Path:
    """Return the physical lock-file path for one canonical database path."""

    canonical = canonical_db_path(db_path)
    return Path(f"{canonical}.mcp.lock")


class MCPInstanceLock:
    """Hold an OS-backed, non-blocking lock for one Bridge database.

    The lock file is only a stable filesystem anchor. Ownership is the byte
    range lock held by ``self._handle``; therefore a process crash releases
    the lock automatically and does not leave stale logical ownership.
    """

    _LOCK_BYTES = 1

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = canonical_db_path(db_path)
        self.lock_path = lock_path_for_db(self.db_path)
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        """Whether this instance currently owns the OS lock."""

        return self._handle is not None

    def acquire(self) -> "MCPInstanceLock":
        """Acquire the lock without waiting; raise on active ownership."""

        if self._handle is not None:
            return self

        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.lock_path.open("a+b")
        except OSError as exc:
            raise MCPInstanceLockError(
                f"unable to open Bridge MCP lock for database: {self.db_path}"
            ) from exc

        try:
            # msvcrt.locking requires a byte to exist at the requested offset.
            # The physical file may remain after exit; only this handle owns
            # the lock, so stale files are harmless.
            handle.seek(0, os.SEEK_END)
            if handle.tell() < self._LOCK_BYTES:
                handle.write(b"\0" * (self._LOCK_BYTES - handle.tell()))
                handle.flush()
            handle.seek(0)
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, self._LOCK_BYTES)
            else:  # pragma: no cover - exercised only on non-Windows hosts
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise MCPInstanceAlreadyRunningError(self.db_path) from exc

        self._handle = handle
        return self

    def release(self) -> None:
        """Release the OS lock and close the owning handle, idempotently."""

        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, self._LOCK_BYTES)
            else:  # pragma: no cover - exercised only on non-Windows hosts
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            # Closing the handle still releases ownership on process exit; a
            # best-effort explicit unlock must not mask server shutdown.
            pass
        finally:
            handle.close()

    def __enter__(self) -> "MCPInstanceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


__all__ = [
    "MCPInstanceAlreadyRunningError",
    "MCPInstanceLock",
    "MCPInstanceLockError",
    "canonical_db_path",
    "lock_path_for_db",
]
