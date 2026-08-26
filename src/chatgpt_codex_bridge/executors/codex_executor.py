"""Codex-backed implementation of the Bridge executor contract."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
import os
from pathlib import Path
from typing import Any

from ..domain.models import ExecutionStatus
from .base import CorrelationCallback, ExecutionRequest, ExecutionResult, Executor, NotificationCallback
from .codex_app_server import (
    AppServerError,
    CloseResult,
    CodexAppServerClient,
    extract_final_agent_message,
    resolve_executable,
)


ClientFactory = Callable[..., CodexAppServerClient]


class CodexExecutor:
    """Run one Bridge request through an app-server owned by this executor."""

    def __init__(
        self,
        executable: str | os.PathLike[str] | None = None,
        *,
        request_timeout: float = 30.0,
        turn_timeout: float = 300.0,
        close_timeout: float = 5.0,
        env: dict[str, str] | None = None,
        client_factory: ClientFactory = CodexAppServerClient,
    ) -> None:
        self.executable = executable
        self.request_timeout = request_timeout
        self.turn_timeout = turn_timeout
        self.close_timeout = close_timeout
        self.env = env
        self.client_factory = client_factory
        self.last_client: CodexAppServerClient | None = None
        self.last_pid: int | None = None
        self.last_close_result: CloseResult | None = None
        self.last_account_type: str | None = None
        self._active_thread_id: str | None = None
        self._active_turn_id: str | None = None
        self._cancel_requested = False

    @staticmethod
    def _result_object(response: dict[str, Any], operation: str) -> dict[str, Any]:
        result = response.get("result")
        if not isinstance(result, dict):
            raise AppServerError(f"{operation} response did not contain a result")
        return result

    @staticmethod
    def _mode_kwargs(callable_object: Any, mode: Any) -> dict[str, Any]:
        """Keep injected legacy test clients compatible with the mode extension."""

        try:
            parameters = inspect.signature(callable_object).parameters
        except (TypeError, ValueError):
            return {"mode": mode}
        if "mode" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return {"mode": mode}
        return {}

    @classmethod
    def _thread_id(cls, response: dict[str, Any]) -> str:
        thread = cls._result_object(response, "thread/start").get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise AppServerError("thread/start response did not contain a thread ID")
        return thread["id"]

    @staticmethod
    def _account_type(response: dict[str, Any]) -> str | None:
        result = response.get("result")
        account = result.get("account") if isinstance(result, dict) else None
        account_type = account.get("type") if isinstance(account, dict) else None
        return account_type if isinstance(account_type, str) else None

    @staticmethod
    def _completed_turn(completed: dict[str, Any], expected_turn_id: str) -> dict[str, Any]:
        params = completed.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        if not isinstance(turn, dict):
            raise AppServerError("turn/completed did not contain a turn")
        turn_id = turn.get("id")
        status = turn.get("status")
        if turn_id != expected_turn_id:
            raise AppServerError(
                f"turn/completed returned unexpected turn ID: {turn_id!r}"
            )
        if status != "completed":
            raise AppServerError(f"turn completed with unexpected status: {status!r}")
        return turn

    async def run(
        self,
        request: ExecutionRequest,
        *,
        on_correlation: CorrelationCallback | None = None,
        on_notification: NotificationCallback | None = None,
    ) -> ExecutionResult:
        """Execute one task and return its final result after durable callbacks."""

        executable = resolve_executable(self.executable)
        client = self.client_factory(
            executable,
            Path(request.cwd),
            request_timeout=self.request_timeout,
            turn_timeout=self.turn_timeout,
            close_timeout=self.close_timeout,
            env=self.env,
            notification_observer=on_notification,
        )
        self.last_client = client
        self.last_pid = None
        self.last_close_result = None
        self.last_account_type = None
        self._active_thread_id = None
        self._active_turn_id = None
        self._cancel_requested = False
        primary_error: BaseException | None = None
        try:
            self.last_pid = await client.start()
            await client.initialize()
            self.last_account_type = self._account_type(await client.account_read())

            thread_start_kwargs = self._mode_kwargs(client.thread_start, request.mode)
            thread_id = self._thread_id(
                await client.thread_start(
                    model=request.model,
                    cwd=request.cwd,
                    ephemeral=True,
                    **thread_start_kwargs,
                )
            )
            self._active_thread_id = thread_id
            if on_correlation is not None:
                on_correlation(thread_id, None)

            def on_turn_started(turn_id: str) -> None:
                self._active_turn_id = turn_id
                if on_correlation is not None:
                    on_correlation(thread_id, turn_id)

            turn_start_kwargs = self._mode_kwargs(client.turn_start, request.mode)
            _turn_started, completed = await client.turn_start(
                thread_id=thread_id,
                cwd=request.cwd,
                model=request.model,
                prompt=request.objective,
                on_turn_started=on_turn_started,
                **turn_start_kwargs,
            )
            completed_turn = self._completed_turn(completed, client_turn_id(completed))
            turn_id = completed_turn["id"]
            return ExecutionResult(
                thread_id=thread_id,
                turn_id=turn_id,
                status=ExecutionStatus.FINISHED,
                final_response=extract_final_agent_message(completed),
            )
        except asyncio.CancelledError as exc:
            primary_error = exc
            try:
                await asyncio.shield(self.cancel_active())
            except BaseException:
                pass
            raise
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                self.last_close_result = await client.close()
            except BaseException:
                if primary_error is None:
                    raise
            self._active_thread_id = None
            self._active_turn_id = None

    async def cancel_active(self) -> bool:
        """Best-effort interrupt for the currently active turn."""

        if self._cancel_requested:
            return False
        client = self.last_client
        thread_id = self._active_thread_id
        turn_id = self._active_turn_id
        interrupt = getattr(client, "turn_interrupt", None) if client is not None else None
        process = getattr(client, "process", object()) if client is not None else None
        if client is None or process is None or not thread_id or not turn_id:
            return False
        if not callable(interrupt):
            return False
        self._cancel_requested = True
        try:
            await interrupt(thread_id=thread_id, turn_id=turn_id)
        except BaseException:
            return False
        return True


def client_turn_id(completed: dict[str, Any]) -> str:
    """Extract the completed turn ID for validation without leaking protocol types."""

    params = completed.get("params")
    turn = params.get("turn") if isinstance(params, dict) else None
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str):
        raise AppServerError("turn/completed did not contain a turn ID")
    return turn_id


__all__ = ["CodexExecutor"]
