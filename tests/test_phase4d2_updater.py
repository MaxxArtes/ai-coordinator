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

    def test_10_version_a_to_b_detection_and_real_signature_verification(self) -> None:
        """Verifies Version A (0.6.1-alpha.48) detects newer Alpha B release and validates Minisign structure."""
        ctrl_a = DesktopUpdaterController(current_version="0.6.1-alpha.48")
        manifest_text = self.manifest_file.read_text(encoding="utf-8")
        result = ctrl_a.check_update(custom_manifest_content=manifest_text)

        self.assertTrue(result.available, "Version A must detect Version B update")
        self.assertEqual(result.current_version, "0.6.1-alpha.48")
        self.assertGreaterEqual(compare_versions(result.latest_version, "0.6.1-alpha.49"), 0)
        self.assertTrue(
            result.download_url.startswith(
                f"https://github.com/MaxxArtes/ai-coordinator/releases/download/v{result.latest_version}/"
            )
        )
        self.assertTrue(len(result.signature) > 64)

        # Test cryptographic verification of real signature if matching staging binary is present
        staging_exe = _repo_root / "desktop" / "test_release_staging" / f"AI-Coordinator-Desktop_{result.latest_version}_x64-setup.exe"
        if staging_exe.exists():
            data_bytes = staging_exe.read_bytes()
            # 1. Real binary with real signature must verify True
            self.assertTrue(ctrl_a.verify_signature(data_bytes, result.signature))

            # 2. Tampered payload (flip first byte) must fail verification
            tampered = bytes([data_bytes[0] ^ 0xFF]) + data_bytes[1:]
            self.assertFalse(ctrl_a.verify_signature(tampered, result.signature))

    def test_11_fail_closed_active_session_inspection(self) -> None:
        """Verifies strict fail-closed behavior when session count inspection encounters errors or invalid state.
        
        Security rule: Unknown/invalid states MUST NEVER be bypassable by force.
        """
        ctrl = DesktopUpdaterController(current_version="0.6.1-alpha.48")

        # Negative count (signaling query failure or error) -> unconditionally blocked even with force=True
        safe, msg = ctrl.can_safely_update(active_sessions_count=-1, force=False)
        self.assertFalse(safe)
        self.assertIn("fail-closed", msg.lower())

        safe_force, msg_force = ctrl.can_safely_update(active_sessions_count=-1, force=True)
        self.assertFalse(safe_force)
        self.assertIn("fail-closed", msg_force.lower())

        # Non-integer / invalid type -> unconditionally blocked
        safe_invalid, _ = ctrl.can_safely_update(active_sessions_count=None, force=True)  # type: ignore
        self.assertFalse(safe_invalid)

        # Known active sessions (> 0): blocked without force, permitted with explicit force
        safe_active, msg_act = ctrl.can_safely_update(active_sessions_count=4, force=False)
        self.assertFalse(safe_active)
        self.assertIn("4 terminais", msg_act)

        safe_active_forced, msg_act_forced = ctrl.can_safely_update(active_sessions_count=4, force=True)
        self.assertTrue(safe_active_forced)
        self.assertIn("4 terminais", msg_act_forced)

    def test_12_versioning_consistency_across_manifests(self) -> None:
        """Verifies consistent SemVer versioning across package.json, Cargo.toml, and tauri.conf.json."""
        pkg_json = json.loads((_repo_root / "desktop" / "package.json").read_text(encoding="utf-8"))
        tauri_conf = json.loads((_repo_root / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        cargo_toml = (_repo_root / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")

        version_pkg = pkg_json["version"]
        version_tauri = tauri_conf["version"]

        self.assertEqual(version_pkg, "0.6.1-alpha.50")
        self.assertEqual(version_tauri, "0.6.1-alpha.50")
        self.assertIn('version = "0.6.1-alpha.50"', cargo_toml)

    def test_13_tab_info_ipc_contract_panes_and_active_counts(self) -> None:
        """Verifies TabInfo IPC contract exposes panes dict, panes_count, and active_panes_count."""
        from unittest.mock import MagicMock
        from desktop.terminal.manager import TabInfo

        # Tab with 0 panes
        t0 = TabInfo(tab_id="tab-0", title="Test 0", project_path="/test")
        d0 = t0.to_dict()
        self.assertEqual(d0["panes"], {})
        self.assertEqual(d0["panes_count"], 0)
        self.assertEqual(d0["active_panes_count"], 0)

        # Tab with 1 active pane
        mock_sess1 = MagicMock()
        mock_sess1.to_dict.return_value = {"session_id": "s-1", "is_alive": True}
        mock_sess1.is_alive.return_value = True

        t1 = TabInfo(tab_id="tab-1", title="Test 1", project_path="/test", panes={0: mock_sess1})
        d1 = t1.to_dict()
        self.assertEqual(len(d1["panes"]), 1)
        self.assertEqual(d1["panes_count"], 1)
        self.assertEqual(d1["active_panes_count"], 1)

        # Tab with 4 panes (3 alive, 1 exited)
        mock_sess_dead = MagicMock()
        mock_sess_dead.to_dict.return_value = {"session_id": "s-dead", "is_alive": False}
        mock_sess_dead.is_alive.return_value = False

        mock_sess2 = MagicMock()
        mock_sess2.to_dict.return_value = {"session_id": "s-2", "is_alive": True}
        mock_sess2.is_alive.return_value = True

        mock_sess3 = MagicMock()
        mock_sess3.to_dict.return_value = {"session_id": "s-3", "is_alive": True}
        mock_sess3.is_alive.return_value = True

        t4 = TabInfo(
            tab_id="tab-4",
            title="Test 4",
            project_path="/test",
            panes={0: mock_sess1, 1: mock_sess2, 2: mock_sess3, 3: mock_sess_dead},
        )
        d4 = t4.to_dict()
        self.assertEqual(d4["panes_count"], 4)
        self.assertEqual(d4["active_panes_count"], 3)
        self.assertIn("0", d4["panes"])
        self.assertIn("3", d4["panes"])

    def test_14_update_freeze_mutual_exclusion_and_multi_tab_count(self) -> None:
        """Verifies TerminalManager + SecureIPCServer enforce mutual exclusion when frozen for update
        and report authoritative counts across multiple tabs."""
        from unittest.mock import MagicMock
        from desktop.terminal.manager import TabInfo, TerminalManager, TerminalManagerError
        from desktop.ipc.server import SecureIPCServer

        auth = MagicMock()
        mgr = TerminalManager(authorizer=auth)

        # Simulate 2 tabs with 2 active panes each = 4 active panes total
        for tab_idx in (1, 2):
            tab_id = f"tab-{tab_idx}"
            panes = {}
            for p_idx in (0, 1):
                s = MagicMock()
                s.to_dict.return_value = {"session_id": f"{tab_id}-p{p_idx}", "is_alive": True}
                s.is_alive.return_value = True
                panes[p_idx] = s
            mgr.tabs[tab_id] = TabInfo(tab_id=tab_id, title=tab_id, project_path="D:\\proj", panes=panes)

        ipc = SecureIPCServer(transport=MagicMock(), terminal_manager=mgr, session_token="tok-secret")

        # Freeze via IPC: must return frozen=True, 2 tabs, panes_total=4, active_sessions_total=4
        req_freeze = json.dumps({
            "jsonrpc": "2.0",
            "id": "1",
            "method": "system.freeze_for_update",
            "params": {},
            "session_token": "tok-secret",
        })
        resp_freeze = ipc.handle_raw_payload(req_freeze)
        self.assertIsNotNone(resp_freeze)
        self.assertIsNone(resp_freeze.error)
        res = resp_freeze.result
        self.assertTrue(res["frozen"])
        self.assertEqual(len(res["tabs"]), 2)
        self.assertEqual(res["panes_total"], 4)
        self.assertEqual(res["active_sessions_total"], 4)

        # While frozen, creating a tab via IPC OR directly MUST fail
        req_create_tab = json.dumps({
            "jsonrpc": "2.0",
            "id": "1b",
            "method": "terminal.create_tab",
            "params": {"tab_id": "tab-blocked", "project_path": str(_repo_root)},
            "session_token": "tok-secret",
        })
        resp_blocked = ipc.handle_raw_payload(req_create_tab)
        self.assertIsNotNone(resp_blocked)
        self.assertIsNotNone(resp_blocked.error)

        with self.assertRaises(TerminalManagerError):
            mgr.create_tab("tok-secret", "tab-new", "New Tab", "D:\\proj")

        with self.assertRaises(TerminalManagerError):
            mgr.create_terminal("tok-secret", "tab-1", pane_id=2)

        # Unfreeze via IPC and verify tab creation resumes
        req_unfreeze = json.dumps({
            "jsonrpc": "2.0",
            "id": "2",
            "method": "system.unfreeze_update",
            "params": {},
            "session_token": "tok-secret",
        })
        resp_unfreeze = ipc.handle_raw_payload(req_unfreeze)
        self.assertIsNotNone(resp_unfreeze)
        self.assertFalse(resp_unfreeze.result["frozen"])

        new_tab = mgr.create_tab("tok-secret", "tab-new", "New Tab", str(_repo_root))
        self.assertEqual(new_tab.tab_id, "tab-new")

    def test_15_real_terminal_session_liveness_property_and_four_session_gate(self) -> None:
        """Real TerminalSession.is_alive is a bool @property (not callable); freeze/to_dict must count 4 real sessions cleanly."""
        import tempfile
        from desktop.auth.authorizer import DesktopAuthorizer
        from desktop.auth.profile import DesktopProfileManager
        from desktop.terminal.manager import TerminalManager
        from desktop.terminal.session import SessionKind

        old_backend = os.environ.get("COORDINATOR_PTY_BACKEND")
        os.environ["COORDINATOR_PTY_BACKEND"] = "pipe"
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
                tmp_path = Path(tmpdir)
                prof_mgr = DesktopProfileManager(root_dir=tmp_path / "profiles")
                authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
                token = authorizer.issue_session("default")

                proj_dir = tmp_path / "proj"
                proj_dir.mkdir(parents=True, exist_ok=True)
                prof_mgr.authorize_project("default", proj_dir)

                mgr = TerminalManager(authorizer=authorizer)
                mgr.create_tab(token=token, tab_id="tab-a", title="A", project_path=proj_dir)
                mgr.create_tab(token=token, tab_id="tab-b", title="B", project_path=proj_dir)
                cmd = [sys.executable, "-u", "-c", "import time; time.sleep(10)"]

                try:
                    for tab_id in ("tab-a", "tab-b"):
                        for pane_idx in range(2):
                            sess = mgr.create_terminal(
                                token=token,
                                tab_id=tab_id,
                                pane_id=pane_idx,
                                kind=SessionKind.HUMAN_TERMINAL,
                                command=cmd,
                            )
                            self.assertIsInstance(sess.is_alive, bool)
                            self.assertTrue(sess.is_alive)

                    tab_a_dict = mgr.get_tab("tab-a").to_dict()
                    self.assertEqual(tab_a_dict["panes_count"], 2)
                    self.assertEqual(tab_a_dict["active_panes_count"], 2)

                    freeze_snapshot = mgr.freeze_for_update()
                    self.assertTrue(freeze_snapshot["frozen"])
                    self.assertEqual(freeze_snapshot["panes_total"], 4)
                    self.assertEqual(freeze_snapshot["active_sessions_total"], 4)

                    ctrl = DesktopUpdaterController("0.6.1-alpha.48")
                    allowed_no_force, reason = ctrl.can_safely_update(
                        active_sessions_count=freeze_snapshot["active_sessions_total"], force=False
                    )
                    self.assertFalse(allowed_no_force)
                    self.assertIn("4", reason)

                    allowed_force, _ = ctrl.can_safely_update(
                        active_sessions_count=freeze_snapshot["active_sessions_total"], force=True
                    )
                    self.assertTrue(allowed_force)
                finally:
                    mgr.terminate_all()
        finally:
            if old_backend is None:
                os.environ.pop("COORDINATOR_PTY_BACKEND", None)
            else:
                os.environ["COORDINATOR_PTY_BACKEND"] = old_backend


if __name__ == "__main__":
    unittest.main()
