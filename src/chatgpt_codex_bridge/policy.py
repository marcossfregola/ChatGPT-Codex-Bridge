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
import subprocess
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


def _git(repo: Path, *args: str, allow_failure: bool = False) -> str:
    command = ("git", "-C", str(repo), *args)
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if allow_failure:
            return ""
        raise GitPreflightError("unable to execute Git") from exc
    if completed.returncode != 0 and not allow_failure:
        raise _GitCommandFailure(command, completed.stderr)
    return completed.stdout


def _git_bytes(repo: Path, *args: str) -> bytes:
    """Run Git without decoding stdout, for binary-safe index reads."""

    command = ("git", "-C", str(repo), *args)
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitPreflightError("unable to execute Git") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        raise _GitCommandFailure(command, stderr)
    return completed.stdout


def _git_branch(repo: Path, *, allow_detached: bool = False) -> str:
    try:
        return _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").strip()
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


def _index_digest(repo: Path, path: str) -> str | None:
    """Read a staged blob from the index without decoding binary content."""

    try:
        content = _git_bytes(repo, "cat-file", "blob", f":{path}")
    except _GitCommandFailure as error:
        # A staged deletion has no index entry and therefore no content to
        # hash.  Other failures remain fatal instead of silently weakening the
        # continuation check.  Gitlinks are commit objects, not file content.
        entries = _git(repo, "ls-files", "--stage", "--", path)
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
) -> tuple[tuple[str, str, str], ...]:
    """Capture bounded SHA-256 evidence for every dirty file with content."""

    entries: list[tuple[str, str, str]] = []
    for path in staged:
        digest = _index_digest(repo, path)
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


def _validate_worktree_root(repo: Path) -> None:
    top_level = _resolved_path(
        _git(repo, "rev-parse", "--show-toplevel").strip(), require_exists=True
    )
    if _path_key(top_level) != _path_key(repo):
        raise GitPreflightError("repo_path is not the Git worktree root")


def _git_state(
    repo: Path,
    *,
    allow_detached: bool = False,
    include_diff: bool = False,
    include_fingerprints: bool = False,
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
    _validate_worktree_root(repo)
    branch = _git_branch(repo, allow_detached=allow_detached)
    head = _git(repo, "rev-parse", "HEAD").strip()
    if not head:
        raise GitPreflightError("Git HEAD is empty")
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    staged, unstaged, untracked = _parse_status(status)
    diff = _git(repo, "diff") if include_diff else ""
    cached_diff = _git(repo, "diff", "--cached") if include_diff else ""
    fingerprints = (
        _content_fingerprints(repo, staged, unstaged, untracked)
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


__all__ = [
    "AUTONOMOUS_WRITE_CONTRACT",
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
    "ensure_autonomous_workspace",
    "git_postflight",
    "git_continuation_preflight",
    "git_preflight",
    "postflight_payload",
    "protected_roots",
]
