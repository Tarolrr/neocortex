"""Offline wire tests for the bounded ACP NDJSON stream."""

from __future__ import annotations

import io
import json

import pytest

from nc.acp_stream import (
    AcpDeadlineExpired,
    AcpEofError,
    AcpJsonRpcStream,
    AcpMalformedFrame,
    AcpWriteBlocked,
)


def frame(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


class Fragmented:
    def __init__(self, data: bytes, size: int = 1) -> None:
        self.data = data
        self.size = size

    def read(self, _: int = -1) -> bytes:
        result, self.data = self.data[:self.size], self.data[self.size:]
        return result


def messages(output: io.BytesIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_fragmented_utf8_and_multiple_frames_are_decoded() -> None:
    incoming = frame({"jsonrpc": "2.0", "method": "session/update", "params": {"text": "€"}})
    incoming += frame({"jsonrpc": "2.0", "id": "one", "result": {"ok": True}})
    seen: list[str] = []
    stream = AcpJsonRpcStream(
        Fragmented(incoming),
        io.BytesIO(),
        notification_handler=lambda value: seen.append(value["method"]),
    )
    stream.send_request("session/prompt", request_id="one")
    response = stream.wait_for("one")
    assert response["result"] == {"ok": True}
    assert seen == ["session/update"]


def test_agent_request_is_answered_while_prompt_waits_even_with_same_id() -> None:
    incoming = frame({"jsonrpc": "2.0", "id": 1, "method": "client/question", "params": {}})
    incoming += frame({"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}})
    output = io.BytesIO()
    stream = AcpJsonRpcStream(
        Fragmented(incoming, 3), output, request_handler=lambda _: {"answer": "no"}
    )
    response = stream.request("session/prompt", {}, request_id=1)
    assert response["result"] == {"stopReason": "end_turn"}
    assert messages(output) == [
        {"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "result": {"answer": "no"}},
    ]


def test_unknown_request_gets_error_and_unknown_response_cannot_complete_prompt() -> None:
    incoming = frame({"jsonrpc": "2.0", "id": "other", "result": {"wrong": True}})
    incoming += frame({"jsonrpc": "2.0", "id": "agent", "method": "dangerous/thing", "params": {}})
    incoming += frame({"jsonrpc": "2.0", "id": "prompt", "result": {"right": True}})
    output = io.BytesIO()
    stream = AcpJsonRpcStream(io.BytesIO(incoming), output)
    stream.send_request("session/prompt", request_id="prompt")
    assert stream.wait_for("prompt")["result"] == {"right": True}
    assert stream.diagnostics == ("late or unexpected response id",)
    assert messages(output)[1]["error"] == {"code": -32601, "message": "Method not supported"}


def test_limits_junk_and_truncated_frames_are_explicit() -> None:
    with pytest.raises(AcpMalformedFrame):
        AcpJsonRpcStream(io.BytesIO(b"warning on stdout\n"), io.BytesIO()).pump()
    with pytest.raises(AcpMalformedFrame):
        AcpJsonRpcStream(io.BytesIO(frame({"x": "y"})), io.BytesIO(), max_message_bytes=4).pump()
    with pytest.raises(AcpEofError, match="truncated"):
        AcpJsonRpcStream(io.BytesIO(b'{"jsonrpc":"2.0"'), io.BytesIO()).pump()


class BlockedWriter:
    def write(self, _: bytes) -> int:
        return 0

    def flush(self) -> None:
        raise AssertionError("must not flush a blocked write")


def test_blocked_write_and_deadline_are_explicit() -> None:
    with pytest.raises(AcpWriteBlocked):
        AcpJsonRpcStream(io.BytesIO(), BlockedWriter()).notify("session/update", {})
    with pytest.raises(AcpDeadlineExpired):
        stream = AcpJsonRpcStream(io.BytesIO(), io.BytesIO(), clock=lambda: 2.0)
        stream.notify("session/update", {}, deadline=1.0)
