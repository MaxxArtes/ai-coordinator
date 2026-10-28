"""Tool Broker and Authorization Subsystem for AI Coordinator (Phase 4B.5.3).

Provides centralized, mediated tool execution for worker processes:
1. Deny-by-default access control for all tools and operations.
2. Contextual authorization binding requests to task_id, run_id, worker process identity and project.
3. Cryptographic, ephemeral capability tokens (HMAC-SHA256) with least privilege, expiration, and replay protection.
4. Per-tool execution policies (allow, deny, requires_approval).
5. Strict mutation protection preventing workers from altering checkpoints, database, private tasks, or credentials.
6. Operational rate limits (max calls, payload bytes, timeout).
7. Structured audit trails with correlation IDs preparing for Phase 4B.5.4.
8. File-based mailbox and loopback mediation for sandboxed workers.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import uuid

from .fs_sandbox import FileSandboxSecurityError, validate_contained_path, PathTraversalError, ReparseEscapeError, is_reparse_point_or_symlink
from .process import ProcessSupervisor


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ToolBrokerError(Exception):
    """Base exception for all tool broker errors."""
    pass


class ToolAuditPersistenceError(ToolBrokerError):
    """Raised when audit log persistence fails under fail-closed policy."""
    pass


class ToolAccessDeniedError(ToolBrokerError):
    """Raised when access to a tool is denied by policy, token scope, or default deny."""
    pass


class InvalidCapabilityTokenError(ToolBrokerError):
    """Raised when a capability token is forged, tampered with, or invalid."""
    pass


class CapabilityTokenExpiredError(ToolBrokerError):
    """Raised when a capability token has expired past its validity window."""
    pass


class CapabilityTokenRevokedError(ToolBrokerError):
    """Raised when a capability token has been explicitly revoked."""
    pass


class BrokerRateLimitExceededError(ToolBrokerError):
    """Raised when worker exceeds allowed tool call count or payload size."""
    pass


class CallerIdentityMismatchError(ToolBrokerError):
    """Raised when process identity (PID/creation time) does not match token claim."""
    pass


class MutationNotPermittedError(ToolBrokerError):
    """Raised when a worker attempts an unauthorized mutation against protected assets."""
    pass


class ToolApprovalRequired(ToolBrokerError):
    """Raised when a tool policy specifies requires_approval and human clearance is pending."""
    def __init__(self, message: str, approval_id: str, tool_name: str, parameters: dict):
        super().__init__(message)
        self.approval_id = approval_id
        self.tool_name = tool_name
        self.parameters = parameters


# ---------------------------------------------------------------------------
# Policies and Data Classes
# ---------------------------------------------------------------------------

class ToolPolicyMode(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRES_APPROVAL = "requires_approval"


@dataclass
class CapabilityToken:
    token_id: str
    task_id: str
    run_id: str
    worker_pid: int
    worker_create_time_ns: Optional[int]
    project_root: str
    allowed_tools: List[str]
    created_at: float
    expires_at: float
    max_calls: int
    calls_made: int = 0
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    revoked: bool = False

    def is_expired(self, current_time: Optional[float] = None) -> bool:
        now = time.time() if current_time is None else current_time
        return now > self.expires_at


@dataclass
class ToolBrokerContext:
    task_id: str
    run_id: str
    caller_pid: int
    project_root: Path
    sandbox_root: Optional[Path]
    token: CapabilityToken
    correlation_id: str
    coordinator_private_dir: Optional[Path] = None


@dataclass
class ToolDefinition:
    name: str
    description: str
    handler: Callable[[ToolBrokerContext, dict], Any]
    policy_mode: ToolPolicyMode = ToolPolicyMode.DENY
    is_mutation: bool = False
    allowed_parameters: Optional[Set[str]] = None
    max_payload_bytes: int = 65536
    timeout_seconds: float = 10.0


@dataclass
class BrokerAuditRecord:
    correlation_id: str
    timestamp: str
    task_id: str
    run_id: str
    caller_pid: Optional[int]
    tool_name: str
    decision: str  # allowed, denied, requires_approval, rate_limited, invalid_token, error
    duration_ms: float
    parameters_summary: str
    result_summary: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Cryptographic Capability Token Manager
# ---------------------------------------------------------------------------

class CapabilityTokenManager:
    """Issues and cryptographically validates HMAC-signed ephemeral capability tokens."""

    def __init__(
        self,
        supervisor_secret: Union[str, bytes],
        store: Optional[Any] = None,
        lock: Optional[threading.RLock] = None,
    ):
        if isinstance(supervisor_secret, str):
            self.secret = supervisor_secret.encode("utf-8")
        else:
            self.secret = bytes(supervisor_secret)
        if not self.secret:
            raise ValueError("supervisor_secret obrigatorio para emissao de tokens de capacidade")

        self.store = store
        self._lock = lock or getattr(store, "_lock", None) or threading.RLock()
        self._active_tokens: Dict[str, CapabilityToken] = {}
        self._revoked_token_ids: Set[str] = set()
        self._init_token_table()

    def _init_token_table(self) -> None:
        """Initialize persistent token storage table in SQLite if store is available."""
        if hasattr(self.store, "db") and self.store.db:
            self.store.db.execute("""
                CREATE TABLE IF NOT EXISTS tool_broker_tokens (
                    token_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    worker_pid INTEGER NOT NULL,
                    worker_create_time_ns INTEGER,
                    project_root TEXT NOT NULL,
                    allowed_tools_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    max_calls INTEGER NOT NULL,
                    calls_made INTEGER NOT NULL DEFAULT 0,
                    nonce TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    revoked_at TEXT,
                    revocation_reason TEXT
                )
            """)
            self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_tb_tokens_task ON tool_broker_tokens(task_id, run_id)")
            self.store.db.commit()

    def issue_token(
        self,
        task_id: str,
        run_id: str,
        worker_pid: int,
        project_root: Union[str, Path],
        allowed_tools: List[str],
        worker_create_time_ns: Optional[int] = None,
        ttl_seconds: float = 300.0,
        max_calls: int = 50,
    ) -> str:
        """Issue an authentic, signed capability token scoped to the worker."""
        if os.environ.get("AI_COORDINATOR_ROLE") == "worker":
            raise ToolAccessDeniedError("Workers subordinados estao proibidos de emitir tokens de capacidade")

        now = time.time()
        token_id = uuid.uuid4().hex[:16]
        cap = CapabilityToken(
            token_id=token_id,
            task_id=str(task_id),
            run_id=str(run_id),
            worker_pid=int(worker_pid),
            worker_create_time_ns=int(worker_create_time_ns) if worker_create_time_ns is not None else None,
            project_root=str(Path(project_root).resolve()),
            allowed_tools=list(allowed_tools),
            created_at=now,
            expires_at=now + max(0.01, float(ttl_seconds)),
            max_calls=max(1, int(max_calls)),
            calls_made=0,
            nonce=uuid.uuid4().hex[:12],
            revoked=False,
        )

        payload_dict = {
            "tid": cap.token_id,
            "tsk": cap.task_id,
            "run": cap.run_id,
            "pid": cap.worker_pid,
            "tns": cap.worker_create_time_ns,
            "prj": cap.project_root,
            "tls": cap.allowed_tools,
            "cat": cap.created_at,
            "exp": cap.expires_at,
            "max": cap.max_calls,
            "nnc": cap.nonce,
        }
        payload_bytes = json.dumps(payload_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")
        b64_payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii")

        sig = hmac.new(self.secret, b64_payload.encode("ascii"), hashlib.sha256).hexdigest()
        token_str = f"cap_{token_id}_{b64_payload}_{sig}"

        self._active_tokens[token_id] = cap

        if hasattr(self.store, "db") and self.store.db:
            self.store.db.execute("""
                INSERT OR REPLACE INTO tool_broker_tokens (
                    token_id, task_id, run_id, worker_pid, worker_create_time_ns,
                    project_root, allowed_tools_json, created_at, expires_at,
                    max_calls, calls_made, nonce, revoked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0)
            """, (
                cap.token_id, cap.task_id, cap.run_id, cap.worker_pid, cap.worker_create_time_ns,
                cap.project_root, json.dumps(cap.allowed_tools), cap.created_at, cap.expires_at,
                cap.max_calls, cap.nonce
            ))
            self.store.db.commit()

        return token_str

    def revoke_token(self, token_id: str, reason: str = "revoked") -> None:
        """Revoke a token immediately in memory and SQLite."""
        self._revoked_token_ids.add(token_id)
        if token_id in self._active_tokens:
            self._active_tokens[token_id].revoked = True

        if hasattr(self.store, "db") and self.store.db:
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.store.db.execute("""
                UPDATE tool_broker_tokens
                SET revoked = 1, revoked_at = ?, revocation_reason = ?
                WHERE token_id = ?
            """, (now_iso, reason, token_id))
            self.store.db.commit()

    def verify_token(
        self,
        token_str: str,
        caller_pid: Optional[int] = None,
        tool_name: Optional[str] = None,
    ) -> CapabilityToken:
        """Verify signature, freshness, caller identity, and limits of a capability token."""
        if not isinstance(token_str, str) or not token_str.startswith("cap_"):
            raise InvalidCapabilityTokenError("Formato de token de capacidade invalido")

        parts = token_str.split("_", 3)
        if len(parts) != 4:
            raise InvalidCapabilityTokenError("Estrutura do token de capacidade incorreta")

        _, token_id, b64_payload, signature = parts

        # 1. Cryptographic HMAC verification
        expected_sig = hmac.new(self.secret, b64_payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            raise InvalidCapabilityTokenError("Assinatura HMAC do token de capacidade falsificada ou invalida")

        # 2. Check revocation in memory or database
        if token_id in self._revoked_token_ids:
            raise CapabilityTokenRevokedError(f"Token de capacidade {token_id} foi revogado")

        if hasattr(self.store, "db") and self.store.db:
            try:
                with self._lock:
                    row = self.store.db.execute(
                        "SELECT revoked, calls_made, max_calls, expires_at FROM tool_broker_tokens WHERE token_id = ?",
                        (token_id,)
                    ).fetchone()
                    if row:
                        db_revoked, db_calls, db_max, db_exp = row
                        if db_revoked:
                            self._revoked_token_ids.add(token_id)
                            raise CapabilityTokenRevokedError(f"Token de capacidade {token_id} foi revogado")
                        if db_calls >= db_max:
                            raise BrokerRateLimitExceededError(
                                f"Limite de chamadas do token ({db_max}) atingido pelo worker"
                            )
            except sqlite3.Error as e:
                raise ToolAuditPersistenceError(f"Falha de persistencia na verificacao de tokens: {e}") from e

        # 3. Decode payload
        try:
            payload_bytes = base64.urlsafe_b64decode(b64_payload.encode("ascii"))
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception as e:
            raise InvalidCapabilityTokenError(f"Falha ao decodificar payload do token: {e}")

        # 4. Check expiration
        now = time.time()
        if now > payload.get("exp", 0):
            raise CapabilityTokenExpiredError("Token de capacidade expirado pelo TTL de seguranca")

        # 5. Retrieve or sync in-memory tracker
        cap = self._active_tokens.get(token_id)
        if not cap:
            cap = CapabilityToken(
                token_id=payload["tid"],
                task_id=payload["tsk"],
                run_id=payload["run"],
                worker_pid=payload["pid"],
                worker_create_time_ns=payload.get("tns"),
                project_root=payload["prj"],
                allowed_tools=payload["tls"],
                created_at=payload["cat"],
                expires_at=payload["exp"],
                max_calls=payload["max"],
                nonce=payload["nnc"],
            )
            self._active_tokens[token_id] = cap

        if cap.revoked:
            raise CapabilityTokenRevokedError(f"Token de capacidade {token_id} revogado")

        # 6. Replay & Call limit enforcement (in-memory check)
        if cap.calls_made >= cap.max_calls:
            raise BrokerRateLimitExceededError(
                f"Limite de chamadas do token ({cap.max_calls}) atingido pelo worker"
            )

        # 7. Process Identity Binding (Anti-Spoofing & PID Recycling Protection)
        if cap.worker_pid and cap.worker_pid > 0:
            if caller_pid is None:
                raise CallerIdentityMismatchError(
                    f"PID do processo chamador e obrigatorio para tokens vinculados a workers (PID esperado: {cap.worker_pid})"
                )
            if int(caller_pid) != cap.worker_pid:
                raise CallerIdentityMismatchError(
                    f"Falsificacao de identidade: PID do chamador ({caller_pid}) difere do worker ({cap.worker_pid})"
                )
            # Verify PID creation time if present
            if cap.worker_create_time_ns is not None:
                ident = ProcessSupervisor.capture_identity(int(caller_pid))
                if not ident:
                    raise CallerIdentityMismatchError(
                        f"Processo do worker PID {caller_pid} inexistente no sistema operacional"
                    )
                if abs(ident.create_time_ns - cap.worker_create_time_ns) > 2_000_000_000:  # 2s tolerance
                    raise CallerIdentityMismatchError(
                        "Alerta de seguranca: PID recycling detectado para o processo do worker"
                    )

        # 8. Check tool scope
        if tool_name is not None and tool_name not in cap.allowed_tools and "*" not in cap.allowed_tools:
            raise ToolAccessDeniedError(
                f"Ferramenta '{tool_name}' fora do escopo autorizado do token de capacidade"
            )

        # 9. Atomic increment in SQLite
        if hasattr(self.store, "db") and self.store.db:
            try:
                with self._lock:
                    cursor = self.store.db.execute("""
                        UPDATE tool_broker_tokens
                        SET calls_made = calls_made + 1
                        WHERE token_id = ? AND calls_made < max_calls AND revoked = 0 AND expires_at > ?
                    """, (token_id, now))
                    self.store.db.commit()
                    if cursor.rowcount == 0:
                        check_row = self.store.db.execute(
                            "SELECT revoked, calls_made, max_calls, expires_at FROM tool_broker_tokens WHERE token_id = ?",
                            (token_id,)
                        ).fetchone()
                        if check_row:
                            if check_row[0]:
                                self._revoked_token_ids.add(token_id)
                                raise CapabilityTokenRevokedError(f"Token de capacidade {token_id} foi revogado")
                            if check_row[1] >= check_row[2]:
                                raise BrokerRateLimitExceededError(f"Limite de chamadas do token ({check_row[2]}) atingido pelo worker")
                            if now >= check_row[3]:
                                raise CapabilityTokenExpiredError("Token de capacidade expirado pelo TTL de seguranca")
                        else:
                            self.store.db.execute("""
                                INSERT INTO tool_broker_tokens (
                                    token_id, task_id, run_id, worker_pid, worker_create_time_ns,
                                    project_root, allowed_tools_json, created_at, expires_at,
                                    max_calls, calls_made, nonce, revoked
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0)
                            """, (
                                cap.token_id, cap.task_id, cap.run_id, cap.worker_pid, cap.worker_create_time_ns,
                                cap.project_root, json.dumps(cap.allowed_tools), cap.created_at, cap.expires_at,
                                cap.max_calls, cap.nonce
                            ))
                            self.store.db.commit()
            except sqlite3.Error as e:
                raise ToolAuditPersistenceError(f"Falha de persistencia no contador de chamadas do token: {e}") from e

        cap.calls_made += 1
        return cap


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Registry maintaining available tools, their policies, and handlers."""

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or not re_valid_name(definition.name):
            raise ValueError(f"Nome de ferramenta invalido: {definition.name}")
        self._tools[definition.name] = definition

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "policy_mode": t.policy_mode.value,
                "is_mutation": t.is_mutation,
            }
            for t in self._tools.values()
        ]


def re_valid_name(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name))


# ---------------------------------------------------------------------------
# Built-in Safe Tool Handlers
# ---------------------------------------------------------------------------

def _builtin_workspace_read_file(ctx: ToolBrokerContext, params: dict) -> dict:
    """Read file strictly inside authorized project root or worker sandbox."""
    raw_path = params.get("path")
    if not raw_path or not isinstance(raw_path, str):
        raise ValueError("Parametro 'path' obrigatorio")

    max_bytes = min(1_048_576, int(params.get("max_bytes", 65536)))

    # Determine base root: sandbox_root if specified, else project_root
    base_root = ctx.sandbox_root if ctx.sandbox_root else ctx.project_root

    # Validate against traversal and symlinks
    try:
        safe_target = validate_contained_path(raw_path, base_root)
    except (PathTraversalError, ReparseEscapeError, ValueError) as err:
        raise ToolAccessDeniedError(f"Acesso negado: caminho escapa da raiz autorizada: {err}")

    # Strictly verify target is NOT inside coordinator private directory
    if ctx.coordinator_private_dir:
        coord_priv = Path(ctx.coordinator_private_dir).resolve()
        if coord_priv == safe_target or coord_priv in safe_target.parents:
            raise ToolAccessDeniedError("Acesso negado: leitura de arquivos confidenciais do coordenador e proibida")

    if not safe_target.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {raw_path}")
    if not safe_target.is_file():
        raise ValueError(f"O caminho especificado nao e um arquivo regular: {raw_path}")

    content = safe_target.read_bytes()[:max_bytes]
    text = content.decode("utf-8", errors="replace")
    return {
        "path": raw_path,
        "bytes_read": len(content),
        "content": text,
    }


def _builtin_workspace_list_dir(ctx: ToolBrokerContext, params: dict) -> dict:
    """List directory contents safely within project or sandbox root."""
    subpath = params.get("subpath", ".")
    if not isinstance(subpath, str):
        raise ValueError("Parametro 'subpath' deve ser string")

    base_root = ctx.sandbox_root if ctx.sandbox_root else ctx.project_root
    try:
        safe_target = validate_contained_path(subpath, base_root)
    except (PathTraversalError, ReparseEscapeError, ValueError) as err:
        raise ToolAccessDeniedError(f"Acesso negado: caminho escapa da raiz autorizada: {err}")

    if ctx.coordinator_private_dir:
        coord_priv = Path(ctx.coordinator_private_dir).resolve()
        if coord_priv == safe_target or coord_priv in safe_target.parents:
            raise ToolAccessDeniedError("Acesso negado: listagem de diretorios confidenciais do coordenador e proibida")

    if not safe_target.exists() or not safe_target.is_dir():
        raise FileNotFoundError(f"Diretorio nao encontrado: {subpath}")

    entries = []
    for item in safe_target.iterdir():
        # Hide coordinator private entries
        if ctx.coordinator_private_dir:
            item_res = item.resolve()
            coord_priv = Path(ctx.coordinator_private_dir).resolve()
            if coord_priv == item_res or coord_priv in item_res.parents:
                continue
        entries.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else 0,
        })

    return {"subpath": subpath, "entries": sorted(entries, key=lambda x: x["name"])}


def _builtin_calculate_hash(ctx: ToolBrokerContext, params: dict) -> dict:
    """Compute cryptographic hash of provided content."""
    data = params.get("content", "")
    algo = params.get("algorithm", "sha256").lower()
    if algo not in ("sha256", "sha512", "md5"):
        raise ValueError(f"Algoritmo de hash nao suportado: {algo}")

    h = hashlib.new(algo)
    h.update(str(data).encode("utf-8"))
    return {"algorithm": algo, "digest": h.hexdigest()}


def _builtin_echo_payload(ctx: ToolBrokerContext, params: dict) -> dict:
    """Diagnostic echo tool."""
    return {"echo": params.get("payload")}


def _builtin_denied_stub(ctx: ToolBrokerContext, params: dict) -> Any:
    raise ToolAccessDeniedError("Esta ferramenta e restrita e nao pode ser executada por workers")


# ---------------------------------------------------------------------------
# Central Tool Broker
# ---------------------------------------------------------------------------

class ToolBroker:
    """Central authority and mediator for all delegated tool operations."""

    def __init__(
        self,
        store: Any,
        token_manager: Optional[CapabilityTokenManager] = None,
        registry: Optional[ToolRegistry] = None,
    ):
        self.store = store
        self._lock = getattr(store, "_lock", None) or threading.RLock()
        self.supervisor_key = getattr(store, "supervisor_key", "default_supervisor_key_hex")
        self.coordinator_private_dir = getattr(store, "root", None)

        self.token_manager = token_manager or CapabilityTokenManager(self.supervisor_key, store=self.store, lock=self._lock)
        self.registry = registry or ToolRegistry()
        self.audit_log: List[BrokerAuditRecord] = []
        self.pending_approvals: Dict[str, Dict[str, Any]] = {}

        self._init_audit_table()
        self._register_default_tools()

    def _init_audit_table(self) -> None:
        """Create tool_broker_audit SQLite ledger and immutability triggers if not present."""
        if hasattr(self.store, "db") and self.store.db:
            self.store.db.execute("""
                CREATE TABLE IF NOT EXISTS tool_broker_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    correlation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    caller_pid INTEGER,
                    tool_name TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    parameters_summary TEXT,
                    result_summary TEXT,
                    error TEXT,
                    duration_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            self.store.db.execute("""
                CREATE TRIGGER IF NOT EXISTS prevent_tool_broker_audit_update
                BEFORE UPDATE ON tool_broker_audit
                BEGIN
                    SELECT RAISE(ABORT, 'tool_broker_audit e imutavel: UPDATE proibido');
                END;
            """)
            self.store.db.execute("""
                CREATE TRIGGER IF NOT EXISTS prevent_tool_broker_audit_delete
                BEFORE DELETE ON tool_broker_audit
                BEGIN
                    SELECT RAISE(ABORT, 'tool_broker_audit e imutavel: DELETE proibido');
                END;
            """)
            self.store.db.commit()

    def _register_default_tools(self) -> None:
        """Register canonical standard tools."""
        # 1. Allowed safe tools
        self.registry.register(ToolDefinition(
            name="workspace_read_file",
            description="Leitura autorizada e segura de arquivo do projeto/sandbox",
            handler=_builtin_workspace_read_file,
            policy_mode=ToolPolicyMode.ALLOW,
            is_mutation=False,
            allowed_parameters={"path", "max_bytes"},
        ))
        self.registry.register(ToolDefinition(
            name="workspace_list_dir",
            description="Listagem autorizada de arquivos no diretorio",
            handler=_builtin_workspace_list_dir,
            policy_mode=ToolPolicyMode.ALLOW,
            is_mutation=False,
            allowed_parameters={"subpath"},
        ))
        self.registry.register(ToolDefinition(
            name="calculate_hash",
            description="Calculo criptografico de resumo hash",
            handler=_builtin_calculate_hash,
            policy_mode=ToolPolicyMode.ALLOW,
            is_mutation=False,
            allowed_parameters={"algorithm", "content"},
        ))
        self.registry.register(ToolDefinition(
            name="echo_payload",
            description="Eco diagnostico de payload",
            handler=_builtin_echo_payload,
            policy_mode=ToolPolicyMode.ALLOW,
            is_mutation=False,
            allowed_parameters={"payload"},
        ))

        # 2. Denied privileged tools
        self.registry.register(ToolDefinition(
            name="read_supervisor_secret",
            description="Proibido: tentativa de leitura direta de segredos de autenticacao",
            handler=_builtin_denied_stub,
            policy_mode=ToolPolicyMode.DENY,
            is_mutation=False,
        ))
        self.registry.register(ToolDefinition(
            name="raw_sqlite_query",
            description="Proibido: consulta SQL direta na base de coordenacao",
            handler=_builtin_denied_stub,
            policy_mode=ToolPolicyMode.DENY,
            is_mutation=False,
        ))
        self.registry.register(ToolDefinition(
            name="mutate_checkpoint",
            description="Mutacao de checkpoint cognitivo (exige aprovacao previa)",
            handler=_builtin_denied_stub,
            policy_mode=ToolPolicyMode.REQUIRES_APPROVAL,
            is_mutation=True,
        ))

    def execute(
        self,
        token_str: str,
        tool_name: str,
        parameters: dict,
        caller_pid: Optional[int] = None,
        sandbox_root: Optional[Union[str, Path]] = None,
    ) -> Dict[str, Any]:
        """Authenticate, authorize, limit, execute, and audit a tool operation."""
        start_time = time.monotonic()
        correlation_id = uuid.uuid4().hex[:12]
        iso_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        param_summary = json.dumps(parameters, default=str)[:500]

        # 1. Authenticate Token and Caller Process
        try:
            token = self.token_manager.verify_token(token_str, caller_pid=caller_pid)
        except ToolBrokerError as auth_err:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            self._record_audit(
                correlation_id=correlation_id,
                timestamp=iso_time,
                task_id="unknown",
                run_id="unknown",
                caller_pid=caller_pid,
                tool_name=tool_name,
                decision="rate_limited" if isinstance(auth_err, BrokerRateLimitExceededError) else ("invalid_token" if isinstance(auth_err, (InvalidCapabilityTokenError, CapabilityTokenExpiredError, CapabilityTokenRevokedError)) else "denied"),
                duration_ms=duration_ms,
                parameters_summary=param_summary,
                error=str(auth_err),
            )
            raise

        task_id = token.task_id
        run_id = token.run_id

        # 2. Lookup Tool Definition (Deny-by-default for unregistered tools)
        tool_def = self.registry.get(tool_name)
        if not tool_def:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            msg = f"Ferramenta desconhecida ou nao cadastrada no broker: '{tool_name}'"
            self._record_audit(
                correlation_id=correlation_id,
                timestamp=iso_time,
                task_id=task_id,
                run_id=run_id,
                caller_pid=caller_pid,
                tool_name=tool_name,
                decision="denied",
                duration_ms=duration_ms,
                parameters_summary=param_summary,
                error=msg,
            )
            raise ToolAccessDeniedError(msg)

        # 3. Check Token Tool Scope
        if tool_name not in token.allowed_tools and "*" not in token.allowed_tools:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            msg = f"Ferramenta '{tool_name}' fora do escopo autorizado do token de capacidade"
            self._record_audit(
                correlation_id=correlation_id,
                timestamp=iso_time,
                task_id=task_id,
                run_id=run_id,
                caller_pid=caller_pid,
                tool_name=tool_name,
                decision="denied",
                duration_ms=duration_ms,
                parameters_summary=param_summary,
                error=msg,
            )
            raise ToolAccessDeniedError(msg)

        # 4. Policy Enforcement
        if tool_def.policy_mode == ToolPolicyMode.DENY:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            msg = f"Operacao estritamente proibida por politica restrita do broker: '{tool_name}'"
            self._record_audit(
                correlation_id=correlation_id,
                timestamp=iso_time,
                task_id=task_id,
                run_id=run_id,
                caller_pid=caller_pid,
                tool_name=tool_name,
                decision="denied",
                duration_ms=duration_ms,
                parameters_summary=param_summary,
                error=msg,
            )
            raise ToolAccessDeniedError(msg)

        if tool_def.policy_mode == ToolPolicyMode.REQUIRES_APPROVAL:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            approval_id = uuid.uuid4().hex[:16]
            self.pending_approvals[approval_id] = {
                "approval_id": approval_id,
                "task_id": task_id,
                "run_id": run_id,
                "tool_name": tool_name,
                "parameters": parameters,
                "created_at": iso_time,
                "status": "pending",
            }
            msg = f"A operacao '{tool_name}' requer aprovacao humana previa (ID: {approval_id})"
            self._record_audit(
                correlation_id=correlation_id,
                timestamp=iso_time,
                task_id=task_id,
                run_id=run_id,
                caller_pid=caller_pid,
                tool_name=tool_name,
                decision="requires_approval",
                duration_ms=duration_ms,
                parameters_summary=param_summary,
                error=msg,
            )
            raise ToolApprovalRequired(msg, approval_id=approval_id, tool_name=tool_name, parameters=parameters)

        # 4. Payload Size Limit
        encoded_payload_len = len(json.dumps(parameters))
        if encoded_payload_len > tool_def.max_payload_bytes:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            msg = f"Tamanho do payload ({encoded_payload_len} bytes) excede o limite ({tool_def.max_payload_bytes} bytes)"
            self._record_audit(
                correlation_id=correlation_id,
                timestamp=iso_time,
                task_id=task_id,
                run_id=run_id,
                caller_pid=caller_pid,
                tool_name=tool_name,
                decision="rate_limited",
                duration_ms=duration_ms,
                parameters_summary=param_summary,
                error=msg,
            )
            raise BrokerRateLimitExceededError(msg)

        # 5. Parameter Validation
        if tool_def.allowed_parameters is not None:
            for k in parameters.keys():
                if k not in tool_def.allowed_parameters:
                    duration_ms = (time.monotonic() - start_time) * 1000.0
                    msg = f"Parametro nao permitido para a ferramenta '{tool_name}': '{k}'"
                    self._record_audit(
                        correlation_id=correlation_id,
                        timestamp=iso_time,
                        task_id=task_id,
                        run_id=run_id,
                        caller_pid=caller_pid,
                        tool_name=tool_name,
                        decision="denied",
                        duration_ms=duration_ms,
                        parameters_summary=param_summary,
                        error=msg,
                    )
                    raise ToolAccessDeniedError(msg)

        # 6. Strict Mutation Defense
        if tool_def.is_mutation:
            # Check target files/paths for unauthorized mutation
            for k in ("path", "target", "file", "destination"):
                val = parameters.get(k)
                if val and isinstance(val, str):
                    if "checkpoint" in val or "coordination" in val or "supervisor" in val or "auth" in val:
                        duration_ms = (time.monotonic() - start_time) * 1000.0
                        msg = f"Tentativa de mutacao de ativo confidencial bloqueada: '{val}'"
                        self._record_audit(
                            correlation_id=correlation_id,
                            timestamp=iso_time,
                            task_id=task_id,
                            run_id=run_id,
                            caller_pid=caller_pid,
                            tool_name=tool_name,
                            decision="denied",
                            duration_ms=duration_ms,
                            parameters_summary=param_summary,
                            error=msg,
                        )
                        raise MutationNotPermittedError(msg)

        # 7. Pre-execution fail-closed audit persistence: record authorization BEFORE running handler
        self._record_audit(
            correlation_id=correlation_id,
            timestamp=iso_time,
            task_id=task_id,
            run_id=run_id,
            caller_pid=caller_pid,
            tool_name=tool_name,
            decision="allowed",
            duration_ms=0.0,
            parameters_summary=param_summary,
        )

        # 8. Execute Tool Handler
        ctx = ToolBrokerContext(
            task_id=task_id,
            run_id=run_id,
            caller_pid=int(caller_pid or token.worker_pid),
            project_root=Path(token.project_root),
            sandbox_root=Path(sandbox_root).resolve() if sandbox_root else None,
            token=token,
            correlation_id=correlation_id,
            coordinator_private_dir=Path(self.coordinator_private_dir).resolve() if self.coordinator_private_dir else None,
        )

        try:
            result = tool_def.handler(ctx, parameters)
            duration_ms = (time.monotonic() - start_time) * 1000.0
            res_summary = json.dumps(result, default=str)[:500]

            self._record_audit(
                correlation_id=correlation_id,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                task_id=task_id,
                run_id=run_id,
                caller_pid=caller_pid,
                tool_name=tool_name,
                decision="completed",
                duration_ms=duration_ms,
                parameters_summary=param_summary,
                result_summary=res_summary,
            )
            return {
                "status": "success",
                "result": result,
                "correlation_id": correlation_id,
                "duration_ms": duration_ms,
            }
        except Exception as e:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            self._record_audit(
                correlation_id=correlation_id,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                task_id=task_id,
                run_id=run_id,
                caller_pid=caller_pid,
                tool_name=tool_name,
                decision="error",
                duration_ms=duration_ms,
                parameters_summary=param_summary,
                error=str(e),
            )
            raise

    def approve_request(self, approval_id: str, supervisor_key: str) -> bool:
        """Approve a pending human-in-the-loop tool operation."""
        if not supervisor_key or not hmac.compare_digest(str(supervisor_key).strip(), str(self.supervisor_key).strip()):
            raise ToolAccessDeniedError("Chave de supervisor invalida para aprovacao de operacao")

        req = self.pending_approvals.get(approval_id)
        if not req:
            return False
        req["status"] = "approved"
        req["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return True

    def process_mailbox(
        self,
        sandbox_root: Union[str, Path],
        expected_worker_pid: int,
    ) -> List[Dict[str, Any]]:
        """Process requests sent through the file-based sandbox mailbox with anti-symlink and anti-spoofing protection."""
        if expected_worker_pid is None or int(expected_worker_pid) <= 0:
            raise CallerIdentityMismatchError("Mailbox exige identificacao confiavel do worker supervisor (expected_worker_pid obrigatorio e positivo)")

        sbox = Path(sandbox_root).resolve()
        req_dir = sbox / ".broker" / "requests"
        res_dir = sbox / ".broker" / "responses"

        if not req_dir.exists():
            return []

        res_dir.mkdir(parents=True, exist_ok=True)
        results = []

        for req_file in sorted(req_dir.glob("*.json")):
            try:
                # Anti-symlink and path containment protection
                if req_file.is_symlink() or is_reparse_point_or_symlink(req_file):
                    try:
                        req_file.unlink()
                    except Exception:
                        pass
                    raise ReparseEscapeError(
                        f"Link simbolico ou reparse point detectado em arquivo de requisicao: {req_file.name}"
                    )

                validate_contained_path(req_file, req_dir)

                raw = req_file.read_text(encoding="utf-8")
                payload = json.loads(raw)
                token_str = payload.get("token")
                tool_name = payload.get("tool")
                params = payload.get("parameters", {})
                declared_pid = payload.get("caller_pid")

                # Anti-spoofing: verify caller PID against sandbox's expected worker PID
                if declared_pid is not None and int(declared_pid) != int(expected_worker_pid):
                    is_descendant = False
                    if os.name != "nt":
                        try:
                            ppid_out = subprocess.check_output(
                                ["ps", "-o", "ppid=", "-p", str(int(declared_pid))],
                                text=True,
                                stderr=subprocess.DEVNULL,
                            ).strip()
                            if ppid_out and int(ppid_out) == int(expected_worker_pid):
                                is_descendant = True
                        except Exception:
                            pass
                    if not is_descendant:
                        raise CallerIdentityMismatchError(
                            f"Mailbox spoofing detectado: caller_pid ({declared_pid}) diverge do worker da sandbox ({expected_worker_pid})"
                        )
                effective_pid = int(expected_worker_pid)

                res = self.execute(
                    token_str=token_str,
                    tool_name=tool_name,
                    parameters=params,
                    caller_pid=effective_pid,
                    sandbox_root=sbox,
                )
                status_obj = {"status": "success", "response": res}
            except (
                CallerIdentityMismatchError,
                InvalidCapabilityTokenError,
                MutationNotPermittedError,
                FileSandboxSecurityError,
                ToolAuditPersistenceError,
            ) as fatal_sec_err:
                # Critical security & integrity violations classified as fatal:
                # DO NOT mask as JSON response! Propagate directly so on_poll / supervisor immediately terminates worker.
                raise
            except Exception as e:
                # Recoverable operational errors (e.g. rate limit, tool not allowed, approval required, expired/revoked token, handler error)
                status_obj = {"status": "error", "error": type(e).__name__, "message": str(e)}

            res_file = res_dir / f"res_{req_file.stem}.json"
            try:
                validate_contained_path(res_file, res_dir)
                if res_file.exists() and (res_file.is_symlink() or is_reparse_point_or_symlink(res_file)):
                    try:
                        res_file.unlink()
                    except Exception:
                        pass
                    raise ReparseEscapeError(f"Symlink detectado no caminho de resposta da mailbox: {res_file.name}")
                res_file.write_text(json.dumps(status_obj, indent=2), encoding="utf-8")
            except (ReparseEscapeError, FileSandboxSecurityError):
                raise
            except Exception as res_err:
                raise OSError(f"Falha de seguranca ao gravar resposta da mailbox ({res_file.name}): {res_err}") from res_err

            try:
                if req_file.exists():
                    req_file.unlink()
            except Exception as unlink_err:
                raise OSError(f"Falha de seguranca ao remover requisicao processada da mailbox ({req_file.name}): {unlink_err}") from unlink_err
            results.append(status_obj)

        return results

    def _record_audit(
        self,
        correlation_id: str,
        timestamp: str,
        task_id: str,
        run_id: str,
        caller_pid: Optional[int],
        tool_name: str,
        decision: str,
        duration_ms: float,
        parameters_summary: str,
        result_summary: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record audit event in memory and SQLite under a fail-closed policy."""
        record = BrokerAuditRecord(
            correlation_id=correlation_id,
            timestamp=timestamp,
            task_id=task_id,
            run_id=run_id,
            caller_pid=caller_pid,
            tool_name=tool_name,
            decision=decision,
            duration_ms=duration_ms,
            parameters_summary=parameters_summary,
            result_summary=result_summary,
            error=error,
        )
        self.audit_log.append(record)

        if hasattr(self.store, "db") and self.store.db:
            try:
                with getattr(self, "_lock", threading.Lock()):
                    self.store.db.execute("""
                        INSERT INTO tool_broker_audit (
                            correlation_id, task_id, run_id, caller_pid,
                            tool_name, decision, parameters_summary,
                            result_summary, error, duration_ms, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        correlation_id, task_id, run_id, caller_pid,
                        tool_name, decision, parameters_summary,
                        result_summary, error, duration_ms, timestamp
                    ))
                    self.store.db.commit()
            except Exception as e:
                raise ToolAuditPersistenceError(f"Falha critica ao persistir auditoria do broker: {e}") from e

    def query_audit_log(
        self,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        decision: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query audited tool decisions."""
        if hasattr(self.store, "db"):
            query = "SELECT * FROM tool_broker_audit WHERE 1=1"
            params: list[Any] = []
            if task_id:
                query += " AND task_id=?"
                params.append(task_id)
            if run_id:
                query += " AND run_id=?"
                params.append(run_id)
            if decision:
                query += " AND decision=?"
                params.append(decision)
            query += " ORDER BY id ASC"
            cursor = self.store.db.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

        # Fallback to in-memory audit log
        res = []
        for r in self.audit_log:
            if task_id and r.task_id != task_id:
                continue
            if run_id and r.run_id != run_id:
                continue
            if decision and r.decision != decision:
                continue
            res.append(asdict(r))
        return res
