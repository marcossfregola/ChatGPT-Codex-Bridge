"""Task execution policy and bounded Git evidence for autonomous writes.

This module deliberately contains policy checks at the Bridge boundary.  It
does not inspect or parse objectives and it does not try to enforce a command
allow-list inside Codex's autonomous sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
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


def git_preflight(repo_path: str | os.PathLike[str]) -> GitCheckpoint:
    """Require a clean Git worktree and capture its durable baseline."""

    repo = _resolved_path(repo_path, require_exists=True)
    top_level = _resolved_path(
        _git(repo, "rev-parse", "--show-toplevel").strip(), require_exists=True
    )
    if _path_key(top_level) != _path_key(repo):
        raise GitPreflightError("repo_path is not the Git worktree root")
    branch = _git_branch(repo)
    head = _git(repo, "rev-parse", "HEAD").strip()
    if not head:
        raise GitPreflightError("Git HEAD is empty")
    status = _git(repo, "status", "--porcelain")
    staged, unstaged, untracked = _parse_status(status)
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
    )


def git_postflight(checkpoint: GitCheckpoint) -> GitPostflight:
    """Collect bounded Git evidence and classify branch/HEAD changes."""

    repo = Path(checkpoint.repo_path)
    final_branch = _git_branch(repo, allow_detached=True)
    final_head = _git(repo, "rev-parse", "HEAD").strip()
    status = _git(repo, "status", "--porcelain")
    diff = _git(repo, "diff")
    cached_diff = _git(repo, "diff", "--cached")
    staged, unstaged, untracked = _parse_status(status)
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
    }


__all__ = [
    "AUTONOMOUS_WRITE_CONTRACT",
    "DirtyWorkingTreeError",
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
    "git_preflight",
    "postflight_payload",
    "protected_roots",
]
