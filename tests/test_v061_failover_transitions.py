"""Regression tests for D1: fallback across candidates after a post-spawn failure.

In v0.6.0 run_worker moved the run to STARTING before every candidate. After a
spawned worker failed (empty output, error exit or timeout) the run was still
RUNNING, RUNNING -> STARTING is not an allowed transition, and the next
candidate was never tried: the run ended INTERRUPTED with
"Transicao de estado invalida: running -> starting".

These tests drive the real run_worker, AppContainer profile, ProcessSupervisor
spawn and Store transitions with real worker processes; nothing on the
transition path is mocked. Only the recursive read grants for directories
outside the test tree (the Python interpreter folder) are skipped, because
icacls over the interpreter takes minutes per attempt and the cmd.exe worker
does not need them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_coordinator import fs_sandbox
from ai_coordinator import runtime
from ai_coordinator.cli import run_worker
from ai_coordinator.resource_ledger import ResourceAction, ResourceType
from ai_coordinator.storage import RunState, Store

pytestmark = pytest.mark.skipif(os.name != "nt", reason="cmd.exe worker and AppContainer spawn")


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    real_run = subprocess.run

    def run(args, *a, **kw):
        if (isinstance(args, (list, tuple)) and args and args[0] == "icacls"
                and str(args[-1]).endswith(":(OI)(CI)RX") and base not in Path(args[1]).resolve().parents
                and Path(args[1]).resolve() != base):
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(fs_sandbox.subprocess, "run", run)

    project = base / "project"
    project.mkdir()
    store = Store(root=base / "coord")
    task = store.new(str(project), "D1 failover regression")
    bindir = base / "bin"
    bindir.mkdir()
    answer = bindir / "answer.json"
    answer.write_text(json.dumps({"result": "# Result ANSWERED_OK", "usage": {"total_tokens": 3}}), encoding="ascii")
    source = base / "input.md"
    source.write_text("Pergunta curta para o worker de teste.", encoding="utf-8")

    # One real cmd.exe worker per behaviour; the arguments run_worker appends
    # (--model, --max-tokens) land on a trailing "rem". Batch files are not used
    # because cmd.exe refuses to run them inside the AppContainer.
    launchers = {
        "openrouter": ["cmd.exe", "/d", "/c", "exit", "1", "&", "rem"],  # exits 1 with no output
        "chatgptweb": ["cmd.exe", "/d", "/c", "for", "/l", "%i", "in", "(1,1,2000000000)", "do", "@rem"],  # hangs
        "geminiweb": ["cmd.exe", "/d", "/c", "type", str(answer), "&", "rem"],  # valid JSON answer
    }
    client_for = {"empty": "openrouter", "sleep": "chatgptweb", "ok": "geminiweb"}

    def config(models, timeout=20):
        return {
            "limits": {"input_chars": 5000, "output_chars": 5000, "worker_calls_per_task": 10,
                       "attempts_per_run": 4, "timeout_seconds": timeout,
                       "max_memory_bytes": 512 * 1024 * 1024, "max_processes": 8},
            "security": {},
            "availability": {"enabled": False},
            "routes": {"general": {"candidates": [
                {"client": client_for[m.split("_")[0]], "model": m, "plain_prompt": True, "timeout_seconds": timeout}
                for m in models]}},
            "launchers": launchers,
        }

    yield store, task["id"], source, config
    store.db.close()


def _run(store, run_id):
    return dict(store.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


def _event_types(store, run_id):
    rows = store.db.execute("SELECT event_type FROM run_events WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
    return [r["event_type"] for r in rows]


def _attempts(store, run_id):
    return [(a["model"], a["status"]) for a in json.loads(_run(store, run_id)["attempts"])]


def test_second_candidate_runs_after_post_spawn_failure(env):
    store, task_id, source, config = env
    summary = run_worker(store, config(["empty_a", "ok_b"]), task_id, "general", str(source))

    assert summary["status"] == "complete", [(a["model"], a.get("error")) for a in summary.get("attempts", [])]
    assert summary["model"] == "ok_b"
    run = _run(store, summary["run_id"])
    assert run["status"] == RunState.COMPLETE
    assert run["pid"] is None
    assert _attempts(store, summary["run_id"]) == [("empty_a", "failed"), ("ok_b", "complete")]
    assert _event_types(store, summary["run_id"]).count("failover_next_candidate") == 1


def test_timeout_then_empty_then_success_keeps_one_run(env):
    store, task_id, source, config = env
    summary = run_worker(store, config(["sleep_a", "empty_b", "ok_c"], timeout=3),
                         task_id, "general", str(source))

    assert summary["status"] == "complete", [(a["model"], a.get("error")) for a in summary.get("attempts", [])]
    assert [r["id"] for r in store.db.execute("SELECT id FROM runs WHERE task_id=?", (task_id,))] == [summary["run_id"]]
    assert _attempts(store, summary["run_id"]) == [
        ("sleep_a", "failed"), ("empty_b", "failed"), ("ok_c", "complete")]
    assert _event_types(store, summary["run_id"]).count("failover_next_candidate") == 2


def test_all_candidates_failing_ends_failed_not_interrupted(env):
    store, task_id, source, config = env
    summary = run_worker(store, config(["empty_a", "empty_b"]), task_id, "general", str(source))
    assert summary["status"] == "failed"
    assert _run(store, summary["run_id"])["status"] == RunState.FAILED
    assert _attempts(store, summary["run_id"]) == [("empty_a", "failed"), ("empty_b", "failed")]


def test_rearm_refused_while_previous_identity_is_recorded(env):
    store, task_id, _, _ = env
    run_id, _, _ = store.reserve(task_id, "general", "prompt", 5)
    store.transition_run(run_id, to_status=RunState.STARTING)
    store.transition_run(run_id, to_status=RunState.RUNNING, pid=4242, process_create_time=1)
    with pytest.raises(ValueError, match="identidade do worker anterior"):
        store.begin_worker_attempt(run_id)
    store.update_run(run_id, pid=None, process_create_time=None)
    assert store.begin_worker_attempt(run_id)["status"] == RunState.STARTING


@pytest.mark.parametrize("terminal", [RunState.CANCEL_REQUESTED, RunState.FAILED, RunState.INTERRUPTED])
def test_rearm_does_not_reopen_cancelled_or_terminal_runs(env, terminal):
    store, task_id, _, _ = env
    run_id, _, _ = store.reserve(task_id, "general", "prompt", 5)
    store.transition_run(run_id, to_status=RunState.STARTING)
    store.transition_run(run_id, to_status=RunState.RUNNING, pid=4242, process_create_time=1)
    store.transition_run(run_id, to_status=terminal)
    store.update_run(run_id, pid=None, process_create_time=None)
    with pytest.raises(ValueError, match="Transicao de estado invalida"):
        store.begin_worker_attempt(run_id)


def test_unsupported_worker_contract_skipped_before_attempt_or_budget_spend(env):
    """B3-07: clients without a worker headless contract are skipped without consuming attempts_per_run."""
    store, task_id, source, config = env
    cfg = config(["ok_b"])
    cfg["limits"]["attempts_per_run"] = 1
    cfg["launchers"]["agy"] = ["cmd.exe", "/d", "/c", "echo", "should_not_run"]
    cfg["routes"]["general"]["candidates"] = [
        {"client": "agy", "model": "agy-coord", "plain_prompt": True},
        {"client": "unknown_client", "model": "mystery-1", "plain_prompt": True},
        *cfg["routes"]["general"]["candidates"],
    ]

    plan = runtime.route_plan(store, cfg, "general", refresh=False)
    assert [c["client"] for c in plan["candidates"]] == ["geminiweb"]
    assert [s["client"] for s in plan["skipped"]] == ["agy", "unknown_client"]
    assert all(s["availability"]["state"] == "contract_unsupported" for s in plan["skipped"])

    summary = run_worker(store, cfg, task_id, "general", str(source))
    assert summary["status"] == "complete"
    assert summary["client"] == "geminiweb"
    assert [s["client"] for s in summary["skipped"]] == ["agy", "unknown_client"]
    assert all(s["reason"]["state"] == "contract_unsupported" for s in summary["skipped"])
    assert _attempts(store, summary["run_id"]) == [("ok_b", "complete")]
    assert _event_types(store, summary["run_id"]) == [
        "reserve", "transition_to_starting", "transition_to_running", "confirm_exit"
    ]


def test_all_candidates_contract_unsupported_fails_without_starting(env):
    store, task_id, source, config = env
    cfg = config([])
    cfg["launchers"]["agy"] = ["cmd.exe", "/d", "/c", "echo", "should_not_run"]
    cfg["routes"]["general"]["candidates"] = [
        {"client": "agy", "model": "agy-coord", "plain_prompt": True},
    ]

    summary = run_worker(store, cfg, task_id, "general", str(source))
    assert summary["status"] == "failed"
    assert summary["attempts"] == []
    assert summary["skipped"][0]["reason"]["state"] == "contract_unsupported"
    runs = store.runs(task_id)
    assert len(runs) == 1
    assert runs[0]["status"] == RunState.FAILED
    assert _event_types(store, runs[0]["id"]) == ["reserve", "confirm_exit"]


def test_failover_preserves_authorizations_and_deduplicates_resource_ledger(env):
    """Failover within the same run_id does not duplicate idempotent ledger intents and isolates per-attempt tokens."""
    store, task_id, source, config = env
    summary = run_worker(store, config(["empty_a", "ok_b"]), task_id, "general", str(source))
    assert summary["status"] == "complete"
    run_id = summary["run_id"]

    # Authorizations / capability tokens issued during attempts remain bounded to their respective PIDs
    tokens = store.db.execute("SELECT task_id, run_id, worker_pid FROM tool_broker_tokens WHERE run_id=?", (run_id,)).fetchall()
    assert len(tokens) == 2
    assert len({r["worker_pid"] for r in tokens}) == 2

    # Resource Ledger idempotency prevents duplicated side effects across failover retries
    idem_key = f"failover-side-effect-{run_id}"
    rec1 = store.resource_ledger.record_intent(
        task_id=task_id,
        plan_revision=1,
        resource_type=ResourceType.EXTERNAL,
        raw_id="https://example.invalid/webhook",
        action=ResourceAction.CALL,
        idempotency_key=idem_key,
        run_id=run_id,
        requires_authorization=True,
    )
    rec2 = store.resource_ledger.record_intent(
        task_id=task_id,
        plan_revision=1,
        resource_type=ResourceType.EXTERNAL,
        raw_id="https://example.invalid/webhook",
        action=ResourceAction.CALL,
        idempotency_key=idem_key,
        run_id=run_id,
        requires_authorization=True,
    )
    assert rec1.id == rec2.id
    rows = store.db.execute(
        "SELECT COUNT(*) FROM resource_ledger WHERE task_id=? AND idempotency_key=?",
        (task_id, idem_key),
    ).fetchone()[0]
    assert rows == 1
