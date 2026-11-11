"""Unit and live integration tests for Marco 4D.1.2: Secure IPC (JSON-RPC 2.0).

Verifies Gate 4D.1.2:
- Real communication between two separate processes over stdin/stdout pipes.
- Public vs. authenticated method enforcement using ephemeral session tokens.
- Malformed JSON and payload size boundaries.
- Graceful BrokenPipe handling and clean exit on EOF.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from desktop.ipc.client import IPCClientProcess
from desktop.ipc.protocol import MAX_PAYLOAD_BYTES, PROTOCOL_VERSION, RPCErrorCode


@pytest.fixture
def ipc_client():
    token = "test-session-secret-token-xyz"
    client = IPCClientProcess(session_token=token)
    client.start()
    assert client.is_alive(), "IPC server failed to start"
    try:
        yield client
    finally:
        client.close()


def test_live_ipc_public_ping(ipc_client: IPCClientProcess):
    """Test 1: Public method system.ping succeeds without session token."""
    resp = ipc_client.request("system.ping", override_token="")
    assert resp.error is None
    assert resp.result["status"] == "pong"
    assert resp.result["protocol"] == PROTOCOL_VERSION


def test_live_ipc_authenticated_status(ipc_client: IPCClientProcess):
    """Test 2: Authenticated method system.status succeeds with valid token."""
    resp = ipc_client.request("system.status")
    assert resp.error is None
    assert resp.result["status"] == "ready"
    assert resp.result["version"] == "0.6.0"
    assert resp.result["authenticated"] is True


def test_live_ipc_auth_rejection_invalid_token(ipc_client: IPCClientProcess):
    """Test 3: Privileged method rejected when token is invalid or missing."""
    resp = ipc_client.request("system.status", override_token="invalid-forged-token")
    assert resp.result is None
    assert resp.error is not None
    assert resp.error["code"] == RPCErrorCode.AUTH_ERROR.value
    assert "Invalid or missing" in resp.error["message"]


def test_live_ipc_malformed_json_handling(ipc_client: IPCClientProcess):
    """Test 4: Malformed JSON payload returns parse error without crashing server."""
    raw_resp = ipc_client.send_raw("{ this is not valid json")
    assert raw_resp is not None
    data = json.loads(raw_resp)
    assert data["error"]["code"] == RPCErrorCode.PARSE_ERROR.value
    # Server must remain alive and responsive after error
    assert ipc_client.is_alive()
    ping_resp = ipc_client.request("system.ping")
    assert ping_resp.result["status"] == "pong"


def test_live_ipc_payload_size_boundary(ipc_client: IPCClientProcess):
    """Test 5: Over-sized payload (> 1 MB) is rejected with PAYLOAD_TOO_LARGE."""
    oversized_data = "X" * (MAX_PAYLOAD_BYTES + 1024)
    raw_req = json.dumps({"jsonrpc": "2.0", "method": "system.ping", "params": {"data": oversized_data}})
    raw_resp = ipc_client.send_raw(raw_req)
    assert raw_resp is not None
    data = json.loads(raw_resp)
    assert data["error"]["code"] == RPCErrorCode.PAYLOAD_TOO_LARGE.value
    assert ipc_client.is_alive()


def test_live_ipc_method_not_found(ipc_client: IPCClientProcess):
    """Test 6: Calling non-existent method returns METHOD_NOT_FOUND."""
    resp = ipc_client.request("nonexistent.rpc.action")
    assert resp.result is None
    assert resp.error is not None
    assert resp.error["code"] == RPCErrorCode.METHOD_NOT_FOUND.value


def test_live_ipc_clean_disconnect_and_broken_pipe():
    """Test 7: Closing pipe signals EOF; server shuts down cleanly without zombie process."""
    client = IPCClientProcess(session_token="shutdown-test-token")
    client.start()
    assert client.is_alive()

    # Close pipe
    exit_code, stderr_output = client.close()
    assert exit_code == 0, f"Expected clean exit code 0, got {exit_code}"
    assert "SecureIPCServer EOF or broken pipe detected, exiting cleanly" in stderr_output
