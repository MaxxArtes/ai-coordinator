"""Multi-Processing Worker Functions for Phase 4C.2.4 Adversarial Concurrency Tests.

These functions execute in completely isolated OS processes to validate:
1. Multi-process SQLite WAL contention for the final unit of max_dispatches.
2. Concurrent multi-process outbox event projection to pilot_audit.jsonl.
3. Multi-process simultaneous conservative crash recovery scanner.
"""
from __future__ import annotations

import sys
import time
from typing import Any

from ai_coordinator.openrouter.atomic_quota import (
    AtomicDispatchQuotaStore,
    BudgetExhaustedError,
    TransactionalOutboxProjector,
)


def mp_contention_worker(
    db_path: str,
    budget_id: str,
    attempt_id: str,
    seq_idx: int,
    target_model: str,
    result_queue: Any,
) -> None:
    """Worker process attempting to acquire a reservation under high process contention."""
    try:
        store = AtomicDispatchQuotaStore(db_path=db_path)
        res = store.acquire_reservation(
            budget_id=budget_id,
            logical_attempt_id=attempt_id,
            sequence_index=seq_idx,
            target_model=target_model,
        )
        result_queue.put(("SUCCESS", res.reservation_id))
    except BudgetExhaustedError as exc:
        result_queue.put(("EXHAUSTED", str(exc)))
    except Exception as exc:
        result_queue.put(("ERROR", f"{type(exc).__name__}: {exc}"))


def mp_outbox_projector_worker(
    db_path: str,
    ledger_path: str,
    result_queue: Any,
) -> None:
    """Worker process projecting outbox events concurrently into shared JSONL ledger."""
    try:
        store = AtomicDispatchQuotaStore(db_path=db_path)
        projector = TransactionalOutboxProjector(store=store, ledger_path=ledger_path)
        count = projector.project_pending_events()
        result_queue.put(("PROJECTED", count))
    except Exception as exc:
        result_queue.put(("ERROR", f"{type(exc).__name__}: {exc}"))


def mp_recovery_scanner_worker(
    db_path: str,
    threshold_sec: float,
    result_queue: Any,
) -> None:
    """Worker process running conservative crash recovery concurrently."""
    try:
        store = AtomicDispatchQuotaStore(db_path=db_path)
        recovered = store.recover_interrupted_reservations()
        result_queue.put(("RECOVERED", len(recovered)))
    except Exception as exc:
        result_queue.put(("ERROR", f"{type(exc).__name__}: {exc}"))


def mp_lock_contention_worker(
    lock_path: str,
    worker_id: str,
    hold_duration_sec: float,
    timeout_sec: float,
    result_queue: Any,
) -> None:
    """Worker process acquiring native inter-process lock, holding it, and reporting status."""
    import os
    from ai_coordinator.openrouter.atomic_quota import _InterProcessFileLock
    try:
        with _InterProcessFileLock(lock_path, timeout_sec=timeout_sec):
            result_queue.put(("ACQUIRED", worker_id, os.getpid()))
            if hold_duration_sec > 0:
                time.sleep(hold_duration_sec)
            result_queue.put(("RELEASED", worker_id, os.getpid()))
    except TimeoutError as exc:
        result_queue.put(("TIMEOUT", worker_id, str(exc)))
    except Exception as exc:
        result_queue.put(("ERROR", worker_id, f"{type(exc).__name__}: {exc}"))
