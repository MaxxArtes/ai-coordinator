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
import platform
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set


VAULT_UNAVAILABLE_CODE = -32004  # RPCErrorCode.VAULT_UNAVAILABLE


class StructuredRPCError(Exception):
    """Handler error reported as a JSON-RPC error with a structured `data` payload {field, reason}."""

    code = -32603  # INTERNAL_ERROR unless a subclass or the caller says otherwise

    def __init__(self, message: str, *, field: str, reason: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.field = field
        self.reason = reason
        if code is not None:
            self.code = code

    def to_data(self) -> Dict[str, str]:
        return {"field": self.field, "reason": self.reason}


class InvalidParamsError(StructuredRPCError, ValueError):
    """Invalid request parameters; reported as JSON-RPC INVALID_PARAMS (-32602)."""

    code = -32602


def validate_secret_key(value: Any) -> str:
    """Vault key names: uppercase identifiers (e.g. OPENROUTER_API_KEY), 1-64 characters."""
    import re

    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
        raise InvalidParamsError(
            "'key' deve ser um identificador em maiúsculas (A-Z, 0-9, _), com até 64 caracteres.",
            field="key",
            reason="invalid_value",
        )
    return value


def validate_command_argv(value: Any) -> Optional[List[str]]:
    """Contract of terminal.create_session `command`: None (default shell) or a non-empty argv list.

    Strings are rejected instead of being split: list2cmdline would turn each character into an argument,
    and shell-style splitting would reintroduce quoting ambiguity. Arguments must be non-empty text without NUL.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise InvalidParamsError(
            "'command' deve ser uma lista não vazia de argumentos (argv), não um texto.", field="command", reason="type"
        )
    for arg in value:
        if not isinstance(arg, str) or not arg or "\x00" in arg:
            raise InvalidParamsError(
                "'command' aceita apenas argumentos de texto não vazios e sem caractere nulo.", field="command", reason="item"
            )
    return list(value)


def _core_version() -> str:
    """Version of the bundled Core package (ai_coordinator.__version__), or "unknown" if it cannot be read."""
    try:
        import ai_coordinator

        return str(ai_coordinator.__version__)
    except Exception:
        return "unknown"

# In isolated mode (-I), Python enables -P (safe path) which strips the script's directory.
# Explicitly ensure the module, desktop, and repo/src paths are in sys.path.
_here = Path(__file__).resolve().parent
_candidates = [
    _here,
    _here.parent,
    _here.parent.parent,
    _here.parent.parent / "src",
    _here.parent / "src",
]
for c in _candidates:
    if c.exists() and str(c) not in sys.path:
        sys.path.insert(0, str(c))

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
        log_file: Optional[Path] = None,
    ) -> None:
        self.reader = reader or sys.stdin
        self.writer = writer or sys.stdout
        self.diag = diag or sys.stderr
        self._log_file_handle: Optional[io.TextIOBase] = None

        target_log = log_file or (Path(os.environ["COORDINATOR_IPC_LOG_FILE"]) if "COORDINATOR_IPC_LOG_FILE" in os.environ else None)
        if target_log:
            try:
                target_log.parent.mkdir(parents=True, exist_ok=True)
                self._log_file_handle = open(target_log, "a", encoding="utf-8", errors="replace")
            except Exception:
                self._log_file_handle = None

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
            clean_text = text
            token = os.environ.get("COORDINATOR_IPC_TOKEN")
            if token and token in clean_text:
                clean_text = clean_text.replace(token, "[REDACTED_TOKEN]")

            entry = f"[IPC_DIAG] {clean_text}\n"
            if self.diag:
                self.diag.write(entry)
                self.diag.flush()

            if self._log_file_handle:
                self._log_file_handle.write(entry)
                self._log_file_handle.flush()
        except Exception:
            pass

    def close(self) -> None:
        if self._log_file_handle:
            try:
                self._log_file_handle.close()
            except Exception:
                pass
            self._log_file_handle = None


class SecureIPCServer:
    """Dispatches validated and authenticated JSON-RPC requests to Core handlers."""

    PUBLIC_METHODS: Set[str] = {"system.ping"}

    def __init__(
        self,
        transport: BaseTransport,
        session_token: str,
        terminal_manager: Optional[Any] = None,
    ) -> None:
        self.transport = transport
        self.session_token = session_token
        self.terminal_manager = terminal_manager
        self.handlers: Dict[str, Callable[..., Any]] = {}
        self.is_running = True
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        self.register("system.ping", self._handle_ping)
        self.register("system.status", self._handle_status)
        self.register("system.freeze_for_update", self._handle_system_freeze_for_update)
        self.register("system.unfreeze_update", self._handle_system_unfreeze_update)
        self.register("profile.get_active", self._handle_profile_get_active)
        self.register("profile.authorize_project", self._handle_profile_authorize_project)
        self.register("profile.list_authorized_projects", self._handle_profile_list_authorized_projects)
        self.register("terminal.create_tab", self._handle_terminal_create_tab)
        self.register("terminal.create_session", self._handle_terminal_create_session)
        self.register("terminal.write", self._handle_terminal_write)
        self.register("terminal.resize", self._handle_terminal_resize)
        self.register("terminal.get_output", self._handle_terminal_get_output)
        self.register("terminal.close_session", self._handle_terminal_close_session)
        self.register("terminal.save_sessions", self._handle_terminal_save_sessions)
        self.register("terminal.list_tabs", self._handle_terminal_list_tabs)
        self.register("settings.get", self._handle_settings_get)
        self.register("settings.update", self._handle_settings_update)
        self.register("vault.status", self._handle_vault_status)
        self.register("vault.list_keys", self._handle_vault_list_keys)
        self.register("vault.set_secret", self._handle_vault_set_secret)
        self.register("vault.delete_secret", self._handle_vault_delete_secret)

    def _get_terminal_manager(self) -> Any:
        if self.terminal_manager is None:
            from desktop.auth.authorizer import DesktopAuthorizer
            from desktop.auth.profile import DesktopProfileManager
            from desktop.terminal.manager import TerminalManager

            prof_mgr = DesktopProfileManager()
            authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
            if self.session_token:
                authorizer._active_sessions[self.session_token] = prof_mgr.get_active_profile().name
            self.terminal_manager = TerminalManager(authorizer=authorizer)
        return self.terminal_manager

    def _handle_terminal_create_tab(self, params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        tab = mgr.create_tab(
            token=token or self.session_token,
            tab_id=params["tab_id"],
            title=params.get("title", params["tab_id"]),
            project_path=params["project_path"],
        )
        return tab.to_dict()

    def _handle_terminal_create_session(self, params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        from desktop.terminal.session import SessionKind

        kind_str = params.get("kind", "human_terminal")
        kind = SessionKind(kind_str)
        sess = mgr.create_terminal(
            token=token or self.session_token,
            tab_id=params["tab_id"],
            pane_id=params["pane_id"],
            kind=kind,
            command=validate_command_argv(params.get("command")),
            cwd=params.get("cwd"),
            cols=params.get("cols", 80),
            rows=params.get("rows", 24),
        )
        return sess.to_dict()

    def _handle_terminal_write(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        written = mgr.write_to_terminal(params["session_id"], params["data"])
        return {"bytes_written": written}

    def _handle_terminal_resize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        ok = mgr.resize_terminal(params["session_id"], params["cols"], params["rows"])
        return {"resized": ok}

    # ---- Settings (Marco 4D.3, Etapa 3): versioned schema validated here, never only in the UI ----
    def _settings_store(self) -> Any:
        from desktop.settings import SettingsStore

        return SettingsStore(self._get_terminal_manager().authorizer.profile_manager)

    def _handle_settings_get(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return self._settings_store().snapshot()

    def _handle_settings_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from desktop.settings import SettingsValidationError

        try:
            return self._settings_store().update(params)
        except SettingsValidationError as exc:
            raise InvalidParamsError(str(exc), field=exc.field, reason=exc.reason) from exc

    # ---- Vault: write and list key names only; secret values never leave the vault through IPC ----
    def _vault_for(self, token: Optional[str]) -> Any:
        authorizer = self._get_terminal_manager().authorizer
        profile_name = authorizer.validate_session(token or self.session_token)
        return authorizer.get_vault(profile_name)

    def _vault_call(self, token: Optional[str], operation: Callable[[Any], Any]) -> Any:
        from desktop.auth.vault import VaultUnavailableError

        try:
            return operation(self._vault_for(token))
        except VaultUnavailableError as exc:
            raise StructuredRPCError(str(exc), field="vault", reason="unavailable", code=VAULT_UNAVAILABLE_CODE) from exc

    def _handle_vault_status(self, _params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        vault = self._vault_for(token)
        return {"available": bool(vault.available), "backend": vault.backend_name}

    def _handle_vault_list_keys(self, _params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        return {"keys": self._vault_call(token, lambda vault: sorted(vault.list_keys()))}

    def _handle_vault_set_secret(self, params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        key = validate_secret_key(params.get("key"))
        value = params.get("value")
        if not isinstance(value, str) or not value:
            raise InvalidParamsError("'value' deve ser um texto não vazio.", field="value", reason="invalid_value")
        self._vault_call(token, lambda vault: vault.set_secret(key, value))
        return {"stored": True, "key": key}

    def _handle_vault_delete_secret(self, params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        key = validate_secret_key(params.get("key"))
        deleted = self._vault_call(token, lambda vault: vault.delete_secret(key))
        return {"deleted": bool(deleted), "key": key}

    def _handle_profile_get_active(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        prof_mgr = mgr.authorizer.profile_manager
        active = prof_mgr.get_active_profile()
        return {
            "name": active.name,
            "display_name": active.display_name,
            "authorized_projects": active.authorized_projects,
            "data_dir": active.data_dir,
        }

    def _handle_profile_authorize_project(self, params: Dict[str, Any], _token: Optional[str] = None) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        prof_mgr = mgr.authorizer.profile_manager
        active = prof_mgr.get_active_profile()
        project_path = params.get("project_path")
        if not project_path or not isinstance(project_path, str):
            raise ValueError("Parameter 'project_path' must be a non-empty string")

        path_obj = Path(project_path).resolve()
        if not path_obj.exists() or not path_obj.is_dir():
            raise ValueError(f"Project directory does not exist or is not a directory: {project_path}")

        prof_mgr.authorize_project(active.name, str(path_obj))
        active = prof_mgr.get_active_profile()
        return {
            "success": True,
            "profile_name": active.name,
            "project_path": str(path_obj),
            "authorized_projects": active.authorized_projects,
        }

    def _handle_profile_list_authorized_projects(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        prof_mgr = mgr.authorizer.profile_manager
        active = prof_mgr.get_active_profile()
        return {"authorized_projects": active.authorized_projects}

    def _handle_terminal_get_output(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        sess = mgr.get_session(params["session_id"])
        if not sess:
            raise ValueError(f"Session '{params['session_id']}' not found")
        since_seq = int(params.get("since_seq", 0))
        raw_text, lines, next_seq = sess.get_new_output(since_seq)
        return {
            "session_id": sess.session_id,
            "raw": raw_text,
            "lines": lines,
            "next_seq": next_seq,
        }

    def _handle_terminal_close_session(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        ok = mgr.close_terminal(params["session_id"])
        return {"closed": ok}

    def _handle_terminal_save_sessions(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        export = mgr.save_tab_sessions(params["tab_id"])
        return export.to_dict()

    def _handle_terminal_list_tabs(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        tabs_dicts = [t.to_dict() for t in mgr.list_tabs()]
        total_active = sum(t.get("active_panes_count", 0) for t in tabs_dicts)
        total_panes = sum(t.get("panes_count", 0) for t in tabs_dicts)
        return {
            "tabs": tabs_dicts,
            "panes_total": total_panes,
            "active_sessions_total": total_active,
        }

    def _handle_system_freeze_for_update(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        return mgr.freeze_for_update()

    def _handle_system_unfreeze_update(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        return mgr.unfreeze_update()

    def _handle_ping(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "pong", "protocol": PROTOCOL_VERSION}

    def _handle_status(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "ready",
            # Read from the bundled Core package and the running interpreter; never hard-coded.
            "version": _core_version(),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": sys.platform,
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
            handler = self.handlers[req.method]
            import inspect
            sig = inspect.signature(handler)
            if len(sig.parameters) >= 2:
                res = handler(req.params, req.session_token)
            else:
                res = handler(req.params)
            return RPCResponse(id=req.id, result=res)
        except StructuredRPCError as e:
            return RPCResponse(
                id=req.id,
                error={"code": e.code, "message": str(e), "data": e.to_data()},
            )
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
        try:
            while self.is_running:
                if not self.process_one():
                    self.transport.log_diagnostic("SecureIPCServer EOF or broken pipe detected, exiting cleanly")
                    break
        finally:
            if self.terminal_manager:
                try:
                    self.transport.log_diagnostic("Shutting down active terminal sessions cleanly")
                    self.terminal_manager.terminate_all()
                except Exception as e:
                    self.transport.log_diagnostic(f"Error terminating terminals on shutdown: {e}")


if __name__ == "__main__":
    token = os.environ.get("COORDINATOR_IPC_TOKEN", "default-dev-token")
    transport = StreamTransport()
    server = SecureIPCServer(transport=transport, session_token=token)
    try:
        server.serve_forever()
    finally:
        if server.terminal_manager:
            try:
                server.terminal_manager.terminate_all()
            except Exception:
                pass
        transport.close()
