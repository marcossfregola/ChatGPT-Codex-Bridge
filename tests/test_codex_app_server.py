from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.executors.codex_app_server import (  # noqa: E402
    AppServerError,
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



class AsyncLifecycleTests(unittest.IsolatedAsyncioTestCase):
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
