"""Read-only, allowlisted metadata for Desktop settings. Never starts CLIs or initializes Core data.

Only CLI names/capabilities, launcher presence, MCP names and routing model identifiers leave this module.
Launcher arguments, config values, URLs, environment, credentials and cached error detail never do.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
from typing import Any

MAX_CONFIG_BYTES = 1024 * 1024
CLI_NAMES = {"codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode", "agy": "AGY"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
SAFE_MODEL = re.compile(r"^[A-Za-z0-9_./:+-]{1,160}$")


def _read(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("configuration too large")
    return raw.decode("utf-8-sig")


def _jsonc(text: str) -> Any:
    # Strip comments only outside quoted strings; URLs and escaped quotes remain intact.
    out = []
    quote = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quote = False
            i += 1
        elif ch == '"':
            quote = True
            out.append(ch)
            i += 1
        elif text[i:i+2] == "//":
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end
        elif text[i:i+2] == "/*":
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated comment")
            out.append(" ")
            i = end + 2
        else:
            out.append(ch)
            i += 1
    # Remove trailing commas outside strings, without touching any value. Linear: a comma is held back until the
    # next significant character shows whether it closes an object or array (BE-09: slicing per comma was O(n^2)).
    text = "".join(out)
    out = []
    quote = escaped = False
    pending_comma = None  # index in `out` of a comma whose fate is not yet known
    for ch in text:
        if quote:
            out.append(ch)
            if escaped: escaped = False
            elif ch == "\\": escaped = True
            elif ch == '"': quote = False
            continue
        if pending_comma is not None and not ch.isspace():
            if ch in "}]":
                out[pending_comma] = ""
            pending_comma = None
        if ch == '"':
            quote = True
        elif ch == ",":
            pending_comma = len(out)
        out.append(ch)
    return json.loads("".join(out))


def core_config() -> tuple[dict, str]:
    from ai_coordinator.data import default_data_dir, PACKAGE_DIR
    current = default_data_dir() / "routes.json"
    path = current if current.exists() else PACKAGE_DIR / "routes.example.json"
    data = json.loads(_read(path))
    if not isinstance(data, dict) or not isinstance(data.get("routes"), dict):
        raise ValueError("invalid routing configuration")
    return data, "configured" if path == current else "packaged_defaults"


def mcp_names(home: Path | None = None) -> tuple[list[str], bool]:
    home = home or Path.home()
    sources = [
        (home / ".codex/config.toml", "mcp_servers", "toml"),
        (home / ".claude.json", "mcpServers", "json"),
        (home / ".config/opencode/opencode.json", "mcp", "json"),
        (home / ".config/opencode/opencode.jsonc", "mcp", "jsonc"),
    ]
    names = set()
    complete = True
    for path, field, kind in sources:
        if not path.exists():
            continue
        try:
            text = _read(path)
            data = tomllib.loads(text) if kind == "toml" else _jsonc(text) if kind == "jsonc" else json.loads(text)
            entries = data.get(field, {}) if isinstance(data, dict) else {}
            if not isinstance(entries, dict):
                raise ValueError("invalid MCP names")
            names.update(name for name in entries if isinstance(name, str) and SAFE_NAME.fullmatch(name))
        except (OSError, ValueError, UnicodeError, RecursionError):
            complete = False
    return sorted(names), complete


AGENT_REASONS = {
    "found": "Launcher encontrado; autenticação e cota não verificadas.",
    "unreadable": "Configuração de rotas ilegível; launcher não verificado.",
    "invalid": "Launcher com formato inválido na configuração.",
    "missing": "Launcher não configurado.",
    "absent": "Executável ou script do launcher não encontrado.",
}


def _launcher_state(config: dict, client: str) -> str:
    """Fixed state names only: the Core's launcher_problem text can contain a path and never leaves this module."""
    from ai_coordinator.runtime import launcher_command, launcher_problem
    launchers = config.get("launchers", {})
    if not isinstance(launchers, dict):
        return "invalid"  # BE-08: the Core would call .get on it and fail
    command = launchers.get(client)
    if command is None or command == []:
        return "missing"
    if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command) or not command[0]:
        return "invalid"
    try:
        return "absent" if launcher_problem(launcher_command(config, client)) else "found"
    except Exception:  # any malformed value is reported, never raised to the IPC loop
        return "invalid"


def agents_snapshot() -> dict:
    from ai_coordinator.contracts import CLIENT_CONTRACTS
    try:
        config, source = core_config()
    except (OSError, ValueError, UnicodeError, RecursionError):
        config, source = {}, "unreadable"
    agents = []
    for client, name in CLI_NAMES.items():
        contract = CLIENT_CONTRACTS.get(client)
        if contract is None:
            continue
        state = "unreadable" if source == "unreadable" else _launcher_state(config, client)
        agents.append({"name": name, "client": client, "available": state == "found",
                       "reason": AGENT_REASONS[state],
                       "worker_headless": contract.supports_worker_headless,
                       "coordinator_headless": contract.supports_coordinator_headless})
    names, complete = mcp_names()
    return {"agents": agents, "mcp_servers": names, "mcp_complete": complete, "source": source,
            "unsupported": [{"name": "Gemini CLI", "reason": "Exige um novo contrato de cliente no Core."}]}


def routes_snapshot() -> dict:
    from ai_coordinator.contracts import CLIENT_CONTRACTS
    try:
        config, source = core_config()
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {"routes": [], "available": False, "source": "unreadable", "reason": "Configuração de rotas ilegível; nada foi alterado.",
                "quota_status": "unknown", "quota_milestone": "4D.4.3"}
    routes = []
    for category, route in config["routes"].items():
        if not isinstance(category, str) or not SAFE_NAME.fullmatch(category) or not isinstance(route, dict):
            continue
        candidates = []
        for candidate in route.get("candidates", []) if isinstance(route.get("candidates"), list) else []:
            if not isinstance(candidate, dict) or candidate.get("client") not in CLIENT_CONTRACTS:
                continue
            model = candidate.get("model")
            if not isinstance(model, str) or not SAFE_MODEL.fullmatch(model):
                continue
            item = {"client": candidate["client"], "model": model}
            if candidate.get("effort") in ("low", "medium", "high", "xhigh", "max", "ultra", "minimal"):
                item["effort"] = candidate["effort"]
            candidates.append(item)
        routes.append({"category": category, "candidates": candidates})
    return {"routes": routes, "available": True, "source": source,
            "quota_status": "unknown", "quota_milestone": "4D.4.3"}
