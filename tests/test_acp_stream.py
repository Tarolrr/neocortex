"""Offline wire tests for the bounded ACP NDJSON stream."""

from __future__ import annotations

import io
import json

import pytest

from nc.acp_stream import (
    AcpCancelled,
    AcpDeadlineExpired,
    AcpEofError,
    AcpJsonRpcStream,
    AcpMalformedFrame,
    AcpWriteBlocked,
    AcpWriteError,
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
    incoming += frame({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    seen: list[str] = []
    stream = AcpJsonRpcStream(
        Fragmented(incoming),
        io.BytesIO(),
        notification_handler=lambda value: seen.append(value["method"]),
    )
    stream.send_request("session/prompt", request_id=1)
    response = stream.wait_for(1)
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
    incoming += frame({"jsonrpc": "2.0", "id": 1, "result": {"right": True}})
    output = io.BytesIO()
    stream = AcpJsonRpcStream(io.BytesIO(incoming), output)
    stream.send_request("session/prompt", request_id=1)
    assert stream.wait_for(1)["result"] == {"right": True}
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


def test_clean_eof_write_failures_and_cancellation_are_explicit() -> None:
    with pytest.raises(AcpEofError, match="stdout reached EOF"):
        AcpJsonRpcStream(io.BytesIO(), io.BytesIO()).pump()

    class FailingWriter:
        def write(self, _: bytes) -> int:
            raise OSError("broken pipe")

        def flush(self) -> None:
            raise OSError("broken pipe")

    with pytest.raises(AcpWriteError, match="write failed"):
        AcpJsonRpcStream(io.BytesIO(), FailingWriter()).notify("session/update")
    with pytest.raises(AcpCancelled):
        AcpJsonRpcStream(io.BytesIO(), io.BytesIO()).pump(cancelled=lambda: True)


def test_duplicate_and_late_responses_never_complete_another_request() -> None:
    incoming = b"".join(frame({"jsonrpc": "2.0", "id": ident, "result": {"id": ident}})
                        for ident in range(1, 6))
    incoming += frame({"jsonrpc": "2.0", "id": 1, "result": {"late": True}})
    incoming += frame({"jsonrpc": "2.0", "id": 6, "result": {"id": 6}})
    stream = AcpJsonRpcStream(io.BytesIO(incoming), io.BytesIO(), max_pending_requests=2)
    for ident in range(1, 6):
        assert stream.request("session/prompt")["result"] == {"id": ident}
    # An old caller-controlled ID cannot recycle the connection's generated ID.
    with pytest.raises(ValueError, match="generated monotonically"):
        stream.send_request("session/prompt", request_id=1)
    stream.send_request("session/prompt")
    assert stream.wait_for(6)["result"] == {"id": 6}
    assert stream.diagnostics == ("late or unexpected response id",)


def test_deadline_expiring_during_deadline_aware_io_is_observed() -> None:
    now = [0.0]

    class StalledReader:
        def read_with_deadline(self, _: int, deadline: float | None, cancelled: object) -> bytes:
            now[0] = 2.0
            return b'{"jsonrpc":"2.0"}'

    with pytest.raises(AcpDeadlineExpired):
        AcpJsonRpcStream(StalledReader(), io.BytesIO(), clock=lambda: now[0]).pump(deadline=1.0)

    class StalledWriter:
        def write_with_deadline(self, data: bytes, deadline: float | None, cancelled: object) -> int:
            now[0] = 2.0
            return len(data)

        def flush_with_deadline(self, deadline: float | None, cancelled: object) -> None:
            return None

    now[0] = 0.0
    with pytest.raises(AcpDeadlineExpired):
        AcpJsonRpcStream(io.BytesIO(), StalledWriter(), clock=lambda: now[0]).notify(
            "session/update", deadline=1.0
        )
