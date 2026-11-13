"""Desktop Authentication, Profiles, and Secure Vault module (Marco 4D.1.3)."""

from .profile import DesktopProfile, DesktopProfileManager
from .vault import (
    AESGCMVault,
    BaseVault,
    UnavailableVault,
    VaultUnavailableError,
    Win32DPAPIVault,
    create_vault,
)
from .authorizer import DesktopAuthorizer, SecurityAuthorizationError

__all__ = [
    "BaseVault",
    "Win32DPAPIVault",
    "AESGCMVault",
    "UnavailableVault",
    "VaultUnavailableError",
    "create_vault",
    "DesktopProfile",
    "DesktopProfileManager",
    "DesktopAuthorizer",
    "SecurityAuthorizationError",
]
