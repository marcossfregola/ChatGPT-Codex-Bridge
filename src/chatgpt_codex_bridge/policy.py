"""Task execution policy and bounded Git evidence for autonomous writes.

This module deliberately contains policy checks at the Bridge boundary.  It
does not inspect or parse objectives and it does not try to enforce a command
allow-list inside Codex's autonomous sandbox.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Iterable

from .domain.models import TaskMode


_MAX_EVIDENCE_TEXT = 16_384
_MAX_EVIDENCE_PATHS = 256
TRUNCATION_SENTINEL = "[TRUNCATED]"
GIT_COMMAND_TIMEOUT_SECONDS = 15.0


class PolicyError(RuntimeError):
    """Base class for a task policy/preflight/postflight failure."""


class ProtectedRootError(PolicyError):
    """Raised when an autonomous task overlaps a protected Bridge root."""


class GitPreflightError(PolicyError):
    """Raised when an autonomous task cannot establish a clean Git baseline."""


class DirtyWorkingTreeError(GitPreflightError):
    """Raised when the requested Git worktree is not clean."""

    def __init__(
        self,
        message: str,
        *,
        status_porcelain: str = "",
        staged_paths: tuple[str, ...] = (),
        unstaged_paths: tuple[str, ...] = (),
        untracked_paths: tuple[str, ...] = (),
    ) -> None:
        self.status_porcelain = status_porcelain
        self.staged_paths = staged_paths
        self.unstaged_paths = unstaged_paths
        self.untracked_paths = untracked_paths
        super().__init__(message)


class ContinuationBaselineError(GitPreflightError):
    """Raised when a dirty worktree is not the last durable baseline."""


class GitPostflightError(PolicyError):
    """Raised when postflight evidence cannot be collected safely."""


class PolicyViolationError(PolicyError):
    """Raised when branch or HEAD changed during an autonomous task."""

    def __init__(self, postflight: "GitPostflight") -> None:
        self.postflight = postflight
        changed = []
        if postflight.final_branch != postflight.baseline_branch:
            changed.append("branch")
        if postflight.final_head != postflight.baseline_head:
            changed.append("HEAD")
        detail = ", ".join(changed) or "Git baseline"
        super().__init__(f"autonomous-write policy violation: {detail} changed")


class CheckpointCommitError(PolicyError):
    """Raised when a verified local checkpoint cannot be created."""


class CheckpointAlreadyCommittedError(CheckpointCommitError):
    """Raised when one task tries to create more than one checkpoint."""


@dataclass(frozen=True)
class GitCheckpoint:
    """Durable evidence captured immediately before Codex starts."""

    repo_path: str
    baseline_branch: str
    baseline_head: str
    status_porcelain: str
    staged_paths: tuple[str, ...]
    unstaged_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    baseline_kind: str = "clean"
    previous_task_id: str | None = None


@dataclass(frozen=True)
class GitPostflight:
    """Bounded evidence captured after Codex returns or is cancelled."""

    repo_path: str
    baseline_branch: str
    baseline_head: str
    final_branch: str
    final_head: str
    status_porcelain: str
    diff: str
    cached_diff: str
    changed_files: tuple[str, ...]
    untracked_files: tuple[str, ...]
    policy_violation: bool
    untracked_fingerprints: tuple[tuple[str, str], ...] = ()
    content_fingerprints: tuple[tuple[str, str, str], ...] = ()


AUTONOMOUS_WRITE_CONTRACT = """[Bridge autonomous-write contract]
Work only on the requested project.
NO commit, NO push, NO tag/release.
NO merge/rebase/reset/clean.
NO install/uninstall of software.
NO modifications to other repositories or either Bridge.
No destructive operations that were not requested.
""".strip()


def augment_objective(objective: str, mode: TaskMode | str) -> str:
    """Append the contractual autonomous-write instructions when applicable."""

    selected = _coerce_mode(mode)
    if selected is not TaskMode.AUTONOMOUS_WRITE:
        return objective
    return f"{objective.rstrip()}\n\n{AUTONOMOUS_WRITE_CONTRACT}"


def _coerce_mode(mode: TaskMode | str) -> TaskMode:
    if isinstance(mode, TaskMode):
        return mode
    try:
        return TaskMode(mode)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid mode: {mode!r}") from exc


def _bounded_text(value: str, limit: int = _MAX_EVIDENCE_TEXT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - len(TRUNCATION_SENTINEL)] + TRUNCATION_SENTINEL


def _bounded_paths(paths: Iterable[str]) -> tuple[str, ...]:
    values = tuple(paths)
    if len(values) <= _MAX_EVIDENCE_PATHS:
        return values
    return values[:_MAX_EVIDENCE_PATHS] + (TRUNCATION_SENTINEL,)


def _bounded_fingerprints(
    fingerprints: Iterable[tuple[str, str, str]],
) -> tuple[tuple[str, str, str], ...]:
    values = tuple(fingerprints)
    if len(values) <= _MAX_EVIDENCE_PATHS:
        return values
    return values[:_MAX_EVIDENCE_PATHS] + ((TRUNCATION_SENTINEL, "", ""),)


def _evidence_is_complete(value: Any) -> bool:
    """Return whether bounded evidence contains no truncation sentinel."""

    if isinstance(value, str):
        return not value.endswith(TRUNCATION_SENTINEL)
    if isinstance(value, (list, tuple)):
        return not any(
            item == TRUNCATION_SENTINEL
            or (
                isinstance(item, (list, tuple))
                and item
                and item[0] == TRUNCATION_SENTINEL
            )
            for item in value
        )
    return True


def _validate_fingerprint_path(path: Any, field_name: str) -> str:
    """Validate one relative Git evidence path without touching the filesystem."""

    if not isinstance(path, str) or not path or "\x00" in path:
        raise ContinuationBaselineError(f"{field_name} contains an invalid path")
    if any(ord(character) < 32 for character in path):
        raise ContinuationBaselineError(f"{field_name} contains an invalid path")
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("./"):
        raise ContinuationBaselineError(f"{field_name} contains an unsafe path")
    if len(normalized) >= 2 and normalized[1] == ":":
        raise ContinuationBaselineError(f"{field_name} contains an absolute path")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ContinuationBaselineError(f"{field_name} contains an unsafe path")
    return path


def validate_continuation_snapshot(
    payload: Mapping[str, Any],
    *,
    expected_repo: str | os.PathLike[str] | None = None,
    expected_branch: str | None = None,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize a complete postflight-shaped Git snapshot.

    Reconciled continuation evidence is durable authority.  It therefore may
    not contain any of the bounded-evidence truncation sentinels used by the
    normal event/response paths.  The returned mapping is JSON-safe and uses
    the exact shape consumed by :func:`git_continuation_preflight`.
    """

    if not isinstance(payload, Mapping):
        raise ContinuationBaselineError("continuation snapshot is not an object")

    required_text = (
        "repo_path",
        "baseline_branch",
        "baseline_head",
        "final_branch",
        "final_head",
        "status_porcelain",
        "diff",
        "cached_diff",
    )
    values: dict[str, Any] = {}
    for key in required_text:
        value = payload.get(key)
        if not isinstance(value, str):
            raise ContinuationBaselineError(
                f"continuation snapshot {key} evidence is incomplete"
            )
        if key in {"repo_path", "baseline_branch", "baseline_head", "final_branch", "final_head"} and not value:
            raise ContinuationBaselineError(
                f"continuation snapshot {key} identity is incomplete"
            )
        if not _evidence_is_complete(value):
            raise ContinuationBaselineError(
                "continuation snapshot Git evidence was truncated"
            )
        values[key] = value

    if payload.get("policy_violation") is not False:
        raise ContinuationBaselineError("continuation snapshot has a policy violation")
    values["policy_violation"] = False

    if expected_repo is not None:
        try:
            expected_root = _resolved_path(str(expected_repo), require_exists=True)
            snapshot_root = _resolved_path(values["repo_path"], require_exists=True)
        except (GitPreflightError, ValueError) as exc:
            raise ContinuationBaselineError(
                "continuation snapshot repo path is invalid"
            ) from exc
        if _path_key(expected_root) != _path_key(snapshot_root):
            raise ContinuationBaselineError("continuation snapshot repo path differs")

    if expected_branch is not None and values["baseline_branch"] != expected_branch:
        raise ContinuationBaselineError("continuation snapshot baseline branch differs")
    if expected_head is not None and values["baseline_head"] != expected_head:
        raise ContinuationBaselineError("continuation snapshot baseline HEAD differs")
    if values["final_branch"] != values["baseline_branch"]:
        raise ContinuationBaselineError("continuation snapshot branch changed")
    if values["final_head"] != values["baseline_head"]:
        raise ContinuationBaselineError("continuation snapshot HEAD changed")

    list_fields = ("changed_files", "untracked_files")
    for key in list_fields:
        value = payload.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ContinuationBaselineError(
                f"continuation snapshot {key} evidence is incomplete"
            )
        if not _evidence_is_complete(value):
            raise ContinuationBaselineError(
                "continuation snapshot paths were truncated"
            )
        for item in value:
            _validate_fingerprint_path(item, f"continuation snapshot {key}")
        values[key] = list(value)

    for key in ("changed_files", "untracked_files"):
        if len(set(values[key])) != len(values[key]):
            raise ContinuationBaselineError(
                f"continuation snapshot {key} contains duplicates"
            )

    raw_fingerprints = payload.get("content_fingerprints")
    if not isinstance(raw_fingerprints, list):
        raise ContinuationBaselineError(
            "continuation snapshot lacks complete dirty content evidence"
        )
    if not _evidence_is_complete(raw_fingerprints):
        raise ContinuationBaselineError(
            "continuation snapshot dirty content evidence was truncated"
        )
    normalized_fingerprints: list[dict[str, str]] = []
    seen_fingerprints: set[tuple[str, str]] = set()
    for item in raw_fingerprints:
        if not isinstance(item, Mapping):
            raise ContinuationBaselineError(
                "continuation snapshot dirty content evidence is invalid"
            )
        path = item.get("path")
        state = item.get("state")
        digest = item.get("sha256")
        if path == TRUNCATION_SENTINEL:
            raise ContinuationBaselineError(
                "continuation snapshot dirty content evidence was truncated"
            )
        _validate_fingerprint_path(path, "continuation snapshot fingerprints")
        if state not in {"staged", "unstaged", "untracked"}:
            raise ContinuationBaselineError(
                "continuation snapshot dirty content evidence is incomplete"
            )
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ContinuationBaselineError(
                "continuation snapshot dirty content evidence is incomplete"
            )
        identity = (path, state)
        if identity in seen_fingerprints:
            raise ContinuationBaselineError(
                "continuation snapshot dirty content evidence contains duplicates"
            )
        seen_fingerprints.add(identity)
        normalized_fingerprints.append(
            {"path": path, "state": state, "sha256": digest}
        )
    normalized_fingerprints.sort(key=lambda item: (item["path"], item["state"]))
    values["content_fingerprints"] = normalized_fingerprints

    raw_untracked = payload.get("untracked_fingerprints")
    if not isinstance(raw_untracked, list):
        raise ContinuationBaselineError(
            "continuation snapshot lacks untracked fingerprints"
        )
    if not _evidence_is_complete(raw_untracked):
        raise ContinuationBaselineError(
            "continuation snapshot untracked fingerprints were truncated"
        )
    normalized_untracked: list[dict[str, str]] = []
    seen_untracked: set[str] = set()
    for item in raw_untracked:
        if not isinstance(item, Mapping):
            raise ContinuationBaselineError(
                "continuation snapshot untracked fingerprints are invalid"
            )
        path = item.get("path")
        digest = item.get("sha256")
        _validate_fingerprint_path(path, "continuation snapshot untracked fingerprints")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ContinuationBaselineError(
                "continuation snapshot untracked fingerprints are incomplete"
            )
        if path in seen_untracked:
            raise ContinuationBaselineError(
                "continuation snapshot untracked fingerprints contain duplicates"
            )
        seen_untracked.add(path)
        normalized_untracked.append({"path": path, "sha256": digest})
    normalized_untracked.sort(key=lambda item: item["path"])
    values["untracked_fingerprints"] = normalized_untracked

    expected_untracked = {
        item["path"]: item["sha256"]
        for item in normalized_fingerprints
        if item["state"] == "untracked"
    }
    actual_untracked = {item["path"]: item["sha256"] for item in normalized_untracked}
    if actual_untracked != expected_untracked:
        raise ContinuationBaselineError(
            "continuation snapshot untracked fingerprints differ"
        )

    # Preserve the established postflight field order/shape while dropping any
    # untrusted extension keys from the durable continuation baseline.
    return {
        "repo_path": values["repo_path"],
        "baseline_branch": values["baseline_branch"],
        "baseline_head": values["baseline_head"],
        "final_branch": values["final_branch"],
        "final_head": values["final_head"],
        "status_porcelain": values["status_porcelain"],
        "diff": values["diff"],
        "cached_diff": values["cached_diff"],
        "changed_files": values["changed_files"],
        "untracked_files": values["untracked_files"],
        "policy_violation": False,
        "untracked_fingerprints": values["untracked_fingerprints"],
        "content_fingerprints": values["content_fingerprints"],
    }


def _resolved_path(value: str | os.PathLike[str], *, require_exists: bool) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("repo_path must be path-like")
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=require_exists)
    except (OSError, RuntimeError) as exc:
        raise GitPreflightError("repo_path could not be canonicalized") from exc
    if require_exists and not resolved.is_dir():
        raise GitPreflightError("repo_path is not a directory")
    return resolved


def _path_key(path: Path) -> str:
    text = os.path.normcase(os.path.abspath(str(path)))
    if len(text) > 3:
        text = text.rstrip("\\/")
    return text


def _paths_overlap(first: Path, second: Path) -> bool:
    first_key = _path_key(first)
    second_key = _path_key(second)
    try:
        common = os.path.normcase(os.path.commonpath([first_key, second_key]))
    except ValueError:
        return False
    return common.rstrip("\\/") == first_key.rstrip("\\/") or common.rstrip(
        "\\/"
    ) == second_key.rstrip("\\/")


def protected_roots(
    local_app_data: str | os.PathLike[str] | None = None,
) -> tuple[Path, ...]:
    """Return Bridge-owned roots that autonomous tasks must not overlap."""

    roots = [Path(__file__).resolve().parents[2]]
    local_root = local_app_data
    if local_root is None:
        local_root = os.environ.get("LOCALAPPDATA")
    if local_root is None:
        local_root = Path.home() / "AppData" / "Local"
    local_path = Path(local_root).expanduser().resolve(strict=False)
    roots.extend(
        local_path / name
        for name in (
            "ChatGPTCodexBridge",
            "ChatGPTOpenCodeBridge",
            "VisorVideosDevBridge",
        )
    )
    return tuple(root.resolve(strict=False) for root in roots)


def ensure_autonomous_workspace(
    repo_path: str | os.PathLike[str],
    *,
    local_app_data: str | os.PathLike[str] | None = None,
) -> Path:
    """Canonicalize and reject a protected or overlapping autonomous root."""

    canonical = _resolved_path(repo_path, require_exists=True)
    for root in protected_roots(local_app_data):
        if _paths_overlap(canonical, root):
            raise ProtectedRootError(
                "autonomous-write repo_path overlaps a protected Bridge root"
            )
    return canonical


class _GitCommandFailure(GitPreflightError):
    def __init__(self, args: tuple[str, ...], stderr: str) -> None:
        self.args_used = args
        self.stderr = _bounded_text(stderr.strip())
        super().__init__(f"git command failed: {args[0] if args else 'git'}")


def _git_with_env(
    repo: Path,
    *args: str,
    allow_failure: bool = False,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> str:
    command = ("git", "-C", str(repo), *args)
    try:
        run_kwargs: dict[str, Any] = {
            "check": False,
            "capture_output": True,
            "input": input_bytes,
            "env": dict(env) if env is not None else None,
            "timeout": GIT_COMMAND_TIMEOUT_SECONDS,
        }
        if input_bytes is None:
            run_kwargs.update(
                {"text": True, "encoding": "utf-8", "errors": "replace"}
            )
        completed = subprocess.run(
            list(command),
            **run_kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if allow_failure:
            return ""
        raise GitPreflightError("unable to execute Git") from exc
    if completed.returncode != 0 and not allow_failure:
        stderr = completed.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise _GitCommandFailure(command, stderr or "")
    stdout = completed.stdout
    if isinstance(stdout, bytes):
        return stdout.decode("utf-8", errors="replace")
    return stdout or ""


def _git(repo: Path, *args: str, allow_failure: bool = False) -> str:
    return _git_with_env(repo, *args, allow_failure=allow_failure)


def _git_bytes_with_env(
    repo: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    """Run Git without decoding stdout, for binary-safe index reads."""

    command = ("git", "-C", str(repo), *args)
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            input=input_bytes,
            capture_output=True,
            env=dict(env) if env is not None else None,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitPreflightError("unable to execute Git") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        raise _GitCommandFailure(command, stderr)
    return completed.stdout


def _git_bytes(repo: Path, *args: str) -> bytes:
    return _git_bytes_with_env(repo, *args)


def _git_branch(
    repo: Path,
    *,
    allow_detached: bool = False,
    env: Mapping[str, str] | None = None,
) -> str:
    try:
        return _git_with_env(
            repo,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            env=env,
        ).strip()
    except _GitCommandFailure:
        if allow_detached:
            return "(detached)"
        raise GitPreflightError("autonomous-write requires a named Git branch")


def _parse_status(
    status: str, *, bounded: bool = True
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for line in status.splitlines():
        if len(line) < 3:
            continue
        code = line[:2]
        path = line[3:]
        if code == "??":
            untracked.append(path)
            continue
        if code[0] != " ":
            staged.append(path)
        if code[1] != " ":
            unstaged.append(path)
    if bounded:
        return _bounded_paths(staged), _bounded_paths(unstaged), _bounded_paths(untracked)
    return tuple(staged), tuple(unstaged), tuple(untracked)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _filesystem_digest(path: Path) -> str | None:
    """Hash one Git-reported worktree file, without walking other paths."""

    try:
        if path.is_symlink():
            return _sha256_bytes(
                os.readlink(path).encode("utf-8", "surrogateescape")
            )
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        if path.exists():
            raise GitPreflightError(
                "Git reported a dirty path that is not a regular file"
            )
        return None
    except (OSError, ValueError) as exc:
        raise GitPreflightError("unable to fingerprint dirty file") from exc


def _index_digest(
    repo: Path,
    path: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Read a staged blob from the index without decoding binary content."""

    try:
        content = _git_bytes_with_env(repo, "cat-file", "blob", f":{path}", env=env)
    except _GitCommandFailure as error:
        # A staged deletion has no index entry and therefore no content to
        # hash.  Other failures remain fatal instead of silently weakening the
        # continuation check.  Gitlinks are commit objects, not file content.
        entries = _git_with_env(repo, "ls-files", "--stage", "--", path, env=env)
        if not entries.strip() or any(
            line.split(maxsplit=1)[0] == "160000"
            for line in entries.splitlines()
            if line.strip()
        ):
            return None
        raise error
    return _sha256_bytes(content)


def _content_fingerprints(
    repo: Path,
    staged: Iterable[str],
    unstaged: Iterable[str],
    untracked: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
    bounded: bool = True,
) -> tuple[tuple[str, str, str], ...]:
    """Capture bounded SHA-256 evidence for every dirty file with content."""

    entries: list[tuple[str, str, str]] = []
    for path in staged:
        digest = _index_digest(repo, path, env=env)
        if digest is not None:
            entries.append((path, "staged", digest))
    for path in unstaged:
        digest = _filesystem_digest(repo / Path(path))
        if digest is not None:
            entries.append((path, "unstaged", digest))
    for path in untracked:
        digest = _filesystem_digest(repo / Path(path))
        if digest is not None:
            entries.append((path, "untracked", digest))
    entries.sort(key=lambda value: (value[0], value[1]))
    return _bounded_fingerprints(entries) if bounded else tuple(entries)


def _legacy_untracked_fingerprints(
    fingerprints: Iterable[tuple[str, str, str]],
) -> tuple[tuple[str, str], ...]:
    """Keep the pre-R1 untracked-only field for payload compatibility."""

    return tuple(
        (path, digest)
        for path, state, digest in fingerprints
        if state == "untracked"
    )


def _validate_worktree_root(
    repo: Path, *, env: Mapping[str, str] | None = None
) -> None:
    top_level = _resolved_path(
        _git_with_env(repo, "rev-parse", "--show-toplevel", env=env).strip(),
        require_exists=True,
    )
    if _path_key(top_level) != _path_key(repo):
        raise GitPreflightError("repo_path is not the Git worktree root")


def _git_state(
    repo: Path,
    *,
    allow_detached: bool = False,
    include_diff: bool = False,
    include_fingerprints: bool = False,
    bounded: bool = True,
    env: Mapping[str, str] | None = None,
) -> tuple[
    str,
    str,
    str,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str,
    str,
    tuple[tuple[str, str, str], ...],
]:
    _validate_worktree_root(repo, env=env)
    branch = _git_branch(repo, allow_detached=allow_detached, env=env)
    head = _git_with_env(repo, "rev-parse", "HEAD", env=env).strip()
    if not head:
        raise GitPreflightError("Git HEAD is empty")
    status = _git_with_env(
        repo, "status", "--porcelain", "--untracked-files=all", env=env
    )
    staged, unstaged, untracked = _parse_status(status, bounded=bounded)
    diff = _git_with_env(repo, "diff", env=env) if include_diff else ""
    cached_diff = (
        _git_with_env(repo, "diff", "--cached", env=env) if include_diff else ""
    )
    fingerprints = (
        _content_fingerprints(
            repo, staged, unstaged, untracked, env=env, bounded=bounded
        )
        if include_fingerprints
        else ()
    )
    return (
        branch,
        head,
        status,
        staged,
        unstaged,
        untracked,
        diff,
        cached_diff,
        fingerprints,
    )


def git_preflight(repo_path: str | os.PathLike[str]) -> GitCheckpoint:
    """Require a clean Git worktree and capture its durable baseline."""

    repo = _resolved_path(repo_path, require_exists=True)
    branch, head, status, staged, unstaged, untracked, _, _, _ = _git_state(repo)
    if status.strip():
        raise DirtyWorkingTreeError(
            "autonomous-write requires a clean Git working tree",
            status_porcelain=_bounded_text(status),
            staged_paths=staged,
            unstaged_paths=unstaged,
            untracked_paths=untracked,
        )
    return GitCheckpoint(
        repo_path=str(repo),
        baseline_branch=branch,
        baseline_head=head,
        status_porcelain=_bounded_text(status),
        staged_paths=staged,
        unstaged_paths=unstaged,
        untracked_paths=untracked,
        baseline_kind="clean",
    )


def git_continuation_preflight(
    repo_path: str | os.PathLike[str],
    *,
    previous_task_id: str,
    previous_postflight: Mapping[str, Any],
) -> GitCheckpoint:
    """Accept a dirty worktree only when it matches durable postflight evidence."""

    repo = _resolved_path(repo_path, require_exists=True)
    if not isinstance(previous_task_id, str) or not previous_task_id.strip():
        raise ContinuationBaselineError("previous task identity is invalid")
    if not isinstance(previous_postflight, Mapping):
        raise ContinuationBaselineError("previous postflight payload is invalid")
    if previous_postflight.get("policy_violation") is not False:
        raise ContinuationBaselineError("previous postflight has a policy violation")

    expected_repo = previous_postflight.get("repo_path")
    expected_branch = previous_postflight.get("final_branch")
    expected_head = previous_postflight.get("final_head")
    expected_status = previous_postflight.get("status_porcelain")
    expected_diff = previous_postflight.get("diff")
    expected_cached_diff = previous_postflight.get("cached_diff")
    expected_changed = previous_postflight.get("changed_files")
    expected_untracked = previous_postflight.get("untracked_files")
    expected_fingerprints = previous_postflight.get("content_fingerprints")
    required_text = (
        expected_repo,
        expected_branch,
        expected_head,
        expected_status,
        expected_diff,
        expected_cached_diff,
    )
    if not all(isinstance(value, str) and value for value in required_text[:3]):
        raise ContinuationBaselineError("previous postflight identity is incomplete")
    if not all(isinstance(value, str) for value in required_text[3:]):
        raise ContinuationBaselineError("previous postflight Git evidence is incomplete")
    if not _evidence_is_complete(expected_status) or not _evidence_is_complete(
        expected_diff
    ) or not _evidence_is_complete(expected_cached_diff):
        raise ContinuationBaselineError("previous postflight Git evidence was truncated")
    try:
        expected_root = _resolved_path(expected_repo, require_exists=True)
    except (GitPreflightError, ValueError) as exc:
        raise ContinuationBaselineError("previous postflight repo path is invalid") from exc
    if _path_key(expected_root) != _path_key(repo):
        raise ContinuationBaselineError("previous postflight repo path differs")
    if not isinstance(expected_changed, list) or not all(
        isinstance(value, str) for value in expected_changed
    ):
        raise ContinuationBaselineError("previous postflight changed paths are incomplete")
    if not isinstance(expected_untracked, list) or not all(
        isinstance(value, str) for value in expected_untracked
    ):
        raise ContinuationBaselineError("previous postflight untracked paths are incomplete")
    if not _evidence_is_complete(expected_changed) or not _evidence_is_complete(
        expected_untracked
    ):
        raise ContinuationBaselineError("previous postflight paths were truncated")
    if not isinstance(expected_fingerprints, list):
        raise ContinuationBaselineError(
            "previous postflight lacks complete dirty content evidence"
        )
    normalized: list[tuple[str, str, str]] = []
    for value in expected_fingerprints:
        if not isinstance(value, dict):
            raise ContinuationBaselineError(
                "previous dirty content evidence is invalid"
            )
        path = value.get("path")
        state = value.get("state")
        digest = value.get("sha256")
        if path == TRUNCATION_SENTINEL:
            raise ContinuationBaselineError(
                "previous dirty content evidence was truncated"
            )
        if (
            not isinstance(path, str)
            or not path
            or state not in {"staged", "unstaged", "untracked"}
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ContinuationBaselineError(
                "previous dirty content evidence is incomplete"
            )
        normalized.append((path, state, digest))
    normalized_fingerprints = tuple(normalized)
    if not _evidence_is_complete(normalized_fingerprints):
        raise ContinuationBaselineError(
            "previous dirty content evidence was truncated"
        )

    capture_complete = any(
        isinstance(value, str) and len(value) > _MAX_EVIDENCE_TEXT
        for value in (expected_status, expected_diff, expected_cached_diff)
    ) or any(
        isinstance(value, list) and len(value) > _MAX_EVIDENCE_PATHS
        for value in (expected_changed, expected_untracked, expected_fingerprints)
    )

    (
        branch,
        head,
        status,
        staged,
        unstaged,
        untracked,
        diff,
        cached_diff,
        fingerprints,
    ) = _git_state(
        repo,
        include_diff=True,
        include_fingerprints=True,
        bounded=not capture_complete,
    )
    current_status = status if capture_complete else _bounded_text(status)
    current_diff = diff if capture_complete else _bounded_text(diff)
    current_cached_diff = cached_diff if capture_complete else _bounded_text(cached_diff)
    current_changed = (
        tuple((*staged, *unstaged))
        if capture_complete
        else _bounded_paths((*staged, *unstaged))
    )
    if not _evidence_is_complete(current_status) or not _evidence_is_complete(
        current_diff
    ) or not _evidence_is_complete(current_cached_diff):
        raise ContinuationBaselineError("current Git evidence is too large to compare")
    if not _evidence_is_complete(current_changed) or not _evidence_is_complete(
        untracked
    ) or not _evidence_is_complete(fingerprints):
        raise ContinuationBaselineError("current Git paths are too numerous to compare")
    expected_staged, expected_unstaged, expected_untracked_paths = _parse_status(
        expected_status, bounded=not capture_complete
    )
    if (
        branch != expected_branch
        or head != expected_head
        or current_status != expected_status
        or staged != expected_staged
        or unstaged != expected_unstaged
        or untracked != expected_untracked_paths
        or list(current_changed) != expected_changed
        or list(untracked) != expected_untracked
        or current_diff != expected_diff
        or current_cached_diff != expected_cached_diff
        or tuple(fingerprints) != normalized_fingerprints
    ):
        raise ContinuationBaselineError(
            "current Git state does not match the previous autonomous postflight"
        )
    return GitCheckpoint(
        repo_path=str(repo),
        baseline_branch=branch,
        baseline_head=head,
        status_porcelain=current_status,
        staged_paths=staged,
        unstaged_paths=unstaged,
        untracked_paths=untracked,
        baseline_kind="continuation",
        previous_task_id=previous_task_id,
    )


def git_postflight(checkpoint: GitCheckpoint) -> GitPostflight:
    """Collect bounded Git evidence and classify branch/HEAD changes."""

    repo = Path(checkpoint.repo_path)
    (
        final_branch,
        final_head,
        status,
        staged,
        unstaged,
        untracked,
        diff,
        cached_diff,
        fingerprints,
    ) = _git_state(
        repo,
        allow_detached=True,
        include_diff=True,
        include_fingerprints=True,
    )
    changed = _bounded_paths((*staged, *unstaged))
    return GitPostflight(
        repo_path=checkpoint.repo_path,
        baseline_branch=checkpoint.baseline_branch,
        baseline_head=checkpoint.baseline_head,
        final_branch=final_branch,
        final_head=final_head,
        status_porcelain=_bounded_text(status),
        diff=_bounded_text(diff),
        cached_diff=_bounded_text(cached_diff),
        changed_files=changed,
        untracked_files=untracked,
        policy_violation=(
            final_branch != checkpoint.baseline_branch
            or final_head != checkpoint.baseline_head
        ),
        untracked_fingerprints=_legacy_untracked_fingerprints(fingerprints),
        content_fingerprints=fingerprints,
    )


def git_postflight_complete(checkpoint: GitCheckpoint) -> GitPostflight:
    """Collect an unbounded Git snapshot for explicit baseline adoption.

    Normal task postflight remains bounded.  Recovery adoption is deliberately
    explicit and durable, so it needs the complete status, diff, path, and
    fingerprint sets rather than the preview sentinels used by MCP/event
    responses.
    """

    repo = Path(checkpoint.repo_path)
    (
        final_branch,
        final_head,
        status,
        staged,
        unstaged,
        untracked,
        diff,
        cached_diff,
        fingerprints,
    ) = _git_state(
        repo,
        allow_detached=True,
        include_diff=True,
        include_fingerprints=True,
        bounded=False,
    )
    return GitPostflight(
        repo_path=checkpoint.repo_path,
        baseline_branch=checkpoint.baseline_branch,
        baseline_head=checkpoint.baseline_head,
        final_branch=final_branch,
        final_head=final_head,
        status_porcelain=status,
        diff=diff,
        cached_diff=cached_diff,
        changed_files=tuple((*staged, *unstaged)),
        untracked_files=untracked,
        policy_violation=(
            final_branch != checkpoint.baseline_branch
            or final_head != checkpoint.baseline_head
        ),
        untracked_fingerprints=_legacy_untracked_fingerprints(fingerprints),
        content_fingerprints=fingerprints,
    )


def checkpoint_payload(checkpoint: GitCheckpoint) -> dict[str, Any]:
    return {
        "repo_path": checkpoint.repo_path,
        "baseline_branch": checkpoint.baseline_branch,
        "baseline_head": checkpoint.baseline_head,
        "status_porcelain": checkpoint.status_porcelain,
        "staged_paths": list(checkpoint.staged_paths),
        "unstaged_paths": list(checkpoint.unstaged_paths),
        "untracked_paths": list(checkpoint.untracked_paths),
        "baseline_kind": checkpoint.baseline_kind,
        "previous_task_id": checkpoint.previous_task_id,
    }


def postflight_payload(postflight: GitPostflight) -> dict[str, Any]:
    return {
        "repo_path": postflight.repo_path,
        "baseline_branch": postflight.baseline_branch,
        "baseline_head": postflight.baseline_head,
        "final_branch": postflight.final_branch,
        "final_head": postflight.final_head,
        "status_porcelain": postflight.status_porcelain,
        "diff": postflight.diff,
        "cached_diff": postflight.cached_diff,
        "changed_files": list(postflight.changed_files),
        "untracked_files": list(postflight.untracked_files),
        "policy_violation": postflight.policy_violation,
        "untracked_fingerprints": [
            {"path": path, "sha256": digest}
            for path, digest in postflight.untracked_fingerprints
        ],
        "content_fingerprints": [
            {"path": path, "state": state, "sha256": digest}
            for path, state, digest in postflight.content_fingerprints
        ],
    }


def _require_checkpoint_paths(
    value: Any, field_name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CheckpointCommitError(f"postflight {field_name} evidence is incomplete")
    if not _evidence_is_complete(value) or (not allow_empty and not value):
        raise CheckpointCommitError(f"postflight {field_name} evidence is incomplete")
    for item in value:
        if not item or "\x00" in item or any(ord(character) < 32 for character in item):
            raise CheckpointCommitError(f"postflight {field_name} contains an invalid path")
        if " -> " in item:
            raise CheckpointCommitError("renamed paths are not supported for checkpoints")
        normalized = item.replace("\\", "/")
        if normalized.startswith("/") or normalized.startswith("./"):
            raise CheckpointCommitError("postflight contains an unsafe path")
        if len(normalized) >= 2 and normalized[1] == ":":
            raise CheckpointCommitError("postflight contains an absolute path")
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise CheckpointCommitError("postflight contains an unsafe path")
    return tuple(value)


def _normalize_checkpoint_fingerprints(
    value: Any,
) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(value, list) or not _evidence_is_complete(value):
        raise CheckpointCommitError(
            "postflight content fingerprints are incomplete"
        )
    normalized: list[tuple[str, str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise CheckpointCommitError(
                "postflight content fingerprints are incomplete"
            )
        path = item.get("path")
        state = item.get("state")
        digest = item.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or state not in {"staged", "unstaged", "untracked"}
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CheckpointCommitError(
                "postflight content fingerprints are incomplete"
            )
        normalized.append((path, state, digest))
    return tuple(normalized)


def _checkpoint_expected_state(
    repo: Path,
    postflight: Mapping[str, Any],
) -> tuple[str, str, tuple[str, ...], tuple[tuple[str, str, str], ...]]:
    """Validate and compare one durable postflight against the live worktree."""

    expected_repo = postflight.get("repo_path")
    expected_branch = postflight.get("final_branch")
    expected_head = postflight.get("final_head")
    baseline_branch = postflight.get("baseline_branch")
    baseline_head = postflight.get("baseline_head")
    expected_status = postflight.get("status_porcelain")
    expected_diff = postflight.get("diff")
    expected_cached_diff = postflight.get("cached_diff")
    if not all(
        isinstance(value, str) and value
        for value in (
            expected_repo,
            expected_branch,
            expected_head,
            baseline_branch,
            baseline_head,
        )
    ):
        raise CheckpointCommitError("postflight Git identity is incomplete")
    if not all(
        isinstance(value, str)
        for value in (expected_status, expected_diff, expected_cached_diff)
    ):
        raise CheckpointCommitError("postflight Git evidence is incomplete")
    if not all(
        _evidence_is_complete(value)
        for value in (expected_status, expected_diff, expected_cached_diff)
    ):
        raise CheckpointCommitError("postflight Git evidence was truncated")
    if postflight.get("policy_violation") is not False:
        raise CheckpointCommitError("task postflight contains a policy violation")
    try:
        expected_root = _resolved_path(expected_repo, require_exists=True)
    except (GitPreflightError, ValueError) as exc:
        raise CheckpointCommitError("postflight repo path is invalid") from exc
    if _path_key(expected_root) != _path_key(repo):
        raise CheckpointCommitError("postflight repo path differs from the project")
    expected_changed = _require_checkpoint_paths(
        postflight.get("changed_files"), "changed_files", allow_empty=True
    )
    expected_untracked = _require_checkpoint_paths(
        postflight.get("untracked_files"), "untracked_files", allow_empty=True
    )
    expected_fingerprints = _normalize_checkpoint_fingerprints(
        postflight.get("content_fingerprints")
    )

    (
        branch,
        head,
        status,
        staged,
        unstaged,
        untracked,
        diff,
        cached_diff,
        fingerprints,
    ) = _git_state(repo, include_diff=True, include_fingerprints=True)
    current_status = _bounded_text(status)
    current_diff = _bounded_text(diff)
    current_cached_diff = _bounded_text(cached_diff)
    current_changed = _bounded_paths((*staged, *unstaged))
    if not all(
        _evidence_is_complete(value)
        for value in (
            current_status,
            current_diff,
            current_cached_diff,
            current_changed,
            untracked,
            fingerprints,
        )
    ):
        raise CheckpointCommitError("current Git evidence is too large to compare")
    if (
        branch != expected_branch
        or head != expected_head
        or expected_branch != baseline_branch
        or expected_head != baseline_head
        or current_status != expected_status
        or list(current_changed) != list(expected_changed)
        or list(untracked) != list(expected_untracked)
        or current_diff != expected_diff
        or current_cached_diff != expected_cached_diff
        or tuple(fingerprints) != expected_fingerprints
    ):
        raise CheckpointCommitError(
            "current Git state does not match durable autonomous postflight"
        )
    paths = tuple(sorted(set((*expected_changed, *expected_untracked))))
    if not paths:
        raise CheckpointCommitError("checkpoint requires Git changes")
    return head, branch, paths, expected_fingerprints


def _checkpoint_git_dir(repo: Path) -> Path:
    raw = _git(repo, "rev-parse", "--git-dir").strip()
    if not raw:
        raise CheckpointCommitError("Git directory could not be resolved")
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    try:
        return git_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CheckpointCommitError("Git directory could not be resolved") from exc


def _checkpoint_index_path(repo: Path) -> Path:
    raw = _git(repo, "rev-parse", "--git-path", "index").strip()
    if not raw:
        raise CheckpointCommitError("Git index path could not be resolved")
    index = Path(raw)
    if not index.is_absolute():
        index = repo / index
    return index.resolve(strict=False)


def _checkpoint_fs_digest(repo: Path, path: str) -> str | None:
    return _filesystem_digest(repo / Path(path))


def _checkpoint_stage_matches_worktree(
    repo: Path, paths: Iterable[str], env: Mapping[str, str]
) -> None:
    for path in paths:
        fs_digest = _checkpoint_fs_digest(repo, path)
        index_entries = _git_with_env(
            repo, "ls-files", "--stage", "--", path, env=env
        ).strip()
        index_oid = ""
        if index_entries:
            fields = index_entries.splitlines()[0].split(maxsplit=2)
            if len(fields) < 2:
                raise CheckpointCommitError("staged Git entry is invalid")
            index_oid = fields[1]
        worktree_oid = ""
        if fs_digest is not None:
            worktree_oid = _git_with_env(
                repo,
                "hash-object",
                f"--path={path}",
                "--",
                path,
                env=env,
            ).strip()
        if index_oid != worktree_oid:
            raise CheckpointCommitError(
                "worktree changed while the checkpoint was being staged"
            )


def _checkpoint_commit_metadata(
    repo: Path,
    previous_head: str,
    commit_head: str,
    env: Mapping[str, str],
) -> tuple[str, str, str, str, str]:
    raw = _git_with_env(
        repo,
        "show",
        "-s",
        "--format=%H%x00%P%x00%an%x00%ae%x00%cn%x00%ce%x00%B",
        commit_head,
        env=env,
    )
    parts = raw.split("\x00", 6)
    if len(parts) != 7:
        raise CheckpointCommitError("checkpoint commit metadata is incomplete")
    actual_head, parents, author_name, author_email, committer_name, committer_email, body = parts
    if actual_head != commit_head or parents.strip() != previous_head:
        raise CheckpointCommitError("checkpoint commit parent or HEAD is invalid")
    if (author_name, author_email, committer_name, committer_email) != (
        "Marcos Sfregola",
        "marcos.sfregola@gmail.com",
        "Marcos Sfregola",
        "marcos.sfregola@gmail.com",
    ):
        raise CheckpointCommitError("checkpoint commit identity is invalid")
    return actual_head, parents.strip(), author_name, author_email, body.rstrip("\r\n")


def _checkpoint_commit_paths(
    repo: Path,
    previous_head: str,
    commit_head: str,
    env: Mapping[str, str],
) -> tuple[str, ...]:
    raw = _git_bytes_with_env(
        repo,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "--no-renames",
        "-r",
        "-z",
        previous_head,
        commit_head,
        env=env,
    )
    values = [
        item.decode("utf-8", errors="surrogateescape")
        for item in raw.split(b"\x00")
        if item
    ]
    if any("\x00" in value for value in values):
        raise CheckpointCommitError("checkpoint commit contains an invalid path")
    return tuple(sorted(values))


__all__ = [
    "AUTONOMOUS_WRITE_CONTRACT",
    "CheckpointAlreadyCommittedError",
    "CheckpointCommitError",
    "CheckpointCommitResult",
    "DirtyWorkingTreeError",
    "ContinuationBaselineError",
    "GitCheckpoint",
    "GitPostflight",
    "GitPostflightError",
    "GitPreflightError",
    "PolicyError",
    "PolicyViolationError",
    "ProtectedRootError",
    "augment_objective",
    "checkpoint_payload",
    "checkpoint_commit",
    "ensure_autonomous_workspace",
    "git_postflight",
    "git_postflight_complete",
    "git_continuation_preflight",
    "git_checkpoint_commit",
    "git_preflight",
    "postflight_payload",
    "protected_roots",
    "TRUNCATION_SENTINEL",
    "validate_continuation_snapshot",
]


# ---------------------------------------------------------------------------
# H4-B checkpoint protocol
# ---------------------------------------------------------------------------


CHECKPOINT_AUTHOR_NAME = "Marcos Sfregola"
CHECKPOINT_AUTHOR_EMAIL = "marcos.sfregola@gmail.com"
CHECKPOINT_PHASE_PREPARE = "PREPARE"
CHECKPOINT_PHASE_STARTED = "STARTED"
CHECKPOINT_PHASE_PRE_CAS = "PRE_CAS"
CHECKPOINT_PHASE_CAS = "CAS"
CHECKPOINT_PHASE_REF_UPDATED = "REF_UPDATED"
CHECKPOINT_PHASE_INDEX = "INDEX_FINALIZATION"
CHECKPOINT_PHASE_CREATED = "CREATED"


class CheckpointPreconditionError(CheckpointCommitError):
    """Raised when a pre-CAS snapshot is no longer authoritative."""

    def __init__(self, message: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass
class PreparedCheckpoint:
    """One immutable candidate plus its temporary index and authority snapshot."""

    attempt_id: str
    snapshot_id: str
    repo_path: str
    branch: str
    branch_ref: str
    expected_head: str
    candidate_parent: str
    candidate_tree: str
    candidate_commit: str
    paths: tuple[str, ...]
    message: str
    message_digest: str
    author_name: str
    author_email: str
    author_timestamp: str
    committer_name: str
    committer_email: str
    committer_timestamp: str
    real_index_path: str
    temporary_index_path: str
    temporary_index_digest: str
    temporary_index_entries_digest: str
    temporary_tree: str
    snapshot: dict[str, Any]
    env: dict[str, str]
    temp_dir: Path

    def started_payload(self) -> dict[str, Any]:
        """Return the complete, JSON-safe durable STARTED payload."""

        return {
            "phase": CHECKPOINT_PHASE_STARTED,
            "attempt_id": self.attempt_id,
            "snapshot_id": self.snapshot_id,
            "task_id": self.snapshot["task_id"],
            "project_id": self.snapshot["project_id"],
            "repo_root": self.repo_path,
            "branch_ref": self.branch_ref,
            "branch": self.branch,
            "expected_head": self.expected_head,
            "candidate_commit": self.candidate_commit,
            "candidate_parent": self.candidate_parent,
            "candidate_tree": self.candidate_tree,
            "task_execution_status": self.snapshot.get("task_execution_status"),
            "task_audit_status": self.snapshot.get("task_audit_status"),
            "paths": list(self.paths),
            "message": self.message,
            "message_digest": self.message_digest,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "author_timestamp": self.author_timestamp,
            "committer_name": self.committer_name,
            "committer_email": self.committer_email,
            "committer_timestamp": self.committer_timestamp,
            "real_index_path": self.real_index_path,
            "real_index_identity": self.snapshot.get("real_index_identity"),
            "real_index_entries_digest": self.snapshot.get("real_index_entries_digest"),
            "temporary_index_path": self.temporary_index_path,
            "temporary_index_digest": self.temporary_index_digest,
            "temporary_index_entries_digest": self.temporary_index_entries_digest,
            "temporary_tree": self.temporary_tree,
            "snapshot": self.snapshot,
        }

    def cleanup(self) -> None:
        """Remove only this attempt's temporary directory."""

        shutil.rmtree(self.temp_dir, ignore_errors=True)


@dataclass(frozen=True)
class CheckpointFinalization:
    """Post-CAS observation; the commit exists for every returned result."""

    commit_head: str
    finalization_status: str
    clean: bool
    observed_head: str
    conflict: dict[str, Any] | None = None


@dataclass(frozen=True)
class CheckpointCommitResult:
    """Public result for the hardened checkpoint helper."""

    previous_head: str
    commit_head: str
    branch: str
    message: str
    paths: tuple[str, ...]
    clean: bool
    attempt_id: str | None = None
    snapshot_id: str | None = None
    finalization_status: str = "CLEAN"
    commit_created: bool = True
    post_state: str = "CLEAN"
    conflict: dict[str, Any] | None = None


def _h4_full_status(status: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for line in status.splitlines():
        if len(line) < 3:
            continue
        code, path = line[:2], line[3:]
        if code == "??":
            untracked.append(path)
            continue
        if code[0] != " ":
            staged.append(path)
        if code[1] != " ":
            unstaged.append(path)
    return tuple(staged), tuple(unstaged), tuple(untracked)


def _h4_full_fingerprints(
    repo: Path,
    staged: Iterable[str],
    unstaged: Iterable[str],
    untracked: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str, str], ...]:
    values: list[tuple[str, str, str]] = []
    for path in staged:
        digest = _index_digest(repo, path, env=env)
        if digest is not None:
            values.append((path, "staged", digest))
    for path in unstaged:
        digest = _filesystem_digest(repo / Path(path))
        if digest is not None:
            values.append((path, "unstaged", digest))
    for path in untracked:
        digest = _filesystem_digest(repo / Path(path))
        if digest is not None:
            values.append((path, "untracked", digest))
    return tuple(sorted(values, key=lambda value: (value[0], value[1])))


def _h4_index_bytes(repo: Path, index_path: Path) -> tuple[bytes, str, str]:
    try:
        content = index_path.read_bytes()
    except FileNotFoundError:
        content = b""
    entries = _git_bytes_with_env(repo, "ls-files", "--stage", "-z", env=None)
    return content, _sha256_bytes(content), _sha256_bytes(entries)


def _h4_index_entries(
    repo: Path, *, env: Mapping[str, str] | None = None
) -> list[dict[str, str]]:
    """Return complete staged index entries, including Git modes.

    The raw ``-z`` form is used so names containing whitespace are not
    truncated.  This is bounded only by the actual index (not by the public
    evidence limits); the resulting snapshot is therefore authoritative for
    checkpoint revalidation.
    """

    raw = _git_bytes_with_env(repo, "ls-files", "--stage", "-z", env=env)
    entries: list[dict[str, str]] = []
    for record in raw.split(b"\x00"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            path = path_bytes.decode("utf-8", errors="surrogateescape")
        except (UnicodeDecodeError, ValueError) as exc:
            raise CheckpointCommitError("Git index entries are invalid") from exc
        entries.append({"mode": mode, "oid": oid, "stage": stage, "path": path})
    return entries


def _h4_observation(
    repo: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Capture complete hashes/paths without retaining unbounded diff text."""

    branch = _git_branch(repo, env=env)
    head = _git_with_env(repo, "rev-parse", "HEAD", env=env).strip()
    status = _git_with_env(
        repo, "status", "--porcelain", "--untracked-files=all", env=env
    )
    staged, unstaged, untracked = _h4_full_status(status)
    diff = _git_bytes_with_env(repo, "diff", "--binary", env=env)
    cached = _git_bytes_with_env(repo, "diff", "--cached", "--binary", env=env)
    fingerprints = _h4_full_fingerprints(
        repo, staged, unstaged, untracked, env=env
    )
    index_path = _checkpoint_index_path(repo)
    if env is not None and env.get("GIT_INDEX_FILE"):
        temp_path = Path(env["GIT_INDEX_FILE"])
        try:
            index_bytes = temp_path.read_bytes()
        except FileNotFoundError:
            index_bytes = b""
    else:
        try:
            index_bytes = index_path.read_bytes()
        except FileNotFoundError:
            index_bytes = b""
    entries = _git_bytes_with_env(repo, "ls-files", "--stage", "-z", env=env)
    return {
        "branch": branch,
        "branch_ref": f"refs/heads/{branch}",
        "head": head,
        "index_path": str(index_path),
        "status": status,
        "staged_paths": list(staged),
        "unstaged_paths": list(unstaged),
        "untracked_paths": list(untracked),
        "diff_digest": _sha256_bytes(diff),
        "cached_diff_digest": _sha256_bytes(cached),
        "worktree_fingerprints": [
            {"path": path, "state": state, "sha256": digest}
            for path, state, digest in fingerprints
        ],
        "real_index_identity": _sha256_bytes(index_bytes),
        "real_index_entries_digest": _sha256_bytes(entries),
        "index_entries": _h4_index_entries(repo, env=env),
    }


def _h4_observation_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    keys = (
        "branch",
        "branch_ref",
        "head",
        "index_path",
        "status",
        "staged_paths",
        "unstaged_paths",
        "untracked_paths",
        "diff_digest",
        "cached_diff_digest",
        "worktree_fingerprints",
        "real_index_identity",
        "real_index_entries_digest",
        "index_entries",
    )
    return all(left.get(key) == right.get(key) for key in keys)


def _h4_commit_details(
    repo: Path,
    commit_head: str,
    *,
    expected_parent: str,
    expected_tree: str,
    expected_message: str,
    env: Mapping[str, str],
) -> dict[str, str]:
    raw = _git_with_env(
        repo,
        "show",
        "-s",
        "--format=%H%x00%P%x00%T%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B",
        commit_head,
        env=env,
    )
    fields = raw.split("\x00", 9)
    if len(fields) != 10:
        raise CheckpointCommitError("candidate commit metadata is incomplete")
    (
        actual,
        parent,
        tree,
        author_name,
        author_email,
        author_timestamp,
        committer_name,
        committer_email,
        committer_timestamp,
        body,
    ) = fields
    if actual != commit_head or parent.strip() != expected_parent or tree.strip() != expected_tree:
        raise CheckpointCommitError("candidate parent or tree is invalid")
    if (author_name, author_email, committer_name, committer_email) != (
        CHECKPOINT_AUTHOR_NAME,
        CHECKPOINT_AUTHOR_EMAIL,
        CHECKPOINT_AUTHOR_NAME,
        CHECKPOINT_AUTHOR_EMAIL,
    ):
        raise CheckpointCommitError("candidate commit identity is invalid")
    if body.rstrip("\r\n") != expected_message:
        raise CheckpointCommitError("candidate commit message is invalid")
    return {
        "commit": actual,
        "parent": parent.strip(),
        "tree": tree.strip(),
        "author_name": author_name,
        "author_email": author_email,
        "author_timestamp": author_timestamp,
        "committer_name": committer_name,
        "committer_email": committer_email,
        "committer_timestamp": committer_timestamp,
    }


def _h4_candidate_index_tree(repo: Path, env: Mapping[str, str]) -> str:
    tree = _git_with_env(repo, "write-tree", env=env).strip()
    if not tree:
        raise CheckpointCommitError("temporary index did not produce a tree")
    return tree


def git_checkpoint_prepare(
    repo_path: str | os.PathLike[str],
    *,
    postflight: Mapping[str, Any] | GitPostflight,
    message: str,
    task_id: str = "",
    project_id: str = "",
    attempt_id: str | None = None,
    snapshot_id: str | None = None,
) -> PreparedCheckpoint:
    """Prepare a candidate commit without moving refs or the real index."""

    if not isinstance(message, str) or not message.strip():
        raise CheckpointCommitError("commit message must be non-empty text")
    if "\x00" in message:
        raise CheckpointCommitError("commit message contains NUL")
    if isinstance(postflight, GitPostflight):
        postflight = postflight_payload(postflight)
    repo = _resolved_path(repo_path, require_exists=True)
    _validate_worktree_root(repo)
    previous_head, branch, paths, _ = _checkpoint_expected_state(repo, postflight)
    branch_ref = f"refs/heads/{branch}"
    git_dir = _checkpoint_git_dir(repo)
    actual_index = _checkpoint_index_path(repo)
    before = _h4_observation(repo)
    temp_dir = Path(tempfile.mkdtemp(prefix=".bridge-checkpoint-", dir=str(git_dir)))
    temp_index = temp_dir / "index"
    if actual_index.exists():
        shutil.copyfile(actual_index, temp_index)
    hooks_dir = temp_dir / "hooks"
    hooks_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "GIT_INDEX_FILE": str(temp_index),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": ":",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    try:
        pathspec = b"".join(
            path.encode("utf-8", errors="surrogateescape") + b"\x00" for path in paths
        )
        _git_with_env(
            repo,
            "--literal-pathspecs",
            "add",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            env=env,
            input_bytes=pathspec,
        )
        staged = _h4_observation(repo, env=env)
        if (
            staged["branch"] != branch
            or staged["head"] != previous_head
            or staged["unstaged_paths"]
            or staged["untracked_paths"]
            or tuple(sorted(staged["staged_paths"])) != tuple(sorted(paths))
        ):
            raise CheckpointCommitError("staged Git state is not the verified checkpoint")
        _checkpoint_stage_matches_worktree(repo, paths, env)
        candidate_tree = _h4_candidate_index_tree(repo, env)
        now = datetime.now(timezone.utc).isoformat()
        commit_env = dict(env)
        commit_env.update(
            {
                "GIT_AUTHOR_NAME": CHECKPOINT_AUTHOR_NAME,
                "GIT_AUTHOR_EMAIL": CHECKPOINT_AUTHOR_EMAIL,
                "GIT_AUTHOR_DATE": now,
                "GIT_COMMITTER_NAME": CHECKPOINT_AUTHOR_NAME,
                "GIT_COMMITTER_EMAIL": CHECKPOINT_AUTHOR_EMAIL,
                "GIT_COMMITTER_DATE": now,
            }
        )
        commit_output = _git_with_env(
            repo,
            "commit-tree",
            candidate_tree,
            "-p",
            previous_head,
            "-F",
            "-",
            env=commit_env,
            input_bytes=(message + "\n").encode("utf-8"),
        ).strip()
        if not commit_output:
            raise CheckpointCommitError("git commit-tree returned no candidate")
        details = _h4_commit_details(
            repo,
            commit_output,
            expected_parent=previous_head,
            expected_tree=candidate_tree,
            expected_message=message,
            env=commit_env,
        )
        committed_paths = _checkpoint_commit_paths(repo, previous_head, commit_output, commit_env)
        if committed_paths != tuple(sorted(paths)):
            raise CheckpointCommitError("candidate commit paths are invalid")
        if not _h4_observation_equal(before, _h4_observation(repo)):
            raise CheckpointPreconditionError(
                "Git state changed during checkpoint preparation",
                reason="PREPARE_STATE_CHANGED",
            )
        temporary_bytes = temp_index.read_bytes() if temp_index.exists() else b""
        temporary_entries = _git_bytes_with_env(
            repo, "ls-files", "--stage", "-z", env=env
        )
        snapshot = {
            "attempt_id": attempt_id or str(uuid.uuid4()),
            "snapshot_id": snapshot_id or str(uuid.uuid4()),
            "snapshot_created_at": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "project_id": project_id,
            "repo_root": str(repo),
            "branch_ref": branch_ref,
            "expected_head": previous_head,
            "paths": list(sorted(paths)),
            "observation": before,
            "status": before["status"],
            "untracked_set": before["untracked_paths"],
            "git_diff_binary_digest": before["diff_digest"],
            "git_diff_cached_binary_digest": before["cached_diff_digest"],
            "real_index_identity": before["real_index_identity"],
            "real_index_entries_digest": before["real_index_entries_digest"],
            "real_index_entries": before["index_entries"],
            "temporary_index_identity": _sha256_bytes(temporary_bytes),
            "temporary_index_entries_digest": _sha256_bytes(temporary_entries),
            "temporary_index_entries": staged["index_entries"],
            "temporary_tree": candidate_tree,
            "candidate_tree": candidate_tree,
            "candidate_commit": commit_output,
            "latest_task_identity": None,
            "task_event_high_water": None,
            "message_digest": _sha256_bytes(message.encode("utf-8")),
            "author_name": details["author_name"],
            "author_email": details["author_email"],
            "author_timestamp": details["author_timestamp"],
            "committer_name": details["committer_name"],
            "committer_email": details["committer_email"],
            "committer_timestamp": details["committer_timestamp"],
        }
        return PreparedCheckpoint(
            attempt_id=snapshot["attempt_id"],
            snapshot_id=snapshot["snapshot_id"],
            repo_path=str(repo),
            branch=branch,
            branch_ref=branch_ref,
            expected_head=previous_head,
            candidate_parent=previous_head,
            candidate_tree=candidate_tree,
            candidate_commit=commit_output,
            paths=tuple(sorted(paths)),
            message=message,
            message_digest=snapshot["message_digest"],
            author_name=details["author_name"],
            author_email=details["author_email"],
            author_timestamp=details["author_timestamp"],
            committer_name=details["committer_name"],
            committer_email=details["committer_email"],
            committer_timestamp=details["committer_timestamp"],
            real_index_path=str(actual_index),
            temporary_index_path=str(temp_index),
            temporary_index_digest=snapshot["temporary_index_identity"],
            temporary_index_entries_digest=snapshot["temporary_index_entries_digest"],
            temporary_tree=candidate_tree,
            snapshot=snapshot,
            env=commit_env,
            temp_dir=temp_dir,
        )
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _h4_final_revalidate(prepared: PreparedCheckpoint) -> None:
    repo = Path(prepared.repo_path)
    observed = _h4_observation(repo)
    expected = prepared.snapshot.get("observation", {})
    if not isinstance(expected, Mapping) or not _h4_observation_equal(observed, expected):
        raise CheckpointPreconditionError(
            "Git snapshot changed before checkpoint CAS",
            reason="PRE_CAS_STATE_CHANGED",
        )
    if observed.get("branch_ref") != prepared.branch_ref or observed.get("head") != prepared.expected_head:
        raise CheckpointPreconditionError(
            "branch or HEAD changed before checkpoint CAS",
            reason="PRE_CAS_HEAD_CHANGED",
        )
    temp_tree = _h4_candidate_index_tree(repo, prepared.env)
    if temp_tree != prepared.candidate_tree:
        raise CheckpointPreconditionError(
            "temporary candidate tree changed before CAS",
            reason="CANDIDATE_TREE_CHANGED",
        )
    details = _h4_commit_details(
        repo,
        prepared.candidate_commit,
        expected_parent=prepared.candidate_parent,
        expected_tree=prepared.candidate_tree,
        expected_message=prepared.message,
        env=prepared.env,
    )
    if details["commit"] != prepared.candidate_commit:
        raise CheckpointPreconditionError(
            "candidate commit changed before CAS",
            reason="CANDIDATE_CHANGED",
        )


def git_checkpoint_cas(prepared: PreparedCheckpoint) -> None:
    """Perform the compare-and-swap ref update after final revalidation."""

    try:
        _h4_final_revalidate(prepared)
    except CheckpointPreconditionError:
        raise
    except Exception as exc:
        raise CheckpointPreconditionError(
            "Git final revalidation could not be completed",
            reason="PRE_CAS_VALIDATION_FAILED",
        ) from exc
    try:
        _git_with_env(
            Path(prepared.repo_path),
            "update-ref",
            prepared.branch_ref,
            prepared.candidate_commit,
            prepared.expected_head,
            env=prepared.env,
        )
    except Exception as exc:
        raise CheckpointPreconditionError(
            "Git ref compare-and-swap failed",
            reason="CAS_FAILED",
        ) from exc


def git_checkpoint_head_relation(prepared: PreparedCheckpoint) -> tuple[str, str]:
    """Return ``expected``, ``candidate``, ``descendant`` or ``unknown``."""

    repo = Path(prepared.repo_path)
    current = _git_with_env(repo, "rev-parse", "HEAD").strip()
    if current == prepared.expected_head:
        return "expected", current
    if current == prepared.candidate_commit:
        return "candidate", current
    probe = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "merge-base",
            "--is-ancestor",
            prepared.candidate_commit,
            current,
        ],
        check=False,
        capture_output=True,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    if probe.returncode == 0:
        return "descendant", current
    return "unknown", current


def _h4_conflict(
    prepared: PreparedCheckpoint,
    *,
    reason: str,
    observed: Mapping[str, Any] | None = None,
) -> CheckpointFinalization:
    return CheckpointFinalization(
        commit_head=prepared.candidate_commit,
        finalization_status="EXTERNAL_STATE_CONFLICT",
        clean=False,
        observed_head=(
            str(observed.get("head"))
            if isinstance(observed, Mapping) and observed.get("head")
            else prepared.candidate_commit
        ),
        conflict={
            "reason": reason,
            "expected": prepared.snapshot.get("observation"),
            "observed": dict(observed) if isinstance(observed, Mapping) else None,
        },
    )


def git_checkpoint_finalize(prepared: PreparedCheckpoint) -> CheckpointFinalization:
    """Finalize the real index after CAS without overwriting external state."""

    repo = Path(prepared.repo_path)
    try:
        current_head = _git_with_env(repo, "rev-parse", "HEAD").strip()
        if current_head != prepared.candidate_commit:
            ancestor = subprocess.run(
                ["git", "-C", str(repo), "merge-base", "--is-ancestor", prepared.candidate_commit, current_head],
                check=False,
                capture_output=True,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            if ancestor.returncode == 0:
                return CheckpointFinalization(
                    commit_head=prepared.candidate_commit,
                    finalization_status="REF_ADVANCED_AFTER_CHECKPOINT",
                    clean=False,
                    observed_head=current_head,
                    conflict={"current_head": current_head},
                )
            return _h4_conflict(prepared, reason="HEAD_NO_LONGER_REFERENCES_CANDIDATE")

        # Check worktree/untracked against the candidate index before publishing.
        temp_status = _git_with_env(
            repo,
            "status",
            "--porcelain",
            "--untracked-files=all",
            env=prepared.env,
        )
        if temp_status.strip():
            observed = _h4_observation(repo)
            return _h4_conflict(prepared, reason="WORKTREE_CHANGED_POST_CAS", observed=observed)

        index_path = Path(_checkpoint_index_path(repo))
        if index_path != Path(prepared.real_index_path).resolve(strict=False):
            return _h4_conflict(prepared, reason="REAL_INDEX_PATH_CHANGED")
        lock_path = Path(f"{index_path}.lock")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            fd = os.open(str(lock_path), flags, 0o666)
        except FileExistsError:
            return _h4_conflict(prepared, reason="INDEX_LOCK_ALREADY_EXISTS")
        lock_identity = os.fstat(fd)
        published = False
        try:
            # Re-check the ref while holding the index lock, immediately before
            # any publication.  A later descendant is observed and classified
            # forward-only; an unrelated ref change is an external conflict.
            locked_head = _git_with_env(repo, "rev-parse", "HEAD").strip()
            if locked_head != prepared.candidate_commit:
                ancestor = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "merge-base",
                        "--is-ancestor",
                        prepared.candidate_commit,
                        locked_head,
                    ],
                    check=False,
                    capture_output=True,
                    timeout=GIT_COMMAND_TIMEOUT_SECONDS,
                )
                if ancestor.returncode == 0:
                    return CheckpointFinalization(
                        commit_head=prepared.candidate_commit,
                        finalization_status="REF_ADVANCED_AFTER_CHECKPOINT",
                        clean=False,
                        observed_head=locked_head,
                        conflict={"current_head": locked_head},
                    )
                return _h4_conflict(
                    prepared,
                    reason="HEAD_NO_LONGER_REFERENCES_CANDIDATE",
                    observed={"head": locked_head},
                )
            current_index_bytes = index_path.read_bytes() if index_path.exists() else b""
            current_index_digest = _sha256_bytes(current_index_bytes)
            candidate_bytes = (
                Path(prepared.temporary_index_path).read_bytes()
                if Path(prepared.temporary_index_path).exists()
                else b""
            )
            candidate_index_digest = _sha256_bytes(candidate_bytes)
            if current_index_digest not in {
                prepared.snapshot.get("real_index_identity"),
                candidate_index_digest,
            }:
                return _h4_conflict(
                    prepared,
                    reason="REAL_INDEX_CHANGED_POST_CAS",
                    observed={"real_index_identity": current_index_digest},
                )
            if current_index_digest != candidate_index_digest:
                # Close the remaining race between the first identity read and
                # writing our lockfile.  Non-cooperating direct writers are
                # detected and never incorporated silently.
                latest_index_bytes = index_path.read_bytes() if index_path.exists() else b""
                if _sha256_bytes(latest_index_bytes) != current_index_digest:
                    return _h4_conflict(
                        prepared,
                        reason="REAL_INDEX_CHANGED_POST_CAS",
                        observed={
                            "real_index_identity": _sha256_bytes(latest_index_bytes)
                        },
                    )
                latest_head = _git_with_env(repo, "rev-parse", "HEAD").strip()
                if latest_head != prepared.candidate_commit:
                    return _h4_conflict(
                        prepared,
                        reason="HEAD_CHANGED_DURING_INDEX_FINALIZATION",
                        observed={"head": latest_head},
                    )
                latest_temp_status = _git_with_env(
                    repo,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    env=prepared.env,
                )
                if latest_temp_status.strip():
                    return _h4_conflict(
                        prepared,
                        reason="WORKTREE_CHANGED_POST_CAS",
                        observed=_h4_observation(repo),
                    )
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    handle.write(candidate_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(str(lock_path), str(index_path))
                published = True
            else:
                os.close(fd)
                fd = -1
        except OSError as exc:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return _h4_conflict(
                prepared,
                reason=f"INDEX_PUBLICATION_FAILED:{type(exc).__name__}",
            )
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if not published:
                try:
                    current_lock = os.stat(lock_path) if lock_path.exists() else None
                    if (
                        current_lock is not None
                        and current_lock.st_dev == lock_identity.st_dev
                        and current_lock.st_ino == lock_identity.st_ino
                    ):
                        lock_path.unlink()
                except OSError:
                    pass

        final_head = _git_with_env(repo, "rev-parse", "HEAD").strip()
        if final_head != prepared.candidate_commit:
            return _h4_conflict(
                prepared,
                reason="HEAD_CHANGED_DURING_INDEX_FINALIZATION",
                observed={"head": final_head},
            )
        final_tree = _git_with_env(repo, "write-tree").strip()
        final_status = _git_with_env(
            repo, "status", "--porcelain", "--untracked-files=all"
        )
        if final_tree != prepared.candidate_tree or final_status.strip():
            return _h4_conflict(
                prepared,
                reason="FINAL_INDEX_OR_WORKTREE_NOT_CLEAN",
                observed={"head": final_head, "tree": final_tree, "status": final_status},
            )
        return CheckpointFinalization(
            commit_head=prepared.candidate_commit,
            finalization_status="CLEAN",
            clean=True,
            observed_head=final_head,
        )
    except Exception as exc:
        return _h4_conflict(
            prepared,
            reason=f"INDEX_FINALIZATION_FAILED:{type(exc).__name__}",
        )


def _h4_rehydrate_prepared(payload: Mapping[str, Any]) -> PreparedCheckpoint:
    """Rehydrate a STARTED candidate, rebuilding only a lost temp index."""

    required = (
        "attempt_id",
        "snapshot_id",
        "repo_root",
        "branch",
        "branch_ref",
        "expected_head",
        "candidate_commit",
        "candidate_parent",
        "candidate_tree",
        "message",
        "message_digest",
        "real_index_path",
    )
    if not all(isinstance(payload.get(key), str) and payload.get(key) for key in required):
        raise CheckpointCommitError("checkpoint STARTED payload is incomplete")
    repo = _resolved_path(str(payload["repo_root"]), require_exists=True)
    git_dir = _checkpoint_git_dir(repo)
    expected_real_index = _checkpoint_index_path(repo)
    try:
        stored_real_index = Path(str(payload["real_index_path"])).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CheckpointCommitError("checkpoint real index path is invalid") from exc
    if stored_real_index != expected_real_index:
        raise CheckpointCommitError("checkpoint real index path changed")

    temp_path = Path(str(payload.get("temporary_index_path") or ""))
    try:
        temp_resolved = temp_path.resolve(strict=False)
        temp_inside_git = (
            temp_resolved.is_relative_to(git_dir)
            and temp_resolved.parent != git_dir
            and temp_resolved.name == "index"
        )
    except (OSError, RuntimeError, ValueError):
        temp_inside_git = False
    if not temp_inside_git:
        temp_path = git_dir / f".bridge-checkpoint-repair-placeholder-{uuid.uuid4()}"
    rebuilt_temp = not temp_path.exists()
    if rebuilt_temp:
        temp_dir = Path(tempfile.mkdtemp(prefix=".bridge-checkpoint-repair-", dir=str(git_dir)))
        temp_path = temp_dir / "index"
        env = os.environ.copy()
        env.update({"GIT_INDEX_FILE": str(temp_path), "GIT_CONFIG_NOSYSTEM": "1"})
        _git_with_env(repo, "read-tree", str(payload["candidate_tree"]), env=env)
    else:
        temp_dir = temp_path.parent
    env = os.environ.copy()
    env.update(
        {
            "GIT_INDEX_FILE": str(temp_path),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": ":",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": str(payload.get("author_name") or CHECKPOINT_AUTHOR_NAME),
            "GIT_AUTHOR_EMAIL": str(payload.get("author_email") or CHECKPOINT_AUTHOR_EMAIL),
            "GIT_AUTHOR_DATE": str(payload.get("author_timestamp") or "now"),
            "GIT_COMMITTER_NAME": str(payload.get("committer_name") or CHECKPOINT_AUTHOR_NAME),
            "GIT_COMMITTER_EMAIL": str(payload.get("committer_email") or CHECKPOINT_AUTHOR_EMAIL),
            "GIT_COMMITTER_DATE": str(payload.get("committer_timestamp") or "now"),
        }
    )
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise CheckpointCommitError("checkpoint STARTED snapshot is incomplete")
    if snapshot.get("candidate_commit") != payload["candidate_commit"] or snapshot.get(
        "candidate_tree"
    ) != payload["candidate_tree"]:
        raise CheckpointCommitError("checkpoint STARTED candidate metadata is inconsistent")
    temporary_bytes = temp_path.read_bytes() if temp_path.exists() else b""
    temporary_digest = _sha256_bytes(temporary_bytes)
    stored_temporary_digest = str(payload.get("temporary_index_digest") or "")
    if stored_temporary_digest and not rebuilt_temp and temporary_digest != stored_temporary_digest:
        # A lost temporary index is rebuilt above.  If one still exists, a
        # different byte identity indicates an external mutation and must not
        # be silently incorporated.
        raise CheckpointCommitError("temporary checkpoint index changed")
    temporary_entries = _git_bytes_with_env(repo, "ls-files", "--stage", "-z", env=env)
    temporary_entries_digest = _sha256_bytes(temporary_entries)
    stored_entries_digest = str(payload.get("temporary_index_entries_digest") or "")
    if stored_entries_digest and not rebuilt_temp and temporary_entries_digest != stored_entries_digest:
        raise CheckpointCommitError("temporary checkpoint index entries changed")
    if _h4_candidate_index_tree(repo, env) != str(payload["candidate_tree"]):
        raise CheckpointCommitError("temporary checkpoint tree changed")
    details = _h4_commit_details(
        repo,
        str(payload["candidate_commit"]),
        expected_parent=str(payload["candidate_parent"]),
        expected_tree=str(payload["candidate_tree"]),
        expected_message=str(payload["message"]),
        env=env,
    )
    for key in (
        "author_name",
        "author_email",
        "committer_name",
        "committer_email",
    ):
        expected_value = payload.get(key)
        if isinstance(expected_value, str) and details[key] != expected_value:
            raise CheckpointCommitError("checkpoint commit identity changed")
    committed_paths = _checkpoint_commit_paths(
        repo, str(payload["candidate_parent"]), str(payload["candidate_commit"]), env
    )
    expected_paths = tuple(sorted(str(item) for item in payload.get("paths", [])))
    if committed_paths != expected_paths:
        raise CheckpointCommitError("checkpoint candidate paths changed")
    return PreparedCheckpoint(
        attempt_id=str(payload["attempt_id"]),
        snapshot_id=str(payload["snapshot_id"]),
        repo_path=str(repo),
        branch=str(payload["branch"]),
        branch_ref=str(payload["branch_ref"]),
        expected_head=str(payload["expected_head"]),
        candidate_parent=str(payload["candidate_parent"]),
        candidate_tree=str(payload["candidate_tree"]),
        candidate_commit=str(payload["candidate_commit"]),
        paths=tuple(str(item) for item in payload.get("paths", [])),
        message=str(payload["message"]),
        message_digest=str(payload["message_digest"]),
        author_name=str(payload.get("author_name") or CHECKPOINT_AUTHOR_NAME),
        author_email=str(payload.get("author_email") or CHECKPOINT_AUTHOR_EMAIL),
        author_timestamp=str(payload.get("author_timestamp") or "now"),
        committer_name=str(payload.get("committer_name") or CHECKPOINT_AUTHOR_NAME),
        committer_email=str(payload.get("committer_email") or CHECKPOINT_AUTHOR_EMAIL),
        committer_timestamp=str(payload.get("committer_timestamp") or "now"),
        real_index_path=str(payload["real_index_path"]),
        temporary_index_path=str(temp_path),
        temporary_index_digest=temporary_digest,
        temporary_index_entries_digest=temporary_entries_digest,
        temporary_tree=str(payload["candidate_tree"]),
        snapshot=dict(snapshot),
        env=env,
        temp_dir=temp_dir,
    )


def git_checkpoint_commit(
    repo_path: str | os.PathLike[str],
    *,
    postflight: Mapping[str, Any] | GitPostflight,
    message: str,
) -> CheckpointCommitResult:
    """Compatibility wrapper executing PREPARE, CAS and finalization."""

    prepared = git_checkpoint_prepare(repo_path, postflight=postflight, message=message)
    try:
        git_checkpoint_cas(prepared)
        finalization = git_checkpoint_finalize(prepared)
        return CheckpointCommitResult(
            previous_head=prepared.expected_head,
            commit_head=prepared.candidate_commit,
            branch=prepared.branch,
            message=prepared.message,
            paths=prepared.paths,
            clean=finalization.clean,
            attempt_id=prepared.attempt_id,
            snapshot_id=prepared.snapshot_id,
            finalization_status=finalization.finalization_status,
            commit_created=True,
            post_state=finalization.finalization_status,
            conflict=finalization.conflict,
        )
    finally:
        prepared.cleanup()


checkpoint_commit = git_checkpoint_commit
__all__.extend(
    [
        "CHECKPOINT_AUTHOR_EMAIL",
        "CHECKPOINT_AUTHOR_NAME",
        "CHECKPOINT_PHASE_CREATED",
        "CHECKPOINT_PHASE_CAS",
        "CHECKPOINT_PHASE_INDEX",
        "CHECKPOINT_PHASE_PRE_CAS",
        "CHECKPOINT_PHASE_PREPARE",
        "CHECKPOINT_PHASE_REF_UPDATED",
        "CHECKPOINT_PHASE_STARTED",
        "CheckpointFinalization",
        "CheckpointPreconditionError",
        "PreparedCheckpoint",
        "git_checkpoint_prepare",
        "git_checkpoint_cas",
        "git_checkpoint_finalize",
        "git_checkpoint_head_relation",
    ]
)
