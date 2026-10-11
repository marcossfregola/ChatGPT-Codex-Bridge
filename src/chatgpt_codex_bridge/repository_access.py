"""Explicit Bridge writer identity and per-repository OS locking."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .single_instance import (
    MCPInstanceAlreadyRunningError,
    MCPInstanceLock,
    MCPInstanceLockError,
)


INSTANCE_ID_ENVIRONMENT_VARIABLE = "CHATGPT_CODEX_BRIDGE_INSTANCE_ID"
UNCONFIGURED_INSTANCE_ID = "UNCONFIGURED"
WRITER_INSTANCE_CONFIG_KEY = "chatgptcodexbridge.writerInstanceId"
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RepositoryAccessError(RuntimeError):
    """Base class for fail-closed repository writer checks."""


class RepositoryOwnershipError(RepositoryAccessError):
    """The Bridge identity is absent, ambiguous, or not the local owner."""


class RepositoryLockError(RepositoryAccessError):
    """The shared per-repository OS lock could not be acquired."""


class RepositoryBusyError(RepositoryLockError):
    """Another Bridge operation currently owns this repository's lock."""


def validate_instance_id(value: object) -> str | None:
    """Return a valid explicit process identity, or ``None`` fail-closed."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _INSTANCE_ID_RE.fullmatch(candidate):
        return None
    return candidate


def process_instance_id() -> str | None:
    """Resolve identity exclusively from this Bridge process environment."""

    return validate_instance_id(os.environ.get(INSTANCE_ID_ENVIRONMENT_VARIABLE))


def _git_output(
    repo_path: str | os.PathLike[str], *args: str
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=False,
        capture_output=True,
    )


def repository_lock_identity(repo_path: str | os.PathLike[str]) -> Path:
    """Canonicalize a worktree to its shared Git common directory when possible."""

    supplied = Path(repo_path).expanduser().resolve(strict=False)
    try:
        top = _git_output(supplied, "rev-parse", "--show-toplevel")
    except OSError:
        return supplied
    if top.returncode != 0:
        return supplied
    try:
        root = Path(top.stdout.decode("utf-8", errors="strict").strip()).resolve(
            strict=True
        )
        common = _git_output(root, "rev-parse", "--git-common-dir")
        if common.returncode != 0:
            return supplied
        common_text = common.stdout.decode("utf-8", errors="strict").strip()
        common_path = Path(common_text)
        if not common_path.is_absolute():
            common_path = root / common_path
        return common_path.resolve(strict=True)
    except (OSError, UnicodeError, ValueError):
        return supplied


def _default_lock_directory() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RepositoryLockError("per-user Bridge lock directory is unavailable")
        base = Path(local_app_data)
    else:
        base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    return base / "ChatGPTCodexBridge" / "repository-locks"


def _lock_path_for_identity(
    identity: Path,
    *,
    lock_directory: str | os.PathLike[str] | None = None,
) -> Path:
    normalized = os.path.normcase(str(identity))
    digest = hashlib.sha256(os.fsencode(normalized)).hexdigest()
    directory = (
        Path(lock_directory)
        if lock_directory is not None
        else _default_lock_directory()
    )
    return directory.expanduser().resolve(strict=False) / f"{digest}.repository.lock"


def repository_lock_path(
    repo_path: str | os.PathLike[str],
    *,
    lock_directory: str | os.PathLike[str] | None = None,
) -> Path:
    """Return a stable lock anchor shared by all projects for one physical repo."""

    return _lock_path_for_identity(
        repository_lock_identity(repo_path),
        lock_directory=lock_directory,
    )


def require_repository_writer(
    repo_path: str | os.PathLike[str], instance_id: str | None
) -> None:
    """Require one explicit process identity to match exactly one local owner."""

    identity = validate_instance_id(instance_id)
    if identity is None:
        raise RepositoryOwnershipError("Bridge instance identity is not configured")
    result = _git_output(
        repo_path,
        "config",
        "--local",
        "--no-includes",
        "--null",
        "--get-all",
        WRITER_INSTANCE_CONFIG_KEY,
    )
    if result.returncode == 1:
        raise RepositoryOwnershipError("repository writer owner is not configured")
    if result.returncode != 0:
        raise RepositoryOwnershipError("repository writer owner cannot be verified")
    try:
        values = result.stdout.split(b"\0")
        if values and values[-1] == b"":
            values.pop()
        decoded = [value.decode("utf-8", errors="strict") for value in values]
    except UnicodeError as error:
        raise RepositoryOwnershipError("repository writer owner is invalid") from error
    if len(decoded) != 1:
        raise RepositoryOwnershipError("repository writer owner is ambiguous")
    owner = validate_instance_id(decoded[0])
    if owner is None:
        raise RepositoryOwnershipError("repository writer owner is invalid")
    if owner != identity:
        raise RepositoryOwnershipError("Bridge instance is not the repository writer")


class RepositoryLock(MCPInstanceLock):
    """A non-blocking OS lock shared across Bridge databases for one repo.

    The hash-keyed anchor lives under a common per-user local directory rather
    than in the Git index or working tree. The OS byte-range lock is the owner;
    the stable anchor is intentionally never unlinked, so a crashed process
    cannot leave stale logical ownership or split the lock among new inodes.
    """

    def __init__(
        self,
        repo_path: str | os.PathLike[str],
        *,
        lock_directory: str | os.PathLike[str] | None = None,
    ) -> None:
        identity = repository_lock_identity(repo_path)
        super().__init__(identity)
        self.lock_path = _lock_path_for_identity(
            identity,
            lock_directory=lock_directory,
        )

    def acquire(self) -> "RepositoryLock":
        try:
            super().acquire()
        except MCPInstanceAlreadyRunningError as error:
            raise RepositoryBusyError(
                "repository is locked by another Bridge operation"
            ) from error
        except MCPInstanceLockError as error:
            raise RepositoryLockError("repository lock is unavailable") from error
        return self


__all__ = [
    "INSTANCE_ID_ENVIRONMENT_VARIABLE",
    "RepositoryAccessError",
    "RepositoryBusyError",
    "RepositoryLock",
    "RepositoryLockError",
    "RepositoryOwnershipError",
    "UNCONFIGURED_INSTANCE_ID",
    "WRITER_INSTANCE_CONFIG_KEY",
    "process_instance_id",
    "repository_lock_identity",
    "repository_lock_path",
    "require_repository_writer",
    "validate_instance_id",
]
