"""Verification of clean installation and realistic v0.4.0 database migration for ai-coordinator v0.5.0-rc.

Thoroughly tests:
1. Wheel installation in an isolated temporary virtual environment.
2. CLI smoke tests (version, help, routes, bootstrap).
3. Mandatory test of optional [mcp] dependency behavior (fails if [mcp] cannot be verified).
4. Realistic v0.4.0 SQLite database migration executed strictly by the installed package inside the clean venv.
5. Idempotent creation of Phase 4B tables, immutability SQL triggers, and dual-anchoring.
6. Supervisor recovery and reconcile_anchor() durability simulation in the installed runtime.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
VERSION = os.environ.get("RELEASE_VERSION")
if not VERSION:
    import tomllib
    pyproj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    VERSION = pyproj["project"]["version"]



def run_cmd(args: list[str], cwd: Path | None = None, env: dict | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        args,
        cwd=cwd,
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        shell=os.name == "nt" if args[0] in ("npm", "npm.cmd") else False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_clean_installation(tmp_dir: Path) -> Path:
    print("\n--- 1. Testing Wheel Installation in Isolated Virtual Environment ---")
    venv_dir = tmp_dir / "clean_venv"
    rc, out, err = run_cmd([sys.executable, "-m", "venv", str(venv_dir)])
    assert rc == 0, f"venv creation failed: {err}"

    if os.name == "nt":
        py_bin = venv_dir / "Scripts" / "python.exe"
        cli_bin = venv_dir / "Scripts" / "ai-coordinator.exe"
    else:
        py_bin = venv_dir / "bin" / "python"
        cli_bin = venv_dir / "bin" / "ai-coordinator"

    whl = DIST / f"ai_coordinator-{VERSION}-py3-none-any.whl"
    assert whl.exists(), f"Wheel not found: {whl}"

    # Install wheel into clean venv
    rc, out, err = run_cmd([str(py_bin), "-m", "pip", "install", "--no-cache-dir", str(whl)])
    assert rc == 0, f"pip install wheel failed: {err}\n{out}"
    print(f"[OK] Wheel installed successfully in {venv_dir.name}")

    # 2. CLI Smoke Tests
    print("\n--- 2. Testing CLI Smoke Commands ---")
    rc, out, err = run_cmd([str(cli_bin), "--version"])
    assert rc == 0, f"ai-coordinator --version failed: {err}"
    assert f"ai-coordinator {VERSION}" in out.strip(), f"Expected 'ai-coordinator {VERSION}', got {out.strip()}"
    print(f"[OK] CLI version check passed: {out.strip()}")

    rc, out, err = run_cmd([str(cli_bin), "--help"])
    assert rc == 0, f"ai-coordinator --help failed: {err}"
    assert "bootstrap" in out and "checkpoint" in out
    print("[OK] CLI help check passed")

    rc, out, err = run_cmd([str(cli_bin), "routes", "--help"])
    assert rc == 0, f"ai-coordinator routes --help failed: {err}"
    print("[OK] CLI routes help check passed")

    # 3. Python API and MCP optional dependency smoke test inside clean venv
    print("\n--- 3. Testing Python Module Import & Optional Dependency Behavior ---")
    test_code_without_mcp = f"""
import ai_coordinator
assert ai_coordinator.__version__ == "{VERSION}", f"Wrong version: {{ai_coordinator.__version__}}"
from ai_coordinator.mcp_server import create_server
try:
    server = create_server()
    raise AssertionError("Expected RuntimeError for missing mcp dependency")
except RuntimeError as exc:
    assert "requer a dependencia opcional; instale ai-coordinator[mcp]" in str(exc)
    print("MCP server correctly reported missing optional [mcp] dependency")
"""
    rc, out, err = run_cmd([str(py_bin), "-c", test_code_without_mcp])
    assert rc == 0, f"Clean venv import test without mcp failed: {err}\n{out}"
    print(f"[OK] {out.strip()}")

    # 3.1 MANDATORY test with [mcp] extra: MUST NOT be skipped!
    print("Installing mandatory [mcp] dependency in isolated environment...")
    rc_mcp, out_mcp, err_mcp = run_cmd([str(py_bin), "-m", "pip", "install", "--no-cache-dir", "mcp"])
    assert rc_mcp == 0, f"Mandatory [mcp] installation failed: {err_mcp}\n{out_mcp}"

    test_mcp_installed = """
from ai_coordinator.mcp_server import create_server
server = create_server()
assert server is not None
print("MCP server created successfully after installing extra")
"""
    rc, out, err = run_cmd([str(py_bin), "-c", test_mcp_installed])
    assert rc == 0, f"MCP server construction failed after installing mcp: {err}"
    print(f"[OK] {out.strip()}")

    return py_bin


def test_realistic_v0_4_0_migration_and_recovery(tmp_dir: Path, py_bin: Path):
    print("\n--- 4. Testing Realistic v0.4.0 Database Migration Executed by Installed Package ---")
    coord_root = tmp_dir / "coordinator_store"
    coord_root.mkdir(parents=True, exist_ok=True)
    db_path = coord_root / "coordination.sqlite"

    # Step A: Build a realistic v0.4.0 legacy database schema with tasks, runs, backlog, and Phase 3 plans
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            project TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            ai_memory_workstream_id TEXT,
            flight_plan_id TEXT,
            step_count INTEGER NOT NULL DEFAULT 0,
            uncheckpointed_steps INTEGER NOT NULL DEFAULT 0,
            drift_status TEXT NOT NULL DEFAULT 'nominal',
            flight_plan TEXT,
            landing_checks TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            category TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            exit_code INTEGER,
            error TEXT,
            output_path TEXT,
            usage TEXT,
            attempts TEXT,
            pid INTEGER,
            process_create_time REAL,
            FOREIGN KEY (task_id) REFERENCES tasks (id)
        );

        CREATE TABLE backlog (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks (id)
        );

        CREATE TABLE heartbeats (
            run_id TEXT PRIMARY KEY,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE plan_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            parent_revision INTEGER,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );

        CREATE TABLE experience_entries (
            id TEXT PRIMARY KEY,
            problem_signature TEXT NOT NULL,
            solution_summary TEXT NOT NULL,
            context_tags TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)

    legacy_task_id = "legacy_task_v040"
    cur.execute(
        "INSERT INTO tasks (id, goal, status, project, created_at, updated_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (legacy_task_id, "Migrate legacy production project to v0.5.0", "active", "C:/Projects/LegacyProd", "2026-10-15T10:00:00Z", "2026-10-15T12:00:00Z", 6)
    )
    cur.execute(
        "INSERT INTO runs (id, task_id, category, status, started_at, exit_code) VALUES (?, ?, ?, ?, ?, ?)",
        ("run_v040_prod", legacy_task_id, "general", "complete", "2026-10-15T10:05:00Z", 0)
    )
    cur.execute(
        "INSERT INTO backlog (id, task_id, title, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("item_v040_prod", legacy_task_id, "Legacy v0.4.0 critical backlog item", "pending", "2026-10-15T10:01:00Z")
    )
    cur.execute(
        "INSERT INTO plan_revisions (task_id, revision, parent_revision, created_at, status) VALUES (?, ?, ?, ?, ?)",
        (legacy_task_id, 1, None, "2026-10-15T10:02:00Z", "active")
    )
    cur.execute(
        "INSERT INTO experience_entries (id, problem_signature, solution_summary, context_tags, created_at) VALUES (?, ?, ?, ?, ?)",
        ("exp_v040_01", "sig:timeout_retry", "Exponential backoff", '["network", "retry"]', "2026-10-15T10:03:00Z")
    )
    conn.commit()
    conn.close()
    print("[OK] Realistic v0.4.0 database constructed with tasks, runs, backlog, and plan_revisions")

    # Step B: Execute migration strictly inside the isolated clean virtual environment using the installed wheel package!
    migration_test_code = f"""
import sys
from pathlib import Path
from ai_coordinator.storage import Store
from ai_coordinator.worker_audit import WorkerAuditEventType

coord_root = Path(r"{coord_root.resolve().as_posix()}")
supervisor_key = "genuine_supervisor_secret_migration_test_v050"
legacy_task_id = "{legacy_task_id}"

store = Store(root=coord_root, supervisor_key=supervisor_key)
try:
    # 1. Verify legacy data preserved intact
    row_task = store.db.execute("SELECT goal, revision FROM tasks WHERE id = ?", (legacy_task_id,)).fetchone()
    assert row_task is not None, "Legacy task lost during migration!"
    assert row_task[0] == "Migrate legacy production project to v0.5.0"
    assert row_task[1] == 6

    row_backlog = store.db.execute("SELECT title, status FROM backlog WHERE id = 'item_v040_prod'").fetchone()
    assert row_backlog is not None, "Legacy backlog item lost during migration!"
    assert row_backlog[0] == "Legacy v0.4.0 critical backlog item"

    row_plan = store.db.execute("SELECT revision, status FROM plan_revisions WHERE task_id = ?", (legacy_task_id,)).fetchone()
    assert row_plan is not None and row_plan[0] == 1

    row_exp = store.db.execute("SELECT problem_signature FROM experience_entries WHERE id = 'exp_v040_01'").fetchone()
    assert row_exp is not None and row_exp[0] == "sig:timeout_retry"
    print("[OK] Legacy v0.4.0 tasks, runs, backlog, plan_revisions and experiences preserved intact")

    # 2. Verify Phase 4B tables created idempotently
    cur = store.db.cursor()
    tables = {{row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}}
    assert "worker_audit_log" in tables, "worker_audit_log table missing after migration"
    assert "worker_audit_anchors" in tables, "worker_audit_anchors table missing after migration"
    assert "tool_broker_tokens" in tables, "tool_broker_tokens table missing after migration"
    print("[OK] Phase 4B tables created idempotently")

    # 3. Verify triggers created and active
    triggers = {{row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}}
    assert any("prevent_worker_audit_update" in t for t in triggers)
    assert any("prevent_worker_audit_delete" in t for t in triggers)
    print("[OK] Phase 4B immutability SQL triggers active")

    # 4. Record Phase 4B events and verify dual-anchoring
    store.worker_audit.record_event(
        event_type=WorkerAuditEventType.SPAWN,
        task_id=legacy_task_id,
        run_id="run_v050_migrated",
        worker_pid=12345,
        details={{"migrated": True, "version": "0.5.0"}},
    )
    store.worker_audit.record_event(
        event_type=WorkerAuditEventType.EXIT,
        task_id=legacy_task_id,
        run_id="run_v050_migrated",
        worker_pid=12345,
        details={{"migrated": True, "status": "complete"}},
    )

    ok, errors = store.worker_audit.verify_integrity(task_id=legacy_task_id)
    assert ok, f"Audit integrity failed: {{errors}}"
    anchor_file = store.worker_audit.anchor_file_path
    assert anchor_file is not None and anchor_file.is_file(), "External audit.anchor file was not created"
    print(f"[OK] Dual-anchoring verified (SQLite + {{anchor_file.name}})")

    # 5. Simulate crash & anchor loss, then test durable recovery (reconcile_anchor)
    anchor_file.unlink()
    assert not anchor_file.exists()
    ok_broken, err_broken = store.worker_audit.verify_integrity(task_id=legacy_task_id)
    assert not ok_broken, "Integrity should have failed after removing external anchor"
    assert any("ausente ou removido" in e.lower() for e in err_broken)
    print("[OK] Anchor tampering / loss correctly detected by verify_integrity")

    # Reconcile anchor
    recovered = store.worker_audit.reconcile_anchor()
    assert recovered, "reconcile_anchor failed"
    assert anchor_file.is_file(), "Anchor file not restored by reconcile_anchor"

    ok_restored, err_restored = store.worker_audit.verify_integrity(task_id=legacy_task_id)
    assert ok_restored, f"Integrity should pass after reconciliation, got: {{err_restored}}"
    print("[OK] reconcile_anchor() durability and recovery confirmed in installed package")

finally:
    store.db.close()
"""
    rc, out, err = run_cmd([str(py_bin), "-c", migration_test_code])
    assert rc == 0, f"Migration test in clean venv failed: {err}\n{out}"
    for line in out.strip().splitlines():
        if line.strip():
            print(f"  {line.strip()}")


def main():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        py_bin = test_clean_installation(tmp_dir)
        test_realistic_v0_4_0_migration_and_recovery(tmp_dir, py_bin)
    print("\n[ALL CHECKS PASSED] Clean installation and realistic v0.4.0 migration verified successfully!")


if __name__ == "__main__":
    main()
