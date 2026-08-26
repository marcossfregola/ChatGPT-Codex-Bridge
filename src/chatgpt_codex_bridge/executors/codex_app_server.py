"""Small stdlib-only client for the Codex app-server JSON-line boundary."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Deque

from .. import THREAD_SOURCE


JsonObject = dict[str, Any]
NotificationObserver = Callable[[str, JsonObject], None]


class AppServerError(RuntimeError):
    """Base class for errors raised by the minimal app-server client."""


class ProtocolError(AppServerError):
    """Raised when an app-server message is invalid or unexpected."""


class ExecutableResolutionError(AppServerError):
    """Raised when no usable Codex executable can be resolved."""


class ExecutableLaunchError(AppServerError):
    """Raised when the resolved executable cannot be started."""


class ServerRequestError(AppServerError):
    """Raised for an unexpected server request that must not be auto-approved."""


def decode_json_line(line: bytes | str) -> JsonObject:
    """Decode one JSON-line message and require a JSON object."""

    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid JSON received from app-server") from exc
    if not isinstance(message, dict):
        raise ProtocolError("app-server JSON message must be an object")
    return message


def classify_message(message: JsonObject) -> str:
    """Classify a JSON-RPC response, notification, or server request."""

    has_method = isinstance(message.get("method"), str)
    has_id = "id" in message
    if has_method and has_id:
        return "server_request"
    if has_method:
        return "notification"
    if has_id and ("result" in message or "error" in message):
        return "response"
    raise ProtocolError("unrecognized app-server message shape")


def is_response_for(message: JsonObject, request_id: int) -> bool:
    """Return whether a message is the response for a request ID."""

    return classify_message(message) == "response" and message.get("id") == request_id


def event_metadata(message: JsonObject) -> JsonObject:
    """Keep only operational identifiers from an event, never its text payload."""

    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
    item = params.get("item")
    if not isinstance(item, dict):
        item = {}
    metadata: JsonObject = {"method": message.get("method")}
    for key in ("threadId", "turnId", "itemId"):
        value = params.get(key)
        if value is not None:
            metadata[key] = value
    if item.get("id") is not None and "itemId" not in metadata:
        metadata["itemId"] = item["id"]
    if message.get("emittedAtMs") is not None:
        metadata["emittedAtMs"] = message["emittedAtMs"]
    return metadata


def extract_final_agent_message(turn_completed: JsonObject) -> str | None:
    """Extract the final agent message without exposing reasoning items."""

    params = turn_completed.get("params")
    turn = params.get("turn") if isinstance(params, dict) else None
    items = turn.get("items") if isinstance(turn, dict) else None
    if not isinstance(items, list):
        return None
    candidates: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if isinstance(text, str):
            if item.get("phase") == "final_answer":
                return text
            candidates.append(text)
    return candidates[-1] if candidates else None


def _redact_stderr(line: str) -> str:
    """Avoid exposing common secret-shaped stderr fields in diagnostics."""

    lowered = line.lower()
    secret_markers = ("token", "secret", "password", "api_key", "apikey", "cookie")
    if any(marker in lowered for marker in secret_markers):
        return "[REDACTED stderr line]"
    return line


def resolve_executable(explicit: str | os.PathLike[str] | None = None) -> str:
    """Resolve Codex from an explicit path, CODEX_EXECUTABLE, PATH, or local fallback."""

    requested = explicit or os.environ.get("CODEX_EXECUTABLE")
    if requested:
        candidate = Path(requested).expanduser()
        if not candidate.is_file():
            raise ExecutableResolutionError(f"Codex executable does not exist: {candidate}")
        return str(candidate)

    path_candidate = shutil.which("codex")
    if path_candidate:
        return path_candidate

    fallback = Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe"
    if fallback.is_file():
        return str(fallback)
    raise ExecutableResolutionError(
        "Codex was not found; pass --executable or set CODEX_EXECUTABLE"
    )


@dataclass(frozen=True)
class CloseResult:
    """Outcome of closing the client-owned process."""

    pid: int | None
    returncode: int | None
    killed: bool


class CodexAppServerClient:
    """Sequential JSON-line client for the small 1B app-server surface.

    ``request_timeout`` is a total deadline for one short RPC request.
    ``turn_timeout`` is an inactivity timeout between messages while waiting
    for one turn; it is intentionally not a total deadline for the turn.
    """

    def __init__(
        self,
        executable: str | os.PathLike[str],
        cwd: str | os.PathLike[str],
        *,
        request_timeout: float = 30.0,
        turn_timeout: float = 300.0,
        close_timeout: float = 5.0,
        env: dict[str, str] | None = None,
        notification_observer: NotificationObserver | None = None,
        observer: NotificationObserver | None = None,
    ) -> None:
        if notification_observer is not None and observer is not None:
            raise ValueError("pass only one notification observer")
        self.executable = str(executable)
        self.cwd = str(Path(cwd).resolve())
        self.request_timeout = request_timeout
        self.turn_timeout = turn_timeout
        self.close_timeout = close_timeout
        self.env = env
        self.notification_observer = notification_observer or observer
        self.process: asyncio.subprocess.Process | None = None
        self.events: list[JsonObject] = []
        self.server_requests: list[JsonObject] = []
        self.stderr_lines: list[str] = []
        self._next_request_id = 1
        # Only early notifications that must be consumed by
        # wait_for_turn_completed() are retained. Responses are never queued:
        # this client deliberately permits one active request at a time.
        # Each queued notification carries an explicit marker indicating that
        # it was already observed when it first arrived in request().
        self._pending_notifications: Deque[tuple[JsonObject, bool]] = deque(maxlen=16)
        self._stderr_task: asyncio.Task[None] | None = None

    def set_notification_observer(
        self, observer: NotificationObserver | None
    ) -> None:
        """Replace the synchronous observer used for future notifications."""

        self.notification_observer = observer

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    async def start(self) -> int:
        """Start only this client's app-server child process."""

        if self.process is not None:
            raise AppServerError("app-server process is already started")
        child_env = dict(os.environ) if self.env is None else dict(self.env)
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.executable,
                "app-server",
                "--listen",
                "stdio://",
                cwd=self.cwd,
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, PermissionError) as exc:
            self.process = None
            if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5:
                raise ExecutableLaunchError(
                    f"Codex executable was denied: {self.executable}; "
                    "pass an explicit executable path"
                ) from exc
            raise ExecutableLaunchError(
                f"unable to start Codex executable: {self.executable}"
            ) from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        return self.process.pid

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                self.stderr_lines.append(_redact_stderr(decoded))
                if len(self.stderr_lines) > 200:
                    del self.stderr_lines[:-200]
        except (OSError, ConnectionError):
            return

    async def _read_message(self, timeout: float | None = None) -> JsonObject:
        process = self.process
        if process is None or process.stdout is None:
            raise AppServerError("app-server process is not started")
        try:
            line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=self.request_timeout if timeout is None else timeout,
            )
        except asyncio.TimeoutError as exc:
            raise AppServerError("timed out waiting for app-server stdout") from exc
        if not line:
            raise AppServerError(
                f"app-server stdout closed (returncode={process.returncode})"
            )
        return decode_json_line(line)

    async def _write_request(self, method: str, params: JsonObject) -> int:
        process = self.process
        if process is None or process.stdin is None:
            raise AppServerError("app-server process is not started")
        request_id = self._next_request_id
        self._next_request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await process.stdin.drain()
        return request_id

    def _record_notification(self, message: JsonObject) -> None:
        metadata = event_metadata(message)
        self.events.append(metadata)
        observer = self.notification_observer
        if observer is None:
            return
        method = message.get("method")
        if not isinstance(method, str):
            raise ProtocolError("notification method must be text")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        observer(method, dict(params))

    def _reject_server_request(self, message: JsonObject) -> None:
        metadata = event_metadata(message)
        self.server_requests.append(metadata)
        method = message.get("method")
        request_id = message.get("id")
        raise ServerRequestError(
            f"unexpected server request {method!r} (id={request_id!r}); "
            "automatic approval is disabled"
        )

    async def request(self, method: str, params: JsonObject) -> JsonObject:
        """Send one request and correlate its response while recording events."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.request_timeout
        request_id = await self._write_request(method, params)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AppServerError(f"timed out waiting for response to {method}")
            # A request deadline is shared by every read. Notifications do not
            # restart the full request timeout.
            message = await self._read_message(timeout=remaining)
            kind = classify_message(message)
            if kind == "response":
                if is_response_for(message, request_id):
                    if "error" in message:
                        raise ProtocolError(f"app-server returned an error for {method}")
                    return message
                raise ProtocolError(
                    f"unexpected app-server response id={message.get('id')!r} "
                    f"while waiting for {method} id={request_id}"
                )
            if kind == "server_request":
                self._reject_server_request(message)
            self._record_notification(message)
            if message.get("method") == "turn/completed":
                self._pending_notifications.append((message, True))

    async def initialize(self) -> JsonObject:
        return await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": THREAD_SOURCE,
                    "title": "ChatGPT–Codex Bridge 1F-D",
                    "version": "0.1.0",
                },
            },
        )

    async def account_read(self) -> JsonObject:
        return await self.request("account/read", {})

    async def thread_start(
        self,
        *,
        model: str,
        cwd: str | os.PathLike[str],
        ephemeral: bool = True,
    ) -> JsonObject:
        return await self.request(
            "thread/start",
            {
                "model": model,
                "cwd": str(Path(cwd).resolve()),
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": "read-only",
                "ephemeral": ephemeral,
                "threadSource": THREAD_SOURCE,
            },
        )

    async def turn_start(
        self,
        *,
        thread_id: str,
        cwd: str | os.PathLike[str],
        model: str,
        prompt: str,
        on_turn_started: Callable[[str], None] | None = None,
    ) -> tuple[JsonObject, JsonObject]:
        response = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "cwd": str(Path(cwd).resolve()),
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "model": model,
                "effort": "low",
            },
        )
        result = response.get("result")
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            raise ProtocolError("turn/start response did not contain a turn ID")
        if on_turn_started is not None:
            on_turn_started(turn_id)
        completed = await self.wait_for_turn_completed(thread_id, turn_id)
        return response, completed

    async def turn_interrupt(self, *, thread_id: str, turn_id: str) -> JsonObject:
        """Request interruption of one active turn without auto-approving anything."""

        return await self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )

    async def wait_for_turn_completed(
        self,
        thread_id: str,
        turn_id: str,
        *,
        timeout: float | None = None,
    ) -> JsonObject:
        """Wait for completion using an inactivity timeout between messages.

        ``turn_timeout`` (or the per-call ``timeout`` override) applies to
        each individual read. It is not a total deadline for a long-running
        turn that continues to emit messages.
        """

        while True:
            pending = self._pending_notifications.popleft() if self._pending_notifications else None
            if pending is None:
                message = await self._read_message(
                    timeout=self.turn_timeout if timeout is None else timeout
                )
                message_already_observed = False
            else:
                message, message_already_observed = pending
            kind = classify_message(message)
            if kind == "response":
                raise ProtocolError(
                    f"unexpected app-server response id={message.get('id')!r} "
                    f"while waiting for turn/completed id={turn_id}"
                )
            if kind == "server_request":
                self._reject_server_request(message)
            if not message_already_observed:
                self._record_notification(message)
            if message.get("method") != "turn/completed":
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                continue
            if params.get("threadId") != thread_id:
                continue
            completed_turn = params.get("turn")
            if isinstance(completed_turn, dict) and completed_turn.get("id") == turn_id:
                return message

    async def close(self) -> CloseResult:
        """Close stdin, wait, and kill only this client-owned process on timeout."""

        process = self.process
        if process is None:
            return CloseResult(pid=None, returncode=None, killed=False)
        pid = process.pid
        killed = False
        if process.stdin is not None:
            try:
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self.close_timeout)
        except asyncio.TimeoutError:
            killed = True
            try:
                process.kill()
            except ProcessLookupError:
                killed = False
            await process.wait()
        finally:
            if self._stderr_task is not None:
                try:
                    await asyncio.wait_for(self._stderr_task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._stderr_task.cancel()
                    try:
                        await self._stderr_task
                    except asyncio.CancelledError:
                        pass
                self._stderr_task = None
        self.process = None
        return CloseResult(pid=pid, returncode=process.returncode, killed=killed)


__all__ = [
    "AppServerError",
    "CodexAppServerClient",
    "CloseResult",
    "ExecutableLaunchError",
    "ExecutableResolutionError",
    "ProtocolError",
    "ServerRequestError",
    "classify_message",
    "decode_json_line",
    "event_metadata",
    "extract_final_agent_message",
    "is_response_for",
    "resolve_executable",
]
