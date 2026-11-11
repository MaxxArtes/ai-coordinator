"""IPC Client Process driver for AI Coordinator Desktop testing & verification (Marco 4D.1.2).

Spawns a real child process running SecureIPCServer, communicating via stdin/stdout
pipes with stderr diagnostic capture and session token authentication.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .protocol import PROTOCOL_VERSION, RPCRequest, RPCResponse


class IPCClientProcess:
    """Manages a live child subprocess running SecureIPCServer."""

    def __init__(self, session_token: str, server_script: Optional[Path] = None) -> None:
        self.session_token = session_token
        self.server_script = server_script or (Path(__file__).resolve().parent / "server.py")
        self.process: Optional[subprocess.Popen] = None
        self._next_id = 1

    def start(self) -> None:
        """Start the live IPC server subprocess with isolated environment."""
        env = os.environ.copy()
        env["COORDINATOR_IPC_TOKEN"] = self.session_token

        cmd = [sys.executable, "-I", str(self.server_script)]
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,  # Line buffered
        )

    def is_alive(self) -> bool:
        if not self.process:
            return False
        return self.process.poll() is None

    def send_raw(self, raw_line: str) -> Optional[str]:
        """Send raw line to child stdin and read single line response from stdout."""
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError("IPC process is not running")

        try:
            self.process.stdin.write(raw_line.strip() + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            return None

        try:
            resp_line = self.process.stdout.readline()
            if not resp_line:
                return None
            return resp_line.strip()
        except (BrokenPipeError, OSError):
            return None

    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        override_token: Optional[str] = None,
    ) -> RPCResponse:
        """Send typed JSON-RPC request and return typed response."""
        req_id = self._next_id
        self._next_id += 1

        token = override_token if override_token is not None else self.session_token
        req = RPCRequest(
            id=req_id,
            method=method,
            params=params or {},
            session_token=token,
        )

        raw_resp = self.send_raw(json.dumps(req.to_dict()))
        if not raw_resp:
            return RPCResponse(
                id=req_id,
                error={"code": -32000, "message": "No response / pipe disconnected"},
            )

        data = json.loads(raw_resp)
        return RPCResponse(
            id=data.get("id"),
            result=data.get("result"),
            error=data.get("error"),
            jsonrpc=data.get("jsonrpc", "2.0"),
            protocol_version=data.get("protocol_version", PROTOCOL_VERSION),
        )

    def close(self) -> Tuple[int, str]:
        """Close stdin to signal EOF and wait for clean process termination."""
        stderr_output = ""
        exit_code = -1
        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                self.process.wait(timeout=2.0)
                exit_code = self.process.returncode
                if self.process.stderr:
                    stderr_output = self.process.stderr.read()
            except Exception:
                self.process.kill()
                exit_code = self.process.poll() or -9
            finally:
                self.process = None
        return exit_code, stderr_output
