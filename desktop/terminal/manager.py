"""Terminal Manager for AI Coordinator Desktop (Marco 4D.2).

Orchestrates multiple tabs, up to 4 pane terminals per tab, and enforces
Core supervision, project path fencing, and fail-closed security.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ai_coordinator.human_workspace import HumanPrincipal
from ai_coordinator.process import ProcessSupervisor

from desktop.auth.authorizer import DesktopAuthorizer, SecurityAuthorizationError
from desktop.auth.profile import DesktopProfileManager

from .buffer import SessionLayoutExport
from .interpreters import DEFAULT_INTERPRETER_ID, InterpreterError, discover_interpreters, resolve_interpreter
from .pty_backend import BasePtyBackend
from .session import SessionKind, TerminalSession

MAX_PANES_PER_TAB = 4


class TerminalManagerError(Exception):
    """Base exception for terminal manager operations."""


class TerminalStartError(TerminalManagerError):
    """Safe terminal startup diagnostic; never stores argv, cwd, environment, or raw exception text."""

    def __init__(self, stage: str, exception_type: str, error_code: Optional[int] = None) -> None:
        super().__init__("Terminal operation failed")
        self.stage = stage
        self.exception_type = exception_type
        self.error_code = error_code

    def diagnostic(self) -> dict:
        return {"stage": self.stage, "exception_type": self.exception_type, "error_code": self.error_code}


class TerminalInterpreterError(TerminalManagerError):
    """Fixed, path-free reason that a selected installed shell cannot start here."""

    MESSAGES = {
        "unavailable": "O interpretador selecionado deixou de estar disponível. Escolha outro terminal.",
        "unsupported_cwd": "O diretório atual não pode ser mapeado para Bash via WSL. Use Git Bash ou um diretório em unidade local.",
    }

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in self.MESSAGES else "unavailable"
        super().__init__(self.MESSAGES[self.reason])


def _session_is_alive(session: Any) -> bool:
    """Safely check liveness supporting @property bool (TerminalSession) or callable mocks."""
    attr = getattr(session, "is_alive", False)
    return bool(attr() if callable(attr) else attr)


@dataclass
class TabInfo:
    tab_id: str
    title: str
    project_path: str
    panes: Dict[int, TerminalSession] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        panes_dict = {str(pane_id): sess.to_dict() for pane_id, sess in self.panes.items()}
        active_count = sum(1 for s in self.panes.values() if _session_is_alive(s))
        return {
            "tab_id": self.tab_id,
            "title": self.title,
            "project_path": self.project_path,
            "panes": panes_dict,
            "panes_count": len(self.panes),
            "active_panes_count": active_count,
        }


class TerminalManager:
    """Manages tabs, pane sessions, and integrates with Core ProcessSupervisor."""

    def __init__(self, authorizer: DesktopAuthorizer) -> None:
        self.authorizer = authorizer
        self.tabs: Dict[str, TabInfo] = {}
        self.sessions: Dict[str, TerminalSession] = {}  # session_id -> TerminalSession
        self._lock = threading.RLock()
        self._update_frozen: bool = False

    def freeze_for_update(self) -> Dict[str, Any]:
        """Atomically freeze new session/tab creation and return authoritative counts."""
        with self._lock:
            self._update_frozen = True
            tabs_dicts = [t.to_dict() for t in self.tabs.values()]
            total_panes = sum(t.get("panes_count", 0) for t in tabs_dicts)
            total_active = sum(t.get("active_panes_count", 0) for t in tabs_dicts)
            return {
                "frozen": True,
                "tabs": tabs_dicts,
                "panes_total": total_panes,
                "active_sessions_total": total_active,
            }

    def unfreeze_update(self) -> Dict[str, Any]:
        """Release update freeze lock so normal terminal operations may resume."""
        with self._lock:
            self._update_frozen = False
            return {"frozen": False}

    def create_tab(self, token: str, tab_id: str, title: str, project_path: Union[str, Path]) -> TabInfo:
        """Create a new tab bound to an authorized project path."""
        with self._lock:
            if self._update_frozen:
                raise TerminalManagerError(
                    "Criação de abas bloqueada: sistema congelado para atualização em andamento."
                )

            # 1. Authorize project access
            self.authorizer.authorize_project_access(token, project_path)
            canon_path = str(Path(project_path).resolve())

            if tab_id in self.tabs:
                existing = self.tabs[tab_id]
                try:
                    same_dir = str(Path(existing.project_path).resolve()) == canon_path
                except OSError:
                    same_dir = existing.project_path == canon_path
                if same_dir:
                    return existing
                raise TerminalManagerError(f"Tab with ID '{tab_id}' already exists")

            tab = TabInfo(tab_id=tab_id, title=title, project_path=canon_path)
            self.tabs[tab_id] = tab
            return tab

    def get_tab(self, tab_id: str) -> Optional[TabInfo]:
        with self._lock:
            return self.tabs.get(tab_id)

    def list_interpreters(self, token: str) -> List[dict]:
        """List allow-listed, locally available interactive shells after validating the active IPC session."""
        self.authorizer.validate_session(token)
        return [item.public_dict() for item in discover_interpreters()]

    def validate_interpreter(self, token: str, interpreter_id: str, cwd: Union[str, Path]) -> dict:
        """Check shell availability, path compatibility, and project authorization before a UI replaces a session."""
        target_cwd = Path(cwd)
        self.authorizer.authorize_project_access(token, target_cwd)
        try:
            interpreter, _command, _launch_cwd, _extra_env = resolve_interpreter(
                interpreter_id, str(target_cwd), available=discover_interpreters()
            )
        except InterpreterError as exc:
            raise TerminalInterpreterError(exc.reason) from None
        return interpreter.public_dict()

    def list_tabs(self) -> List[TabInfo]:
        with self._lock:
            return list(self.tabs.values())

    def create_terminal(
        self,
        token: str,
        tab_id: str,
        pane_id: int,
        kind: SessionKind = SessionKind.HUMAN_TERMINAL,
        command: Optional[List[str]] = None,
        interpreter_id: Optional[str] = None,
        cwd: Optional[Union[str, Path]] = None,
        cols: int = 80,
        rows: int = 24,
        backend: Optional[BasePtyBackend] = None,
    ) -> TerminalSession:
        """Spawn a new terminal session in a tab, in a free pane slot (0-3).

        The identity of a terminal is its ``session_id``; ``pane_id`` is only a slot. A registered session, alive or
        ended by ``exit``, keeps its slot until it is closed and is never overwritten (F10): when the requested slot is
        taken, the lowest free slot is used instead, and the session reports the slot it got.
        """
        with self._lock:
            if self._update_frozen:
                raise TerminalManagerError(
                    "Criação de terminais bloqueada: sistema congelado para atualização em andamento."
                )

            # 1. Validate pane index
            if pane_id < 0 or pane_id >= MAX_PANES_PER_TAB:
                raise TerminalManagerError(
                    f"Invalid pane index {pane_id}. Must be between 0 and {MAX_PANES_PER_TAB - 1}."
                )

            tab = self.tabs.get(tab_id)
            if not tab:
                raise TerminalManagerError(f"Tab '{tab_id}' does not exist")

            if pane_id in tab.panes:
                free = [slot for slot in range(MAX_PANES_PER_TAB) if slot not in tab.panes]
                if not free:
                    raise TerminalManagerError(
                        f"Tab '{tab_id}' already has {MAX_PANES_PER_TAB} registered terminals "
                        "(ended ones included): close one before opening another"
                    )
                pane_id = free[0]

            # 2. Determine target working directory
            target_cwd = Path(cwd) if cwd else Path(tab.project_path)

            # 3. Validate authorization and directory boundary
            self.authorizer.authorize_project_access(token, target_cwd)
            profile_name = self.authorizer.validate_session(token)
            principal = self.authorizer.create_human_principal(token)

            # 4. Human terminals use an installed, allow-listed interpreter. Other command use remains explicit.
            interpreter = None
            shell_env: Dict[str, str] = {}
            launch_cwd = target_cwd
            use_installed_shell = interpreter_id is not None or (
                not command and kind == SessionKind.HUMAN_TERMINAL and sys.platform == "win32"
            )
            if use_installed_shell:
                if kind != SessionKind.HUMAN_TERMINAL:
                    raise TerminalManagerError("A seleção de interpretador é exclusiva de terminais humanos.")
                if interpreter_id is not None and not isinstance(interpreter_id, str):
                    raise TerminalManagerError("O identificador do interpretador é inválido.")
                requested = interpreter_id or DEFAULT_INTERPRETER_ID
                try:
                    interpreter, resolved_command, resolved_launch_cwd, shell_env = resolve_interpreter(
                        requested, str(target_cwd), available=discover_interpreters()
                    )
                except InterpreterError as exc:
                    raise TerminalInterpreterError(exc.reason) from None
                command = resolved_command
                if resolved_launch_cwd:
                    launch_cwd = Path(resolved_launch_cwd)
            elif not command:
                if sys.platform == "win32":
                    command = ["cmd.exe"]
                else:
                    shell = os.environ.get("SHELL", "/bin/sh")
                    command = [shell]

            # 5. Build and start session (spawn from launch_cwd while preserving logical authorized target_cwd)
            session = TerminalSession(
                tab_id=tab_id,
                pane_id=pane_id,
                kind=kind,
                command=command,
                cwd=launch_cwd,
                profile_name=profile_name,
                principal=principal,
                backend=backend,
                interpreter_id=interpreter.id if interpreter else None,
                interpreter_label=interpreter.label if interpreter else None,
                cols=cols,
                rows=rows,
            )

            # Build sanitized child process environment
            # Critical hygiene: NEVER propagate COORDINATOR_IPC_TOKEN or internal supervisor credentials
            env = dict(os.environ)
            env.update(shell_env)
            for k in list(env.keys()):
                kl = k.lower()
                if kl in ("coordinator_ipc_token", "coordinator_supervisor_secret") or "supervisor_secret" in kl:
                    env.pop(k, None)

            # If governed agent, enforce worker environment sanitization
            if kind == SessionKind.GOVERNED_AGENT:
                env["AI_COORDINATOR_ROLE"] = "worker"
                # Strip sensitive secrets from agent environment
                for k in list(env.keys()):
                    kl = k.lower()
                    if any(s in kl for s in ("secret", "token", "password", "key")):
                        env.pop(k, None)

            # The human terminal runs with the operator's own rights (no isolation): a 256 MB cap on its whole process
            # tree only made ordinary tools die at start (Claude Code, OpenCode: allocation failures). The tree is still
            # contained and killed on close. Governed agents keep the cap.
            memory_cap_mb = 256 if kind == SessionKind.GOVERNED_AGENT else None
            try:
                session.start(env=env, max_memory_mb=memory_cap_mb)
            except Exception as exc:
                code = getattr(exc, "winerror", None)
                if code is None:
                    code = getattr(exc, "errno", None)
                if not isinstance(code, int):
                    code = None
                raise TerminalStartError("spawn", type(exc).__name__, code) from exc
            session.cwd = target_cwd.resolve()

            # Core ProcessSupervisor integration: capture identity & bind
            if session.pid:
                try:
                    proc_identity = ProcessSupervisor.capture_identity(session.pid)
                except Exception as exc:
                    # A child exists only after a successful, supervised spawn; close that exact session before failing.
                    closed = session.terminate()
                    code = getattr(exc, "winerror", None)
                    if code is None:
                        code = getattr(exc, "errno", None)
                    if not isinstance(code, int):
                        code = None
                    if not closed:
                        tab.panes[pane_id] = session
                        self.sessions[session.session_id] = session
                    raise TerminalStartError("capture_identity", type(exc).__name__, code) from exc
                session.identity = proc_identity

            tab.panes[pane_id] = session
            self.sessions[session.session_id] = session
            return session

    def get_session(self, session_id: str) -> Optional[TerminalSession]:
        return self.sessions.get(session_id)

    def write_to_terminal(self, session_id: str, data: Union[str, bytes]) -> int:
        session = self.get_session(session_id)
        if not session:
            raise TerminalManagerError(f"Terminal session '{session_id}' not found")
        return session.write(data)

    def resize_terminal(self, session_id: str, cols: int, rows: int) -> bool:
        session = self.get_session(session_id)
        if not session:
            raise TerminalManagerError(f"Terminal session '{session_id}' not found")
        return session.resize(cols, rows)

    def close_terminal(self, session_id: str) -> bool:
        with self._lock:
            session = self.get_session(session_id)
            if not session:
                return False

            # Keep the session supervised and its slot reserved until termination is confirmed.
            # A false result or exception leaves both registries intact, so the operator can retry (F10).
            if session.terminate() is not True:
                return False

            # Only this very session leaves its slot: never the one that may occupy the slot later (F10).
            tab = self.tabs.get(session.tab_id)
            if tab and tab.panes.get(session.pane_id) is session:
                tab.panes.pop(session.pane_id, None)

            self.sessions.pop(session_id, None)
            return True

    def close_tab(self, tab_id: str) -> bool:
        """Close tab and cleanly terminate all contained terminals."""
        with self._lock:
            tab = self.tabs.get(tab_id)
            if not tab:
                return False
            # close_terminal uses this same RLock. Failed sessions stay in the tab, with their slots reserved.
            for session in list(tab.panes.values()):
                if not self.close_terminal(session.session_id):
                    return False
            self.tabs.pop(tab_id, None)
            return True

    def save_tab_sessions(self, tab_id: str) -> SessionLayoutExport:
        """Export sanitized visual buffer and layout metadata for the tab."""
        with self._lock:
            tab = self.tabs.get(tab_id)
            if not tab:
                raise TerminalManagerError(f"Tab '{tab_id}' not found")

            buffers: Dict[str, List[str]] = {}
            for pane_id, session in tab.panes.items():
                buffers[f"pane_{pane_id}"] = session.get_sanitized_export()

            active_prof = self.authorizer.profile_manager.get_active_profile().name
            return SessionLayoutExport(
                tab_id=tab.tab_id,
                profile_name=active_prof,
                project_path=tab.project_path,
                terminals=buffers,
                metadata={"title": tab.title, "pane_count": len(tab.panes)},
            )

    def terminate_all(self) -> None:
        """Cleanly terminate all terminal sessions across all tabs."""
        with self._lock:
            sessions_to_terminate = list(self.sessions.values())
            self.sessions.clear()
            self.tabs.clear()

        for session in sessions_to_terminate:
            session.terminate()
