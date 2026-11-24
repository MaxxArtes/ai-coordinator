"""IPC Server for AI Coordinator Desktop implementing JSON-RPC 2.0 (Marco 4D.1.2).

Features:
- Enforces session token authentication (mere knowledge of PID or owner_id does NOT authorize).
- NDJSON over stdin/stdout, UTF-8 in both directions whatever the locale code page; diagnostic logs strictly to stderr.
- Payloads capped at MAX_PAYLOAD_BYTES (1 MB).
- Fault-tolerant transport detecting BrokenPipe and disconnects without crashing.
- Preserves Core v0.6.0 MetricProvenance contract while providing Desktop DataOrigin metadata.
"""

from __future__ import annotations

import abc
import io
import json
import math
import secrets
import os
import platform
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union


MAX_JSON_DEPTH = 64
MAX_NUMBER_DIGITS = 128
MAX_RPC_IDENTIFIER_LENGTH = 256


def _json_integer(text: str) -> int:
    if len(text.lstrip("-")) > MAX_NUMBER_DIGITS:
        raise ValueError("JSON integer exceeds the permitted precision")
    return int(text)


def _json_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def _reject_json_constant(_text: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _encodable(text: str) -> bool:
    if text.isascii():
        return True
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _json_problem(value: Any) -> Optional[str]:
    """None for a usable document, "max_depth" for excessive nesting, "lone_surrogate" for a string (key or value)
    that only an escape such as \\ud800 can produce: it cannot be stored as UTF-8 nor read back by the host (E2-12)."""
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, str):
            if not _encodable(item):
                return "lone_surrogate"
            continue
        if not isinstance(item, (dict, list)):
            continue
        if depth >= MAX_JSON_DEPTH:
            return "max_depth"
        if isinstance(item, dict):
            if not all(_encodable(key) for key in item):
                return "lone_surrogate"
            children = item.values()
        else:
            children = item
        pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list, str)))
    return None


def _safe_rpc_id(value: Any) -> bool:
    # A bool is an int in Python, but never a JSON-RPC identifier.
    return value is None or type(value) is int or (
        isinstance(value, str) and len(value) <= MAX_RPC_IDENTIFIER_LENGTH
    )


VAULT_UNAVAILABLE_CODE = -32004  # RPCErrorCode.VAULT_UNAVAILABLE
VAULT_CONFLICT_CODE = -32005
BUSY_CODE = -32006  # another window holds the profile or vault lock; nothing was changed
# -32007 is reserved for the host (IpcBridge, main.rs): the Core did not answer in time or closed the stream (L2).
PROFILE_BUSY_MESSAGE = "Outra janela está usando o perfil. Tente novamente."
VAULT_BUSY_MESSAGE = "Outra janela está usando o cofre. Tente novamente."
VAULT_STATUS_LOCK_TIMEOUT = 2.0


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


def validate_expected_revision(value: Any) -> Optional[str]:
    """Optional vault revision (E.1, L6): absent/null, or the 64 lowercase hex characters vault.list_keys reported."""
    import re

    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InvalidParamsError("A revisão do cofre é inválida.", field="expected_revision", reason="invalid_value")
    return value


def validate_optional_flag(params: Dict[str, Any], name: str) -> bool:
    """Optional boolean parameter: absent or null is False; anything but true/false is refused (never coerced)."""
    value = params.get(name)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise InvalidParamsError(f"'{name}' deve ser verdadeiro ou falso.", field=name, reason="invalid_value")
    return value


BROAD_SCOPE_MESSAGE = ("Este diretório dá acesso amplo (raiz do disco ou pasta pessoal inteira). "
                       "Confirme explicitamente para autorizar.")


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


def validate_optional_interpreter_id(value: Any) -> Optional[str]:
    """Interpreter selection is an identifier from the Core allow-list, never an executable path or argv."""
    if value is None:
        return None
    if not isinstance(value, str) or value not in {"cmd", "powershell", "bash_git", "bash_wsl"}:
        raise InvalidParamsError("O interpretador selecionado é inválido.", field="interpreter_id", reason="invalid_value")
    return value


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


class UndecodableLine:
    """A request line whose bytes are not valid UTF-8 (E2-1).

    Keeps only its size: the content is never kept, logged or echoed, because the line may carry the value of
    vault.set_secret. The server answers it with a structured PARSE_ERROR and keeps serving.
    """

    __slots__ = ("size_bytes",)

    def __init__(self, size_bytes: int) -> None:
        self.size_bytes = size_bytes


def _is_binary_stream(stream: Any) -> bool:
    return isinstance(stream, (io.RawIOBase, io.BufferedIOBase))


class BaseTransport(abc.ABC):
    @abc.abstractmethod
    def read_message(self) -> Union[str, UndecodableLine, None]:
        """Next request line (stripped), an UndecodableLine, or None at end of input."""
        pass

    @abc.abstractmethod
    def write_message(self, message: str) -> bool:
        pass

    @abc.abstractmethod
    def log_diagnostic(self, text: str) -> None:
        pass


class StreamTransport(BaseTransport):
    """NDJSON stream transport over standard handles or in-memory streams.

    The wire is UTF-8 in both directions (the Tauri host writes and reads Rust Strings). The process's own stdin and
    stdout are therefore used through their binary buffers and decoded/encoded here: the host starts Python with -I,
    which ignores PYTHONIOENCODING/PYTHONUTF8, and on Windows piped text handles use the locale code page (cp1252),
    which garbled accents and crashed the server on characters such as "Á" (E2-1). Injected streams may be text
    (used as given) or binary (UTF-8).
    """

    def __init__(
        self,
        reader: Optional[Union[io.TextIOBase, io.BufferedIOBase]] = None,
        writer: Optional[Union[io.TextIOBase, io.BufferedIOBase]] = None,
        diag: Optional[io.TextIOBase] = None,
        log_file: Optional[Path] = None,
    ) -> None:
        self.reader = reader if reader is not None else getattr(sys.stdin, "buffer", sys.stdin)
        self.writer = writer if writer is not None else getattr(sys.stdout, "buffer", sys.stdout)
        self.diag = diag or sys.stderr
        self._log_file_handle: Optional[io.TextIOBase] = None

        target_log = log_file or (Path(os.environ["COORDINATOR_IPC_LOG_FILE"]) if "COORDINATOR_IPC_LOG_FILE" in os.environ else None)
        if target_log is None and diag is None and "PYTEST_CURRENT_TEST" not in os.environ:
            target_log = Path.home() / ".ai-coordinator" / "desktop" / "ipc-diagnostics.log"
        if target_log:
            try:
                target_log.parent.mkdir(parents=True, exist_ok=True)
                self._log_file_handle = open(target_log, "a", encoding="utf-8", errors="replace")
            except Exception:
                self._log_file_handle = None

    def read_message(self) -> Union[str, UndecodableLine, None]:
        try:
            # Bound each allocation and drain an oversized frame to its newline, so the next request is intact.
            line = self.reader.readline(MAX_PAYLOAD_BYTES + 2)
            newline = b"\n" if isinstance(line, (bytes, bytearray)) else "\n"
            if len(line) > MAX_PAYLOAD_BYTES and not line.endswith(newline):
                while line and not line.endswith(newline):
                    line = self.reader.readline(MAX_PAYLOAD_BYTES + 2)
                return UndecodableLine(MAX_PAYLOAD_BYTES + 1)  # size check takes precedence over encoding
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:
            # Exception type only: a decoding error message may quote bytes of the received line.
            self.log_diagnostic(f"[IPC_READ_ERROR] {type(e).__name__}")
            return None
        if not line:
            return None
        if isinstance(line, (bytes, bytearray)):
            try:
                line = bytes(line).decode("utf-8")
            except UnicodeDecodeError:
                # A bad line is answered and skipped, never treated as end of input.
                size = len(line.strip())
                self.log_diagnostic(f"[IPC_READ_ERROR] Request line is not valid UTF-8 ({size} bytes; content not logged)")
                return UndecodableLine(size)
        return line.strip()

    def write_message(self, message: str) -> bool:
        try:
            line = message.strip() + "\n"
            if _is_binary_stream(self.writer):
                # "\n" only (no text-mode "\r\n"); always valid UTF-8 for the host, even for an unpaired surrogate.
                self.writer.write(line.encode("utf-8", errors="replace"))
            else:
                self.writer.write(line)
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
        process_collector: Optional[Any] = None,
    ) -> None:
        self.transport = transport
        self.session_token = session_token
        self.terminal_manager = terminal_manager
        if process_collector is None:
            from desktop.ipc.collector import ProcessTreeCollector
            process_collector = ProcessTreeCollector(core_pid=os.getpid())
        self.process_collector = process_collector
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
        self.register("profile.revoke_project", self._handle_profile_revoke_project)
        self.register("profile.remember_project", self._handle_profile_remember_project)
        self.register("security.status", self._handle_security_status)
        self.register("discovery.agents", self._handle_discovery_agents)
        self.register("providers.routes", self._handle_providers_routes)
        self.register("terminal.create_tab", self._handle_terminal_create_tab)
        self.register("terminal.create_session", self._handle_terminal_create_session)
        self.register("terminal.list_interpreters", self._handle_terminal_list_interpreters)
        self.register("terminal.validate_interpreter", self._handle_terminal_validate_interpreter)
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
        self.register("telemetry.process_tree_metrics", self._handle_telemetry_process_tree_metrics)
        self.register("telemetry.providers_snapshot", self._handle_telemetry_providers_snapshot)

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
            interpreter_id=validate_optional_interpreter_id(params.get("interpreter_id")),
            cwd=params.get("cwd"),
            cols=params.get("cols", 80),
            rows=params.get("rows", 24),
        )
        return sess.to_dict()

    def _handle_terminal_list_interpreters(self, _params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        return {"interpreters": mgr.list_interpreters(token or self.session_token)}

    def _handle_terminal_validate_interpreter(self, params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        interpreter_id = validate_optional_interpreter_id(params.get("interpreter_id"))
        cwd = params.get("cwd")
        if interpreter_id is None or not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
            raise InvalidParamsError("Informe um interpretador e diretório válidos.", field="interpreter_id", reason="invalid_value")
        return self._get_terminal_manager().validate_interpreter(token or self.session_token, interpreter_id, cwd)

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
        from desktop.settings import SettingsValidationError

        # Stored data is reported in the snapshot (read_only, stored_issue, invalid_fields), never as a failure.
        try:
            return self._settings_store().snapshot()
        except SettingsValidationError as exc:
            raise InvalidParamsError(str(exc), field=exc.field, reason=exc.reason) from None

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
        from desktop.auth.file_lock import FileLockTimeoutError
        from desktop.auth.vault import (VaultBusyError, VaultConflictError, VaultError, VaultExistsError,
                                        VaultUnavailableError)

        try:
            return operation(self._vault_for(token))
        except VaultUnavailableError:
            raise StructuredRPCError("Cofre seguro indisponível nesta plataforma. Nenhuma credencial foi alterada.",
                                     field="vault", reason="unavailable", code=VAULT_UNAVAILABLE_CODE) from None
        except VaultExistsError:
            raise StructuredRPCError("Já existe uma credencial com este nome. Recarregue e confirme a substituição.",
                                     field="vault", reason="conflict", code=VAULT_CONFLICT_CODE) from None
        except VaultConflictError:
            raise StructuredRPCError("O cofre mudou desde a conferência. Recarregue e confirme novamente.",
                                     field="vault", reason="conflict", code=VAULT_CONFLICT_CODE) from None
        except (VaultBusyError, FileLockTimeoutError):
            # FileLockTimeoutError is an OSError: it must be told apart before the generic "unreadable".
            raise StructuredRPCError(VAULT_BUSY_MESSAGE, field="vault", reason="busy", code=BUSY_CODE) from None
        except (VaultError, OSError, ValueError):
            raise StructuredRPCError("Não foi possível ler ou gravar o cofre. Os dados existentes foram preservados.",
                                     field="vault", reason="unreadable", code=VAULT_UNAVAILABLE_CODE) from None

    def _handle_vault_status(self, _params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        from desktop.auth.file_lock import FileLockTimeoutError
        from desktop.auth.vault import VaultBusyError, VaultError
        vault = None
        try:
            vault = self._vault_for(token)
            if not vault.available:
                return {"available": False, "backend": vault.backend_name, "reason": "unavailable"}
            # A backend that exists but cannot decrypt is not available. A short wait: status must not block the IPC.
            vault.get_keys_snapshot(lock_timeout=VAULT_STATUS_LOCK_TIMEOUT)
            return {"available": True, "backend": vault.backend_name}
        except (VaultBusyError, FileLockTimeoutError):
            return {"available": False, "backend": getattr(vault, "backend_name", "unknown"), "reason": "busy"}
        except (VaultError, OSError, ValueError):
            return {"available": False, "backend": "unreadable", "reason": "unreadable"}

    def _handle_vault_list_keys(self, _params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        return self._vault_call(token, lambda vault: vault.get_keys_snapshot())

    def _handle_vault_set_secret(self, params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        key = validate_secret_key(params.get("key"))
        value = params.get("value")
        # E2-4: spaces and line breaks around a pasted credential are never part of it; blank is not a credential.
        if not isinstance(value, str) or not value.strip():
            raise InvalidParamsError("'value' deve ser um texto não vazio.", field="value", reason="invalid_value")
        value = value.strip()
        expected_revision = validate_expected_revision(params.get("expected_revision"))
        self._vault_call(token, lambda vault: vault.set_secret_conditional(key, value, expected_revision))
        return {"stored": True, "key": key}

    def _handle_vault_delete_secret(self, params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        key = validate_secret_key(params.get("key"))
        # L6: with the revision of the operator's own reading, a vault changed since then is a conflict and nothing is
        # removed; without it, the name is removed if present (unchanged behaviour).
        expected_revision = validate_expected_revision(params.get("expected_revision"))
        if expected_revision is None:
            deleted = self._vault_call(token, lambda vault: vault.delete_secret(key))
        else:
            deleted = self._vault_call(
                token, lambda vault: vault.delete_secret(key, expected_revision=expected_revision))
        return {"deleted": bool(deleted), "key": key}

    def _handle_profile_get_active(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        mgr = self._get_terminal_manager()
        prof_mgr = mgr.authorizer.profile_manager
        active = prof_mgr.get_active_profile()
        recovery = getattr(prof_mgr, "recovery", None)
        return {
            "name": active.name,
            "display_name": active.display_name,
            "authorized_projects": active.authorized_projects,
            "data_dir": active.data_dir,
            "last_project": getattr(active, "last_project", None),
            # E2-6: the active profile ("profile_unreadable": recreated empty) or the selection in active_profile.txt
            # ("active_selection_invalid": the default profile was opened) could not be used; its bytes were kept in
            # the file named by backup_file (name only).
            "recovery": dict(recovery) if isinstance(recovery, dict) else None,
        }

    def _handle_profile_authorize_project(self, params: Dict[str, Any], _token: Optional[str] = None) -> Dict[str, Any]:
        from desktop.auth.profile import is_broad_directory

        mgr = self._get_terminal_manager()
        prof_mgr = mgr.authorizer.profile_manager
        self._project_text(params)
        confirm_broad = validate_optional_flag(params, "confirm_broad")
        path_obj = self._project_param(params)
        if not path_obj.is_dir():
            # Fixed text: the requested path is never echoed back.
            raise InvalidParamsError("O diretório do projeto não existe ou não é uma pasta.",
                                     field="project_path", reason="path_missing")
        # L5: a drive/volume or share root, or the whole home folder, needs an explicit confirmation. Judged by where
        # the request really leads (resolved); entries already stored are kept and reported by security.status.
        if is_broad_directory(path_obj) and not confirm_broad:
            raise InvalidParamsError(BROAD_SCOPE_MESSAGE, field="project_path", reason="broad_scope")
        active = prof_mgr.get_active_profile()
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

    @staticmethod
    def _project_text(params: Dict[str, Any]) -> str:
        path = params.get("project_path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise InvalidParamsError("Informe um diretório de projeto válido.", field="project_path", reason="invalid_value")
        return path.strip()

    @classmethod
    def _project_param(cls, params: Dict[str, Any]) -> Path:
        try:
            return Path(cls._project_text(params)).resolve()
        except (ValueError, OSError, RuntimeError):
            raise InvalidParamsError("Diretório de projeto inválido.", field="project_path", reason="invalid_value") from None

    def _handle_profile_revoke_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from desktop.auth.profile import path_matches

        mgr = self._get_terminal_manager()
        profiles = mgr.authorizer.profile_manager
        # The listed text is compared as is and resolved (os.path.normcase), like the authorization itself: an entry
        # renamed only in letter case, replaced by a junction or written another way can still be revoked (K4). It is
        # never required to resolve: a folder on a disconnected mapped drive (winerror 1328) must stay revocable.
        requested = self._project_text(params)

        def refuse_with_sessions(matching: List[str]) -> None:
            targets = [requested, *matching]
            for session in mgr.sessions.values():
                if any(path_matches(str(session.cwd), target, include_children=True) for target in targets):
                    raise StructuredRPCError("Feche os terminais deste diretório antes de revogar a autorização.",
                                             field="project_path", reason="active_sessions", code=-32003)

        # Same lock order as creation: manager lock, then profile lock. Do not revoke a registered session's fence.
        with mgr._lock:
            active = profiles.get_active_profile()
            revoked = profiles.revoke_project(active.name, requested, guard=refuse_with_sessions)
            active = profiles.get_active_profile()
        return {"revoked": revoked, "authorized_projects": active.authorized_projects,
                "last_project": getattr(active, "last_project", None)}

    def _handle_profile_remember_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from desktop.auth.profile import ProfileValidationError
        profiles = self._get_terminal_manager().authorizer.profile_manager
        target = self._project_param(params)
        if not target.is_dir():
            raise InvalidParamsError("O diretório do projeto não está disponível.", field="project_path", reason="path_missing")
        active = profiles.get_active_profile()
        try:
            profiles.remember_project(active.name, target)
        except ProfileValidationError:
            raise StructuredRPCError("O projeto deve estar autorizado antes de ser lembrado.",
                                     field="project_path", reason="not_authorized", code=-32003) from None
        return {"remembered": True, "last_project": str(target)}

    def _handle_security_status(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        from desktop.auth.profile import is_broad_directory

        active = self._get_terminal_manager().authorizer.profile_manager.get_active_profile()
        # L5: authorized entries that open a whole disk or home folder (confirmed, or stored before the confirmation
        # existed), listed with their stored text so the interface can mark them; nothing is changed here.
        broad = [entry for entry in active.authorized_projects if is_broad_directory(entry)]
        # Only what the Desktop really enforces (BE-14): sessions open only in authorized directories (checked again
        # from disk by every window, while the active_sessions refusal of a revocation only sees this window's
        # terminals); nothing isolates the shell; TerminalManager only drops agent variables whose NAME contains
        # secret, token, password or key, so other credentials in the environment still reach an agent.
        return {"authorized_projects": active.authorized_projects, "broad_projects": broad, "policy": {
            "project_fencing": True,
            "human_terminal": "operator_permissions",
            "governed_agent": "env_sanitized_only",
            "human_terminal_filesystem_isolation": False,
            "description": "A autorização limita os diretórios em que terminais podem ser abertos. Revogar um diretório "
                           "impede novos terminais nele em todas as janelas do Desktop, mas só confere os terminais "
                           "desta janela: um terminal aberto nele em outra janela continua até ser fechado. O terminal "
                           "humano usa as permissões do operador do sistema; não há isolamento de filesystem do shell. "
                           "A interface do Desktop ainda não abre agentes governados. Um agente aberto pelo IPC herda o "
                           "ambiente do Desktop sem o token do IPC e sem as variáveis cujo nome contém secret, token, "
                           "password ou key; outras credenciais do ambiente (como PAT, URL com senha e SSH_AUTH_SOCK) "
                           "passam, e não há sandbox nem política do Core aplicada a eles.",
        }}

    def _handle_discovery_agents(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        from desktop.ipc.discovery import agents_snapshot
        return agents_snapshot()

    def _handle_providers_routes(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        from desktop.ipc.discovery import routes_snapshot
        return routes_snapshot()

    def _handle_telemetry_process_tree_metrics(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from desktop.ipc.collector import validate_sample_interval_ms

        raw_interval = params.get("min_sample_interval_ms")
        sample_interval_ms: Optional[int] = None
        if raw_interval is not None:
            try:
                sample_interval_ms = validate_sample_interval_ms(raw_interval)
            except ValueError as exc:
                raise InvalidParamsError(
                    str(exc),
                    field="min_sample_interval_ms",
                    reason="invalid_value",
                ) from exc

        raw_host_pid = params.get("host_pid")
        host_pid: Optional[int] = None
        if raw_host_pid is not None:
            if isinstance(raw_host_pid, bool) or not isinstance(raw_host_pid, int) or raw_host_pid <= 0:
                raise InvalidParamsError(
                    "'host_pid' deve ser um inteiro positivo.",
                    field="host_pid",
                    reason="invalid_value",
                )
            host_pid = raw_host_pid

        raw_include_nodes = params.get("include_nodes")
        if raw_include_nodes is None:
            include_nodes = True
        elif isinstance(raw_include_nodes, bool):
            include_nodes = raw_include_nodes
        else:
            raise InvalidParamsError(
                "'include_nodes' deve ser verdadeiro ou falso.",
                field="include_nodes",
                reason="invalid_value",
            )

        terminal_pids: Dict[int, Optional[int]] = {}
        agent_pids: Dict[int, tuple] = {}
        if self.terminal_manager is not None:
            lock = getattr(self.terminal_manager, "_lock", None)
            if lock is not None:
                with lock:
                    sessions = list(getattr(self.terminal_manager, "sessions", {}).values())
            else:
                sessions = list(getattr(self.terminal_manager, "sessions", {}).values())
            for sess in sessions:
                pid = getattr(sess, "pid", None)
                if isinstance(pid, int) and pid > 0:
                    identity = getattr(sess, "identity", None)
                    create_ns = getattr(identity, "create_time_ns", None) if identity is not None else None
                    c_ns = create_ns if isinstance(create_ns, int) else None
                    terminal_pids[pid] = c_ns
                    kind_obj = getattr(sess, "kind", None)
                    kind_val = getattr(kind_obj, "value", kind_obj)
                    if kind_val == "governed_agent":
                        agent_pids[pid] = (
                            c_ns,
                            getattr(sess, "agent_kind", None),
                            getattr(sess, "session_id", None),
                        )

        snapshot = self.process_collector.collect(
            host_pid=host_pid,
            core_pid=os.getpid(),
            terminal_pids=terminal_pids,
            agent_pids=agent_pids or None,
            min_sample_interval_ms=sample_interval_ms,
        )
        return snapshot.to_dict(include_nodes=include_nodes)

    def _handle_telemetry_providers_snapshot(self, _params: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        from desktop.auth.file_lock import FileLockTimeoutError
        from desktop.auth.vault import VaultBusyError, VaultError
        from desktop.ipc.provider_adapters import read_local_provider_telemetry
        from desktop.ipc.providers_telemetry import collect_providers_telemetry

        mgr = self._get_terminal_manager()
        prof_mgr = mgr.authorizer.profile_manager
        active = prof_mgr.get_active_profile()

        vault_keys: Optional[List[str]] = None
        try:
            vault = self._vault_for(token)
            if getattr(vault, "available", False):
                snap = vault.get_keys_snapshot(lock_timeout=VAULT_STATUS_LOCK_TIMEOUT)
                keys = snap.get("keys") if isinstance(snap, dict) else None
                if isinstance(keys, list):
                    vault_keys = [str(k) for k in keys if isinstance(k, str)]
        except (VaultBusyError, FileLockTimeoutError, VaultError, OSError, ValueError):
            vault_keys = None

        coord_budget: Optional[Dict[str, Any]] = None
        try:
            settings_snap = self._settings_store().snapshot()
            coord_cat = settings_snap.get("categories", {}).get("coordinator", {})
            vals = coord_cat.get("values") if isinstance(coord_cat, dict) else None
            if isinstance(vals, dict) and isinstance(vals.get("flight_budget"), dict):
                coord_budget = vals["flight_budget"]
        except Exception:
            coord_budget = None

        local_readings: Optional[Dict[str, Any]] = None
        try:
            local_readings = read_local_provider_telemetry()
        except Exception:
            local_readings = None

        return collect_providers_telemetry(
            profile_name=active.name,
            vault_keys=vault_keys,
            coordinator_budget=coord_budget,
            local_readings=local_readings,
        )

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

    def handle_raw_payload(self, raw_line: Union[str, UndecodableLine]) -> Optional[RPCResponse]:
        try:
            return self._handle_payload(raw_line)
        except Exception:
            self.transport.log_diagnostic("IPC request rejected after unexpected validation failure (content omitted)")
            return RPCResponse(id=None, error={"code": RPCErrorCode.INTERNAL_ERROR.value,
                                              "message": "Internal error: request could not be processed",
                                              "data": {"field": "request", "reason": "internal_error"}})

    def _handle_payload(self, raw_line: Union[str, UndecodableLine]) -> Optional[RPCResponse]:
        if isinstance(raw_line, UndecodableLine):
            size, valid_utf8 = raw_line.size_bytes, False
        else:
            if not raw_line.strip():
                # Answered like any other bad line (E2-11/C3). The host (IpcBridge::exchange) writes exactly one
                # serde_json line plus "\n" per request and then reads exactly one line, so it never sends a blank
                # line and this answer cannot shift its request/response pairing.
                return RPCResponse(id=None, error={"code": RPCErrorCode.INVALID_REQUEST.value,
                                                  "message": "Invalid request: empty line",
                                                  "data": {"field": "request", "reason": "empty_request"}})
            try:
                size, valid_utf8 = len(raw_line.encode("utf-8")), True
            except UnicodeEncodeError:
                # Unpaired surrogates: text decoded with surrogateescape from bytes that are not UTF-8.
                size, valid_utf8 = len(raw_line.encode("utf-8", errors="surrogatepass")), False

        # 1. Payload size check (UTF-8 bytes of the stripped line, checked before anything else)
        if size > MAX_PAYLOAD_BYTES:
            self.transport.log_diagnostic(f"Payload exceeded {MAX_PAYLOAD_BYTES} bytes")
            return RPCResponse(
                id=None,
                error={
                    "code": RPCErrorCode.PAYLOAD_TOO_LARGE.value,
                    "message": f"Payload exceeds {MAX_PAYLOAD_BYTES} bytes",
                    "data": {"field": "request", "reason": "payload_too_large"},
                },
            )

        # 2. Encoding check: the wire is UTF-8; the content of a bad line is never echoed back
        if not valid_utf8:
            return RPCResponse(
                id=None,
                error={
                    "code": RPCErrorCode.PARSE_ERROR.value,
                    "message": "Parse error: request line is not valid UTF-8",
                    "data": {"field": "request", "reason": "invalid_utf8"},
                },
            )

        # 3. Parse within bounded depth/precision. Error strings never quote request content.
        try:
            data = json.loads(raw_line, parse_int=_json_integer, parse_float=_json_float,
                              parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, RecursionError, ValueError, OverflowError):
            return RPCResponse(id=None, error={
                "code": RPCErrorCode.PARSE_ERROR.value,
                "message": "Parse error: invalid or excessively complex JSON",
                "data": {"field": "request", "reason": "invalid_json"},
            })

        problem = _json_problem(data)
        if problem == "max_depth":
            return RPCResponse(id=None, error={"code": RPCErrorCode.PARSE_ERROR.value,
                                              "message": "Parse error: excessively complex JSON",
                                              "data": {"field": "request", "reason": "max_depth"}})
        if problem == "lone_surrogate":
            # E2-12: valid JSON, but text that is not Unicode; never stored, never echoed.
            return RPCResponse(id=None, error={"code": RPCErrorCode.INVALID_REQUEST.value,
                                              "message": "Invalid request: text with an unpaired surrogate escape",
                                              "data": {"field": "request", "reason": "lone_surrogate"}})
        if not isinstance(data, dict):
            return RPCResponse(id=None, error={"code": RPCErrorCode.INVALID_REQUEST.value,
                                              "message": "Invalid request envelope",
                                              "data": {"field": "request", "reason": "invalid_envelope"}})
        identifier = data.get("id")
        method = data.get("method")
        token = data.get("session_token")
        if (not _safe_rpc_id(identifier) or data.get("jsonrpc") != "2.0"
                or data.get("protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION
                or not isinstance(method, str) or not method or len(method) > MAX_RPC_IDENTIFIER_LENGTH
                or (token is not None and not isinstance(token, str))):
            return RPCResponse(id=None, error={"code": RPCErrorCode.INVALID_REQUEST.value,
                                              "message": "Invalid request envelope",
                                              "data": {"field": "request", "reason": "invalid_envelope"}})
        if not isinstance(data.get("params", {}), dict):
            return RPCResponse(id=identifier, error={
                "code": RPCErrorCode.INVALID_PARAMS.value,
                "message": "Parameters must be an object",
                "data": {"field": "params", "reason": "type"},
            })
        req = RPCRequest.from_dict(data)

        # 4. Authenticate with a constant-time comparison, never logging a supplied method or token.
        if req.method not in self.PUBLIC_METHODS:
            if (not req.session_token or not self.session_token or not secrets.compare_digest(
                    req.session_token.encode("utf-8", errors="surrogatepass"),
                    self.session_token.encode("utf-8", errors="surrogatepass"))):
                self.transport.log_diagnostic("Unauthorized IPC call (request content omitted)")
                return RPCResponse(id=req.id, error={
                    "code": RPCErrorCode.AUTH_ERROR.value,
                    "message": "Invalid or missing ephemeral session token",
                })

        # 5. Method dispatch
        if req.method not in self.handlers:
            return RPCResponse(
                id=req.id,
                error={"code": RPCErrorCode.METHOD_NOT_FOUND.value, "message": "Method not found"},
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
            # Arbitrary exception text may include a credential value. Known user errors get fixed messages only.
            from desktop.auth.authorizer import SecurityAuthorizationError
            from desktop.auth.file_lock import FileLockTimeoutError
            from desktop.auth.profile import ProfileError
            from desktop.terminal.manager import TerminalInterpreterError, TerminalManagerError, TerminalStartError

            if isinstance(e, FileLockTimeoutError):
                return RPCResponse(id=req.id, error={"code": BUSY_CODE, "message": PROFILE_BUSY_MESSAGE,
                                                     "data": {"field": "profile", "reason": "busy"}})
            if isinstance(e, TerminalInterpreterError):
                return RPCResponse(id=req.id, error={
                    "code": RPCErrorCode.INVALID_PARAMS.value,
                    "message": str(e),
                    "data": {"field": "interpreter_id", "reason": e.reason},
                })
            if isinstance(e, ProfileError):
                # Every ProfileError message is a fixed text from desktop/auth/profile.py, never file content.
                self.transport.log_diagnostic(f"IPC handler failed: {type(e).__name__} (content omitted)")
                return RPCResponse(id=req.id, error={"code": RPCErrorCode.INTERNAL_ERROR.value, "message": str(e),
                                                     "data": {"field": "profile", "reason": "profile_unreadable"}})
            message = "Internal error: operation could not be completed"
            data: Optional[Dict[str, Any]] = None
            if isinstance(e, SecurityAuthorizationError):
                message = "Project not authorized or session invalid"
            elif isinstance(e, TerminalStartError):
                message = "Terminal operation failed"
                data = {"field": "terminal", "reason": "start_failed", **e.diagnostic()}
            elif isinstance(e, TerminalManagerError):
                err_text = str(e)
                if "does not exist" in err_text:
                    message = "Requested tab or terminal does not exist"
                    data = {"field": "terminal", "reason": "not_found"}
                elif "already exists" in err_text:
                    message = "Terminal operation failed: tab_exists"
                    data = {"field": "tab_id", "reason": "tab_exists"}
                elif "Maximum limit" in err_text or "already has" in err_text:
                    message = "Terminal operation failed: tab_full"
                    data = {"field": "pane_id", "reason": "tab_full"}
                elif "congelado" in err_text or "frozen" in err_text.lower():
                    message = "Terminal operation failed: update_frozen"
                    data = {"field": "terminal", "reason": "update_frozen"}
                else:
                    message = "Terminal operation failed"
                    data = {"field": "terminal", "reason": "manager_error"}
            if isinstance(e, TerminalStartError):
                # Preserve actionable spawn/capture evidence without writing paths, argv, environment, or raw exception
                # text to the Core log. The previous logger kept only the exception class, so it could not identify
                # which terminal startup operation failed.
                self.transport.log_diagnostic(
                    "[TERMINAL_START_FAILURE] "
                    f"stage={e.stage} exception_type={e.exception_type} error_code={e.error_code}"
                )
            else:
                self.transport.log_diagnostic(f"IPC handler failed: {type(e).__name__} (content omitted)")
            err_obj: Dict[str, Any] = {"code": RPCErrorCode.INTERNAL_ERROR.value, "message": message}
            if data is not None:
                err_obj["data"] = data
            return RPCResponse(id=req.id, error=err_obj)

    def send_notification(self, event: str, data: Dict[str, Any]) -> bool:
        notif = RPCNotification(event=event, data=data)
        return self.transport.write_message(json.dumps(notif.to_dict()))

    def process_one(self) -> bool:
        raw = self.transport.read_message()
        if raw is None:
            return False

        resp = self.handle_raw_payload(raw)
        if resp is not None:
            try:
                out = json.dumps(resp.to_dict(), allow_nan=False)
            except (TypeError, ValueError, RecursionError, OverflowError):
                self.transport.log_diagnostic("IPC response serialization failed (content omitted)")
                out = json.dumps(RPCResponse(id=resp.id, error={
                    "code": RPCErrorCode.INTERNAL_ERROR.value,
                    "message": "Internal error: invalid handler response",
                }).to_dict())
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
    token = os.environ.get("COORDINATOR_IPC_TOKEN")
    if not token:
        sys.stderr.write("IPC startup refused: ephemeral session token is required.\n")
        raise SystemExit(2)
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
