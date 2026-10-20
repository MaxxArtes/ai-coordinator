"""availability.py: Estado de disponibilidade compartilhado em SQLite com TTL."""

import json
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple


def provider_of(client: str, model: str) -> str:
    """Determina o provider com base no client e no model original."""
    if client == "opencode":
        parts = model.split("/", 1)
        return parts[0] if parts[0] else client
    return client


def normalize_model(client: str, model: str) -> str:
    """Retorna modelo sem prefixo provider/ apenas se client for opencode."""
    if client == "opencode" and "/" in model:
        return model.split("/", 1)[1]
    return model


def classify(client: str, model: str, message: str) -> Dict[str, Any]:
    """Classifica mensagens de erro determinando kind, scope, cooldown e detail sanitizado.

    Usa o modelo original como chave, sem equivalência entre rotas distintas.
    """
    msg_raw = str(message or "")
    # Sanitização: remove possíveis chaves/tokens e cabeçalhos sensíveis
    sanitized = re.sub(
        r"(Bearer\s+[A-Za-z0-9_\-\.]+)|([a-zA-Z0-9_\-]{24,})",
        "[REDACTED]",
        msg_raw,
        flags=re.IGNORECASE,
    )
    detail = sanitized.strip()[:350]
    msg_lower = msg_raw.lower()

    # Quota explícita de conta/saldo
    quota_terms = [
        "usage limit",
        "quota exceeded",
        "insufficient credits",
        "credit balance",
        "usage limit reached",
        "out of extra usage",
        "cota esgotada",
    ]
    is_explicit_quota = any(term in msg_lower for term in quota_terms)

    # 429 / Rate limit genérico
    is_429 = (
        "429" in msg_lower
        or "too many requests" in msg_lower
        or "model rate limit" in msg_lower
        or "rate limit" in msg_lower
        or "rate-limited" in msg_lower
    )

    # Autenticação
    is_auth = (
        "401" in msg_lower
        or "authentication" in msg_lower
        or "invalid key" in msg_lower
        or "unauthorized" in msg_lower
    )

    # Permissão / Forbidden: apenas status HTTP 403 ou códigos de autorizacao estruturados de API
    is_forbidden = (
        bool(re.search(r"\b403\b", msg_lower))
        or "status: 403" in msg_lower
        or "http 403" in msg_lower
        or "forbidden: access denied" in msg_lower
        or "permission_denied" in msg_lower
        or "access denied to model" in msg_lower
        or "insufficient permissions for model" in msg_lower
    )

    # Not found / Endpoints
    is_not_found = (
        "404" in msg_lower
        or "model not found" in msg_lower
        or "no endpoints" in msg_lower
    )

    # Timeout
    is_timeout = (
        "timeout" in msg_lower
        or "timed out" in msg_lower
        or "tempo limite" in msg_lower
    )

    # Invalid output
    is_invalid_output = "invalid output" in msg_lower

    # 0. Ponte do ChatGPT web sem servidor/login: pausa o cliente, nao so o modelo,
    # para cada chamada nao gastar uma tentativa esperando a tarefa agendada.
    if client in ("chatgptweb", "geminiweb") and "fora do ar" in msg_lower:
        return {
            "kind": "offline",
            "scope": "client",
            "cooldown": 600,
            "detail": detail,
        }

    # 1. Quota explícita
    if is_explicit_quota:
        if client in ("codex", "claude", "agy", "chatgptweb", "geminiweb"):
            return {
                "kind": "quota",
                "scope": "client",
                "cooldown": 1800,
                "detail": detail,
            }
        if client in ("opencode", "openrouter") and any(term in msg_lower for term in ("insufficient credits", "credit balance", "account quota", "usage limit")):
            # Apenas se for saldo/cota explícito de conta
            return {
                "kind": "quota",
                "scope": "provider",
                "cooldown": 1800,
                "detail": detail,
            }

    # 2. 429 genérico (não promove a quota de conta)
    if is_429:
        return {
            "kind": "rate_limit",
            "scope": "model",
            "cooldown": 300,
            "detail": detail,
        }

    # 3. 401 / Autenticação
    if is_auth:
        return {
            "kind": "auth",
            "scope": "provider",
            "cooldown": 3600,
            "detail": detail,
        }

    # 4. 403 / Permissão (nunca bloqueia client/provider inteiro)
    if is_forbidden:
        return {
            "kind": "permission",
            "scope": "model",
            "cooldown": 3600,
            "detail": detail,
        }

    # 5. 404 / Model not found
    if is_not_found:
        return {
            "kind": "not_found",
            "scope": "model",
            "cooldown": 900,
            "detail": detail,
        }

    # 6. Timeout
    if is_timeout:
        return {
            "kind": "timeout",
            "scope": "model",
            "cooldown": 120,
            "detail": detail,
        }

    # 7. Invalid output
    if is_invalid_output:
        return {
            "kind": "invalid_output",
            "scope": "model",
            "cooldown": 60,
            "detail": detail,
        }

    # 8. Erro desconhecido
    return {
        "kind": "unknown_error",
        "scope": "model",
        "cooldown": 60,
        "detail": detail,
    }


class Health:
    """Gerenciador de integridade e catálogo com persistência SQLite e TTL."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._init_db()

    def _init_db(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS route_health (
                key TEXT PRIMARY KEY,
                client TEXT,
                provider TEXT,
                model TEXT,
                kind TEXT,
                detail TEXT,
                observed REAL,
                retry_at REAL,
                expires REAL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS catalogs (
                client TEXT,
                provider TEXT,
                ids_json TEXT,
                observed REAL,
                expires REAL,
                error TEXT,
                PRIMARY KEY (client, provider)
            )
            """
        )
        self.conn.commit()

    def record(
        self,
        client: str,
        model: str,
        success: bool,
        message: str = "",
        at: Optional[float] = None,
        retry_after: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Registra sucesso ou falha com upsert. Não remove bloqueios prévios de client/provider."""
        now = time.time() if at is None else float(at)
        prov = provider_of(client, model)
        cursor = self.conn.cursor()

        if success:
            key = f"model|{client}|{prov}|{model}"
            record_data = {
                "key": key,
                "client": client,
                "provider": prov,
                "model": model,
                "kind": "success",
                "detail": (message or "")[:350],
                "observed": now,
                "retry_at": 0.0,
                "expires": now + 600.0,
            }
            cursor.execute(
                """
                INSERT INTO route_health (key, client, provider, model, kind, detail, observed, retry_at, expires)
                VALUES (:key, :client, :provider, :model, :kind, :detail, :observed, :retry_at, :expires)
                ON CONFLICT(key) DO UPDATE SET
                    kind = excluded.kind,
                    detail = excluded.detail,
                    observed = excluded.observed,
                    retry_at = excluded.retry_at,
                    expires = excluded.expires
                """,
                record_data,
            )
            self.conn.commit()
            return record_data

        # Caso de Falha
        info = classify(client, model, message)
        scope = info["scope"]
        cooldown = float(info["cooldown"])

        if retry_after is not None:
            try:
                cooldown = max(0.0, min(86400.0, float(retry_after)))
            except (ValueError, TypeError):
                pass

        retry_at = now + cooldown
        expires = retry_at

        if scope == "client":
            key = f"client|{client}"
            db_provider = ""
            db_model = ""
        elif scope == "provider":
            key = f"provider|{client}|{prov}"
            db_provider = prov
            db_model = ""
        else:  # model
            key = f"model|{client}|{prov}|{model}"
            db_provider = prov
            db_model = model

        record_data = {
            "key": key,
            "client": client,
            "provider": db_provider,
            "model": db_model,
            "kind": info["kind"],
            "detail": info["detail"],
            "observed": now,
            "retry_at": retry_at,
            "expires": expires,
        }

        cursor.execute(
            """
            INSERT INTO route_health (key, client, provider, model, kind, detail, observed, retry_at, expires)
            VALUES (:key, :client, :provider, :model, :kind, :detail, :observed, :retry_at, :expires)
            ON CONFLICT(key) DO UPDATE SET
                kind = excluded.kind,
                detail = excluded.detail,
                observed = excluded.observed,
                retry_at = excluded.retry_at,
                expires = excluded.expires
            """,
            record_data,
        )
        self.conn.commit()
        return record_data

    def status(
        self, client: str, model: str, at: Optional[float] = None
    ) -> Dict[str, Any]:
        """Consulta status por prioridade: client > provider > model."""
        now = time.time() if at is None else float(at)
        prov = provider_of(client, model)
        cursor = self.conn.cursor()

        key_client = f"client|{client}"
        key_provider = f"provider|{client}|{prov}"
        key_model = f"model|{client}|{prov}|{model}"

        keys = [key_client, key_provider, key_model]
        placeholders = ",".join("?" for _ in keys)
        cursor.execute(
            f"SELECT key, kind, detail, observed, retry_at, expires FROM route_health WHERE key IN ({placeholders})",
            keys,
        )
        rows = {row[0]: row for row in cursor.fetchall()}

        # 1. Verifica bloqueios ativos (cooldown) na ordem estrita de prioridade
        for target_key in keys:
            if target_key in rows:
                r = rows[target_key]
                # row can be tuple or sqlite3.Row
                kind = r[1]
                detail = r[2]
                retry_at = r[4]
                if kind != "success" and retry_at > now:
                    return {
                        "state": "cooldown",
                        "kind": kind,
                        "retry_at": retry_at,
                        "detail": detail,
                    }

        # 2. Verifica sucesso recente para a chave de modelo
        if key_model in rows:
            r = rows[key_model]
            kind = r[1]
            observed = r[3]
            expires = r[5]
            if kind == "success" and expires >= now:
                return {
                    "state": "recent_success",
                    "observed": observed,
                }

        return {"state": "unknown"}

    def catalog(
        self,
        client: str,
        provider: str,
        ids: Optional[List[str]],
        at: Optional[float] = None,
        ttl: float = 3600.0,
        error: str = "",
    ) -> bool:
        """Atualiza catálogo. Em erro, preserva dados e expires anteriores.

        Não grava lista vazia. Se não houver cache e falhar, retorna False sem criar entrada vazia.
        """
        now = time.time() if at is None else float(at)
        cursor = self.conn.cursor()

        cursor.execute(
            "SELECT ids_json, observed, expires, error FROM catalogs WHERE client = ? AND provider = ?",
            (client, provider),
        )
        existing = cursor.fetchone()

        # Caso de Erro
        if error or ids is None or not isinstance(ids, list) or len(ids) == 0:
            if existing is not None:
                # Mantém ids_json e expires anterior, apenas atualiza erro observado
                cursor.execute(
                    """
                    UPDATE catalogs
                    SET error = ?
                    WHERE client = ? AND provider = ?
                    """,
                    (str(error)[:350], client, provider),
                )
                self.conn.commit()
                return False
            return False

        # Sucesso: lista válida não-vazia
        if not all(isinstance(value, str) and value for value in ids):
            return False
        ids_json = json.dumps(sorted(set(ids)))
        expires = now + float(ttl)
        cursor.execute(
            """
            INSERT INTO catalogs (client, provider, ids_json, observed, expires, error)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(client, provider) DO UPDATE SET
                ids_json = excluded.ids_json,
                observed = excluded.observed,
                expires = excluded.expires,
                error = excluded.error
            """,
            (client, provider, ids_json, now, expires, ""),
        )
        self.conn.commit()
        return True

    def catalog_status(
        self,
        client: str,
        provider: str,
        model: str,
        at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Verifica se modelo está no catálogo."""
        now = time.time() if at is None else float(at)
        cursor = self.conn.cursor()

        cursor.execute(
            "SELECT ids_json, observed, expires FROM catalogs WHERE client = ? AND provider = ?",
            (client, provider),
        )
        row = cursor.fetchone()
        if not row:
            return {"state": "unknown", "observed": None}

        ids_json, observed, expires = row[0], row[1], row[2]
        try:
            ids_list = json.loads(ids_json) if ids_json else []
        except (TypeError, ValueError):
            return {"state": "unknown", "observed": observed}
        if not isinstance(ids_list, list) or not all(isinstance(value, str) for value in ids_list):
            return {"state": "unknown", "observed": observed}

        is_present = (model in ids_list) or (
            normalize_model(client, model) in ids_list
        )
        is_fresh = expires >= now

        if not is_fresh:
            return {"state": "stale", "observed": observed}

        if is_present:
            return {"state": "present", "observed": observed}

        return {"state": "missing", "observed": observed}

    def eligible(
        self, candidate: Tuple[str, str], at: Optional[float] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """Determina se candidate (client, model) está elegível."""
        now = time.time() if at is None else float(at)
        client, model = (candidate['client'], candidate['model']) if isinstance(candidate, dict) else candidate
        prov = provider_of(client, model)

        st = self.status(client, model, at=now)
        if st.get("state") == "cooldown":
            return False, st

        cat_st = self.catalog_status(client, prov, model, at=now)
        if cat_st.get("state") == "missing":
            # missing em catálogo fresco desqualifica
            return False, {"state": "catalog_missing", "catalog": cat_st}

        return True, {"state": st.get("state"), "catalog": cat_st.get("state")}

    def snapshot(self, at: Optional[float] = None) -> List[Dict[str, Any]]:
        """Retorna lista determinística dos registros não expirados (limite 50)."""
        now = time.time() if at is None else float(at)
        cursor = self.conn.cursor()

        cursor.execute(
            """
            SELECT key, client, provider, model, kind, detail, observed, retry_at, expires
            FROM route_health
            WHERE expires >= ?
            ORDER BY expires ASC, key ASC
            LIMIT 50
            """,
            (now,),
        )

        rows = cursor.fetchall()
        result = []
        for r in rows:
            result.append(
                {
                    "key": r[0],
                    "client": r[1],
                    "provider": r[2],
                    "model": r[3],
                    "kind": r[4],
                    "detail": r[5],
                    "observed": r[6],
                    "retry_at": r[7],
                    "expires": r[8],
                }
            )
        return result
