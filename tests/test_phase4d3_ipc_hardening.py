"""Etapa 7 / E2-11: malformed input is one rejected line, never the end of the Core or its terminals.

No test touches a real desktop profile. The live transport fixture uses an isolated temporary USERPROFILE.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop.ipc.server import SecureIPCServer, StreamTransport
from test_phase4d3_ipc_utf8 import HostLikeServer

TOKEN = "test-hardening-token"
MARKER = "DO_NOT_ECHO_FAKE_SECRET"
PING = json.dumps({"jsonrpc": "2.0", "id": "next", "method": "system.ping", "params": {}})

def request(**overrides):
    value = {"jsonrpc": "2.0", "id": "bad", "method": "system.ping", "params": {}, "session_token": TOKEN}
    value.update(overrides)
    return json.dumps(value)

BAD_LINES = [
    ("nested_parser", '{"jsonrpc":"2.0","method":"system.ping","params":{"deep":' + '[' * 10000 + '0' + ']' * 10000 + '}}', -32700),
    ("huge_integer_param", '{"jsonrpc":"2.0","method":"system.ping","params":{"secret":"' + MARKER + '","n":' + '9' * 5000 + '}}', -32700),
    ("huge_integer_id", '{"jsonrpc":"2.0","method":"system.ping","id":' + '9' * 5000 + '}', -32700),
    ("nan", '{"jsonrpc":"2.0","method":"system.ping","params":{"n":NaN}}', -32700),
    ("infinity", '{"jsonrpc":"2.0","method":"system.ping","params":{"n":Infinity}}', -32700),
    ("float_overflow", '{"jsonrpc":"2.0","method":"system.ping","params":{"n":1e9999}}', -32700),
    ("syntax", '{"method":"' + MARKER + '",', -32700),
    ("no_version", '{"method":"system.ping"}', -32600),
    ("version", request(jsonrpc="1.0"), -32600),
    ("protocol", request(protocol_version="unsupported"), -32600),
    ("array_envelope", '[]', -32600),
    ("null_envelope", 'null', -32600),
    ("method_missing", '{"jsonrpc":"2.0","params":{}}', -32600),
    ("empty_method", request(method=""), -32600),
    ("version_null", request(jsonrpc=None), -32600),
    ("version_list", request(jsonrpc=[]), -32600),
    ("id_too_long", request(id="x" * 257), -32600),
    ("method_too_long", request(method="x" * 257), -32600),
    *[("method_" + name, request(method=value, params={"secret": MARKER}), -32600) for name, value in [
        ("list", []), ("object", {}), ("number", 10), ("null", None), ("boolean", True)]],
    *[("id_" + name, request(id=value), -32600) for name, value in [
        ("list", []), ("object", {"secret": MARKER}), ("boolean", True), ("float", 1.5)]],
    *[("params_" + name, request(params=value), -32602) for name, value in [
        ("list", []), ("null", None), ("number", 3), ("text", MARKER)]],
    ("token_type", request(session_token=[MARKER]), -32600),
]

@pytest.mark.parametrize("name,line,code", BAD_LINES, ids=[v[0] for v in BAD_LINES])
def test_malformed_line_is_rejected_then_ping_works_without_touching_sessions(name, line, code):
    writer, diagnostics = io.StringIO(), io.StringIO()
    manager = MagicMock()
    manager.sessions = {"retained-session": object()}
    transport = StreamTransport(reader=io.StringIO(line + "\n" + PING + "\n"), writer=writer, diag=diagnostics)
    server = SecureIPCServer(transport=transport, session_token=TOKEN, terminal_manager=manager)
    before = dict(manager.sessions)
    assert server.process_one() is True
    assert server.process_one() is True
    first, second = [json.loads(x) for x in writer.getvalue().splitlines()]
    assert first["error"]["code"] == code
    assert second["id"] == "next" and second["result"]["status"] == "pong"
    assert manager.sessions == before
    manager.terminate_all.assert_not_called()
    assert MARKER not in writer.getvalue() + diagnostics.getvalue()

def test_handler_exceptions_do_not_echo_secrets_into_response_or_diagnostics():
    diag = io.StringIO()
    server = SecureIPCServer(StreamTransport(io.StringIO(), io.StringIO(), diag), TOKEN, MagicMock())
    def fail(_params):
        raise ValueError(MARKER)
    server.register("fake.failure", fail)
    response = server.handle_raw_payload(request(method="fake.failure"))
    assert response.error["code"] == -32603
    assert MARKER not in json.dumps(response.to_dict()) + diag.getvalue()
    assert server.handle_raw_payload(PING).result["status"] == "pong"

def test_unknown_methods_and_auth_failures_do_not_echo_arbitrary_method_text():
    diag = io.StringIO()
    server = SecureIPCServer(StreamTransport(io.StringIO(), io.StringIO(), diag), TOKEN, MagicMock())
    for token in [TOKEN, "wrong"]:
        response = server.handle_raw_payload(request(method=MARKER, session_token=token))
        assert response.error["code"] in (-32601, -32000)
        assert MARKER not in json.dumps(response.to_dict()) + diag.getvalue()

def test_bad_handler_result_is_rejected_without_killing_the_next_request():
    writer, diag = io.StringIO(), io.StringIO()
    transport = StreamTransport(io.StringIO(request(method="fake.result") + "\n" + PING + "\n"), writer, diag)
    server = SecureIPCServer(transport, TOKEN, MagicMock())
    server.register("fake.result", lambda _p: {"not_serializable": object()})
    assert server.process_one() and server.process_one()
    errors, ping = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert errors["error"]["code"] == -32603
    assert ping["result"]["status"] == "pong"

def test_server_cli_refuses_missing_token_instead_of_default_development_secret(tmp_path):
    env = dict(os.environ)
    env.pop("COORDINATOR_IPC_TOKEN", None)
    env.pop("COORDINATOR_IPC_LOG_FILE", None)
    env["USERPROFILE"] = str(tmp_path)
    env["HOME"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-I", str(ROOT / "desktop/ipc/server.py")],
                            input=b"", capture_output=True, env=env, timeout=20)
    assert result.returncode != 0
    assert b"default-dev-token" not in result.stdout + result.stderr
    assert not (tmp_path / ".ai-coordinator").exists()

def test_all_malformed_vectors_preserve_a_real_child_terminal(tmp_path):
    srv = HostLikeServer(tmp_path)
    try:
        project = srv.home / "project"
        project.mkdir()
        assert srv.call("profile.authorize_project", {"project_path": str(project)}).get("error") is None
        assert srv.call("terminal.create_tab", {"tab_id": "tab", "project_path": str(project)}).get("error") is None
        created = srv.call("terminal.create_session", {"tab_id": "tab", "pane_id": 0,
                           "command": [sys.executable, "-u", "-c", "import time; time.sleep(120)"]})
        assert created.get("error") is None
        sid = created["result"]["session_id"]
        for _name, line, code in BAD_LINES:
            answer = srv.send_raw((line + "\n").encode("utf-8"))
            assert answer["error"]["code"] == code
            srv.assert_alive()
            listing = srv.call("terminal.list_tabs")["result"]
            pane = listing["tabs"][0]["panes"]["0"]
            assert pane["session_id"] == sid and pane["is_alive"] is True
        assert MARKER not in srv.stderr_text()
    finally:
        srv.close()


def test_large_frames_are_drained_with_bounded_reads_and_the_next_request_survives():
    from desktop.ipc.protocol import MAX_PAYLOAD_BYTES
    class BoundedReader(io.BytesIO):
        sizes = []
        def readline(self, size=-1):
            self.sizes.append(size)
            assert 0 < size <= MAX_PAYLOAD_BYTES + 2
            return super().readline(size)
    reader = BoundedReader(b"X" * (MAX_PAYLOAD_BYTES * 3) + b"\n" + (PING + "\n").encode())
    writer = io.BytesIO()
    server = SecureIPCServer(StreamTransport(reader, writer, io.StringIO()), TOKEN, MagicMock())
    assert server.process_one() and server.process_one()
    error, ping = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert error["error"]["code"] == -32001
    assert ping["result"]["status"] == "pong"
    assert len(reader.sizes) > 2


def test_corpus_has_at_least_thirty_vectors_and_a_flood_keeps_request_boundaries():
    assert len(BAD_LINES) >= 30
    requests = []
    for i in range(300):
        requests.extend(["{", PING])
    writer = io.StringIO()
    manager = MagicMock()
    server = SecureIPCServer(StreamTransport(io.StringIO("\n".join(requests) + "\n"), writer, io.StringIO()), TOKEN, manager)
    for _ in requests:
        assert server.process_one()
    responses = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert len(responses) == 600
    assert all(r["error"]["code"] == -32700 for r in responses[::2])
    assert all(r["result"]["status"] == "pong" for r in responses[1::2])
    manager.terminate_all.assert_not_called()
