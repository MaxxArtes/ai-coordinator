"""Typed JSON-RPC 2.0 versioned protocol specification for AI Coordinator Desktop IPC (Marco 4D.1.2).

Follows Operator Directives:
- Protocol version: 1.0.0
- Strict separation: NDJSON over stdout/stdin, diagnostics strictly to stderr
- Session Token Authentication: every privileged command requires a valid ephemeral session token
- Payload size boundaries: limits incoming message size to prevent memory exhaustion
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

PROTOCOL_VERSION = "1.0.0"
MAX_PAYLOAD_BYTES = 1024 * 1024  # 1 MB boundary cap


class RPCErrorCode(int, Enum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    AUTH_ERROR = -32000
    PAYLOAD_TOO_LARGE = -32001
    SESSION_NOT_FOUND = -32002
    SECURITY_VIOLATION = -32003


@dataclass
class RPCRequest:
    method: str
    params: Dict[str, Any] = field(default_factory=dict)
    id: Optional[Union[str, int]] = None
    session_token: Optional[str] = None
    jsonrpc: str = "2.0"
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "protocol_version": self.protocol_version,
            "method": self.method,
            "params": self.params,
            "id": self.id,
        }
        if self.session_token:
            d["session_token"] = self.session_token
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RPCRequest":
        return cls(
            method=data["method"],
            params=data.get("params", {}),
            id=data.get("id"),
            session_token=data.get("session_token"),
            jsonrpc=data.get("jsonrpc", "2.0"),
            protocol_version=data.get("protocol_version", PROTOCOL_VERSION),
        )


@dataclass
class RPCResponse:
    id: Optional[Union[str, int]]
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None
    jsonrpc: str = "2.0"
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "protocol_version": self.protocol_version,
            "id": self.id,
        }
        if self.error is not None:
            d["error"] = self.error
        else:
            d["result"] = self.result
        return d


@dataclass
class RPCNotification:
    event: str
    data: Dict[str, Any]
    jsonrpc: str = "2.0"
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "jsonrpc": self.jsonrpc,
            "protocol_version": self.protocol_version,
            "event": self.event,
            "data": self.data,
        }
