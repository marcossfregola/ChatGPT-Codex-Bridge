"""Task execution policy and bounded Git evidence for autonomous writes.

This module deliberately contains policy checks at the Bridge boundary.  It
does not inspect or parse objectives and it does not try to enforce a command
allow-list inside Codex's autonomous sandbox.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Iterable

from .domain.models import TaskMode


_MAX_EVIDENCE_TEXT = 16_384
_MAX_EVIDENCE_PATHS = 256


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


@dataclass(frozen=True)
class CheckpointCommitResult:
    """Evidence returned after one successful local checkpoint commit."""

    previous_head: str
    commit_head: str
    branch: str
    message: str
    paths: tuple[str, ...]
    clean: bool


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
    marker = "[TRUNCATED]"
    return value[: limit - len(marker)] + marker


def _bounded_paths(paths: Iterable[str]) -> tuple[str, ...]:
    values = tuple(paths)
    if len(values) <= _MAX_EVIDENCE_PATHS:
        return values
    return values[:_MAX_EVIDENCE_PATHS] + ("[TRUNCATED]",)


def _bounded_fingerprints(
    fingerprints: Iterable[tuple[str, str, str]],
) -> tuple[tuple[str, str, str], ...]:
    values = tuple(fingerprints)
    if len(values) <= _MAX_EVIDENCE_PATHS:
        return values
    return values[:_MAX_EVIDENCE_PATHS] + (("[TRUNCATED]", "", ""),)


def _evidence_is_complete(value: Any) -> bool:
    """Return whether bounded evidence contains no truncation sentinel."""

    if isinstance(value, str):
        return not value.endswith("[TRUNCATED]")
    if isinstance(value, (list, tuple)):
        return not any(
            item == "[TRUNCATED]"
            or (
                isinstance(item, (list, tuple))
                and item
                and item[0] == "[TRUNCATED]"
            )
            for item in value
        )
    return True


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
            "timeout": 15,
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
            timeout=15,
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


def _parse_status(status: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
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
    return _bounded_paths(staged), _bounded_paths(unstaged), _bounded_paths(untracked)


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
    return _bounded_fingerprints(entries)


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
    staged, unstaged, untracked = _parse_status(status)
    diff = _git_with_env(repo, "diff", env=env) if include_diff else ""
    cached_diff = (
        _git_with_env(repo, "diff", "--cached", env=env) if include_diff else ""
    )
    fingerprints = (
        _content_fingerprints(repo, staged, unstaged, untracked, env=env)
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
        if path == "[TRUNCATED]":
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
    if not _evidence_is_complete(current_status) or not _evidence_is_complete(
        current_diff
    ) or not _evidence_is_complete(current_cached_diff):
        raise ContinuationBaselineError("current Git evidence is too large to compare")
    if not _evidence_is_complete(current_changed) or not _evidence_is_complete(
        untracked
    ) or not _evidence_is_complete(fingerprints):
        raise ContinuationBaselineError("current Git paths are too numerous to compare")
    expected_staged, expected_unstaged, expected_untracked_paths = _parse_status(
        expected_status
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


def git_checkpoint_commit(
    repo_path: str | os.PathLike[str],
    *,
    postflight: Mapping[str, Any] | GitPostflight,
    message: str,
) -> CheckpointCommitResult:
    """Create one exact local commit from durable autonomous-write evidence.

    All mutable index operations use a temporary index.  The caller's index
    and worktree therefore remain byte-for-byte unchanged on every failure
    before HEAD moves.
    """

    if not isinstance(message, str) or not message.strip():
        raise CheckpointCommitError("commit message must be non-empty text")
    if "\x00" in message:
        raise CheckpointCommitError("commit message contains NUL")
    if isinstance(postflight, GitPostflight):
        postflight = postflight_payload(postflight)
    repo = _resolved_path(repo_path, require_exists=True)
    _validate_worktree_root(repo)
    previous_head, branch, paths, _ = _checkpoint_expected_state(repo, postflight)

    git_dir = _checkpoint_git_dir(repo)
    actual_index = _checkpoint_index_path(repo)
    temp_dir = Path(tempfile.mkdtemp(prefix=".bridge-checkpoint-", dir=str(git_dir)))
    temp_index = temp_dir / "index"
    hooks_dir = temp_dir / "hooks"
    hooks_dir.mkdir()
    original_index_exists = actual_index.exists()
    original_index = actual_index.read_bytes() if original_index_exists else None
    if original_index_exists:
        shutil.copyfile(actual_index, temp_index)
    env = os.environ.copy()
    env.update(
        {
            "GIT_INDEX_FILE": str(temp_index),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": ":",
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
        (
            staged_branch,
            staged_head,
            staged_status,
            staged_paths,
            staged_unstaged,
            staged_untracked,
            staged_diff,
            staged_cached_diff,
            staged_fingerprints,
        ) = _git_state(
            repo,
            include_diff=True,
            include_fingerprints=True,
            env=env,
        )
        if (
            staged_branch != branch
            or staged_head != previous_head
            or staged_unstaged
            or staged_untracked
            or tuple(sorted(set(staged_paths))) != paths
            or not staged_cached_diff
            or staged_diff
        ):
            raise CheckpointCommitError("staged Git state is not the verified checkpoint")
        _checkpoint_stage_matches_worktree(repo, paths, env)
        if not _evidence_is_complete(staged_fingerprints):
            raise CheckpointCommitError("staged Git evidence was truncated")
        precommit_branch = _git_branch(repo, env=env)
        precommit_head = _git_with_env(repo, "rev-parse", "HEAD", env=env).strip()
        if precommit_branch != branch or precommit_head != previous_head:
            raise CheckpointCommitError("Git branch or HEAD changed before commit")

        commit_args = (
            "-c",
            "user.name=Marcos Sfregola",
            "-c",
            "user.email=marcos.sfregola@gmail.com",
            "-c",
            f"core.hooksPath={hooks_dir}",
            "-c",
            "commit.gpgSign=false",
            "--no-pager",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            message,
        )
        _git_with_env(repo, *commit_args, env=env)
        commit_head = _git_with_env(repo, "rev-parse", "HEAD", env=env).strip()
        if not commit_head or commit_head == previous_head:
            raise CheckpointCommitError("checkpoint did not create a new HEAD")
        actual_head, _, _, _, actual_message = _checkpoint_commit_metadata(
            repo, previous_head, commit_head, env
        )
        if actual_message != message:
            raise CheckpointCommitError("checkpoint commit message is invalid")
        committed_paths = _checkpoint_commit_paths(repo, previous_head, commit_head, env)
        if committed_paths != paths:
            raise CheckpointCommitError("checkpoint commit paths are invalid")
        (
            final_branch,
            final_head,
            final_status,
            _,
            final_unstaged,
            final_untracked,
            final_diff,
            final_cached_diff,
            _,
        ) = _git_state(repo, include_diff=True, include_fingerprints=True, env=env)
        if (
            final_branch != branch
            or final_head != commit_head
            or final_status.strip()
            or final_unstaged
            or final_untracked
            or final_diff
            or final_cached_diff
        ):
            raise CheckpointCommitError("checkpoint postcommit Git state is not clean")
        if actual_index.exists() != original_index_exists or (
            original_index_exists and actual_index.read_bytes() != original_index
        ):
            raise CheckpointCommitError("Git index changed concurrently during commit")
        try:
            os.replace(temp_index, actual_index)
        except OSError as exc:
            # HEAD has already advanced.  Leave the real index/worktree as-is
            # and surface a recoverable, non-destructive failure.
            raise CheckpointCommitError(
                "checkpoint commit created but real Git index could not be installed"
            ) from exc
        (
            installed_branch,
            installed_head,
            installed_status,
            _,
            installed_unstaged,
            installed_untracked,
            installed_diff,
            installed_cached_diff,
            _,
        ) = _git_state(repo, include_diff=True, include_fingerprints=True)
        if (
            installed_branch != branch
            or installed_head != commit_head
            or installed_status.strip()
            or installed_unstaged
            or installed_untracked
            or installed_diff
            or installed_cached_diff
        ):
            raise CheckpointCommitError("installed Git index is not clean")
        return CheckpointCommitResult(
            previous_head=previous_head,
            commit_head=actual_head,
            branch=branch,
            message=message,
            paths=paths,
            clean=True,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# Short public alias for callers that keep Git policy operations behind a
# generic checkpoint namespace.
checkpoint_commit = git_checkpoint_commit


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
    "git_continuation_preflight",
    "git_checkpoint_commit",
    "git_preflight",
    "postflight_payload",
    "protected_roots",
]
