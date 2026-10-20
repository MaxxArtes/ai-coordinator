"""Formal headless contracts and capability verification for workers and coordinators.

Guarantees that workers cannot execute tools or mutate projects unrestricted,
and that coordinators cannot hang on terminal prompts in headless mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class HeadlessContract:
    """Declared and enforced capabilities of an agent CLI adapter."""
    client: str
    supports_worker_headless: bool
    supports_coordinator_headless: bool
    permission_mode: str  # "deny_all" | "needs_input" | "read_only"
    disallowed_flags: Tuple[str, ...]
    required_flags: Tuple[str, ...]
    notes: str = ""


# Canonical capability registry
CLIENT_CONTRACTS: Dict[str, HeadlessContract] = {
    "codex": HeadlessContract(
        client="codex",
        supports_worker_headless=True,
        supports_coordinator_headless=True,
        permission_mode="deny_all",
        disallowed_flags=(
            "--dangerously-skip-permissions",
            "--yolo",
            "-y",
            "--approval-policy=always",
        ),
        required_flags=(
            "--sandbox",
            "read-only",
            "--ephemeral",
            '-c',
            'approval_policy="never"',
        ),
        notes="Worker codex operates under --sandbox read-only with ephemeral session",
    ),
    "opencode": HeadlessContract(
        client="opencode",
        supports_worker_headless=True,
        supports_coordinator_headless=False,
        permission_mode="deny_all",
        disallowed_flags=(
            "--dangerously-skip-permissions",
            "--auto-approve",
        ),
        required_flags=(),
        notes="Worker opencode enforces permission={'*': 'deny'} via OPENCODE_CONFIG_CONTENT",
    ),
    "claude": HeadlessContract(
        client="claude",
        supports_worker_headless=True,
        supports_coordinator_headless=True,
        permission_mode="deny_all",
        disallowed_flags=(
            "--dangerously-skip-permissions",
            "--allow-all",
        ),
        required_flags=(
            "--print",
            "--tools",
            "",
        ),
        notes="Worker claude runs with --tools '' disabling all tool use",
    ),
    "agy": HeadlessContract(
        client="agy",
        supports_worker_headless=False,  # agy is a coordinator
        supports_coordinator_headless=True,
        permission_mode="needs_input",
        disallowed_flags=(),
        required_flags=(
            "-p",
            "--output-format",
            "stream-json",
        ),
        notes="Coordinator agy runs in print mode with stream-json",
    ),
    "chatgptweb": HeadlessContract(
        client="chatgptweb",
        supports_worker_headless=True,
        supports_coordinator_headless=False,
        permission_mode="deny_all",
        disallowed_flags=(),
        required_flags=(),
        notes="Web bridge without local filesystem tool execution",
    ),
    "geminiweb": HeadlessContract(
        client="geminiweb",
        supports_worker_headless=True,
        supports_coordinator_headless=False,
        permission_mode="deny_all",
        disallowed_flags=(),
        required_flags=(),
        notes="Web bridge without local filesystem tool execution",
    ),
    "openrouter": HeadlessContract(
        client="openrouter",
        supports_worker_headless=True,
        supports_coordinator_headless=False,
        permission_mode="deny_all",
        disallowed_flags=(),
        required_flags=(),
        notes="Direct HTTP API worker without local execution tools",
    ),
}


def get_client_contract(client: str, role: str = "worker") -> Optional[HeadlessContract]:
    """Retrieve and verify client capability for the specified role."""
    contract = CLIENT_CONTRACTS.get(client)
    if not contract:
        return None
    if role == "worker" and not contract.supports_worker_headless:
        return None
    if role == "coordinator" and not contract.supports_coordinator_headless:
        return None
    return contract


def validate_headless_arguments(client: str, role: str, command: List[str]) -> None:
    """Validate that command line flags satisfy safety constraints, include required security flags, and prevent overrides."""
    contract = CLIENT_CONTRACTS.get(client)
    if not contract:
        raise ValueError(f"Cliente desconhecido para execucao headless: {client}")
    if role == "worker" and not contract.supports_worker_headless:
        raise ValueError(f"Cliente '{client}' nao suporta execucao como worker_headless")
    if role == "coordinator" and not contract.supports_coordinator_headless:
        raise ValueError(f"Cliente '{client}' nao suporta execucao como coordinator_headless")

    cmd_joined = " ".join(command)

    # 1. Prohibited flags check
    for forbidden in contract.disallowed_flags:
        if forbidden in command or forbidden in cmd_joined:
            raise ValueError(f"Flag proibida em execucao segura: {forbidden} ({client})")

    # 2. Mandatory positive requirements check
    if contract.required_flags:
        # Check that required flags/tokens appear in command arguments
        for req in contract.required_flags:
            if not req:
                continue
            found = False
            for arg in command:
                if arg == req or req in arg:
                    found = True
                    break
            if not found and req not in cmd_joined:
                raise ValueError(
                    f"Contrato headless violado: argumento de seguranca obrigatorio '{req}' ausente no comando ({client})"
                )

    # 3. Security neutralization and override checks
    if client == "codex":
        # Ensure sandbox is strictly read-only and not overridden later
        for idx, arg in enumerate(command):
            if arg in ("--sandbox", "-s") and idx + 1 < len(command):
                val = command[idx + 1]
                if val != "read-only":
                    raise ValueError(f"Override de sandbox proibido em execucao segura: {val} (codex)")
            elif arg.startswith("--sandbox="):
                val = arg.split("=", 1)[1]
                if val != "read-only":
                    raise ValueError(f"Override de sandbox proibido em execucao segura: {val} (codex)")

    elif client == "claude":
        # Ensure tools remain empty/disabled
        for idx, arg in enumerate(command):
            if arg == "--tools" and idx + 1 < len(command):
                val = command[idx + 1]
                if val != "":
                    raise ValueError(f"Habilitacao de ferramentas proibida para worker headless: {val} (claude)")
            elif arg.startswith("--tools="):
                val = arg.split("=", 1)[1]
                if val not in ('""', "''", ""):
                    raise ValueError(f"Habilitacao de ferramentas proibida para worker headless: {val} (claude)")
