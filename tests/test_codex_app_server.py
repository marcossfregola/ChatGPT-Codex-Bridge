from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.executors.codex_app_server import (  # noqa: E402
    APP_SERVER_STREAM_LIMIT,
    AppServerError,
    AppServerMessageLimitError,
    ProtocolError,
    ServerRequestError,
    CodexAppServerClient,
    classify_message,
    decode_json_line,
    extract_final_agent_message,
    is_response_for,
    resolve_executable,
)


class ProtocolHelpersTests(unittest.TestCase):
    def test_response_correlation_by_id(self) -> None:
        response = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
        self.assertEqual(classify_message(response), "response")
        self.assertTrue(is_response_for(response, 7))
        self.assertFalse(is_response_for(response, 8))

    def test_notification_is_not_response(self) -> None:
        notification = {
            "method": "turn/completed",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
        self.assertEqual(classify_message(notification), "notification")

    def test_server_request_is_distinguishable_and_not_approved(self) -> None:
        server_request = {
            "id": 9,
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
        self.assertEqual(classify_message(server_request), "server_request")
        client = CodexAppServerClient("codex", ROOT)
        with self.assertRaises(ServerRequestError):
            client._reject_server_request(server_request)
        self.assertEqual(len(client.server_requests), 1)

    def test_invalid_json_is_explicit_error(self) -> None:
        with self.assertRaises(ProtocolError):
            decode_json_line(b"not-json\n")

    def test_lifecycle_and_final_message_helpers(self) -> None:
        self.assertEqual(resolve_executable(sys.executable), sys.executable)
        client = CodexAppServerClient("codex", ROOT)
        self.assertEqual(client.request_timeout, 30.0)
        self.assertEqual(client.turn_timeout, 300.0)
        self.assertEqual(client.close_timeout, 5.0)
        completion = {
            "params": {
                "turn": {
                    "items": [
                        {"type": "reasoningSummary", "summary": "not retained"},
                        {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "BRIDGE_1B_OK",
                        },
                    ]
                }
            }
        }
        self.assertEqual(extract_final_agent_message(completion), "BRIDGE_1B_OK")

    def test_notification_observer_receives_real_method_and_params(self) -> None:
        seen: list[tuple[str, dict[str, object]]] = []
        client = CodexAppServerClient(
            "codex",
            ROOT,
            notification_observer=lambda method, params: seen.append((method, params)),
        )

        client._record_notification(
            {
                "method": "turn/started",
                "params": {"threadId": "thread", "turnId": "turn"},
            }
        )

        self.assertEqual(
            seen,
            [("turn/started", {"threadId": "thread", "turnId": "turn"})],
        )
        self.assertEqual(client.events[-1]["method"], "turn/started")

    def test_notification_observer_failure_propagates(self) -> None:
        client = CodexAppServerClient(
            "codex",
            ROOT,
            observer=lambda _method, _params: (_ for _ in ()).throw(
                RuntimeError("observer failed")
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "observer failed"):
            client._record_notification({"method": "turn/started", "params": {}})



class AsyncLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _feed(reader: asyncio.StreamReader, message: dict[str, object]) -> None:
        reader.feed_data((json.dumps(message) + "\n").encode())

    async def test_direct_turn_completed_reaches_observer_once(self) -> None:
        reader = asyncio.StreamReader()
        seen: list[tuple[str, dict[str, object]]] = []
        client = CodexAppServerClient(
            "codex",
            ROOT,
            turn_timeout=0.1,
            notification_observer=lambda method, params: seen.append((method, params)),
        )
        client.process = SimpleNamespace(stdout=reader, returncode=None)
        self._feed(
            reader,
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread",
                    "turn": {"id": "turn", "status": "completed"},
                },
            },
        )

        result = await client.wait_for_turn_completed("thread", "turn")

        self.assertEqual(result["method"], "turn/completed")
        self.assertEqual(seen, [("turn/completed", result["params"])])

    async def test_turn_interrupt_uses_correlated_ids(self) -> None:
        client = CodexAppServerClient("codex", ROOT)
        seen: list[tuple[str, dict[str, object]]] = []

        async def fake_request(method: str, params: dict[str, object]):
            seen.append((method, params))
            return {"result": {}}

        client.request = fake_request  # type: ignore[method-assign]

        result = await client.turn_interrupt(
            thread_id="thread-active", turn_id="turn-active"
        )

        self.assertEqual(result, {"result": {}})
        self.assertEqual(
            seen,
            [
                (
                    "turn/interrupt",
                    {"threadId": "thread-active", "turnId": "turn-active"},
                )
            ],
        )

    async def test_1fd_source_is_used_for_initialize_and_thread_start(self) -> None:
        client = CodexAppServerClient("codex", ROOT)
        seen: list[tuple[str, dict[str, object]]] = []

        async def fake_request(method: str, params: dict[str, object]):
            seen.append((method, params))
            if method == "thread/start":
                return {"result": {"thread": {"id": "thread-1fd"}}}
            return {"result": {}}

        client.request = fake_request  # type: ignore[method-assign]

        await client.initialize()
        await client.thread_start(model="gpt-5.6-luna", cwd=ROOT)

        self.assertEqual(seen[0][1]["clientInfo"]["name"], "chatgpt-codex-bridge-1f-d")
        self.assertEqual(seen[1][1]["threadSource"], "chatgpt-codex-bridge-1f-d")

    async def test_early_turn_completed_is_observed_once_and_consumed_later(self) -> None:
        reader = asyncio.StreamReader()
        seen: list[str] = []
        client = CodexAppServerClient(
            "codex",
            ROOT,
            request_timeout=0.1,
            turn_timeout=0.1,
            notification_observer=lambda method, _params: seen.append(method),
        )
        client.process = SimpleNamespace(stdout=reader, returncode=None)

        async def fake_write(_method: str, _params: dict[str, object]) -> int:
            return 1

        client._write_request = fake_write  # type: ignore[method-assign]
        early = {
            "method": "turn/completed",
            "params": {
                "threadId": "thread",
                "turn": {"id": "turn", "status": "completed"},
            },
        }
        self._feed(reader, early)
        self._feed(reader, {"jsonrpc": "2.0", "id": 1, "result": {}})

        await client.request("example/request", {})
        self.assertEqual(seen, ["turn/completed"])
        self.assertEqual(len(client._pending_notifications), 1)

        result = await client.wait_for_turn_completed("thread", "turn")

        self.assertEqual(result, early)
        self.assertEqual(seen, ["turn/completed"])
        self.assertFalse(client._pending_notifications)

    async def test_two_identical_notifications_are_both_observed(self) -> None:
        reader = asyncio.StreamReader()
        seen: list[str] = []
        client = CodexAppServerClient(
            "codex",
            ROOT,
            request_timeout=0.1,
            notification_observer=lambda method, _params: seen.append(method),
        )
        client.process = SimpleNamespace(stdout=reader, returncode=None)

        async def fake_write(_method: str, _params: dict[str, object]) -> int:
            return 1

        client._write_request = fake_write  # type: ignore[method-assign]
        identical = {"method": "thread/status/changed", "params": {"status": "active"}}
        self._feed(reader, identical)
        self._feed(reader, identical)
        self._feed(reader, {"jsonrpc": "2.0", "id": 1, "result": {}})

        await client.request("example/request", {})

        self.assertEqual(seen, ["thread/status/changed", "thread/status/changed"])

    async def test_response_is_never_added_to_pending_notifications(self) -> None:
        reader = asyncio.StreamReader()
        client = CodexAppServerClient("codex", ROOT, request_timeout=0.1)
        client.process = SimpleNamespace(stdout=reader, returncode=None)

        async def fake_write(_method: str, _params: dict[str, object]) -> int:
            return 1

        client._write_request = fake_write  # type: ignore[method-assign]
        self._feed(reader, {"jsonrpc": "2.0", "id": 1, "result": {}})

        await client.request("example/request", {})

        self.assertFalse(client._pending_notifications)

    async def test_eof_is_explicit_error(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        client = CodexAppServerClient("codex", ROOT)
        client.process = SimpleNamespace(stdout=reader, returncode=0)
        with self.assertRaises(AppServerError):
            await client._read_message(timeout=0.1)

    async def test_timeout_is_explicit_error(self) -> None:
        reader = asyncio.StreamReader()
        client = CodexAppServerClient("codex", ROOT)
        client.process = SimpleNamespace(stdout=reader, returncode=None)
        with self.assertRaises(AppServerError):
            await client._read_message(timeout=0.01)

    async def test_stdout_line_over_64kib_within_configured_limit_is_read(self) -> None:
        code = (
            "import json,sys; "
            "sys.stdout.write(json.dumps({'data':'x'*70000})+'\\n'); "
            "sys.stdout.flush()"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=APP_SERVER_STREAM_LIMIT,
        )
        client = CodexAppServerClient("codex", ROOT)
        client.process = process
        try:
            message = await client._read_message(timeout=2.0)
        finally:
            await client.close()
        self.assertEqual(len(message["data"]), 70000)

    async def test_stderr_line_over_64kib_within_configured_limit_drains(self) -> None:
        code = "import sys; sys.stderr.write('x'*70000+'\\n'); sys.stderr.flush()"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=APP_SERVER_STREAM_LIMIT,
        )
        client = CodexAppServerClient("codex", ROOT)
        client.process = process
        client._stderr_task = asyncio.create_task(client._drain_stderr())
        await process.wait()
        await client._stderr_task
        self.assertIsNone(client._stderr_error)
        self.assertEqual(len(client.stderr_lines), 1)
        self.assertEqual(len(client.stderr_lines[0]), 70000)
        await client.close()

    async def test_stdout_line_over_configured_limit_is_controlled_error(self) -> None:
        reader = asyncio.StreamReader(limit=APP_SERVER_STREAM_LIMIT)
        reader.feed_data(b"x" * (APP_SERVER_STREAM_LIMIT + 1) + b"\n")
        client = CodexAppServerClient("codex", ROOT)
        client.process = SimpleNamespace(stdout=reader, returncode=None)
        with self.assertRaises(AppServerMessageLimitError) as raised:
            await client._read_message(timeout=1.0)
        self.assertIn("stdout", str(raised.exception))
        self.assertNotIsInstance(raised.exception, ValueError)

    async def test_stderr_line_over_configured_limit_has_no_background_exception(self) -> None:
        reader = asyncio.StreamReader(limit=APP_SERVER_STREAM_LIMIT)
        reader.feed_data(b"x" * (APP_SERVER_STREAM_LIMIT + 1) + b"\n")
        client = CodexAppServerClient("codex", ROOT)
        client.process = SimpleNamespace(stderr=reader)
        await client._drain_stderr()
        self.assertIsInstance(client._stderr_error, AppServerMessageLimitError)
        self.assertEqual(len(client.stderr_lines), 1)
        self.assertIn("TRUNCATED", client.stderr_lines[0])

    async def test_unexpected_response_does_not_requeue_in_request(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"jsonrpc":"2.0","id":99,"result":{}}\n')
        client = CodexAppServerClient("codex", ROOT, request_timeout=0.1)
        client.process = SimpleNamespace(stdout=reader, returncode=None)

        async def fake_write(method: str, params: dict[str, object]) -> int:
            return 1

        client._write_request = fake_write  # type: ignore[method-assign]
        with self.assertRaises(ProtocolError):
            await client.request("example/request", {})
        self.assertFalse(client._pending_notifications)

    async def test_unexpected_response_during_turn_wait_is_explicit(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"jsonrpc":"2.0","id":99,"result":{}}\n')
        client = CodexAppServerClient("codex", ROOT, turn_timeout=0.1)
        client.process = SimpleNamespace(stdout=reader, returncode=None)
        with self.assertRaises(ProtocolError):
            await client.wait_for_turn_completed("thread", "turn")

if __name__ == "__main__":
    unittest.main()
