"""Marco 4D.3, Etapa 3 — settings persistence and vault access through the desktop IPC (decisions D3, D4, D8)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

import pytest

import desktop.auth.vault as vault_mod
from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.ipc.protocol import RPCErrorCode
from desktop.ipc.server import SecureIPCServer
from desktop.terminal.manager import TerminalManager


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp) / "profiles"
        profiles = DesktopProfileManager(root_dir=root)
        authorizer = DesktopAuthorizer(profile_manager=profiles)
        token = authorizer.issue_session("default")
        server = SecureIPCServer(transport=MagicMock(), terminal_manager=TerminalManager(authorizer=authorizer), session_token=token)
        yield {"server": server, "token": token, "root": root, "profiles": profiles}


def call(env, method, params=None):
    payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}, "session_token": env["token"]}
    return env["server"].handle_raw_payload(json.dumps(payload))


def update(env, category, values, schema_version=1):
    return call(env, "settings.update", {"schema_version": schema_version, "category": category, "values": values})


def stored_settings(env):
    return DesktopProfileManager(root_dir=env["root"]).get_active_profile().settings


def test_get_reports_defaults_and_honest_states(env):
    snap = call(env, "settings.get").result
    assert snap["schema_version"] == 1
    cats = snap["categories"]
    assert set(cats) == {"perfil", "geral", "aparencia", "terminal", "coordinator"}
    assert cats["terminal"]["values"]["font_size"] == 13
    assert cats["terminal"]["saved_fields"] == []
    assert all(c["applied"] is False for c in cats.values())  # D4: nothing claims to be applied yet
    assert cats["coordinator"]["application"] == "unsupported"
    assert cats["aparencia"]["application"] == "pending_ui"
    assert {"agentes", "provedores", "seguranca", "sobre"} <= set(snap["unsupported_categories"])


def test_valid_update_is_persisted_and_reported_as_saved(env):
    resp = update(env, "terminal", {"font_size": 15, "cursor_blink": False})
    assert resp.error is None, resp.error
    term = resp.result["categories"]["terminal"]
    assert term["values"]["font_size"] == 15 and term["values"]["cursor_blink"] is False
    assert term["saved_fields"] == ["cursor_blink", "font_size"]
    # Re-read from disk with a new manager: really persisted, in the versioned envelope.
    assert stored_settings(env) == {"schema_version": 1, "values": {"terminal": {"font_size": 15, "cursor_blink": False}}}


@pytest.mark.parametrize(
    "category, values, schema_version, field, reason",
    [
        ("inexistente", {"x": 1}, 1, "category", "unknown_category"),
        ("provedores", {"OPENAI": "sk"}, 1, "category", "unsupported_category"),
        ("terminal", {"font_color": "red"}, 1, "terminal.font_color", "unknown_field"),
        ("terminal", {"font_size": 99}, 1, "terminal.font_size", "invalid_value"),
        ("terminal", {"font_size": "14"}, 1, "terminal.font_size", "invalid_value"),
        ("terminal", {"cursor_blink": "yes"}, 1, "terminal.cursor_blink", "invalid_value"),
        ("aparencia", {"stimulation": "neon"}, 1, "aparencia.stimulation", "invalid_value"),
        ("perfil", {"display_name": "   "}, 1, "perfil.display_name", "invalid_value"),
        ("terminal", {}, 1, "values", "invalid_value"),
        ("terminal", {"font_size": 14}, 2, "schema_version", "incompatible_version"),
    ],
)
def test_invalid_updates_are_rejected_with_structured_error_and_nothing_saved(env, category, values, schema_version, field, reason):
    resp = update(env, category, values, schema_version)
    assert resp.result is None
    assert resp.error["code"] == RPCErrorCode.INVALID_PARAMS.value
    assert resp.error["data"] == {"field": field, "reason": reason}
    assert stored_settings(env) == {}


def test_partially_invalid_update_saves_nothing(env):
    resp = update(env, "terminal", {"font_size": 14, "scrollback_lines": 5000})
    assert resp.error["data"] == {"field": "terminal.scrollback_lines", "reason": "invalid_value"}
    assert stored_settings(env) == {}


def test_mission_budget_uses_core_validator_and_is_not_presented_as_applied(env):
    budget = {"max_steps": 20, "max_elapsed_seconds": 1800, "max_uncheckpointed_steps": 5, "max_worker_calls": 10}
    resp = update(env, "coordinator", {"flight_budget": budget})
    assert resp.error is None, resp.error
    coord = resp.result["categories"]["coordinator"]
    assert coord["values"]["flight_budget"]["max_steps"] == 20
    assert coord["applied"] is False and coord["application"] == "unsupported"

    for bad, field in (
        ({"max_steps": "abc"}, "coordinator.flight_budget.max_steps"),
        ({"max_steps": 0}, "coordinator.flight_budget.max_steps"),
        ({"max_tokens": 5}, "coordinator.flight_budget.max_tokens"),
    ):
        err = update(env, "coordinator", {"flight_budget": bad}).error
        assert err["code"] == RPCErrorCode.INVALID_PARAMS.value
        assert err["data"]["field"] == field


def test_vault_without_secure_store_is_unavailable_with_dedicated_code(env, monkeypatch):
    monkeypatch.setattr(vault_mod.sys, "platform", "linux")
    status = call(env, "vault.status").result
    assert status == {"available": False, "backend": "unavailable"}

    resp = call(env, "vault.set_secret", {"key": "OPENROUTER_API_KEY", "value": "sk-should-not-persist"})
    assert resp.error["code"] == RPCErrorCode.VAULT_UNAVAILABLE.value == -32004
    assert resp.error["data"] == {"field": "vault", "reason": "unavailable"}
    assert not any(p.name == "vault.enc" for p in env["root"].rglob("*"))


def test_vault_rejects_invalid_key_names(env):
    resp = call(env, "vault.set_secret", {"key": "openrouter key", "value": "x"})
    assert resp.error["code"] == RPCErrorCode.INVALID_PARAMS.value
    assert resp.error["data"] == {"field": "key", "reason": "invalid_value"}


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-specific")
def test_vault_on_windows_stores_and_lists_names_only(env):
    secret = "sk-test-desktop-0123456789"
    resp = call(env, "vault.set_secret", {"key": "OPENROUTER_API_KEY", "value": secret})
    assert resp.result == {"stored": True, "key": "OPENROUTER_API_KEY"}
    listed = call(env, "vault.list_keys")
    assert listed.result == {"keys": ["OPENROUTER_API_KEY"]}
    # No IPC response ever carries the secret value.
    assert secret not in json.dumps(resp.to_dict() if hasattr(resp, "to_dict") else resp.result)
    assert secret not in json.dumps(listed.result)
    assert call(env, "vault.delete_secret", {"key": "OPENROUTER_API_KEY"}).result == {"deleted": True, "key": "OPENROUTER_API_KEY"}
    # Settings never hold credentials.
    assert stored_settings(env) == {}
