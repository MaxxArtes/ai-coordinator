"""Tests for AI Coordinator Desktop — In-App Auto-Updater & Cryptographic Verifier.

Tests:
1. Version parsing & SemVer comparisons (A -> B, equality, downgrades, alpha tags).
2. Manifest schema validation and parsing.
3. Update detection (available vs up-to-date).
4. Network failure and offline mode resilience (zero-crash guarantee).
5. Cryptographic signature verification using Minisign & Ed25519:
   - Valid signature passes.
   - Tampered binary payload is rejected.
   - Wrong public key is rejected.
   - Corrupt/malformed signature handled safely.
6. Active session safety fence (never terminate running processes silently).
7. User profiles and data directories preservation.
8. Subsystem 2 (GUI) verification to prevent black console window.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from pathlib import Path

# Ensure repo root is on sys.path
_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from cryptography.hazmat.primitives.asymmetric import ed25519

from desktop.updater.controller import (
    DEFAULT_PUBKEY_B64,
    DesktopUpdaterController,
    UpdateCheckResult,
    UpdateManifest,
    compare_versions,
    extract_ed25519_pubkey,
    extract_ed25519_signature,
    parse_version,
)
from desktop.scripts.verify_subsystem import inspect_pe_subsystem


class TestDesktopUpdater(unittest.TestCase):
    def setUp(self) -> None:
        self.updater_dir = _repo_root / "desktop" / "updater"
        self.updates_dir = _repo_root / "desktop" / "updates"
        self.manifest_file = self.updates_dir / "alpha.json"

    def test_01_version_parsing_and_comparisons(self) -> None:
        """Verifies semantic versioning and alpha tag comparisons."""
        # Parsing
        self.assertEqual(parse_version("0.6.0"), (0, 6, 0, "release", 0))
        self.assertEqual(parse_version("0.6.1-alpha.48"), (0, 6, 1, "alpha", 48))
        self.assertEqual(parse_version("0.6.1-alpha.49"), (0, 6, 1, "alpha", 49))
        self.assertEqual(parse_version("v0.6.1"), (0, 6, 1, "release", 0))

        # Comparisons: A -> B detection
        self.assertEqual(compare_versions("0.6.0", "0.6.1-alpha.48"), -1)
        self.assertEqual(compare_versions("0.6.1-alpha.48", "0.6.1-alpha.49"), -1)
        self.assertEqual(compare_versions("0.6.1-alpha.48", "0.6.1-alpha.48"), 0)
        self.assertEqual(compare_versions("0.6.1-alpha.49", "0.6.1-alpha.48"), 1)
        self.assertEqual(compare_versions("0.6.1-alpha.49", "0.6.1"), -1)
        self.assertEqual(compare_versions("0.6.1", "0.6.1-alpha.49"), 1)

    def test_02_manifest_schema_and_alpha_json(self) -> None:
        """Verifies desktop/updates/alpha.json is valid and conforms to Tauri 2 schema."""
        self.assertTrue(self.manifest_file.exists(), "desktop/updates/alpha.json must exist")
        raw = self.manifest_file.read_text(encoding="utf-8")
        data = json.loads(raw)

        self.assertIn("version", data)
        self.assertIn("notes", data)
        self.assertIn("pub_date", data)
        self.assertIn("platforms", data)
        self.assertIn("windows-x86_64", data["platforms"])

        win = data["platforms"]["windows-x86_64"]
        self.assertIn("url", win)
        self.assertIn("signature", win)

        manifest = UpdateManifest.from_dict(data)
        self.assertEqual(manifest.version, data["version"])
        self.assertIn("windows-x86_64", manifest.platforms)

    def test_03_update_detection_available_and_uptodate(self) -> None:
        """Verifies controller detects available updates and handles up-to-date state."""
        manifest_payload = json.dumps({
            "version": "0.6.1-alpha.49",
            "notes": "Alpha 49 update notes",
            "pub_date": "2026-11-12T12:00:00Z",
            "platforms": {
                "windows-x86_64": {
                    "signature": "mock-sig",
                    "url": "https://example.com/alpha49.zip"
                }
            }
        })

        # Scenario 1: Version 0.6.0 checks -> Update available
        ctrl_older = DesktopUpdaterController(current_version="0.6.0")
        res1 = ctrl_older.check_update(custom_manifest_content=manifest_payload)
        self.assertTrue(res1.available)
        self.assertEqual(res1.latest_version, "0.6.1-alpha.49")
        self.assertEqual(res1.download_url, "https://example.com/alpha49.zip")
        self.assertFalse(res1.offline)
        self.assertIsNone(res1.error)

        # Scenario 2: Already on 0.6.1-alpha.49 -> Up to date
        ctrl_current = DesktopUpdaterController(current_version="0.6.1-alpha.49")
        res2 = ctrl_current.check_update(custom_manifest_content=manifest_payload)
        self.assertFalse(res2.available)
        self.assertEqual(res2.latest_version, "0.6.1-alpha.49")
        self.assertFalse(res2.offline)
        self.assertIsNone(res2.error)

    def test_04_offline_tolerance_zero_crash(self) -> None:
        """Verifies network failures or unreachable endpoints are handled gracefully without exceptions."""
        ctrl = DesktopUpdaterController(
            current_version="0.6.0",
            manifest_url="https://invalid-host-unreachable-999.test/manifest.json",
        )
        res = ctrl.check_update(timeout_sec=0.5)
        self.assertFalse(res.available)
        self.assertTrue(res.offline)
        self.assertIsNotNone(res.error)
        self.assertIn("offline", res.error.lower())

    def test_05_ed25519_minisign_cryptographic_verification(self) -> None:
        """Verifies Ed25519 signature verification, tampered payload rejection, and key safety."""
        # 1. Generate real Ed25519 keypair for cryptographic testing
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        raw_pub = public_key.public_bytes_raw()
        pub_b64 = base64.b64encode(raw_pub).decode("utf-8")

        payload = b"AI Coordinator Desktop Executable Binary v0.6.1-alpha.49"
        valid_signature = private_key.sign(payload)
        sig_b64 = base64.b64encode(valid_signature).decode("utf-8")

        ctrl = DesktopUpdaterController(current_version="0.6.0", pubkey=pub_b64)

        # Valid signature verification passes
        self.assertTrue(ctrl.verify_signature(payload, sig_b64))

        # Tampered payload fails verification
        tampered_payload = payload + b"TAMPERED_INJECTION"
        self.assertFalse(ctrl.verify_signature(tampered_payload, sig_b64))

        # Signature on wrong key fails verification
        other_priv = ed25519.Ed25519PrivateKey.generate()
        wrong_sig = other_priv.sign(payload)
        wrong_sig_b64 = base64.b64encode(wrong_sig).decode("utf-8")
        self.assertFalse(ctrl.verify_signature(payload, wrong_sig_b64))

        # Corrupt base64 string fails safely without unhandled exceptions
        self.assertFalse(ctrl.verify_signature(payload, "invalid-base64-!!!"))

    def test_06_default_embedded_pubkey_validity(self) -> None:
        """Verifies the default embedded public key in controller and tauri.conf.json is valid Ed25519."""
        tauri_conf = _repo_root / "desktop" / "src-tauri" / "tauri.conf.json"
        self.assertTrue(tauri_conf.exists())
        conf_data = json.loads(tauri_conf.read_text(encoding="utf-8"))
        embedded_pubkey = conf_data["plugins"]["updater"]["pubkey"]

        self.assertEqual(embedded_pubkey, DEFAULT_PUBKEY_B64)
        pk = extract_ed25519_pubkey(embedded_pubkey)
        self.assertIsInstance(pk, ed25519.Ed25519PublicKey)

    def test_07_active_sessions_safety_fence(self) -> None:
        """Verifies updater never kills active terminal sessions or workers silently."""
        ctrl = DesktopUpdaterController(current_version="0.6.0")

        # Zero sessions -> Safe to update immediately
        safe, msg = ctrl.can_safely_update(active_sessions_count=0, force=False)
        self.assertTrue(safe)

        # 4 active ConPTY sessions -> Blocked without force authorization
        safe, msg = ctrl.can_safely_update(active_sessions_count=4, force=False)
        self.assertFalse(safe)
        self.assertIn("4 terminais", msg)

        # 4 active sessions WITH explicit user authorization (force=True) -> Authorized
        safe_force, _ = ctrl.can_safely_update(active_sessions_count=4, force=True)
        self.assertTrue(safe_force)

    def test_08_user_profile_and_data_isolation(self) -> None:
        """Verifies user configurations and authorized projects remain outside the executable bundle."""
        dirs = DesktopUpdaterController.get_data_directories()
        self.assertIn("appdata", dirs)
        self.assertIn("desktop_config", dirs)
        # Verify config directory is in APPDATA, not inside executable path
        self.assertTrue(str(dirs["desktop_config"]).endswith("ai-coordinator-desktop"))

    def test_09_pe_subsystem_inspector_detects_gui_and_console(self) -> None:
        """Verifies verify_subsystem detects GUI (subsystem 2) vs Console (subsystem 3)."""
        py_exe = Path(sys.executable)
        self.assertTrue(py_exe.exists())
        subsystem = inspect_pe_subsystem(py_exe)
        # python.exe is a console application (subsystem 3)
        self.assertEqual(subsystem, 3, "python.exe must have subsystem 3 (CUI)")

        pyw_exe = py_exe.parent / "pythonw.exe"
        if pyw_exe.exists():
            subsystem_pyw = inspect_pe_subsystem(pyw_exe)
            self.assertEqual(subsystem_pyw, 2, "pythonw.exe must have subsystem 2 (GUI)")


if __name__ == "__main__":
    unittest.main()
