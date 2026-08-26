"""Run the real 1B CodexExecutor smoke without persisting Bridge state."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from chatgpt_codex_bridge.executors import (  # noqa: E402
    CodexAppServerClient,
    extract_final_agent_message,
    resolve_executable,
)


MODEL = "gpt-5.6-luna"
PROMPT = """Respond exactly with: BRIDGE_1B_OK

Do not use tools.
Do not read files.
Do not modify files."""


def _result_object(response: dict[str, object]) -> dict[str, object]:
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("app-server response did not contain a result")
    return result


def _account_type(response: dict[str, object]) -> str:
    account = _result_object(response).get("account")
    if not isinstance(account, dict):
        raise RuntimeError("account/read did not return an account")
    account_type = account.get("type")
    if not isinstance(account_type, str):
        raise RuntimeError("account/read did not return an account type")
    return account_type


def _thread_id(response: dict[str, object]) -> str:
    thread = _result_object(response).get("thread")
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
        raise RuntimeError("thread/start did not return a thread ID")
    return thread["id"]


async def _run(executable: str | None) -> int:
    resolved = resolve_executable(executable)
    client = CodexAppServerClient(resolved, REPO_ROOT)
    pid: int | None = None
    close_result = None
    try:
        pid = await client.start()
        print(f"app-server: started pid={pid}")
        await client.initialize()
        account_type = _account_type(await client.account_read())
        if account_type != "chatgpt":
            raise RuntimeError(f"unexpected account type: {account_type}")
        print(f"account: {account_type}")

        thread_response = await client.thread_start(
            model=MODEL,
            cwd=REPO_ROOT,
            ephemeral=True,
        )
        thread_id = _thread_id(thread_response)
        _turn_response, completed = await client.turn_start(
            thread_id=thread_id,
            cwd=REPO_ROOT,
            model=MODEL,
            prompt=PROMPT,
        )
        params = completed.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        if not isinstance(turn, dict):
            raise RuntimeError("turn/completed did not contain a turn")
        turn_id = turn.get("id")
        status = turn.get("status")
        if not isinstance(turn_id, str) or status != "completed":
            raise RuntimeError(f"unexpected turn completion: status={status!r}")
        response = extract_final_agent_message(completed)
        if response != "BRIDGE_1B_OK":
            raise RuntimeError(f"unexpected final response: {response!r}")

        print(f"model: {MODEL}")
        print(f"thread: {thread_id}")
        print(f"turn: {turn_id}")
        print(f"status: {status}")
        print(f"response: {response}")
        event_methods = sorted({str(event.get("method")) for event in client.events})
        print(f"events: {len(client.events)} ({', '.join(event_methods)})")
        print(f"server_requests: {len(client.server_requests)}")
        return 0
    except Exception as exc:  # keep diagnostics safe and concise
        print(f"SMOKE_FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if client.process is not None:
            close_result = await client.close()
        if close_result is not None:
            print(
                "app-server: closed "
                f"pid={close_result.pid} returncode={close_result.returncode} "
                f"killed={close_result.killed} stderr_lines={len(client.stderr_lines)}"
            )
        elif pid is None:
            print("app-server: not started")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--executable",
        help="explicit Codex executable; otherwise CODEX_EXECUTABLE/PATH/fallback is used",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.executable))


if __name__ == "__main__":
    raise SystemExit(main())
