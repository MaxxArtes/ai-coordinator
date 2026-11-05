"""Model availability, price change tracking, and audit ledger (Phase 4C.1).

Maintains an immutable historical record of:
1. Dynamic model discovery and catalog snapshots.
2. Availability states, HTTP 429 rate limits, and 503 provider errors.
3. Price mutations and removals.
4. Model identity preservation across benchmark iterations.
5. Persistent audit storage across process restarts.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from enum import Enum
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AvailabilityEventType(str, Enum):
    """Categorized lifecycle and availability events."""
    MODEL_DISCOVERED = "MODEL_DISCOVERED"
    MODEL_AVAILABLE = "MODEL_AVAILABLE"
    RATE_LIMITED_429 = "RATE_LIMITED_429"
    PROVIDER_UNAVAILABLE_503 = "PROVIDER_UNAVAILABLE_503"
    PRICE_CHANGED_BLOCKED = "PRICE_CHANGED_BLOCKED"
    MODEL_REMOVED = "MODEL_REMOVED"
    BENCHMARK_EXECUTED = "BENCHMARK_EXECUTED"
    CIRCUIT_BREAKER_TRIPPED = "CIRCUIT_BREAKER_TRIPPED"


@dataclasses.dataclass(frozen=True)
class AvailabilityEvent:
    """Immutable event record in the availability ledger."""
    event_id: str
    timestamp: str
    timestamp_epoch: float
    model_id: str
    event_type: AvailabilityEventType
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "timestamp_epoch": self.timestamp_epoch,
            "model_id": self.model_id,
            "event_type": self.event_type.value,
            "details": self.details,
        }


class AvailabilityLedger:
    """Persistent ledger tracking model availability, errors, and catalog snapshots."""

    def __init__(self, store: Optional[Any] = None, storage_path: Optional[str] = None) -> None:
        self.store = store
        self.storage_path = storage_path
        self._events: List[AvailabilityEvent] = []
        self._snapshots: List[Dict[str, Any]] = []

        if self.storage_path:
            self._load_persisted_audit_trail()

    def _load_persisted_audit_trail(self) -> None:
        """Load historical events and snapshots from persistent storage."""
        if not self.storage_path or not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    rec_type = rec.get("record_type")
                    if rec_type == "event":
                        ev = AvailabilityEvent(
                            event_id=rec["event_id"],
                            timestamp=rec["timestamp"],
                            timestamp_epoch=float(rec["timestamp_epoch"]),
                            model_id=rec["model_id"],
                            event_type=AvailabilityEventType(rec["event_type"]),
                            details=rec.get("details", {}),
                        )
                        self._events.append(ev)
                    elif rec_type == "snapshot":
                        self._snapshots.append({
                            "recorded_at": rec["recorded_at"],
                            "data": rec["data"],
                        })
        except Exception as exc:
            logger.debug("Falha ao restaurar trilha de auditoria em %s: %s", self.storage_path, exc)

    def _persist_record(self, record: Dict[str, Any]) -> None:
        """Append record synchronously to persistent audit storage."""
        if not self.storage_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.storage_path)), exist_ok=True)
            with open(self.storage_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception as exc:
            logger.debug("Falha ao persistir registro em %s: %s", self.storage_path, exc)

    def record_event(
        self,
        model_id: str,
        event_type: AvailabilityEventType,
        details: Optional[Dict[str, Any]] = None,
    ) -> AvailabilityEvent:
        """Record an availability or lifecycle event and persist it."""
        now_utc = dt.datetime.now(dt.timezone.utc).isoformat()
        epoch = time.time()
        ev_id = f"avail_{int(epoch * 1000)}_{len(self._events)}"

        event = AvailabilityEvent(
            event_id=ev_id,
            timestamp=now_utc,
            timestamp_epoch=epoch,
            model_id=model_id,
            event_type=event_type,
            details=details or {},
        )
        self._events.append(event)

        # Synchronous append to audit storage
        self._persist_record({
            "record_type": "event",
            **event.to_dict(),
        })
        return event

    def record_snapshot(self, snapshot_data: Dict[str, Any]) -> None:
        """Record a full catalog snapshot for provenance and audit."""
        rec_time = dt.datetime.now(dt.timezone.utc).isoformat()
        snapshot_entry = {
            "recorded_at": rec_time,
            "data": snapshot_data,
        }
        self._snapshots.append(snapshot_entry)

        # Synchronous append to audit storage
        self._persist_record({
            "record_type": "snapshot",
            "recorded_at": rec_time,
            "data": snapshot_data,
        })

    def get_events_for_model(self, model_id: str) -> List[AvailabilityEvent]:
        """Retrieve all events associated with a specific model ID."""
        return [e for e in self._events if e.model_id == model_id]

    def count_recent_errors(self, model_id: str, window_seconds: float = 300.0) -> int:
        """Count 429 and 503 occurrences within the given time window."""
        now = time.time()
        cutoff = now - window_seconds
        error_types = {
            AvailabilityEventType.RATE_LIMITED_429,
            AvailabilityEventType.PROVIDER_UNAVAILABLE_503,
            AvailabilityEventType.CIRCUIT_BREAKER_TRIPPED,
        }
        return sum(
            1 for e in self._events
            if e.model_id == model_id and e.event_type in error_types and e.timestamp_epoch >= cutoff
        )

    def is_model_healthy(self, model_id: str, max_recent_errors: int = 3, window_seconds: float = 300.0) -> bool:
        """True if the model has experienced fewer than max_recent_errors recently."""
        return self.count_recent_errors(model_id, window_seconds=window_seconds) < max_recent_errors

    def export_audit_trail(self) -> Dict[str, Any]:
        """Export serialized ledger audit trail."""
        return {
            "total_events": len(self._events),
            "total_snapshots": len(self._snapshots),
            "events": [e.to_dict() for e in self._events],
            "snapshots_summary": [
                {"recorded_at": s["recorded_at"], "total_models": s["data"].get("total_models", 0)}
                for s in self._snapshots
            ],
        }
