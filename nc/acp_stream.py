"""Bounded NDJSON JSON-RPC transport for one ACP stdio connection.

ACP's stdio transport is one UTF-8 JSON-RPC object per LF.  This module owns
only that wire: it deliberately has no process, stderr, filesystem, tool, or
permission authority.  A caller supplies the two protocol streams and, where
needed, a clock.
"""

from __future__ import annotations

import io
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol


class BinaryReadable(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class BinaryWritable(Protocol):
    def write(self, data: bytes) -> int | None: ...

    def flush(self) -> None: ...


class AcpStreamError(RuntimeError):
    """Base class for explicit stream-layer failures."""


class AcpEofError(AcpStreamError):
    """The peer closed stdout, optionally in the middle of a frame."""


class AcpMalformedFrame(AcpStreamError):
    """A line was oversized, invalid UTF-8/JSON, or not a JSON-RPC envelope."""


class AcpProtocolError(AcpStreamError):
    """The peer sent a structurally invalid JSON-RPC message."""


class AcpWriteError(AcpStreamError):
    """Writing or flushing the protocol output failed."""


class AcpWriteBlocked(AcpWriteError):
    """The injected writer accepted no bytes; a host must wait for writability."""


class AcpDeadlineExpired(AcpStreamError):
    """An injected deadline elapsed before the operation could finish."""


class AcpCancelled(AcpStreamError):
    """An injected cancellation predicate requested cancellation."""


class AcpPendingLimit(AcpStreamError):
    """The bounded host-generated request table is full."""


class AcpDeadlineSupportError(AcpStreamError):
    """A deadline/cancellation needs a deadline-aware injected operation."""


JsonId = str | int
MessageKind = Literal["request", "notification", "response"]


@dataclass(frozen=True)
class AcpMessage:
    """A validated inbound JSON-RPC envelope."""

    kind: MessageKind
    value: dict[str, object]


def _valid_id(value: object) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _validated(value: object) -> AcpMessage:
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise AcpProtocolError("JSON-RPC version must be exactly 2.0")
    has_method = "method" in value
    has_result = "result" in value
    has_error = "error" in value
    has_id = "id" in value
    if has_method:
        if has_result or has_error or not isinstance(value["method"], str) or not value["method"]:
            raise AcpProtocolError("invalid JSON-RPC request envelope")
        if "params" in value and not isinstance(value["params"], (dict, list)):
            raise AcpProtocolError("JSON-RPC params must be an object or array")
        if has_id:
            if not _valid_id(value["id"]):
                raise AcpProtocolError("JSON-RPC request id is invalid")
            return AcpMessage("request", value)
        return AcpMessage("notification", value)
    if not has_id or has_result == has_error or not _valid_id(value["id"]):
        raise AcpProtocolError("invalid JSON-RPC response envelope")
    if has_error:
        error = value["error"]
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), int)
            or isinstance(error.get("code"), bool)
        ):
            raise AcpProtocolError("JSON-RPC error object is invalid")
        if not isinstance(error.get("message"), str):
            raise AcpProtocolError("JSON-RPC error message is invalid")
    return AcpMessage("response", value)


class AcpJsonRpcStream:
    """A synchronous, injectable, bounded ACP NDJSON connection.

    ``request_handler`` may return a result value for a peer request.  It is
    never invoked for a response, so equal IDs in the two directions remain
    distinct.  Returning ``None`` is a valid JSON-RPC result.  Without a
    handler peer requests receive ``-32601``; the stream itself grants no
    permissions.
    """

    def __init__(
        self,
        reader: BinaryReadable | io.TextIOBase,
        writer: BinaryWritable | io.TextIOBase,
        *,
        max_message_bytes: int = 1_048_576,
        max_pending_requests: int = 64,
        max_diagnostics: int = 32,
        clock: Callable[[], float] = time.monotonic,
        notification_handler: Callable[[dict[str, object]], None] | None = None,
        request_handler: Callable[[dict[str, object]], object] | None = None,
    ) -> None:
        if max_message_bytes < 2 or max_pending_requests < 1 or max_diagnostics < 0:
            raise ValueError("stream bounds must be positive (diagnostics may be zero)")
        self._reader = reader
        self._writer = writer
        self._text_reader = isinstance(reader, io.TextIOBase)
        self._text_writer = isinstance(writer, io.TextIOBase)
        self._max_message_bytes = max_message_bytes
        self._max_pending_requests = max_pending_requests
        self._clock = clock
        self._notification_handler = notification_handler
        self._request_handler = request_handler
        self._buffer = b""
        self._pending: dict[JsonId, dict[str, object] | None] = {}
        self._diagnostics: deque[str] = deque(maxlen=max_diagnostics)
        self._next_id = 1

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """A bounded record of ignored/unexpected peer traffic."""
        return tuple(self._diagnostics)

    @property
    def pending_ids(self) -> tuple[JsonId, ...]:
        return tuple(self._pending)

    def _check(self, deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
        if cancelled is not None and cancelled():
            raise AcpCancelled("ACP operation cancelled")
        if deadline is not None and self._clock() >= deadline:
            raise AcpDeadlineExpired("ACP operation deadline expired")

    def _note(self, text: str) -> None:
        # Never retain a peer-controlled transcript without a fixed bound.
        self._diagnostics.append(" ".join(text.split())[:240])

    def _read(self, deadline: float | None, cancelled: Callable[[], bool] | None) -> object:
        """Read once without allowing a deadline to be hidden by blocking I/O.

        Injectable live streams must expose ``read_with_deadline(size, deadline,
        cancelled)`` when a deadline or cancellation is supplied.  In-memory
        streams are known nonblocking and remain convenient for offline users.
        The operation may return after a deadline, in which case the following
        check turns that into the explicit signal instead of accepting data.
        """
        if deadline is not None or cancelled is not None:
            operation = getattr(self._reader, "read_with_deadline", None)
            if operation is not None:
                piece = operation(4096, deadline, cancelled)
                self._check(deadline, cancelled)
                return piece
            if not isinstance(self._reader, (io.BytesIO, io.StringIO, io.TextIOBase)):
                raise AcpDeadlineSupportError(
                    "reader must provide read_with_deadline for deadlines or cancellation"
                )
        return self._reader.read(4096)

    def _write_operation(
        self, data: bytes | str, deadline: float | None, cancelled: Callable[[], bool] | None,
    ) -> int | None:
        if deadline is not None or cancelled is not None:
            operation = getattr(self._writer, "write_with_deadline", None)
            if operation is not None:
                count = operation(data, deadline, cancelled)
                self._check(deadline, cancelled)
                return count
            if not isinstance(self._writer, (io.BytesIO, io.StringIO, io.TextIOBase)):
                raise AcpDeadlineSupportError(
                    "writer must provide write_with_deadline for deadlines or cancellation"
                )
        return self._writer.write(data)  # type: ignore[arg-type]

    def _flush(self, deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
        if deadline is not None or cancelled is not None:
            operation = getattr(self._writer, "flush_with_deadline", None)
            if operation is not None:
                operation(deadline, cancelled)
                self._check(deadline, cancelled)
                return
            if not isinstance(self._writer, (io.BytesIO, io.StringIO, io.TextIOBase)):
                raise AcpDeadlineSupportError(
                    "writer must provide flush_with_deadline for deadlines or cancellation"
                )
        self._writer.flush()

    def _read_frame(self, deadline: float | None, cancelled: Callable[[], bool] | None) -> bytes:
        while True:
            self._check(deadline, cancelled)
            line_end = self._buffer.find(b"\n")
            if line_end >= 0:
                line, self._buffer = self._buffer[:line_end], self._buffer[line_end + 1:]
                if line.endswith(b"\r"):
                    line = line[:-1]
                if not line:
                    raise AcpMalformedFrame("empty ACP frame")
                return line
            if len(self._buffer) > self._max_message_bytes:
                raise AcpMalformedFrame("ACP frame exceeds message limit")
            try:
                piece = self._read(deadline, cancelled)
            except AcpStreamError:
                raise
            except Exception as exc:  # stream implementations have varied exception types
                raise AcpEofError("ACP read failed") from exc
            if isinstance(piece, str):
                piece = piece.encode("utf-8")
            if not isinstance(piece, bytes):
                raise AcpEofError("ACP reader returned a non-byte value")
            if not piece:
                message = "truncated ACP frame" if self._buffer else "ACP stdout reached EOF"
                raise AcpEofError(message)
            self._buffer += piece

    def _write(
        self, message: dict[str, object], deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self._check(deadline, cancelled)
        try:
            raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ) + b"\n"
        except (TypeError, ValueError) as exc:
            raise AcpWriteError("message is not JSON serializable") from exc
        if len(raw) - 1 > self._max_message_bytes:
            raise AcpWriteError("outgoing ACP frame exceeds message limit")
        data: bytes | str = raw.decode("utf-8") if self._text_writer else raw
        offset = 0
        while offset < len(data):
            self._check(deadline, cancelled)
            try:
                count = self._write_operation(data[offset:], deadline, cancelled)
            except AcpStreamError:
                raise
            except Exception as exc:
                raise AcpWriteError("ACP write failed") from exc
            if count is None:
                offset = len(data)
            elif not isinstance(count, int) or count <= 0:
                raise AcpWriteBlocked("ACP writer would block")
            else:
                offset += count
        try:
            self._flush(deadline, cancelled)
        except AcpStreamError:
            raise
        except Exception as exc:
            raise AcpWriteError("ACP flush failed") from exc

    def send_request(
        self, method: str, params: dict[str, object] | list[object] | None = None,
        *, request_id: JsonId | None = None,
        deadline: float | None = None, cancelled: Callable[[], bool] | None = None,
    ) -> JsonId:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a nonempty string")
        if len(self._pending) >= self._max_pending_requests:
            raise AcpPendingLimit("too many pending ACP requests")
        # IDs are generated monotonically for the connection lifetime.  This
        # avoids retaining an unbounded tombstone table merely to reject a
        # delayed response after an ID has been recycled.  ``request_id`` is a
        # narrow test/caller assertion, not an override of generation.
        generated_id = self._next_id
        if request_id is not None and request_id != generated_id:
            raise ValueError("host request ids are generated monotonically")
        request_id = generated_id
        self._next_id += 1
        message: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message, deadline, cancelled)
        self._pending[request_id] = None
        return request_id

    def notify(
        self, method: str, params: dict[str, object] | list[object] | None = None,
        *, deadline: float | None = None,
    ) -> None:
        message: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message, deadline, None)

    def _answer_request(self, value: dict[str, object], deadline: float | None) -> None:
        request_id = value["id"]
        assert _valid_id(request_id)
        if self._request_handler is None:
            # These are the only client-directed request paths in the pinned
            # Codex ACP profile.  A bare transport has no policy owner, so it
            # must fail closed instead of inventing an approval.  The shapes
            # are the ACP v1 cancellation responses, not local conventions.
            if value["method"] == "session/request_permission":
                self._write(
                    {"jsonrpc": "2.0", "id": request_id,
                     "result": {"outcome": {"outcome": "cancelled"}}},
                    deadline,
                    None,
                )
                return
            if value["method"] == "elicitation/create":
                self._write(
                    {"jsonrpc": "2.0", "id": request_id,
                     "result": {"action": "cancel", "content": None}},
                    deadline,
                    None,
                )
                return
            self._write(
                {"jsonrpc": "2.0", "id": request_id,
                 "error": {"code": -32601, "message": "Method not supported"}}, deadline, None,
            )
            return
        try:
            result = self._request_handler(value)
        except (
            ArithmeticError,
            AttributeError,
            LookupError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            self._write(
                {"jsonrpc": "2.0", "id": request_id,
                 "error": {"code": -32603, "message": "Client request failed"}}, deadline, None,
            )
            return
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result}, deadline, None)

    def pump(
        self, *, deadline: float | None = None, cancelled: Callable[[], bool] | None = None,
    ) -> AcpMessage:
        """Read and dispatch exactly one peer frame, including peer requests."""
        raw = self._read_frame(deadline, cancelled)
        if len(raw) > self._max_message_bytes:
            raise AcpMalformedFrame("ACP frame exceeds message limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcpMalformedFrame("ACP frame is not valid UTF-8 JSON") from exc
        message = _validated(value)
        if message.kind == "response":
            request_id = message.value["id"]
            assert _valid_id(request_id)
            if request_id not in self._pending:
                self._note("late or unexpected response id")
            elif self._pending[request_id] is not None:
                self._note("duplicate response id")
            else:
                self._pending[request_id] = message.value
        elif message.kind == "notification":
            if self._notification_handler is not None:
                self._notification_handler(message.value)
        else:
            self._answer_request(message.value, deadline)
        return message

    def wait_for(
        self, request_id: JsonId, *, deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Pump interleaved traffic until the exact host request has a response."""
        if request_id not in self._pending:
            raise ValueError("request id is not pending")
        while self._pending[request_id] is None:
            self.pump(deadline=deadline, cancelled=cancelled)
        response = self._pending.pop(request_id)
        assert response is not None
        return response

    def request(
        self, method: str, params: dict[str, object] | list[object] | None = None,
        *, request_id: JsonId | None = None, deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Send one host request and wait while servicing all interleaved traffic."""
        request_id = self.send_request(
            method, params, request_id=request_id, deadline=deadline, cancelled=cancelled,
        )
        return self.wait_for(request_id, deadline=deadline, cancelled=cancelled)


# A descriptive alias for callers that prefer the protocol name first.
AcpStreamConnection = AcpJsonRpcStream
