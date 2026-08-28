"""Windows READ_ONLY access preflight for a repository's real Git index.

The Bridge runs outside Codex's restricted READ_ONLY identity.  On Windows it
may therefore grant that identity one explicit, read-only ACE on the *real*
Git index when the repository's DACL is otherwise safe to preserve.  The
implementation deliberately uses only the OWNER/GROUP/DACL security
information classes exposed by pywin32; SACLs are never queried or written.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any, Literal

from .policy import PolicyError


READ_ONLY_INDEX_OUTCOMES = frozenset({"already_readable", "corrected", "noop"})
READ_ONLY_INDEX_FAILURE_REASONS = frozenset(
    {
        "sandbox_group_missing",
        "sandbox_group_not_local",
        "git_metadata_mismatch",
        "git_metadata_unknown",
        "index_unreadable",
        "acl_unreadable",
        "unsupported_ace",
        "conflicting_deny",
        "acl_rebuild_failed",
        "concurrent_change",
        "write_failed",
        "post_write_verification_failed",
        "rollback_failed",
        "internal_error",
    }
)


@dataclass(frozen=True)
class ReadOnlyGitIndexResult:
    """Bounded result from the READ_ONLY Git-index preflight."""

    outcome: Literal["already_readable", "corrected", "noop"]
    reason: str

    def __post_init__(self) -> None:
        if self.outcome not in READ_ONLY_INDEX_OUTCOMES:
            raise ValueError(f"invalid READ_ONLY index outcome: {self.outcome!r}")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("READ_ONLY index reason must be non-empty text")


class ReadOnlyGitIndexError(PolicyError):
    """Fail-closed READ_ONLY index preflight error with safe audit fields."""

    def __init__(self, reason: str, *, rollback_status: str | None = None) -> None:
        if reason not in READ_ONLY_INDEX_FAILURE_REASONS:
            reason = "internal_error"
        if rollback_status not in {None, "rollback_verified", "rollback_failed"}:
            rollback_status = "rollback_failed"
        self.outcome = "failed"
        self.reason = reason
        self.rollback_status = rollback_status
        detail = f"; {rollback_status}" if rollback_status is not None else ""
        # Keep the exception text bounded and path-free.  Core persists this
        # text in its generic failure event as well as the dedicated event.
        super().__init__(f"read-only Git index preflight failed: {reason}{detail}")


@dataclass(frozen=True)
class _AceSnapshot:
    """The complete structured form of one ordinary access ACE."""

    ace_type: int
    ace_flags: int
    mask: int
    sid_bytes: bytes

    @property
    def key(self) -> tuple[int, int, int, bytes]:
        return (self.ace_type, self.ace_flags, self.mask, self.sid_bytes)


@dataclass(frozen=True)
class _SecuritySnapshot:
    """OWNER/GROUP/DACL state sufficient for exact normal restoration."""

    owner_bytes: bytes | None
    group_bytes: bytes | None
    control: int
    control_revision: int
    dacl_present: bool
    dacl_is_null: bool
    dacl_defaulted: bool
    acl_revision: int
    acl_size: int
    aces: tuple[_AceSnapshot, ...]
    descriptor_bytes: bytes

    @property
    def dacl_control(self) -> int:
        # These are the DACL state bits.  SACL control bits are deliberately
        # excluded because this feature never reads or writes SACLs.
        return self.control & _DACL_CONTROL_MASK


@dataclass(frozen=True)
class _IndexFingerprint:
    sha256: str
    length: int


@dataclass(frozen=True)
class _GitIndexLocation:
    repo: Path
    index: Path


_DACL_CONTROL_MASK = 0x00000004 | 0x00000008 | 0x00000400 | 0x00001000
_ACE_ALLOWED = 0
_ACE_DENIED = 1
_ACE_INHERIT_ONLY = 0x08
_ACL_REVISION = 2


def _load_win32() -> tuple[Any, Any, Any]:
    """Import pywin32 lazily so importing the Bridge stays portable."""

    import ntsecuritycon
    import win32api
    import win32security

    return win32security, ntsecuritycon, win32api


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _resolve_path(value: str | os.PathLike[str], *, strict: bool) -> Path:
    return Path(value).resolve(strict=strict)


def _git_command(repo: Path, *args: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return completed.returncode, completed.stdout.strip()


def _resolve_git_output(repo: Path, value: str, *, require_exists: bool) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repo / candidate
    return candidate.resolve(strict=require_exists)


def _locate_real_index(repo_path: str | os.PathLike[str]) -> _GitIndexLocation | None:
    """Locate the index and prove it belongs to this Project's Git metadata."""

    try:
        repo = _resolve_path(repo_path, strict=True)
    except (OSError, RuntimeError):
        return None
    if not repo.is_dir():
        return None

    show_code, show_top = _git_command(repo, "rev-parse", "--show-toplevel")
    if show_code != 0 or not show_top:
        # A directory that is not a Git worktree is a safe no-op.  A command
        # that did identify a worktree but then failed below is handled as an
        # unknown metadata state instead of guessing a path.
        return None
    try:
        top_level = _resolve_git_output(repo, show_top, require_exists=True)
    except (OSError, RuntimeError):
        raise ReadOnlyGitIndexError("git_metadata_unknown")
    if _path_key(top_level) != _path_key(repo):
        raise ReadOnlyGitIndexError("git_metadata_mismatch")

    git_dir_code, git_dir_text = _git_command(repo, "rev-parse", "--git-dir")
    common_code, common_dir_text = _git_command(repo, "rev-parse", "--git-common-dir")
    index_code, index_text = _git_command(repo, "rev-parse", "--git-path", "index")
    if git_dir_code != 0 or index_code != 0 or not git_dir_text or not index_text:
        raise ReadOnlyGitIndexError("git_metadata_unknown")
    try:
        git_dir = _resolve_git_output(repo, git_dir_text, require_exists=True)
        common_dir = (
            _resolve_git_output(repo, common_dir_text, require_exists=True)
            if common_code == 0 and common_dir_text
            else git_dir
        )
        # Resolve lexically even when an empty/new repository has no index
        # yet; that case is an explicit safe no-op rather than metadata error.
        index = _resolve_git_output(repo, index_text, require_exists=False)
    except (OSError, RuntimeError):
        raise ReadOnlyGitIndexError("git_metadata_unknown")
    if not git_dir.is_dir() or not common_dir.is_dir() or not index.is_file():
        # An empty/new repository may legitimately have no index yet.
        if not index.exists():
            return None
        raise ReadOnlyGitIndexError("git_metadata_unknown")

    try:
        in_git_dir = index.is_relative_to(git_dir)
        in_common_dir = index.is_relative_to(common_dir)
    except AttributeError:  # pragma: no cover - Python 3.13 always has this
        in_git_dir = str(index).startswith(str(git_dir) + os.sep)
        in_common_dir = str(index).startswith(str(common_dir) + os.sep)
    if not (in_git_dir or in_common_dir):
        raise ReadOnlyGitIndexError("git_metadata_mismatch")
    return _GitIndexLocation(repo=repo, index=index)


def _fingerprint(path: Path) -> _IndexFingerprint:
    try:
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ReadOnlyGitIndexError("index_unreadable") from exc
    return _IndexFingerprint(hashlib.sha256(data).hexdigest(), len(data))


def _sid_bytes(sid: Any) -> bytes:
    try:
        value = bytes(sid)
    except (TypeError, ValueError) as exc:
        raise ReadOnlyGitIndexError("acl_unreadable") from exc
    if not value:
        raise ReadOnlyGitIndexError("acl_unreadable")
    return value


def _parse_ace(ace: Any) -> _AceSnapshot:
    """Parse only ordinary allow/deny ACEs; reject everything else."""

    if not isinstance(ace, tuple) or len(ace) != 3:
        raise ReadOnlyGitIndexError("unsupported_ace")
    header, mask, sid = ace
    if (
        not isinstance(header, tuple)
        or len(header) != 2
        or not isinstance(header[0], int)
        or not isinstance(header[1], int)
        or header[0] not in {_ACE_ALLOWED, _ACE_DENIED}
        or not isinstance(mask, int)
    ):
        raise ReadOnlyGitIndexError("unsupported_ace")
    return _AceSnapshot(header[0], header[1], mask, _sid_bytes(sid))


def _capture_security(path: Path) -> _SecuritySnapshot:
    win32security, _ntsecuritycon, _win32api = _load_win32()
    info = (
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.GROUP_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
    )
    try:
        descriptor = win32security.GetFileSecurity(str(path), info)
        owner = descriptor.GetSecurityDescriptorOwner()
        group = descriptor.GetSecurityDescriptorGroup()
        control, control_revision = descriptor.GetSecurityDescriptorControl()
        dacl = descriptor.GetSecurityDescriptorDacl()
        descriptor_bytes = bytes(descriptor)
    except Exception as exc:
        raise ReadOnlyGitIndexError("acl_unreadable") from exc

    try:
        control = int(control)
        control_revision = int(control_revision)
        owner_bytes = None if owner is None else _sid_bytes(owner)
        group_bytes = None if group is None else _sid_bytes(group)
        if dacl is None:
            acl_revision = _ACL_REVISION
            acl_size = 0
            aces: tuple[_AceSnapshot, ...] = ()
        else:
            acl_revision = int(dacl.GetAclRevision())
            acl_size = int(dacl.GetAclSize())
            aces = tuple(
                _parse_ace(dacl.GetAce(index))
                for index in range(int(dacl.GetAceCount()))
            )
    except ReadOnlyGitIndexError:
        raise
    except Exception as exc:
        raise ReadOnlyGitIndexError("acl_unreadable") from exc
    return _SecuritySnapshot(
        owner_bytes=owner_bytes,
        group_bytes=group_bytes,
        control=control,
        control_revision=control_revision,
        dacl_present=bool(control & win32security.SE_DACL_PRESENT),
        dacl_is_null=dacl is None,
        dacl_defaulted=bool(control & win32security.SE_DACL_DEFAULTED),
        acl_revision=acl_revision,
        acl_size=acl_size,
        aces=aces,
        descriptor_bytes=descriptor_bytes,
    )


def _sid_from_bytes(win32security: Any, value: bytes | None) -> Any:
    return None if value is None else win32security.SID(value)


def _build_acl(snapshot: _SecuritySnapshot, group_sid_bytes: bytes | None = None) -> Any:
    """Rebuild an ACL while retaining every ordinary ACE in its old order."""

    win32security, _ntsecuritycon, _win32api = _load_win32()
    try:
        # The extra capacity is bounded to the exact existing ACL plus one
        # ordinary ACE; no arbitrary large allocation is needed.
        extra = 64 + (len(group_sid_bytes) if group_sid_bytes is not None else 0)
        acl = win32security.ACL(
            max(64, snapshot.acl_size + extra, len(snapshot.aces) * 80 + extra),
            snapshot.acl_revision,
        )
        for ace in snapshot.aces:
            sid = win32security.SID(ace.sid_bytes)
            if ace.ace_type == _ACE_ALLOWED:
                acl.AddAccessAllowedAceEx(
                    snapshot.acl_revision, ace.ace_flags, ace.mask, sid
                )
            elif ace.ace_type == _ACE_DENIED:
                acl.AddAccessDeniedAceEx(
                    snapshot.acl_revision, ace.ace_flags, ace.mask, sid
                )
            else:  # defensive; _parse_ace already rejects this
                raise ReadOnlyGitIndexError("unsupported_ace")
    except ReadOnlyGitIndexError:
        raise
    except Exception as exc:
        raise ReadOnlyGitIndexError("acl_rebuild_failed") from exc
    return acl


def _dacl_for_snapshot(snapshot: _SecuritySnapshot) -> Any:
    return _build_acl(snapshot)


def _append_read_ace(snapshot: _SecuritySnapshot, group_sid_bytes: bytes) -> Any:
    win32security, ntsecuritycon, _win32api = _load_win32()
    acl = _build_acl(snapshot, group_sid_bytes)
    try:
        group_sid = win32security.SID(group_sid_bytes)
        acl.AddAccessAllowedAceEx(
            snapshot.acl_revision,
            0,
            int(ntsecuritycon.FILE_GENERIC_READ),
            group_sid,
        )
    except Exception as exc:
        raise ReadOnlyGitIndexError("acl_rebuild_failed") from exc
    return acl


def _well_known_sids(win32security: Any) -> set[bytes]:
    values = set()
    for kind in (
        win32security.WinWorldSid,
        win32security.WinAuthenticatedUserSid,
        win32security.WinBuiltinUsersSid,
        win32security.WinLocalSid,
        win32security.WinInteractiveSid,
    ):
        try:
            values.add(bytes(win32security.CreateWellKnownSid(kind, None)))
        except Exception:
            # A missing well-known SID is not an unsafe ACL condition; the
            # direct local-group SID remains mandatory below.
            continue
    # Local interactive accounts also carry these well-known token groups on
    # current Windows builds.  Include them when evaluating DENY applicability
    # so a broad deny cannot be bypassed by adding the direct group ACE.
    for text_sid in (
        "S-1-2-1",  # Console Logon
        "S-1-5-15",  # This Organization
        "S-1-5-64-10",  # NTLM Authentication
        "S-1-5-113",  # Local Account
    ):
        try:
            values.add(bytes(win32security.ConvertStringSidToSid(text_sid)))
        except Exception:
            continue
    return values


def _resolve_sandbox_group() -> bytes:
    win32security, _ntsecuritycon, win32api = _load_win32()
    try:
        sid, domain, account_type = win32security.LookupAccountName(
            None, "CodexSandboxUsers"
        )
    except Exception as exc:
        raise ReadOnlyGitIndexError("sandbox_group_missing") from exc
    if account_type != win32security.SidTypeAlias:
        raise ReadOnlyGitIndexError("sandbox_group_not_local")
    try:
        computer = win32api.GetComputerName()
    except Exception as exc:
        raise ReadOnlyGitIndexError("sandbox_group_not_local") from exc
    if not isinstance(domain, str) or domain.casefold() != str(computer).casefold():
        raise ReadOnlyGitIndexError("sandbox_group_not_local")
    try:
        value = bytes(sid)
    except (TypeError, ValueError) as exc:
        raise ReadOnlyGitIndexError("sandbox_group_missing") from exc
    if not value:
        raise ReadOnlyGitIndexError("sandbox_group_missing")
    return value


def _expanded_read_mask(mask: int, ntsecuritycon: Any) -> int:
    """Map generic read/all bits for conservative deny/allow analysis."""

    value = int(mask)
    if value & 0x80000000:  # GENERIC_READ
        value |= int(ntsecuritycon.FILE_GENERIC_READ)
    if value & 0x10000000:  # GENERIC_ALL
        value |= int(ntsecuritycon.FILE_GENERIC_READ)
    return value


def _has_sufficient_read(snapshot: _SecuritySnapshot, group_sid: bytes) -> bool:
    if snapshot.dacl_is_null or not snapshot.dacl_present:
        # A NULL/absent DACL grants access to everyone.  It is already
        # readable and must not be replaced with an explicit allow ACE.
        return True
    win32security, ntsecuritycon, _win32api = _load_win32()
    applicable = _well_known_sids(win32security)
    applicable.add(group_sid)
    minimum = int(ntsecuritycon.FILE_GENERIC_READ)
    allowed = 0
    for ace in snapshot.aces:
        if ace.sid_bytes not in applicable:
            continue
        if ace.ace_flags & _ACE_INHERIT_ONLY:
            # INHERIT_ONLY ACEs are preserved but do not apply to this file;
            # treating one as effective would create a false already-readable
            # result or a false deny.
            continue
        mask = _expanded_read_mask(ace.mask, ntsecuritycon)
        if ace.ace_type == _ACE_DENIED and mask & minimum:
            raise ReadOnlyGitIndexError("conflicting_deny")
        if ace.ace_type == _ACE_ALLOWED:
            allowed |= mask
    return (allowed & minimum) == minimum


def _ace_keys_from_acl(acl: Any) -> tuple[tuple[int, int, int, bytes], ...]:
    try:
        return tuple(_parse_ace(acl.GetAce(i)).key for i in range(acl.GetAceCount()))
    except ReadOnlyGitIndexError:
        raise
    except Exception as exc:
        raise ReadOnlyGitIndexError("acl_unreadable") from exc


def _original_sequence_is_preserved(
    original: tuple[_AceSnapshot, ...], actual: tuple[tuple[int, int, int, bytes], ...]
) -> bool:
    """Check exact old ACE multiset/order as a subsequence after one insert."""

    position = 0
    for ace in original:
        try:
            position = actual.index(ace.key, position) + 1
        except ValueError:
            return False
    return True


def _capture_and_verify_after_write(
    path: Path,
    original: _SecuritySnapshot,
    before_index: _IndexFingerprint,
    group_sid: bytes,
) -> None:
    current = _capture_security(path)
    if current.owner_bytes != original.owner_bytes or current.group_bytes != original.group_bytes:
        raise ReadOnlyGitIndexError("post_write_verification_failed")
    if current.control_revision != original.control_revision:
        raise ReadOnlyGitIndexError("post_write_verification_failed")
    if current.dacl_control != original.dacl_control:
        raise ReadOnlyGitIndexError("post_write_verification_failed")
    if (
        current.dacl_present != original.dacl_present
        or current.dacl_is_null != original.dacl_is_null
        or current.dacl_defaulted != original.dacl_defaulted
    ):
        raise ReadOnlyGitIndexError("post_write_verification_failed")
    if current.acl_revision != original.acl_revision:
        raise ReadOnlyGitIndexError("post_write_verification_failed")
    if len(current.aces) != len(original.aces) + 1:
        raise ReadOnlyGitIndexError("post_write_verification_failed")
    actual = tuple(ace.key for ace in current.aces)
    _win32security, ntsecuritycon, _win32api = _load_win32()
    new_key = (
        _ACE_ALLOWED,
        0,
        int(ntsecuritycon.FILE_GENERIC_READ),
        group_sid,
    )
    if actual.count(new_key) < 1 or not _original_sequence_is_preserved(original.aces, actual):
        raise ReadOnlyGitIndexError("post_write_verification_failed")
    # The newly introduced mask itself is the only permission this operation
    # can grant; make the no-write invariant explicit at verification time.
    write_bits = (
        int(ntsecuritycon.FILE_WRITE_DATA)
        | int(ntsecuritycon.FILE_APPEND_DATA)
        | int(ntsecuritycon.FILE_WRITE_EA)
        | int(ntsecuritycon.FILE_WRITE_ATTRIBUTES)
        | int(ntsecuritycon.DELETE)
        | int(ntsecuritycon.WRITE_DAC)
        | int(ntsecuritycon.WRITE_OWNER)
        | 0x40000000  # GENERIC_WRITE
        | 0x10000000  # GENERIC_ALL
    )
    if int(ntsecuritycon.FILE_GENERIC_READ) & write_bits:
        raise ReadOnlyGitIndexError("post_write_verification_failed")
    after_index = _fingerprint(path)
    if after_index != before_index:
        raise ReadOnlyGitIndexError("post_write_verification_failed")


def _set_dacl(path: Path, snapshot: _SecuritySnapshot, dacl: Any) -> None:
    win32security, _ntsecuritycon, _win32api = _load_win32()
    try:
        # SetFileSecurity with a self-relative descriptor changes only the
        # DACL and does not ask Windows to reapply parent inheritance.  For an
        # auto-inherited descriptor Windows preserves that state through the
        # named API, whereas SetFileSecurity would clear the auto-inherited
        # control bit; use the corresponding API deliberately.
        if snapshot.control & int(win32security.SE_DACL_AUTO_INHERITED):
            win32security.SetNamedSecurityInfo(
                str(path),
                win32security.SE_FILE_OBJECT,
                int(win32security.DACL_SECURITY_INFORMATION),
                None,
                None,
                dacl,
                None,
            )
        else:
            descriptor = win32security.SECURITY_DESCRIPTOR(snapshot.descriptor_bytes)
            descriptor.SetSecurityDescriptorDacl(
                1,
                dacl,
                1 if snapshot.dacl_defaulted else 0,
            )
            win32security.SetFileSecurity(
                str(path), int(win32security.DACL_SECURITY_INFORMATION), descriptor
            )
    except Exception as exc:
        raise ReadOnlyGitIndexError("write_failed") from exc


def _restore_snapshot(path: Path, snapshot: _SecuritySnapshot) -> None:
    """Restore DACL first, then owner/group only if an external change moved them."""

    win32security, _ntsecuritycon, _win32api = _load_win32()
    dacl = _dacl_for_snapshot(snapshot)
    try:
        _set_dacl(path, snapshot, dacl)
        restored = _capture_security(path)
        if restored.owner_bytes == snapshot.owner_bytes and restored.group_bytes == snapshot.group_bytes:
            return
        # The normal operation never supplies owner/group.  This branch is a
        # defensive attempt for a concurrent external owner/group mutation.
        owner = _sid_from_bytes(win32security, snapshot.owner_bytes)
        group = _sid_from_bytes(win32security, snapshot.group_bytes)
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            int(win32security.OWNER_SECURITY_INFORMATION)
            | int(win32security.GROUP_SECURITY_INFORMATION)
            | int(win32security.DACL_SECURITY_INFORMATION),
            owner,
            group,
            dacl,
            None,
        )
    except Exception as exc:
        raise ReadOnlyGitIndexError("rollback_failed") from exc


def _verify_restored(path: Path, snapshot: _SecuritySnapshot, before_index: _IndexFingerprint) -> None:
    restored = _capture_security(path)
    if restored.owner_bytes != snapshot.owner_bytes or restored.group_bytes != snapshot.group_bytes:
        raise ReadOnlyGitIndexError("rollback_failed")
    if restored.control_revision != snapshot.control_revision:
        raise ReadOnlyGitIndexError("rollback_failed")
    if restored.dacl_control != snapshot.dacl_control:
        raise ReadOnlyGitIndexError("rollback_failed")
    if (
        restored.dacl_present != snapshot.dacl_present
        or restored.dacl_is_null != snapshot.dacl_is_null
        or restored.dacl_defaulted != snapshot.dacl_defaulted
    ):
        raise ReadOnlyGitIndexError("rollback_failed")
    if restored.acl_revision != snapshot.acl_revision:
        raise ReadOnlyGitIndexError("rollback_failed")
    if tuple(ace.key for ace in restored.aces) != tuple(ace.key for ace in snapshot.aces):
        raise ReadOnlyGitIndexError("rollback_failed")
    if restored.descriptor_bytes != snapshot.descriptor_bytes:
        raise ReadOnlyGitIndexError("rollback_failed")
    if _fingerprint(path) != before_index:
        raise ReadOnlyGitIndexError("rollback_failed")


def preflight_read_only_git_index(
    repo_path: str | os.PathLike[str],
) -> ReadOnlyGitIndexResult:
    """Ensure CodexSandboxUsers can read a Git index, or return a safe no-op.

    This function is intentionally a no-op on non-Windows.  On Windows it
    returns ``already_readable`` or ``corrected`` for an existing index and
    raises :class:`ReadOnlyGitIndexError` for every unsafe/unknown condition.
    """

    if os.name != "nt":
        return ReadOnlyGitIndexResult("noop", "non_windows")

    try:
        location = _locate_real_index(repo_path)
        if location is None:
            return ReadOnlyGitIndexResult("noop", "not_git_or_index_missing")
        group_sid = _resolve_sandbox_group()
        original = _capture_security(location.index)
        if _has_sufficient_read(original, group_sid):
            return ReadOnlyGitIndexResult("already_readable", "sufficient_allow")
        # Inspect the DACL before opening the index.  A conflicting deny must
        # be reported as such even when that deny would also block a generic
        # file read by the Bridge identity.
        before_index = _fingerprint(location.index)

        # Re-read both the ACL and index immediately before writing.  This
        # closes the check/use race without broadening the allowed mutation.
        current = _capture_security(location.index)
        current_index = _fingerprint(location.index)
        if current.descriptor_bytes != original.descriptor_bytes or current_index != before_index:
            raise ReadOnlyGitIndexError("concurrent_change")
        new_dacl = _append_read_ace(original, group_sid)
        try:
            _set_dacl(location.index, original, new_dacl)
            _capture_and_verify_after_write(
                location.index, original, before_index, group_sid
            )
        except Exception as verification_error:
            try:
                _restore_snapshot(location.index, original)
                _verify_restored(location.index, original, before_index)
            except ReadOnlyGitIndexError as rollback_error:
                raise ReadOnlyGitIndexError(
                    "rollback_failed", rollback_status="rollback_failed"
                ) from rollback_error
            except Exception as rollback_error:
                raise ReadOnlyGitIndexError(
                    "rollback_failed", rollback_status="rollback_failed"
                ) from rollback_error
            failure_reason = (
                verification_error.reason
                if isinstance(verification_error, ReadOnlyGitIndexError)
                and verification_error.reason == "write_failed"
                else "post_write_verification_failed"
            )
            raise ReadOnlyGitIndexError(
                failure_reason, rollback_status="rollback_verified"
            ) from verification_error
        return ReadOnlyGitIndexResult("corrected", "added_minimum_read")
    except ReadOnlyGitIndexError:
        raise
    except Exception as exc:
        # Do not leak OS errors, paths, or account details into durable
        # telemetry.  Unknown implementation states fail closed.
        raise ReadOnlyGitIndexError("internal_error") from exc


__all__ = [
    "ReadOnlyGitIndexError",
    "ReadOnlyGitIndexResult",
    "preflight_read_only_git_index",
]
