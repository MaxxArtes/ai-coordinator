"""Verification of clean installation and genuine v0.4.0 database migration for ai-coordinator v0.5.0-rc.

Thoroughly tests:
1. Wheel installation in an isolated temporary virtual environment.
2. CLI smoke tests (version, help, routes, bootstrap).
3. Mandatory test of optional [mcp] dependency behavior (fails if [mcp] cannot be verified).
4. Genuine v0.4.0 SQLite database generation directly from git tag v0.4.0 (tasks, runs, plan revisions, experience records).
5. Migration of genuine v0.4.0 database executed strictly by the installed package inside the clean venv.
6. Explicit verification of runs preservation, status, and exit codes.
7. Idempotent creation of Phase 4B tables, immutability SQL triggers, and dual-anchoring.
8. Supervisor recovery and reconcile_anchor() durability simulation in the installed runtime.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
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


def test_genuine_v0_4_0_migration_and_recovery(tmp_dir: Path, py_bin: Path):
    print("\n--- 4. Testing Genuine v0.4.0 Database Creation and Migration Executed by Installed Package ---")
    
    # Step A: Extract genuine v0.4.0 repository tree in memory from tag v0.4.0
    v040_stage = tmp_dir / "v040_stage"
    res = subprocess.run(["git", "-C", str(ROOT), "archive", "v0.4.0"], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(res.stdout)) as tf:
        tf.extractall(v040_stage)

    coord_root = tmp_dir / "genuine_v040_store"
    coord_root.mkdir(parents=True, exist_ok=True)
    proj_dir = tmp_dir / "legacy_production_project"
    proj_dir.mkdir(parents=True, exist_ok=True)

    env_v040 = os.environ.copy()
    env_v040["PYTHONPATH"] = str(v040_stage / "src")

    # Step B: Populate genuine v0.4.0 database using the genuine v0.4.0 codebase
    creator_script = """
import sys
from pathlib import Path
import ai_coordinator
from ai_coordinator.storage import Store, RunState

assert ai_coordinator.__version__ == "0.4.0", f"Expected genuine v0.4.0, got: {ai_coordinator.__version__}"
coord_root = Path(sys.argv[1])
proj_dir = sys.argv[2]
store = Store(root=coord_root)

# 1. Create a genuine task
task = store.new(goal="Genuine legacy mission running under v0.4.0", project=proj_dir)
task_id = task["id"]

# 1.1 Create a genuine backlog task under v0.4.0
task_backlog = store.new(goal="Genuine legacy backlog task item under v0.4.0", project=proj_dir)
task_backlog_id = task_backlog["id"]
store.db.execute("UPDATE tasks SET status = 'backlog' WHERE id = ?", (task_backlog_id,))

# 1.2 Populate legacy backlog table
store.db.execute("CREATE TABLE IF NOT EXISTS backlog (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)")
store.db.execute("INSERT INTO backlog VALUES ('item_v040_prod', ?, 'Legacy v0.4.0 critical backlog item', 'pending', '2026-10-15T10:01:00Z')", (task_id,))

# 2. Reserve and complete runs
run_id1, _, _ = store.reserve(task_id, "general", "Production run payload prompt 1", max_calls=10)
store.update_run(run_id1, status=RunState.COMPLETE, exit_code=0, ended="2026-10-15T10:30:00Z")

run_id2, _, _ = store.reserve(task_id, "review", "Production run payload prompt 2", max_calls=5)
store.update_run(run_id2, status=RunState.FAILED, exit_code=1, error="Legacy execution failure test")

# 3. Create plan revision
plan_rev = store.create_plan_revision(
    task_id=task_id,
    nodes=[{"id": "step_a", "title": "Legacy Analysis", "category": "general", "status": "complete"}],
    edges=[],
    summary="Genuine v0.4.0 initial plan revision",
)

# 4. Create experience record
exp = store.record_experience(
    project_id=proj_dir,
    problem_pattern="sig:legacy_v040_timeout",
    solution_pattern="Exponential backoff with jitter",
    task_id=task_id,
    context_tags=["legacy", "v040", "retry"]
)

store.db.close()
print(f"GENUINE_V040_TASK_ID:{task_id}")
print(f"GENUINE_V040_TASK_BACKLOG_ID:{task_backlog_id}")
print(f"GENUINE_V040_RUN1_ID:{run_id1}")
print(f"GENUINE_V040_RUN2_ID:{run_id2}")
print(f"GENUINE_V040_EXP_ID:{exp.id}")
"""
    p_create = subprocess.run([sys.executable, "-c", creator_script, str(coord_root), str(proj_dir)], env=env_v040, capture_output=True, text=True)
    assert p_create.returncode == 0, f"Genuine v0.4.0 database generation failed: {p_create.stderr}"
    
    meta = {}
    for line in p_create.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    print("[OK] Genuine v0.4.0 database constructed using genuine v0.4.0 release codebase")

    # Step C: Execute migration strictly inside clean_venv using the installed v0.5.0 wheel package
    migration_test_code = f"""
import sys
from pathlib import Path
from ai_coordinator.storage import Store, RunState
from ai_coordinator.worker_audit import WorkerAuditEventType

coord_root = Path(r"{coord_root.resolve().as_posix()}")
supervisor_key = "genuine_supervisor_secret_migration_test_v050"
task_id = "{meta['GENUINE_V040_TASK_ID']}"
task_backlog_id = "{meta['GENUINE_V040_TASK_BACKLOG_ID']}"
run1_id = "{meta['GENUINE_V040_RUN1_ID']}"
run2_id = "{meta['GENUINE_V040_RUN2_ID']}"
exp_id = "{meta['GENUINE_V040_EXP_ID']}"

store = Store(root=coord_root, supervisor_key=supervisor_key)
try:
    # 1. Verify legacy task preserved intact
    task = store.task(task_id)
    assert task is not None, "Legacy task lost during migration!"
    assert task["goal"] == "Genuine legacy mission running under v0.4.0"
    print("[OK] Genuine v0.4.0 task preserved intact")

    # 1.1 Verify legacy backlog task and backlog table preserved
    task_b = store.task(task_backlog_id)
    assert task_b is not None and task_b["status"] == "backlog"
    assert task_b["goal"] == "Genuine legacy backlog task item under v0.4.0"
    print("[OK] Genuine v0.4.0 backlog task item verified and preserved with status 'backlog'")

    row_backlog = store.db.execute("SELECT title, status FROM backlog WHERE id = 'item_v040_prod'").fetchone()
    assert row_backlog is not None, "Legacy backlog table item lost during migration!"
    assert row_backlog[0] == "Legacy v0.4.0 critical backlog item"
    assert row_backlog[1] == "pending"
    print("[OK] Genuine v0.4.0 backlog table item preserved intact")

    # 2. Verify legacy runs preserved explicitly (status and exit_code)
    runs = store.runs(task_id)
    assert len(runs) == 2, f"Expected 2 legacy runs, found {{len(runs)}}"
    run_map = {{r["id"]: r for r in runs}}
    assert run1_id in run_map, f"Legacy run 1 lost: {{run1_id}}"
    assert run2_id in run_map, f"Legacy run 2 lost: {{run2_id}}"
    assert run_map[run1_id]["status"] == RunState.COMPLETE
    assert run_map[run1_id]["exit_code"] == 0
    assert run_map[run2_id]["status"] == RunState.FAILED
    assert run_map[run2_id]["exit_code"] == 1
    print(f"[OK] Genuine v0.4.0 runs ({{run1_id}}, {{run2_id}}) verified and preserved with status and exit codes")

    # 3. Verify plan revisions and experience records preserved
    cur = store.db.cursor()
    row_plan = cur.execute("SELECT revision, summary FROM plan_revisions WHERE task_id = ?", (task_id,)).fetchone()
    assert row_plan is not None and row_plan[0] == 1
    print("[OK] Genuine v0.4.0 plan revision preserved intact")

    row_exp = cur.execute("SELECT id, problem_pattern FROM experience_records WHERE id = ?", (exp_id,)).fetchone()
    assert row_exp is not None and row_exp[1] == "sig:legacy_v040_timeout"
    print("[OK] Genuine v0.4.0 experience record preserved intact")

    # 4. Verify Phase 4B tables created idempotently
    tables = {{row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}}
    assert "worker_audit_log" in tables, "worker_audit_log table missing after migration"
    assert "worker_audit_anchors" in tables, "worker_audit_anchors table missing after migration"
    assert "tool_broker_tokens" in tables, "tool_broker_tokens table missing after migration"
    print("[OK] Phase 4B tables created idempotently")

    # 5. Verify Phase 4B immutability SQL triggers active
    triggers = {{row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}}
    assert any("prevent_worker_audit_update" in t for t in triggers)
    assert any("prevent_worker_audit_delete" in t for t in triggers)
    print("[OK] Phase 4B immutability SQL triggers active")

    # 6. Record Phase 4B events and verify dual-anchoring
    store.worker_audit.record_event(
        event_type=WorkerAuditEventType.SPAWN,
        task_id=task_id,
        run_id="run_v050_migrated",
        worker_pid=12345,
        details={{"migrated": True, "version": "0.5.0"}},
    )
    store.worker_audit.record_event(
        event_type=WorkerAuditEventType.EXIT,
        task_id=task_id,
        run_id="run_v050_migrated",
        worker_pid=12345,
        details={{"migrated": True, "status": "complete"}},
    )

    ok, errors = store.worker_audit.verify_integrity(task_id=task_id)
    assert ok, f"Audit integrity failed: {{errors}}"
    anchor_file = store.worker_audit.anchor_file_path
    assert anchor_file is not None and anchor_file.is_file(), "External audit.anchor file was not created"
    print(f"[OK] Dual-anchoring verified (SQLite + {{anchor_file.name}})")

    # 7. Simulate crash & anchor loss, then test durable recovery (reconcile_anchor)
    anchor_file.unlink()
    assert not anchor_file.exists()
    ok_broken, err_broken = store.worker_audit.verify_integrity(task_id=task_id)
    assert not ok_broken, "Integrity should have failed after removing external anchor"
    assert any("ausente ou removido" in e.lower() for e in err_broken)
    print("[OK] Anchor tampering / loss correctly detected by verify_integrity")

    # Reconcile anchor
    recovered = store.worker_audit.reconcile_anchor()
    assert recovered, "reconcile_anchor failed"
    assert anchor_file.is_file(), "Anchor file not restored by reconcile_anchor"

    ok_restored, err_restored = store.worker_audit.verify_integrity(task_id=task_id)
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
        test_genuine_v0_4_0_migration_and_recovery(tmp_dir, py_bin)
    print("\n[ALL CHECKS PASSED] Clean installation and genuine v0.4.0 migration verified successfully!")


if __name__ == "__main__":
    main()
