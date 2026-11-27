"""Emergency Resource Governor & Cascade Guard for AI Coordinator Core.

Provides machine-wide resource monitoring (zero external dependencies),
configurable CPU/RAM reserves (default 10% free each), hysteresis-based
backpressure bands, atomic shared admission across governed sessions via
SQLite, abandoned-lease recovery, and cascade protection.

Clearly distinguishes OS-enforced limits on governed process trees (e.g.
Win32 Job Objects) from observed external machine load outside Coordinator
authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime as dt
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple
import uuid

from .process import ProcessSupervisor, WindowsProcessController, safe_process_alive

if os.name == "nt":
    import ctypes
    import ctypes.wintypes as wintypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    _kernel32 = ctypes.windll.kernel32


class ResourceGovernorError(ValueError):
    """Raised when admission is denied due to resource saturation or cascade limits."""

    def __init__(self, reason: str, message: str, snapshot: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.reason = reason
        self.snapshot = snapshot or {}


class SaturationBand:
    NORMAL = "normal"          # < 75%
    ELEVATED = "elevated"      # 75% - 85%
    RESTRICTED = "restricted"  # 85% - 90%
    CRITICAL = "critical"      # 90% - 95%
    EMERGENCY = "emergency"    # >= 95% or reserve breached


@dataclass(frozen=True)
class SystemSnapshot:
    """Observed machine-wide resource utilization (governed + external)."""

    cpu_used_pct: float
    cpu_free_pct: float
    ram_used_pct: float
    ram_free_pct: float
    ram_total_mb: int
    ram_avail_mb: int
    disk_free_gb: float
    disk_used_pct: float
    observed_at: str
    probe_duration_ms: float
    platform: str = "windows" if os.name == "nt" else "posix"
    authority_scope: str = "governed_processes_only"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GovernorConfig:
    """Configurable reserves and cascade limits."""

    min_free_cpu_pct: float = 10.0
    min_free_ram_pct: float = 10.0
    max_global_concurrent: int = 4
    max_delegation_depth: int = 3
    max_delegations_per_window: int = 12
    rate_window_seconds: float = 60.0
    lease_ttl_seconds: float = 300.0
    worker_max_subprocesses: int = 16
    worker_cpu_cap_pct: Optional[float] = 75.0
    hysteresis_margin_pct: float = 5.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _filetime_to_int(ft: Any) -> int:
    return (int(ft.dwHighDateTime) << 32) + int(ft.dwLowDateTime)


def _sample_windows_cpu(sample_seconds: float = 0.2) -> float:
    idle1, kern1, user1 = _FILETIME(), _FILETIME(), _FILETIME()
    if not _kernel32.GetSystemTimes(ctypes.byref(idle1), ctypes.byref(kern1), ctypes.byref(user1)):
        return 0.0
    time.sleep(max(0.05, sample_seconds))
    idle2, kern2, user2 = _FILETIME(), _FILETIME(), _FILETIME()
    if not _kernel32.GetSystemTimes(ctypes.byref(idle2), ctypes.byref(kern2), ctypes.byref(user2)):
        return 0.0
    d_idle = _filetime_to_int(idle2) - _filetime_to_int(idle1)
    d_kern = _filetime_to_int(kern2) - _filetime_to_int(kern1)
    d_user = _filetime_to_int(user2) - _filetime_to_int(user1)
    total = d_kern + d_user
    if total <= 0:
        return 0.0
    busy = max(0, total - d_idle)
    return round(min(100.0, max(0.0, (busy / total) * 100.0)), 2)


def _sample_posix_cpu(sample_seconds: float = 0.12) -> float:
    stat_path = Path("/proc/stat")
    if stat_path.is_file():
        def _read_cpu() -> Tuple[int, int]:
            line = stat_path.read_text(encoding="utf-8").splitlines()[0]
            parts = [int(x) for x in line.split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
            return idle, sum(parts)

        idle1, tot1 = _read_cpu()
        time.sleep(max(0.02, sample_seconds))
        idle2, tot2 = _read_cpu()
        dtot = tot2 - tot1
        if dtot > 0:
            return round(min(100.0, max(0.0, ((dtot - (idle2 - idle1)) / dtot) * 100.0)), 2)
    try:
        load1 = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        return round(min(100.0, max(0.0, (load1 / cores) * 100.0)), 2)
    except Exception:
        return 0.0


def _sample_memory() -> Tuple[int, int, float]:
    if os.name == "nt":
        mem = _MEMORYSTATUSEX(dwLength=ctypes.sizeof(_MEMORYSTATUSEX))
        if _kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)) and mem.ullTotalPhys > 0:
            total_mb = int(mem.ullTotalPhys // (1024 * 1024))
            avail_mb = int(mem.ullAvailPhys // (1024 * 1024))
            used_pct = round(100.0 * (1.0 - (mem.ullAvailPhys / mem.ullTotalPhys)), 2)
            return total_mb, avail_mb, used_pct
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        vals: Dict[str, int] = {}
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k, rest = line.split(":", 1)
                num = rest.strip().split()[0]
                if num.isdigit():
                    vals[k] = int(num)
        tot_kb = vals.get("MemTotal", 0)
        avail_kb = vals.get("MemAvailable", vals.get("MemFree", 0))
        if tot_kb > 0:
            used_pct = round(100.0 * (1.0 - (avail_kb / tot_kb)), 2)
            return tot_kb // 1024, avail_kb // 1024, used_pct
    if sys.platform == "darwin":
        try:
            total_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=2).strip())
            vm_out = subprocess.check_output(["vm_stat"], text=True, timeout=2)
            page_size = 4096
            pages: Dict[str, int] = {}
            for line in vm_out.splitlines():
                if "page size of" in line:
                    for tok in line.split():
                        if tok.isdigit():
                            page_size = int(tok)
                            break
                elif ":" in line:
                    k, v = line.split(":", 1)
                    num = v.strip().rstrip(".")
                    if num.isdigit():
                        pages[k.strip()] = int(num)
            free_pages = (
                pages.get("Pages free", 0)
                + pages.get("Pages inactive", 0)
                + pages.get("Pages speculative", 0)
            )
            avail_bytes = free_pages * page_size
            total_mb = total_bytes // (1024 * 1024)
            avail_mb = min(total_mb, avail_bytes // (1024 * 1024))
            used_pct = round(100.0 * (1.0 - (avail_mb / total_mb)), 2) if total_mb > 0 else 0.0
            return total_mb, avail_mb, max(0.0, min(100.0, used_pct))
        except Exception:
            pass
    return 0, 0, 0.0


def probe_system_snapshot(
    disk_path: str | Path = ".",
    *,
    cpu_sample_seconds: float = 0.12,
) -> SystemSnapshot:
    """Measure global CPU, RAM, and disk utilization with zero external deps."""
    t0 = time.perf_counter()
    cpu_used = _sample_windows_cpu(cpu_sample_seconds) if os.name == "nt" else _sample_posix_cpu(cpu_sample_seconds)
    total_mb, avail_mb, ram_used = _sample_memory()
    try:
        du = shutil.disk_usage(str(disk_path))
        disk_free_gb = round(du.free / (1024 ** 3), 2)
        disk_used_pct = round((du.used / du.total) * 100.0, 2) if du.total > 0 else 0.0
    except Exception:
        disk_free_gb = 0.0
        disk_used_pct = 0.0
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return SystemSnapshot(
        cpu_used_pct=cpu_used,
        cpu_free_pct=round(max(0.0, 100.0 - cpu_used), 2),
        ram_used_pct=ram_used,
        ram_free_pct=round(max(0.0, 100.0 - ram_used), 2),
        ram_total_mb=total_mb,
        ram_avail_mb=avail_mb,
        disk_free_gb=disk_free_gb,
        disk_used_pct=disk_used_pct,
        observed_at=_now_iso(),
        probe_duration_ms=elapsed_ms,
    )


def classify_saturation_band(
    snapshot: SystemSnapshot,
    config: GovernorConfig,
    previous_band: str = SaturationBand.NORMAL,
) -> str:
    """Classify machine load into bands with hysteresis to avoid oscillation."""
    if (
        snapshot.cpu_free_pct < config.min_free_cpu_pct
        or snapshot.ram_free_pct < config.min_free_ram_pct
        or snapshot.cpu_used_pct >= 95.0
        or snapshot.ram_used_pct >= 95.0
    ):
        return SaturationBand.EMERGENCY

    peak = max(snapshot.cpu_used_pct, snapshot.ram_used_pct)
    if peak >= 90.0:
        return SaturationBand.CRITICAL
    if peak >= 85.0:
        return SaturationBand.RESTRICTED
    if peak >= 75.0:
        return SaturationBand.ELEVATED

    # Hysteresis: when recovering from a degraded band, require a margin below 75%
    margin = max(0.0, config.hysteresis_margin_pct)
    if previous_band in (SaturationBand.EMERGENCY, SaturationBand.CRITICAL, SaturationBand.RESTRICTED):
        if peak >= (75.0 - margin):
            return SaturationBand.ELEVATED
    return SaturationBand.NORMAL


def effective_concurrency_cap(band: str, config: GovernorConfig) -> int:
    base = max(1, int(config.max_global_concurrent))
    if band == SaturationBand.EMERGENCY:
        return 0
    if band == SaturationBand.CRITICAL:
        return 1
    if band == SaturationBand.RESTRICTED:
        return 1
    if band == SaturationBand.ELEVATED:
        return max(1, base // 2)
    return base


def effective_worker_cpu_cap(band: str, config: GovernorConfig) -> Optional[float]:
    cap = config.worker_cpu_cap_pct
    if cap is None:
        return None
    if band == SaturationBand.CRITICAL:
        return min(cap, 25.0)
    if band == SaturationBand.RESTRICTED:
        return min(cap, 50.0)
    if band == SaturationBand.ELEVATED:
        return min(cap, 65.0)
    return cap


def resolve_global_governor_db(fallback_root: Path) -> Path:
    """Resolve the machine-shared governor database across all roots/profiles/USERPROFILEs.

    AI_COORDINATOR_GLOBAL_GOVERNOR_DIR is honored only under pytest so production
    sessions cannot bypass the shared machine authority via environment overrides.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        override = os.environ.get("AI_COORDINATOR_GLOBAL_GOVERNOR_DIR")
        if override:
            base = Path(override).expanduser().resolve()
            base.mkdir(parents=True, exist_ok=True)
            return base / "governor.sqlite"

    candidates = []
    if os.name == "nt":
        prog_data = os.environ.get("ProgramData") or os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData"
        sys_drive = os.environ.get("SystemDrive", "C:")
        candidates.append(Path(prog_data) / "ai-coordinator" / "global_governor")
        candidates.append(Path(sys_drive + r"\Users\Public\ai-coordinator\global_governor"))
    else:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        candidates.append(Path(f"/var/tmp/ai-coordinator-governor-{uid}"))

    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base / "governor.sqlite"
        except OSError:
            continue
    return fallback_root / "coordination.sqlite"


class ResourceGovernor:
    """Shared SQLite-coordinated admission controller and lease manager."""

    def __init__(
        self,
        db: sqlite3.Connection,
        root: Path,
        *,
        probe_fn: Optional[Callable[[], SystemSnapshot]] = None,
        shared_db_path: Optional[Path] = None,
    ):
        self.root = root
        self._owns_db = False
        target_path = shared_db_path or resolve_global_governor_db(root)
        if target_path == root / "coordination.sqlite":
            self.db = db
        else:
            self.db = sqlite3.connect(target_path, timeout=20.0, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA busy_timeout=20000")
            self._owns_db = True
        self._probe_fn = probe_fn or (lambda: probe_system_snapshot(self.root))
        self._init_schema()

    def close(self) -> None:
        if self._owns_db:
            try:
                self.db.close()
            except Exception:
                pass

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS resource_governor_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                min_free_cpu_pct REAL NOT NULL DEFAULT 10.0,
                min_free_ram_pct REAL NOT NULL DEFAULT 10.0,
                max_global_concurrent INTEGER NOT NULL DEFAULT 4,
                max_delegation_depth INTEGER NOT NULL DEFAULT 3,
                max_delegations_per_window INTEGER NOT NULL DEFAULT 12,
                rate_window_seconds REAL NOT NULL DEFAULT 60.0,
                lease_ttl_seconds REAL NOT NULL DEFAULT 300.0,
                worker_max_subprocesses INTEGER NOT NULL DEFAULT 16,
                worker_cpu_cap_pct REAL DEFAULT 75.0,
                hysteresis_margin_pct REAL NOT NULL DEFAULT 5.0,
                last_band TEXT NOT NULL DEFAULT 'normal',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS resource_governor_leases (
                lease_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                run_id TEXT,
                project TEXT NOT NULL,
                profile TEXT NOT NULL DEFAULT 'default',
                activity_weight TEXT NOT NULL DEFAULT 'medium',
                owner_pid INTEGER NOT NULL,
                owner_create_time INTEGER,
                worker_pid INTEGER,
                worker_create_time INTEGER,
                delegation_depth INTEGER NOT NULL DEFAULT 0,
                parent_lease_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                released_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_rg_leases_status ON resource_governor_leases(status, expires_at);

            CREATE TABLE IF NOT EXISTS resource_governor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                band TEXT NOT NULL,
                cpu_used_pct REAL NOT NULL,
                ram_used_pct REAL NOT NULL,
                active_leases INTEGER NOT NULL,
                detail TEXT,
                created_at REAL NOT NULL
            );
            """
        )
        row = self.db.execute("SELECT id FROM resource_governor_config WHERE id=1").fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO resource_governor_config (id, updated_at) VALUES (1, ?)",
                (_now_iso(),),
            )
        self.db.commit()

    def get_config(self) -> Tuple[GovernorConfig, str]:
        row = self.db.execute("SELECT * FROM resource_governor_config WHERE id=1").fetchone()
        cfg = GovernorConfig(
            min_free_cpu_pct=float(row["min_free_cpu_pct"]),
            min_free_ram_pct=float(row["min_free_ram_pct"]),
            max_global_concurrent=int(row["max_global_concurrent"]),
            max_delegation_depth=int(row["max_delegation_depth"]),
            max_delegations_per_window=int(row["max_delegations_per_window"]),
            rate_window_seconds=float(row["rate_window_seconds"]),
            lease_ttl_seconds=float(row["lease_ttl_seconds"]),
            worker_max_subprocesses=int(row["worker_max_subprocesses"]),
            worker_cpu_cap_pct=(
                float(row["worker_cpu_cap_pct"]) if row["worker_cpu_cap_pct"] is not None else None
            ),
            hysteresis_margin_pct=float(row["hysteresis_margin_pct"]),
        )
        return cfg, str(row["last_band"])

    def update_config(self, **updates: Any) -> GovernorConfig:
        allowed = {
            "min_free_cpu_pct",
            "min_free_ram_pct",
            "max_global_concurrent",
            "max_delegation_depth",
            "max_delegations_per_window",
            "rate_window_seconds",
            "lease_ttl_seconds",
            "worker_max_subprocesses",
            "worker_cpu_cap_pct",
            "hysteresis_margin_pct",
        }
        bad = set(updates) - allowed
        if bad:
            raise ValueError(f"Parametros invalidos do governor: {sorted(bad)}")
        if not updates:
            return self.get_config()[0]
        for k in ("min_free_cpu_pct", "min_free_ram_pct"):
            if k in updates and not (0.0 <= float(updates[k]) <= 90.0):
                raise ValueError(f"{k} deve estar entre 0 e 90")
        if "max_global_concurrent" in updates and int(updates["max_global_concurrent"]) < 1:
            raise ValueError("max_global_concurrent deve ser >= 1")
        if "max_delegation_depth" in updates and int(updates["max_delegation_depth"]) < 0:
            raise ValueError("max_delegation_depth deve ser >= 0")

        keys = list(updates)
        self.db.execute(
            "UPDATE resource_governor_config SET "
            + ", ".join(f"{k}=?" for k in keys)
            + ", updated_at=? WHERE id=1",
            [updates[k] for k in keys] + [_now_iso()],
        )
        self.db.commit()
        return self.get_config()[0]

    def reclaim_abandoned_leases(self, now_ts: Optional[float] = None) -> int:
        """Expire leases only when OS processes have actually exited; renew live leases."""
        ts = now_ts if now_ts is not None else time.time()
        cfg, _ = self.get_config()
        rows = self.db.execute(
            "SELECT lease_id, owner_pid, owner_create_time, worker_pid, worker_create_time, expires_at "
            "FROM resource_governor_leases WHERE status='active'"
        ).fetchall()
        reclaimed = 0
        for r in rows:
            expired = float(r["expires_at"]) <= ts
            owner_dead = not safe_process_alive(int(r["owner_pid"]), r["owner_create_time"])
            worker_pid = r["worker_pid"]
            worker_dead = (
                worker_pid is not None
                and not safe_process_alive(int(worker_pid), r["worker_create_time"])
            )
            # Never free a lease while either the owner or worker OS process is still alive
            process_gone = owner_dead and (worker_pid is None or worker_dead)
            if process_gone:
                self.db.execute(
                    "UPDATE resource_governor_leases SET status='expired', released_at=? WHERE lease_id=?",
                    (ts, r["lease_id"]),
                )
                reclaimed += 1
            elif expired:
                # Process is still alive past initial TTL: renew lease instead of freeing its slot
                self.db.execute(
                    "UPDATE resource_governor_leases SET expires_at=? WHERE lease_id=? AND status='active'",
                    (ts + cfg.lease_ttl_seconds, r["lease_id"]),
                )
        return reclaimed

    def status(self) -> Dict[str, Any]:
        """Return non-mutating operational status (reclaims dead leases on read)."""
        cfg, prev_band = self.get_config()
        reclaimed = self.reclaim_abandoned_leases()
        if reclaimed:
            self.db.commit()
        snap = self._probe_fn()
        band = classify_saturation_band(snap, cfg, prev_band)
        active = self.db.execute(
            "SELECT lease_id, task_id, run_id, project, profile, activity_weight, owner_pid, worker_pid, delegation_depth "
            "FROM resource_governor_leases WHERE status='active'"
        ).fetchall()
        return {
            "band": band,
            "previous_band": prev_band,
            "admitting": band != SaturationBand.EMERGENCY and len(active) < effective_concurrency_cap(band, cfg),
            "effective_concurrency_cap": effective_concurrency_cap(band, cfg),
            "effective_worker_cpu_cap_pct": effective_worker_cpu_cap(band, cfg),
            "active_leases_count": len(active),
            "active_leases": [dict(r) for r in active],
            "reclaimed_leases": reclaimed,
            "reserves": {
                "min_free_cpu_pct": cfg.min_free_cpu_pct,
                "min_free_ram_pct": cfg.min_free_ram_pct,
                "cpu_reserve_met": snap.cpu_free_pct >= cfg.min_free_cpu_pct,
                "ram_reserve_met": snap.ram_free_pct >= cfg.min_free_ram_pct,
            },
            "config": cfg.to_dict(),
            "system": snap.to_dict(),
        }

    def acquire_lease(
        self,
        *,
        task_id: str,
        project: str,
        run_id: Optional[str] = None,
        profile: str = "default",
        activity_weight: str = "medium",
        delegation_depth: int = 0,
        parent_lease_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically evaluate saturation, cascade limits, and acquire an execution lease."""
        if self._owns_db:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            res = self._acquire_lease_locked(
                task_id=task_id,
                project=project,
                run_id=run_id,
                profile=profile,
                activity_weight=activity_weight,
                delegation_depth=delegation_depth,
                parent_lease_id=parent_lease_id,
            )
            if self._owns_db:
                self.db.commit()
            return res
        except Exception:
            if self._owns_db:
                self.db.rollback()
            raise

    def _acquire_lease_locked(
        self,
        *,
        task_id: str,
        project: str,
        run_id: Optional[str] = None,
        profile: str = "default",
        activity_weight: str = "medium",
        delegation_depth: int = 0,
        parent_lease_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now_ts = time.time()
        cfg, prev_band = self.get_config()
        self.reclaim_abandoned_leases(now_ts)
        snap = self._probe_fn()
        band = classify_saturation_band(snap, cfg, prev_band)
        self.db.execute(
            "UPDATE resource_governor_config SET last_band=?, updated_at=? WHERE id=1",
            (band, _now_iso()),
        )

        active_count = int(
            self.db.execute("SELECT COUNT(*) FROM resource_governor_leases WHERE status='active'").fetchone()[0]
        )
        eff_cap = effective_concurrency_cap(band, cfg)

        def _deny(reason: str, msg: str) -> None:
            self.db.execute(
                "INSERT INTO resource_governor_events(event_type, band, cpu_used_pct, ram_used_pct, active_leases, detail, created_at) "
                "VALUES ('denied', ?, ?, ?, ?, ?, ?)",
                (band, snap.cpu_used_pct, snap.ram_used_pct, active_count, f"{reason}: {msg}", now_ts),
            )
            raise ResourceGovernorError(
                reason,
                msg,
                {"band": band, "active_leases": active_count, "system": snap.to_dict()},
            )

        if delegation_depth > cfg.max_delegation_depth:
            _deny(
                "cascade_depth_exceeded",
                f"Profundidade de delegacao {delegation_depth} excede limite {cfg.max_delegation_depth}",
            )

        recent_acquires = int(
            self.db.execute(
                "SELECT COUNT(*) FROM resource_governor_events WHERE event_type='acquired' AND created_at >= ?",
                (now_ts - cfg.rate_window_seconds,),
            ).fetchone()[0]
        )
        if recent_acquires >= cfg.max_delegations_per_window:
            _deny(
                "rate_limit_exceeded",
                f"Frequencia de solicitacoes ({recent_acquires}/{cfg.max_delegations_per_window} em {cfg.rate_window_seconds}s) excedida",
            )

        if band == SaturationBand.EMERGENCY:
            _deny(
                "saturation_emergency",
                f"Contencao emergencial ativa (CPU usada {snap.cpu_used_pct}%, RAM usada {snap.ram_used_pct}%)",
            )

        if band in (SaturationBand.RESTRICTED, SaturationBand.CRITICAL) and activity_weight == "heavy":
            _deny(
                "heavy_activity_blocked",
                f"Atividade pesada bloqueada na faixa '{band}' (CPU {snap.cpu_used_pct}%, RAM {snap.ram_used_pct}%)",
            )

        if active_count >= eff_cap:
            _deny(
                "concurrency_limit",
                f"Concorrencia governada atingiu o limite efetivo ({active_count}/{eff_cap}) na faixa '{band}'",
            )

        lease_id = uuid.uuid4().hex[:16]
        owner_pid = os.getpid()
        owner_ident = ProcessSupervisor.capture_identity(owner_pid)
        owner_ct = owner_ident.create_time_ns if owner_ident else None
        self.db.execute(
            """INSERT INTO resource_governor_leases(
                lease_id, task_id, run_id, project, profile, activity_weight,
                owner_pid, owner_create_time, delegation_depth, parent_lease_id,
                status, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                lease_id,
                task_id,
                run_id,
                project,
                profile,
                activity_weight,
                owner_pid,
                owner_ct,
                delegation_depth,
                parent_lease_id,
                now_ts,
                now_ts + cfg.lease_ttl_seconds,
            ),
        )
        self.db.execute(
            "INSERT INTO resource_governor_events(event_type, band, cpu_used_pct, ram_used_pct, active_leases, detail, created_at) "
            "VALUES ('acquired', ?, ?, ?, ?, ?, ?)",
            (band, snap.cpu_used_pct, snap.ram_used_pct, active_count + 1, f"lease={lease_id}", now_ts),
        )
        return {
            "lease_id": lease_id,
            "band": band,
            "effective_concurrency_cap": eff_cap,
            "effective_worker_cpu_cap_pct": effective_worker_cpu_cap(band, cfg),
            "worker_max_subprocesses": cfg.worker_max_subprocesses,
        }

    def attach_worker(self, lease_id: str, worker_pid: int, worker_create_time: Optional[int]) -> None:
        self.db.execute(
            "UPDATE resource_governor_leases SET worker_pid=?, worker_create_time=? WHERE lease_id=? AND status='active'",
            (worker_pid, worker_create_time, lease_id),
        )

    def release_lease(self, lease_id: str) -> None:
        self.db.execute(
            "UPDATE resource_governor_leases SET status='released', released_at=? WHERE lease_id=? AND status='active'",
            (time.time(), lease_id),
        )
        if self._owns_db:
            self.db.commit()

    def release_for_run(self, run_id: str) -> None:
        self.db.execute(
            "UPDATE resource_governor_leases SET status='released', released_at=? WHERE run_id=? AND status='active'",
            (time.time(), run_id),
        )
        if self._owns_db:
            self.db.commit()

    def rebalance_active_job(self, job_handle: Optional[int], lease_id: Optional[str] = None) -> Dict[str, Any]:
        """Dynamically throttle an already-active Win32 Job Object based on current load & active leases."""
        cfg, prev_band = self.get_config()
        snap = self._probe_fn()
        band = classify_saturation_band(snap, cfg, prev_band)
        self.db.execute(
            "UPDATE resource_governor_config SET last_band=?, updated_at=? WHERE id=1",
            (band, _now_iso()),
        )
        active_count = max(
            1,
            int(self.db.execute("SELECT COUNT(*) FROM resource_governor_leases WHERE status='active'").fetchone()[0]),
        )
        base_cap = effective_worker_cpu_cap(band, cfg)
        if base_cap is None:
            base_cap = 100.0
        if band == SaturationBand.EMERGENCY:
            per_job_cap = max(5.0, round(min(15.0, base_cap / active_count), 2))
        else:
            per_job_cap = max(5.0, round(base_cap / active_count, 2))

        applied = False
        if os.name == "nt" and job_handle:
            applied = WindowsProcessController.set_job_cpu_rate(job_handle, per_job_cap)
        if self._owns_db:
            self.db.commit()
        return {
            "band": band,
            "active_leases": active_count,
            "applied_cpu_cap_pct": per_job_cap,
            "job_updated": applied,
            "lease_id": lease_id,
        }
