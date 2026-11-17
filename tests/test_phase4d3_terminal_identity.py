"""Marco 4D.3, Etapa 6 (F10): stable identity of every terminal in a tab.

Operator criterion (16/11): no live process may be left without a supervisor or be confused with another terminal
after a close, an ``exit`` or the reuse of a pane position; closing one terminal never affects the others.

- The identity is the ``session_id``. A pane position (0-3) is only a slot: a registered session (alive or ended by
  ``exit``) keeps its slot until it is closed, and a new terminal never overwrites it.
- Closing removes only the session with that ``session_id``.
- Closing the tab ends every session of the tab, the ended ones included.

Checks: the two scenarios of the defect, 200 random sequences of up to 20 operations with a simulated process (five
invariants after every operation), and on Windows the same scenarios with real ConPTY and ``cmd.exe``.
"""

from __future__ import annotations

import random
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import pytest

from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.terminal.manager import MAX_PANES_PER_TAB, TerminalManager, TerminalManagerError
from desktop.terminal.pty_backend import BasePtyBackend

TAB = "tab-main"


class SimulatedProcess(BasePtyBackend):
    """A process stand-in: alive after spawn, ends by ``exit`` or by ``terminate``."""

    registry: List["SimulatedProcess"] = []

    def __init__(self) -> None:
        super().__init__()
        self.alive = False
        self.code: Optional[int] = None
        SimulatedProcess.registry.append(self)

    def spawn(self, cmd, cwd, env=None, max_memory_mb=256):
        self.alive = True
        return SimpleNamespace(pid=None)

    def write(self, data):
        return len(data)

    def resize(self, cols, rows):
        return True

    def is_alive(self) -> bool:
        return self.alive

    def get_exit_code(self) -> Optional[int]:
        return self.code

    def terminate(self) -> bool:
        if self.alive:
            self.code = -1
        self.alive = False
        return True

    def exit(self, code: int = 0) -> None:
        self.alive = False
        self.code = code


@pytest.fixture
def env():
    SimulatedProcess.registry = []
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        prof_mgr = DesktopProfileManager(root_dir=tmp_path / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
        token = authorizer.issue_session("default")
        project = tmp_path / "repo"
        project.mkdir()
        prof_mgr.authorize_project("default", project)
        mgr = TerminalManager(authorizer=authorizer)
        mgr.create_tab(token=token, tab_id=TAB, title="Projeto", project_path=project)
        yield SimpleNamespace(mgr=mgr, token=token, project=project)


def _open(env, pane_id: int):
    return env.mgr.create_terminal(token=env.token, tab_id=TAB, pane_id=pane_id, backend=SimulatedProcess())


def _registered(mgr: TerminalManager, tab_id: str = TAB) -> Dict[int, object]:
    tab = mgr.get_tab(tab_id)
    return dict(tab.panes) if tab else {}


def test_scenario_1_open_three_close_the_middle_open_one(env):
    """Open 3, close the middle one, open 1 (the old UI asked for slot panes.length = 2): no error, 3 alive."""
    first, middle, third = (_open(env, i) for i in range(3))
    assert env.mgr.close_terminal(middle.session_id) is True

    fourth = _open(env, 2)  # slot 2 is taken by `third`: the manager picks a free slot instead of failing

    panes = _registered(env.mgr)
    assert sorted(s.session_id for s in panes.values()) == sorted([first.session_id, third.session_id, fourth.session_id])
    assert fourth.pane_id == 1
    assert len({s.pane_id for s in panes.values()}) == 3
    assert all(s.is_alive for s in panes.values())
    assert env.mgr.get_tab(TAB).to_dict()["active_panes_count"] == 3


def test_scenario_2_ended_terminal_keeps_its_slot_and_is_never_overwritten(env):
    """``exit`` in the 3rd: it stays registered as ended; a new terminal never overwrites it; closing it touches no other."""
    sessions = [_open(env, i) for i in range(3)]
    sessions[2].backend.exit(0)

    tab_dict = env.mgr.get_tab(TAB).to_dict()
    assert tab_dict["panes_count"] == 3
    assert tab_dict["active_panes_count"] == 2
    ended = tab_dict["panes"]["2"]
    assert ended["is_alive"] is False and ended["exit_code"] == 0

    newcomer = _open(env, 2)  # asks for the ended slot
    assert newcomer.pane_id == 3
    assert _registered(env.mgr)[2] is sessions[2]

    assert env.mgr.close_terminal(sessions[2].session_id) is True
    panes = _registered(env.mgr)
    assert 2 not in panes
    assert sorted(s.session_id for s in panes.values()) == sorted(
        [sessions[0].session_id, sessions[1].session_id, newcomer.session_id]
    )
    assert all(s.is_alive for s in panes.values())


def test_a_full_tab_refuses_a_fifth_terminal_without_touching_the_others(env):
    sessions = [_open(env, i) for i in range(MAX_PANES_PER_TAB)]
    sessions[1].backend.exit(3)  # ended but still registered: the slot stays taken
    with pytest.raises(TerminalManagerError):
        _open(env, 1)
    assert [_registered(env.mgr)[i] for i in range(4)] == sessions
    assert sum(1 for p in SimulatedProcess.registry if p.alive) == 3


def test_closing_a_stale_session_never_unregisters_the_slot_owner(env):
    """Even if a stale object pointing at a slot is closed, only the session with that id goes away."""
    owner = _open(env, 0)
    assert env.mgr.close_terminal("sessao-que-nao-existe") is False
    assert _registered(env.mgr)[0] is owner
    assert owner.is_alive


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("whole_tab", [False, True])
def test_failed_close_keeps_process_registered_and_slot_reserved_until_retry(env, raises, whole_tab):
    first = _open(env, 0)
    other = _open(env, 1)
    original = first.backend.terminate

    def fail():
        if raises:
            raise RuntimeError("termination refused")
        return False

    first.backend.terminate = fail
    close = (lambda: env.mgr.close_tab(TAB)) if whole_tab else (lambda: env.mgr.close_terminal(first.session_id))
    if raises:
        with pytest.raises(RuntimeError, match="termination refused"):
            close()
    else:
        assert close() is False
    assert first.is_alive and other.is_alive
    assert env.mgr.get_session(first.session_id) is first
    assert _registered(env.mgr)[0] is first
    newcomer = _open(env, 0)
    assert newcomer.pane_id == 2
    assert _registered(env.mgr)[0] is first
    assert env.mgr.get_session(other.session_id) is other
    first.backend.terminate = original
    assert close() is True
    assert not first.is_alive
    assert env.mgr.get_session(first.session_id) is None
    if whole_tab:
        assert env.mgr.get_tab(TAB) is None
        assert not other.is_alive and not newcomer.is_alive
    else:
        assert other.is_alive and newcomer.is_alive
        assert env.mgr.close_tab(TAB) is True


def test_failed_tab_close_keeps_only_unclosed_sessions_until_retry(env):
    first, middle, third = (_open(env, i) for i in range(3))
    original = middle.backend.terminate
    middle.backend.terminate = lambda: False
    assert env.mgr.close_tab(TAB) is False
    assert not first.is_alive
    assert env.mgr.get_session(first.session_id) is None
    assert _registered(env.mgr) == {1: middle, 2: third}
    assert middle.is_alive and third.is_alive
    middle.backend.terminate = original
    assert env.mgr.close_tab(TAB) is True
    assert env.mgr.sessions == {}


def _check_invariants(mgr: TerminalManager, closed_ids: set) -> None:
    tab = mgr.get_tab(TAB)
    panes = list(tab.panes.values()) if tab else []
    ids = [s.session_id for s in panes]
    # 1. every live process is registered in the tab and sits in exactly one pane
    live = [p for p in SimulatedProcess.registry if p.alive]
    owners = [s for s in panes if s.backend in live]
    assert len(owners) == len(live), "processo vivo fora da aba"
    # 2. no two panes with the same session_id nor the same slot
    assert len(ids) == len(set(ids))
    assert len({s.pane_id for s in panes}) == len(panes)
    assert all(0 <= s.pane_id < MAX_PANES_PER_TAB for s in panes)
    # 3. the active count of the tab equals the number of live processes
    if tab:
        assert tab.to_dict()["active_panes_count"] == len(live)
    # 4. closed sessions are gone from the tab and from the manager, and their process is not alive
    for sid in closed_ids:
        assert sid not in ids and mgr.get_session(sid) is None
    # every registered session is also known to the manager (no orphan in either direction)
    assert all(mgr.get_session(sid) is not None for sid in ids)
    assert set(mgr.sessions) == set(ids)


@pytest.mark.parametrize("seed", range(200))
def test_random_sequences_keep_every_process_supervised(env, seed):
    rng = random.Random(seed)
    closed: set = set()
    for _ in range(rng.randint(1, 20)):
        op = rng.choice(["open", "open", "close", "exit", "close_tab"])
        panes = _registered(env.mgr)
        if op == "open":
            before = {sid: s for sid, s in env.mgr.sessions.items()}
            try:
                created = _open(env, rng.randrange(MAX_PANES_PER_TAB))
            except TerminalManagerError:
                assert len(panes) == MAX_PANES_PER_TAB, "recusa com posição livre"
            else:
                assert created.session_id not in before
                assert all(env.mgr.get_session(sid) is s for sid, s in before.items()), "abrir mexeu em outra sessão"
        elif op == "close" and panes:
            victim = rng.choice(list(panes.values()))
            others = {s.session_id: s.is_alive for s in panes.values() if s is not victim}
            env.mgr.close_terminal(victim.session_id)
            closed.add(victim.session_id)
            assert not victim.is_alive
            # 5. closing one pane never ends nor unregisters another session
            for sid, was_alive in others.items():
                assert env.mgr.get_session(sid) is not None
                assert env.mgr.get_session(sid).is_alive == was_alive
        elif op == "exit":
            alive = [s for s in panes.values() if s.is_alive]
            if alive:
                rng.choice(alive).backend.exit(rng.choice([0, 1, 42]))
        elif op == "close_tab":
            registered = list(panes.values())
            env.mgr.close_tab(TAB)
            closed.update(s.session_id for s in registered)
            # closing the tab ends every session of it, the ended ones included
            assert not any(p.alive for p in SimulatedProcess.registry)
            assert env.mgr.sessions == {}
            env.mgr.create_tab(token=env.token, tab_id=TAB, title="Projeto", project_path=env.project)
        _check_invariants(env.mgr, closed)


# ---------------------------------------------------------------- Windows: real ConPTY and cmd.exe


def _pid_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _wait(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


@pytest.mark.skipif(sys.platform != "win32", reason="ConPTY is Windows-specific")
def test_real_cmd_exe_processes_match_the_registered_sessions():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        prof_mgr = DesktopProfileManager(root_dir=tmp_path / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
        token = authorizer.issue_session("default")
        project = tmp_path / "repo"
        project.mkdir()
        prof_mgr.authorize_project("default", project)
        mgr = TerminalManager(authorizer=authorizer)
        mgr.create_tab(token=token, tab_id=TAB, title="Projeto", project_path=project)
        every_pid: List[int] = []

        def open_cmd(pane_id: int):
            sess = mgr.create_terminal(token=token, tab_id=TAB, pane_id=pane_id)
            assert sess.pid
            every_pid.append(sess.pid)
            return sess

        try:
            # Scenario 1
            first, middle, third = (open_cmd(i) for i in range(3))
            assert mgr.close_terminal(middle.session_id)
            assert _wait(lambda: not _pid_alive(middle.pid))
            fourth = open_cmd(2)
            assert fourth.pane_id == 1
            # Scenario 2: exit in the 3rd
            third.write("exit\r\n")
            assert _wait(lambda: not third.is_alive)
            fifth = open_cmd(2)
            assert fifth.pane_id == 3 and _registered(mgr)[2] is third

            registered = list(_registered(mgr).values())
            alive_pids = {s.pid for s in registered if s.is_alive}
            assert alive_pids == {first.pid, fourth.pid, fifth.pid}
            assert {pid for pid in every_pid if _pid_alive(pid)} == alive_pids

            assert mgr.close_terminal(third.session_id)  # the ended one: nobody else is touched
            assert {pid for pid in every_pid if _pid_alive(pid)} == alive_pids
        finally:
            mgr.close_tab(TAB)
            assert _wait(lambda: not any(_pid_alive(pid) for pid in every_pid)), "cmd.exe vivo depois de fechar a aba"
