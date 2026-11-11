"""IPC Server for AI Coordinator Desktop implementing JSON-RPC 2.0 (Marco 4D.1.2).

Features:
- Enforces session token authentication (mere knowledge of PID or owner_id does NOT authorize).
- NDJSON over stdin/stdout; diagnostic logs strictly to stderr.
- Payloads capped at MAX_PAYLOAD_BYTES (1 MB).
- Fault-tolerant transport detecting BrokenPipe and disconnects without crashing.
- Preserves Core v0.6.0 MetricProvenance contract while providing Desktop DataOrigin metadata.
"""

from __future__ import annotations

import abc
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

# In isolated mode (-I), Python enables -P (safe path) which strips the script's directory.
# Explicitly ensure the module and repo paths are in sys.path.
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))
_repo_root = _here.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

try:
    from .protocol import (
        MAX_PAYLOAD_BYTES,
        PROTOCOL_VERSION,
        RPCErrorCode,
        RPCNotification,
        RPCRequest,
        RPCResponse,
    )
except (ImportError, ValueError):
    from protocol import (
        MAX_PAYLOAD_BYTES,
        PROTOCOL_VERSION,
        RPCErrorCode,
        RPCNotification,
        RPCRequest,
        RPCResponse,
    )


class BaseTransport(abc.ABC):
    @abc.abstractmethod
    def read_message(self) -> Optional[str]:
        pass

    @abc.abstractmethod
    def write_message(self, message: str) -> bool:
        pass

    @abc.abstractmethod
    def log_diagnostic(self, text: str) -> None:
        pass


class StreamTransport(BaseTransport):
    """NDJSON stream transport over standard handles or in-memory streams."""

    def __init__(
        self,
        reader: Optional[io.TextIOBase] = None,
        writer: Optional[io.TextIOBase] = None,
        diag: Optional[io.TextIOBase] = None,
    ) -> None:
        self.reader = reader or sys.stdin
        self.writer = writer or sys.stdout
        self.diag = diag or sys.stderr

    def read_message(self) -> Optional[str]:
        try:
            line = self.reader.readline()
            if not line:
                return None
            return line.strip()
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:
            self.log_diagnostic(f"[IPC_READ_ERROR] {e}")
            return None

    def write_message(self, message: str) -> bool:
        try:
            self.writer.write(message.strip() + "\n")
            self.writer.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False
        except Exception as e:
            self.log_diagnostic(f"[IPC_WRITE_ERROR] {e}")
            return False

    def log_diagnostic(self, text: str) -> None:
        try:
            self.diag.write(f"[IPC_DIAG] {text}\n")
            self.diag.flush()
        except Exception:
            pass


class SecureIPCServer:
    """Dispatches validated and authenticated JSON-RPC requests to Core handlers."""

    PUBLIC_METHODS: Set[str] = {"system.ping"}

    def __init__(self, transport: BaseTransport, session_token: str) -> None:
        self.transport = transport
        self.session_token = session_token
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self.is_running = True
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        self.register("system.ping", self._handle_ping)
        self.register("system.status", self._handle_status)

    def _handle_ping(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "pong", "protocol": PROTOCOL_VERSION}

    def _handle_status(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "ready",
            "version": "0.6.0",
            "protocol_version": PROTOCOL_VERSION,
            "authenticated": True,
        }

    def register(self, method_name: str, handler: Callable[[Dict[str, Any]], Any]) -> None:
        self.handlers[method_name] = handler

    def handle_raw_payload(self, raw_line: str) -> Optional[RPCResponse]:
        if not raw_line.strip():
            return None

        # 1. Payload size check
        if len(raw_line.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            self.transport.log_diagnostic(f"Payload exceeded {MAX_PAYLOAD_BYTES} bytes")
            return RPCResponse(
                id=None,
                error={
                    "code": RPCErrorCode.PAYLOAD_TOO_LARGE.value,
                    "message": f"Payload exceeds {MAX_PAYLOAD_BYTES} bytes",
                },
            )

        # 2. JSON syntax check
        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError as err:
            return RPCResponse(
                id=None,
                error={"code": RPCErrorCode.PARSE_ERROR.value, "message": f"Parse error: {err}"},
            )

        if not isinstance(data, dict) or "method" not in data:
            return RPCResponse(
                id=data.get("id") if isinstance(data, dict) else None,
                error={"code": RPCErrorCode.INVALID_REQUEST.value, "message": "Missing 'method' field"},
            )

        req = RPCRequest.from_dict(data)

        # 3. Authentication check for non-public methods
        if req.method not in self.PUBLIC_METHODS:
            if not req.session_token or req.session_token != self.session_token:
                self.transport.log_diagnostic(f"Unauthorized call to {req.method}")
                return RPCResponse(
                    id=req.id,
                    error={
                        "code": RPCErrorCode.AUTH_ERROR.value,
                        "message": "Invalid or missing ephemeral session token",
                    },
                )

        # 4. Method dispatch
        if req.method not in self.handlers:
            return RPCResponse(
                id=req.id,
                error={"code": RPCErrorCode.METHOD_NOT_FOUND.value, "message": f"Method '{req.method}' not found"},
            )

        try:
            res = self.handlers[req.method](req.params)
            return RPCResponse(id=req.id, result=res)
        except Exception as e:
            self.transport.log_diagnostic(f"Handler error in {req.method}: {e}")
            return RPCResponse(
                id=req.id,
                error={"code": RPCErrorCode.INTERNAL_ERROR.value, "message": str(e)},
            )

    def send_notification(self, event: str, data: Dict[str, Any]) -> bool:
        notif = RPCNotification(event=event, data=data)
        return self.transport.write_message(json.dumps(notif.to_dict()))

    def process_one(self) -> bool:
        raw = self.transport.read_message()
        if raw is None:
            return False

        resp = self.handle_raw_payload(raw)
        if resp is not None:
            out = json.dumps(resp.to_dict())
            if not self.transport.write_message(out):
                return False
        return True

    def serve_forever(self) -> None:
        self.transport.log_diagnostic("SecureIPCServer entering event loop")
        while self.is_running:
            if not self.process_one():
                self.transport.log_diagnostic("SecureIPCServer EOF or broken pipe detected, exiting cleanly")
                break


if __name__ == "__main__":
    token = os.environ.get("COORDINATOR_IPC_TOKEN", "default-dev-token")
    transport = StreamTransport()
    server = SecureIPCServer(transport=transport, session_token=token)
    server.serve_forever()
