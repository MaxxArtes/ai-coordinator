"""Five-Tier Security and Authorization Boundary for AI Coordinator Desktop (Marco 4D.1.3).

Enforces strict separation between:
1. OS User (Host OS level)
2. Desktop Profile (Data & Project boundary)
3. Ephemeral IPC Session (Cryptographic token)
4. Native Credential Vault (Encrypted storage)
5. HumanPrincipal / Core Authority (HumanWorkspace & ManifestAuth)

Ensures that mere knowledge of PID or owner_id does NOT grant execution privileges.
"""

from __future__ import annotations

import getpass
import os
import secrets
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

from ai_coordinator.human_workspace import HumanPrincipal

from .profile import DesktopProfile, DesktopProfileManager
from .vault import BaseVault, create_vault


class SecurityAuthorizationError(Exception):
    """Raised when an operation violates security policy or authentication boundaries."""


class DesktopAuthorizer:
    """Central authorizer enforcing 5-tier security boundaries for AI Coordinator Desktop."""

    def __init__(
        self,
        profile_manager: DesktopProfileManager,
        vault_backend: str = "auto",
        master_password: Optional[str] = None,
    ) -> None:
        self.profile_manager = profile_manager
        self.vault_backend = vault_backend
        self.master_password = master_password
        self._active_sessions: Dict[str, str] = {}  # token -> profile_name
        self._vault_instances: Dict[str, BaseVault] = {}

    def get_effective_os_user(self) -> str:
        """Tier 1: Query host OS user identity."""
        try:
            return getpass.getuser()
        except Exception:
            return os.environ.get("USERNAME", os.environ.get("USER", "unknown"))

    def issue_session(self, profile_name: Optional[str] = None) -> str:
        """Tier 2 & 3: Issue an ephemeral cryptographically secure session token for a profile."""
        self._assert_not_worker()

        if profile_name is None:
            profile = self.profile_manager.get_active_profile()
        else:
            profile = self.profile_manager.get_profile(profile_name)
            if not profile:
                raise SecurityAuthorizationError(f"Cannot issue session: profile '{profile_name}' does not exist")

        token = f"dtsess_{profile.name}_{secrets.token_hex(32)}"
        self._active_sessions[token] = profile.name
        return token

    def revoke_session(self, token: str) -> bool:
        """Revoke an active session token."""
        return self._active_sessions.pop(token, None) is not None

    def validate_session(self, token: Optional[str]) -> str:
        """Tier 3: Validate ephemeral session token with constant-time comparison.

        Returns:
            The associated profile name.
        Raises:
            SecurityAuthorizationError: if token is invalid or missing.
        """
        self._assert_not_worker()

        if not token or not isinstance(token, str):
            raise SecurityAuthorizationError("Authentication failed: missing or empty session token")

        # Constant-time comparison across active sessions
        matched_profile = None
        for active_token, prof_name in self._active_sessions.items():
            if secrets.compare_digest(active_token, token):
                matched_profile = prof_name
                break

        if not matched_profile:
            raise SecurityAuthorizationError("Authentication failed: invalid or expired session token")

        return matched_profile

    def get_vault(self, profile_name: str) -> BaseVault:
        """Tier 4: Retrieve or instantiate the secure vault for a given profile."""
        self._assert_not_worker()

        profile = self.profile_manager.get_profile(profile_name)
        if not profile:
            raise SecurityAuthorizationError(f"Profile '{profile_name}' not found for vault access")

        if profile_name not in self._vault_instances:
            prof_dir = Path(profile.data_dir)
            vault = create_vault(
                profile_dir=prof_dir,
                profile_name=profile.name,
                backend=self.vault_backend,
                master_password=self.master_password,
            )
            self._vault_instances[profile_name] = vault

        return self._vault_instances[profile_name]

    def authorize_vault_read(self, session_token: str, secret_key: str) -> Optional[str]:
        """Authorized access to secret vault; requires valid session token."""
        profile_name = self.validate_session(session_token)
        vault = self.get_vault(profile_name)
        return vault.get_secret(secret_key)

    def authorize_vault_write(self, session_token: str, secret_key: str, secret_value: str) -> None:
        """Authorized write to secret vault; requires valid session token."""
        profile_name = self.validate_session(session_token)
        vault = self.get_vault(profile_name)
        vault.set_secret(secret_key, secret_value)

    def authorize_project_access(
        self,
        session_token: str,
        project_path: Union[str, Path],
    ) -> bool:
        """Tier 2 & 5: Validate that the session is authorized to operate within project_path."""
        profile_name = self.validate_session(session_token)
        is_auth = self.profile_manager.is_project_authorized(profile_name, project_path)
        if not is_auth:
            raise SecurityAuthorizationError(
                f"Access denied: project '{project_path}' is not authorized for profile '{profile_name}'"
            )
        return True

    def create_human_principal(self, session_token: str) -> HumanPrincipal:
        """Tier 5: Issue a valid HumanPrincipal bound to the active session and profile.

        Mere possession of an owner_id is insufficient; requires an active authenticated
        desktop session.
        """
        profile_name = self.validate_session(session_token)
        # Session token for HumanPrincipal
        return HumanPrincipal(
            owner_id=profile_name,
            role="human",
            session_token=session_token,
        )

    def _assert_not_worker(self) -> None:
        """Enforces worker lockout fail-closed check."""
        if os.environ.get("AI_COORDINATOR_ROLE") == "worker":
            raise SecurityAuthorizationError("Subordinate workers are strictly forbidden from desktop security authorization")
