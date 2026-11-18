"""Achado E2-1: o servidor IPC fala UTF-8 nos handles reais, como o host Tauri.

O host (desktop/src-tauri/src/main.rs) inicia `python -I desktop/ipc/server.py` com stdin/stdout em pipe, escreve
cada pedido como UTF-8 + "\\n" (serde_json não escapa acentos nem emoji) e lê cada resposta como UTF-8. Com `-I` o
Python ignora PYTHONIOENCODING/PYTHONUTF8, e no Windows o stdin em pipe usava a codificação local (cp1252): "Á"
(C3 81) virava "Ã" + "\\udc81" e derrubava o servidor; "ç"/"ã" chegavam trocados com resposta de sucesso.

Estes testes iniciam o servidor exatamente assim, com perfil e cofre num diretório temporário (USERPROFILE/HOME
apontam para ele; nunca o perfil real do usuário).
"""

from __future__ import annotations

import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest

from desktop.ipc.protocol import MAX_PAYLOAD_BYTES, RPCErrorCode
from desktop.ipc.server import SecureIPCServer, StreamTransport

SERVER_SCRIPT = ROOT_DIR / "desktop" / "ipc" / "server.py"
TOKEN = "test-token-e2-1-utf8"
ANSWER_TIMEOUT = 60.0

# Cada amostra cobre um modo de falha do cp1252: Á/Í têm bytes de continuação sem caractere no cp1252 (0x81, 0x8D)
# e derrubavam o servidor; ç/ã viravam "Ã§"/"Ã£"; aspas curvas e emoji (fora do BMP, 4 bytes) e CJK viravam lixo.
SAMPLES = [
    "Álvaro Íris",
    "Conceição São João",
    "“aspas” ‘curvas’",
    "emoji 😀🚀 fora do BMP",
    "中文 日本語 한국어",
    "Á Í ç ã “x” 😀 中",
]


def _host_line(payload: Dict[str, Any]) -> bytes:
    """A request line as the Rust host writes it: serde_json::to_string (raw UTF-8, compact) + b"\\n"."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


class HostLikeServer:
    """`sys.executable -I server.py` with binary pipes, driven like IpcBridge::exchange."""

    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "home"
        self.home.mkdir()
        self.stderr_path = tmp / "server-stderr.log"
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("PYTHON") and k not in {"COORDINATOR_IPC_LOG_FILE", "AI_COORDINATOR_ROLE"}
        }
        env.update({"COORDINATOR_IPC_TOKEN": TOKEN, "USERPROFILE": str(self.home), "HOME": str(self.home)})
        self.env = env
        self._assert_profile_is_isolated()

        self._stderr = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(SERVER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=env,
        )
        self._lines: "queue.Queue[bytes]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._seq = 0

    def _assert_profile_is_isolated(self) -> None:
        # The server keeps profiles under Path.home(); refuse to start it unless that resolves to the temporary home.
        probe = subprocess.run(
            [sys.executable, "-I", "-c", "from pathlib import Path; print(Path.home())"],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        assert Path(probe.stdout.strip()).resolve() == self.home.resolve(), "perfil não isolado no diretório temporário"

    def _pump(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            self._lines.put(line)
        self._lines.put(b"")

    def stderr_text(self) -> str:
        # The child writes straight to the file handle; this process never buffers anything into it.
        return self.stderr_path.read_bytes().decode("utf-8", "replace")

    def read_response(self) -> Dict[str, Any]:
        try:
            line = self._lines.get(timeout=ANSWER_TIMEOUT)
        except queue.Empty:
            pytest.fail(f"o servidor não respondeu em {ANSWER_TIMEOUT:.0f} s")
        if not line:
            self.proc.wait(timeout=10)
            pytest.fail(f"o servidor encerrou (código {self.proc.returncode}) em vez de responder")
        # Compatible with the host reader: one line ending in "\n" (read_line/lines() drop "\n" or "\r\n"),
        # strictly valid UTF-8 (a Rust String), one JSON object.
        assert line.endswith(b"\n")
        body = line.rstrip(b"\r\n")
        assert b"\n" not in body and b"\r" not in body
        return json.loads(body.decode("utf-8"))

    def send_raw(self, raw: bytes) -> Dict[str, Any]:
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()
        return self.read_response()

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._seq += 1
        req_id = str(self._seq)
        resp = self.send_raw(
            _host_line({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}, "session_token": TOKEN})
        )
        assert resp["id"] == req_id
        return resp

    def assert_alive(self) -> None:
        assert self.proc.poll() is None, f"o servidor encerrou com código {self.proc.returncode}"
        assert self.call("system.ping")["result"]["status"] == "pong"

    def close(self) -> int:
        try:
            if self.proc.poll() is None:
                self.proc.stdin.close()
                self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=30)
        finally:
            self._stderr.close()
        return self.proc.returncode


@pytest.fixture
def server():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        srv = HostLikeServer(Path(tmp))
        try:
            yield srv
        finally:
            srv.close()


def _display_name(resp: Dict[str, Any]) -> Any:
    assert resp.get("error") is None, resp.get("error")
    return resp["result"]["categories"]["perfil"]["values"]["display_name"]


def test_settings_round_trip_keeps_non_ascii_text_and_the_server_alive(server):
    data_dir = Path(server.call("profile.get_active")["result"]["data_dir"])
    assert server.home.resolve() in data_dir.resolve().parents  # profile lives in the temporary home only

    for sample in SAMPLES:
        updated = server.call("settings.update", {"schema_version": 1, "category": "perfil", "values": {"display_name": sample}})
        assert _display_name(updated) == sample
        assert _display_name(server.call("settings.get")) == sample
        stored = json.loads((data_dir / "profile.json").read_text(encoding="utf-8"))
        assert stored["settings"]["values"]["perfil"]["display_name"] == sample

    server.assert_alive()
    assert server.close() == 0


@pytest.mark.skipif(sys.platform != "win32", reason="cofre seguro só existe com DPAPI no Windows (decisão D8)")
def test_vault_stores_non_ascii_value_exactly(server):
    from desktop.auth.vault import Win32DPAPIVault

    key = "TEST_UTF8_KEY"
    value = "sénha-Álvaro-Í-ç-ã-“curvas”-😀-中文"  # test value, not a real credential
    data_dir = Path(server.call("profile.get_active")["result"]["data_dir"])
    assert server.home.resolve() in data_dir.resolve().parents

    resp = server.call("vault.set_secret", {"key": key, "value": value})
    assert resp.get("error") is None, resp.get("error")
    assert resp["result"] == {"stored": True, "key": key}
    assert value not in json.dumps(resp, ensure_ascii=False)
    listed = server.call("vault.list_keys")["result"]
    assert set(listed) == {"keys", "revision"} and listed["keys"] == [key]
    assert re.fullmatch(r"[0-9a-f]{64}", listed["revision"])

    # Read back from the temporary vault with the same DPAPI user and profile entropy: the exact text was stored.
    assert Win32DPAPIVault("default", data_dir / "vault.enc").get_secret(key) == value

    assert server.call("vault.delete_secret", {"key": key})["result"] == {"deleted": True, "key": key}
    server.assert_alive()
    assert value not in server.stderr_text()
    assert server.close() == 0


def test_invalid_utf8_line_gets_structured_error_and_the_next_request_is_served(server):
    marker = b"nao-registrar-este-conteudo"
    # 0x81 is invalid UTF-8 and has no cp1252 character; 0xFF is invalid UTF-8 as well.
    bad_line = b'{"jsonrpc":"2.0","id":"x","method":"vault.set_secret","params":{"key":"K","value":"' + marker + b'\x81\xff"}}\n'

    err = server.send_raw(bad_line)
    assert err["id"] is None
    assert err["error"]["code"] == RPCErrorCode.PARSE_ERROR.value == -32700
    assert err["error"]["data"] == {"field": "request", "reason": "invalid_utf8"}
    assert "result" not in err

    assert server.call("system.ping")["result"]["status"] == "pong"
    updated = server.call("settings.update", {"schema_version": 1, "category": "perfil", "values": {"display_name": "Ísis"}})
    assert _display_name(updated) == "Ísis"

    server.assert_alive()
    assert server.close() == 0
    assert marker.decode() not in server.stderr_text()  # the read diagnostic never dumps the received line


def _ping_line_of_size(size: int) -> bytes:
    """A valid ping request whose line (without "\\n") is exactly `size` UTF-8 bytes, padded with 2-byte "Á"."""
    base = _host_line({"jsonrpc": "2.0", "id": "big", "method": "system.ping", "params": {"pad": ""}})[:-1]
    remaining = size - len(base)
    pad = "Á" * (remaining // 2) + "a" * (remaining % 2)
    line = _host_line({"jsonrpc": "2.0", "id": "big", "method": "system.ping", "params": {"pad": pad}})
    assert len(line) - 1 == size
    return line


def test_payload_limit_counts_utf8_bytes_and_keeps_serving(server):
    at_limit = server.send_raw(_ping_line_of_size(MAX_PAYLOAD_BYTES))
    assert at_limit.get("error") is None and at_limit["result"]["status"] == "pong"

    over = server.send_raw(_ping_line_of_size(MAX_PAYLOAD_BYTES + 1))
    assert over["id"] is None and over["error"]["code"] == RPCErrorCode.PAYLOAD_TOO_LARGE.value

    # An oversized line is reported as too large even when it is not valid UTF-8 (the size check comes first).
    over_invalid = server.send_raw(b"\x81" * (MAX_PAYLOAD_BYTES + 1) + b"\n")
    assert over_invalid["id"] is None and over_invalid["error"]["code"] == RPCErrorCode.PAYLOAD_TOO_LARGE.value

    server.assert_alive()
    assert server.close() == 0


# ---- In-process transport: injected streams keep working; binary streams are decoded as UTF-8 ----


def _serve(reader: Any, writer: Any) -> io.StringIO:
    diag = io.StringIO()
    transport = StreamTransport(reader=reader, writer=writer, diag=diag)
    SecureIPCServer(transport=transport, session_token=TOKEN, terminal_manager=MagicMock()).serve_forever()
    transport.close()
    return diag


def test_binary_streams_decode_utf8_and_survive_an_invalid_line():
    ping = _host_line({"jsonrpc": "2.0", "id": "1", "method": "system.ping", "params": {"nome": "Álvaro 😀"}})
    reader = io.BytesIO(b'{"value":"segredo-\x81"}\n' + ping)
    writer = io.BytesIO()
    diag = _serve(reader, writer)

    lines = writer.getvalue().split(b"\n")
    assert lines[-1] == b"" and len(lines) == 3
    first, second = (json.loads(line.decode("utf-8")) for line in lines[:2])
    assert first["error"]["code"] == -32700 and first["id"] is None
    assert second["id"] == "1" and second["result"]["status"] == "pong"
    assert "segredo" not in diag.getvalue()


def test_injected_text_streams_still_work():
    ping = _host_line({"jsonrpc": "2.0", "id": "1", "method": "system.ping", "params": {"nome": "Álvaro 😀"}}).decode("utf-8")
    writer = io.StringIO()
    _serve(io.StringIO(ping), writer)
    resp = json.loads(writer.getvalue())
    assert resp["id"] == "1" and resp["result"]["status"] == "pong"


def test_text_with_lone_surrogates_is_rejected_instead_of_crashing():
    """A text transport that decoded non-UTF-8 bytes with surrogateescape (the old failure) gets -32700, not a crash."""
    srv = SecureIPCServer(transport=MagicMock(), session_token=TOKEN, terminal_manager=MagicMock())
    resp = srv.handle_raw_payload('{"jsonrpc":"2.0","id":"1","method":"system.ping","params":{"x":"Ã\udc81"}}')
    assert resp.id is None
    assert resp.error["code"] == -32700
    assert resp.error["data"] == {"field": "request", "reason": "invalid_utf8"}
