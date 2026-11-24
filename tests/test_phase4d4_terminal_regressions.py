"""Fase 4D.4, rodada consolidada r2, bloco 4: regressões dos terminais no Core do desktop (manager, session, IPC).

A lista nominal das 7 regressões não ficou registrada pelo agente anterior; esta é a reconstrução a partir dos
checkpoints. O lado da interface está em desktop/src/App.views.test.tsx (bloco "regressões dos terminais"),
desktop/src/components/TabContextMenu.test.tsx e useTerminalStates.test.tsx. Testes que já existiam ficam citados e
não foram repetidos.

- R1. "Abrir projeto" falhava com "Terminal operation failed" porque o Core já tinha "tab-main" (Tab with ID 'tab-main'
  already exists): a interface usa a próxima tab-N livre. Já existia (diag.3): conflito estruturado tab_exists e
  criação idempotente com o mesmo diretório, em test_phase4d3_terminal_interpreters::
  test_open_initial_project_then_second_project_preserves_both_sessions_and_cwds. Aqui: vários ids ocupados, a mesma
  pasta escrita de outro jeito, e o conflito não altera a aba existente.
- R2. Abrir um segundo projeto não descarta a sessão anterior. Já existia (diag.3), com dois projetos. Aqui: três
  projetos, cada sessão no seu diretório e na sua aba.
- R3. Supervisão de todas as abas: terminal.list_tabs informa estado e código de saída das sessões de todas as abas.
- R4. Ids de aba sincronizados, fechar um terminal não afeta os outros, identidade estável por sessão, entre abas
  (test_phase4d3_terminal_identity cobre uma aba só).
- R5. PATH e diretório preservados por sessão ao abrir projetos em sequência e ao trocar o interpretador (CMD,
  PowerShell, Git Bash). Já existia (diag.3): perfil isolado com PATH e cwd lógico de uma sessão WSL.
- R6. Menu de contexto da aba: só interface (TabContextMenu.test.tsx).
- R7. Projeto digitado: caminho com espaços (também no Bash WSL), caminho inexistente com mensagem clara (também
  quando um projeto autorizado some do disco: corrigido nesta rodada, antes saía "Terminal operation failed") e o
  histórico de cada terminal preservado ao abrir e fechar outros. Já existia: path_missing na autorização
  (test_phase4d3_etapa7_ipc::test_authorize_missing_or_invalid_directory_is_structured_and_does_not_echo_the_path).

Nenhum processo real é iniciado: o backend de PTY é substituído por um processo simulado que registra o comando, o
diretório e o ambiente recebidos e falha como os backends reais quando o diretório não existe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.ipc.server import PROJECT_DIR_MISSING_MESSAGE, SecureIPCServer
from desktop.terminal import manager as manager_module
from desktop.terminal import session as session_module
from desktop.terminal.interpreters import Interpreter, resolve_interpreter
from desktop.terminal.manager import TerminalManager
from desktop.terminal.pty_backend import BasePtyBackend

INTERPRETERS = [
    Interpreter("cmd", "CMD", r"C:\Windows\System32\cmd.exe"),
    Interpreter("powershell", "PowerShell 7", r"C:\Program Files\PowerShell\7\pwsh.exe", ("-NoLogo",)),
    Interpreter("bash_git", "Bash (Git Bash)", r"C:\Program Files\Git\bin\bash.exe", ("--login", "-i")),
]
PATH_MARKER = "r2-bloco4-cli-bin"


class FakeShell(BasePtyBackend):
    """A shell stand-in: records what the real backend would receive and fails like it on a missing directory."""

    spawned: List["FakeShell"] = []
    missing_dir_error: Exception = RuntimeError("CreateProcessW failed with Win32 error code 267")

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        super().__init__(cols, rows)
        self.alive = False
        self.code: Optional[int] = None
        self.cmd: List[str] = []
        self.cwd: Optional[Path] = None
        self.env: dict = {}

    def spawn(self, cmd, cwd, env=None, max_memory_mb=256):
        if not Path(cwd).is_dir():
            raise FakeShell.missing_dir_error
        self.cmd, self.cwd, self.env = list(cmd), Path(cwd), dict(env or {})
        self.alive = True
        FakeShell.spawned.append(self)
        return SimpleNamespace(pid=None)

    def emit(self, text: str) -> None:
        assert self._on_output_callback is not None
        self._on_output_callback(text)

    def exit(self, code: int) -> None:
        self.alive = False
        self.code = code

    def write(self, data):
        return len(data)

    def resize(self, cols, rows):
        return True

    def is_alive(self):
        return self.alive

    def get_exit_code(self):
        return self.code

    def terminate(self):
        if self.alive:
            self.code = -1
        self.alive = False
        return True


@pytest.fixture
def core(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for name in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("AI_COORDINATOR_HOME", str(tmp_path / "core-home"))
    monkeypatch.delenv("AI_COORDINATOR_ROLE", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / PATH_MARKER), os.environ.get("PATH", "")]))
    monkeypatch.setenv("COORDINATOR_IPC_TOKEN", "nunca-no-terminal")
    monkeypatch.delenv("CHERE_INVOKING", raising=False)
    monkeypatch.setattr(manager_module.sys, "platform", "win32")
    monkeypatch.setattr(manager_module, "discover_interpreters", lambda: list(INTERPRETERS))
    monkeypatch.setattr(session_module, "create_pty_backend", lambda cols=80, rows=24: FakeShell(cols, rows))
    FakeShell.spawned = []

    profiles = DesktopProfileManager(root_dir=tmp_path / "profiles")
    authorizer = DesktopAuthorizer(profile_manager=profiles)
    token = authorizer.issue_session("default")
    manager = TerminalManager(authorizer)
    server = SecureIPCServer(transport=MagicMock(), terminal_manager=manager, session_token=token)
    projects = {}
    for key, name in (("home", "10864"), ("repo", "ai-coordinator"), ("proj", "proj")):
        projects[key] = tmp_path / name
        projects[key].mkdir()
        profiles.authorize_project("default", projects[key])
    env = SimpleNamespace(server=server, manager=manager, token=token, profiles=profiles, tmp=tmp_path, seq=0, **projects)
    yield env
    manager.terminate_all()


def rpc(env, method: str, params: Optional[dict] = None):
    env.seq += 1
    payload = {"jsonrpc": "2.0", "id": env.seq, "method": method, "session_token": env.token, "params": params or {}}
    return env.server.handle_raw_payload(json.dumps(payload))


def ok(response) -> dict:
    assert response.error is None, response.error
    return response.result


def open_tab(env, tab_id: str, project: Path) -> dict:
    return ok(rpc(env, "terminal.create_tab", {"tab_id": tab_id, "project_path": str(project)}))


def open_session(env, tab_id: str, pane_id: int, cwd: Path, interpreter_id: Optional[str] = None) -> dict:
    params = {"tab_id": tab_id, "pane_id": pane_id, "kind": "human_terminal", "cwd": str(cwd)}
    if interpreter_id:
        params["interpreter_id"] = interpreter_id
    return ok(rpc(env, "terminal.create_session", params))


def tabs_by_id(env) -> dict:
    return {tab["tab_id"]: tab for tab in ok(rpc(env, "terminal.list_tabs"))["tabs"]}


def backend_of(env, session_id: str) -> FakeShell:
    return env.manager.get_session(session_id).backend


# ---- R1 ----

def test_r1_every_taken_tab_id_answers_tab_exists_without_touching_it_and_the_next_free_id_opens(core):
    open_tab(core, "tab-main", core.home)
    open_tab(core, "tab-2", core.proj)
    first = open_session(core, "tab-main", 0, core.home)

    for taken in ("tab-main", "tab-2"):
        response = rpc(core, "terminal.create_tab", {"tab_id": taken, "project_path": str(core.repo)})
        assert response.error["message"] == "Terminal operation failed: tab_exists"
        assert response.error["data"] == {"field": "tab_id", "reason": "tab_exists"}
        assert str(core.repo) not in json.dumps(response.error)
    assert open_tab(core, "tab-3", core.repo)["project_path"] == str(core.repo.resolve())

    tabs = tabs_by_id(core)
    assert {tab_id: tab["project_path"] for tab_id, tab in tabs.items()} == {
        "tab-main": str(core.home.resolve()),
        "tab-2": str(core.proj.resolve()),
        "tab-3": str(core.repo.resolve()),
    }
    assert tabs["tab-main"]["panes"]["0"]["session_id"] == first["session_id"]
    assert tabs["tab-main"]["panes"]["0"]["is_alive"] is True


def test_r1_reopening_the_same_directory_spelled_another_way_reuses_the_tab_and_its_session(core):
    open_tab(core, "tab-main", core.home)
    first = open_session(core, "tab-main", 0, core.home)

    again = open_tab(core, "tab-main", Path(str(core.home) + os.sep))
    assert again["panes_count"] == 1
    assert again["panes"]["0"]["session_id"] == first["session_id"]
    # A refreshed page asks for slot 0 again: the Core keeps the old session there and reports the slot it used.
    second = open_session(core, "tab-main", 0, core.home)
    assert second["pane_id"] == 1
    assert core.manager.get_session(first["session_id"]).is_alive


# ---- R2 / R3 ----

def test_r2_r3_three_projects_in_sequence_stay_alive_in_their_own_tabs_and_list_tabs_reports_every_tab(core):
    opened = []
    for tab_id, pane_id, project in (("tab-main", 0, core.home), ("tab-2", 1, core.proj), ("tab-3", 2, core.repo)):
        open_tab(core, tab_id, project)
        opened.append((tab_id, pane_id, project, open_session(core, tab_id, pane_id, project)))

    backend_of(core, opened[0][3]["session_id"]).exit(0)  # `exit` in the first project, not the last one opened

    tabs = tabs_by_id(core)
    for tab_id, pane_id, project, session in opened:
        pane = tabs[tab_id]["panes"][str(pane_id)]
        assert pane["session_id"] == session["session_id"]
        assert pane["cwd"] == str(project.resolve())
    first, second, third = (tabs[t]["panes"][str(p)] for t, p, _, _ in opened)
    assert (first["is_alive"], first["exit_code"]) == (False, 0)
    assert second["is_alive"] is True and third["is_alive"] is True
    listed = ok(rpc(core, "terminal.list_tabs"))
    assert (listed["panes_total"], listed["active_sessions_total"]) == (3, 2)


# ---- R4 ----

def test_r4_closing_one_terminal_never_touches_the_terminals_of_other_tabs_and_a_stale_id_stays_dead(core):
    open_tab(core, "tab-main", core.home)
    open_tab(core, "tab-2", core.proj)
    main0 = open_session(core, "tab-main", 0, core.home)
    main1 = open_session(core, "tab-main", 1, core.home)
    # The interface numbers slots across the window: tab-2 gets slot 2, the same number tab-main could use later.
    other = open_session(core, "tab-2", 2, core.proj)
    assert other["pane_id"] == 2

    assert ok(rpc(core, "terminal.close_session", {"session_id": main1["session_id"]})) == {"closed": True}
    tabs = tabs_by_id(core)
    assert set(tabs["tab-main"]["panes"]) == {"0"}
    assert tabs["tab-2"]["panes"]["2"]["session_id"] == other["session_id"]
    assert backend_of(core, main0["session_id"]).alive and backend_of(core, other["session_id"]).alive

    # Reopened in the same slot: a new identity; the old id neither closes nor reaches the new terminal.
    reopened = open_session(core, "tab-main", 1, core.home)
    assert reopened["pane_id"] == 1 and reopened["session_id"] != main1["session_id"]
    assert ok(rpc(core, "terminal.close_session", {"session_id": main1["session_id"]})) == {"closed": False}
    stale_write = rpc(core, "terminal.write", {"session_id": main1["session_id"], "data": "dir\r"})
    assert stale_write.error is not None
    tabs = tabs_by_id(core)
    assert tabs["tab-main"]["panes"]["1"]["session_id"] == reopened["session_id"]
    assert tabs["tab-2"]["panes"]["2"]["session_id"] == other["session_id"]

    # The tab of a project left without terminals stays bound, so reopening it needs no new tab id.
    assert ok(rpc(core, "terminal.close_session", {"session_id": other["session_id"]})) == {"closed": True}
    assert tabs_by_id(core)["tab-2"]["panes"] == {}
    back = open_session(core, "tab-2", 2, core.proj)
    assert back["tab_id"] == "tab-2" and back["cwd"] == str(core.proj.resolve())
    assert sorted(core.manager.sessions) == sorted([main0["session_id"], reopened["session_id"], back["session_id"]])


# ---- R5 ----

def test_r5_each_session_keeps_its_directory_and_path_across_projects_and_interpreter_switches(core):
    open_tab(core, "tab-main", core.home)
    open_tab(core, "tab-2", core.repo)
    home_ps = open_session(core, "tab-main", 0, core.home, "powershell")
    repo_bash = open_session(core, "tab-2", 1, core.repo, "bash_git")

    # Switching the first terminal to CMD, as the interface does: validate, close, reopen in the same tab, slot and cwd.
    assert ok(rpc(core, "terminal.validate_interpreter", {"interpreter_id": "cmd", "cwd": str(core.home)}))["id"] == "cmd"
    assert ok(rpc(core, "terminal.close_session", {"session_id": home_ps["session_id"]}))["closed"] is True
    home_cmd = open_session(core, "tab-main", 0, core.home, "cmd")
    # And the second one back to PowerShell.
    assert ok(rpc(core, "terminal.close_session", {"session_id": repo_bash["session_id"]}))["closed"] is True
    repo_ps = open_session(core, "tab-2", 1, core.repo, "powershell")

    expected = [
        (home_ps, core.home, INTERPRETERS[1]),
        (repo_bash, core.repo, INTERPRETERS[2]),
        (home_cmd, core.home, INTERPRETERS[0]),
        (repo_ps, core.repo, INTERPRETERS[1]),
    ]
    assert len(FakeShell.spawned) == len(expected)
    for (session, project, interpreter), shell in zip(expected, FakeShell.spawned):
        assert session["cwd"] == str(project.resolve())
        assert session["interpreter_id"] == interpreter.id
        assert shell.cwd == project.resolve()
        assert shell.cmd == [interpreter.executable, *interpreter.arguments]
        assert shell.env["PATH"] == os.environ["PATH"]
        assert shell.env["PATH"].split(os.pathsep)[0].endswith(PATH_MARKER)
        assert "COORDINATOR_IPC_TOKEN" not in shell.env
        assert ("CHERE_INVOKING" in shell.env) is (interpreter.id == "bash_git")
    assert "CHERE_INVOKING" not in os.environ  # the Git Bash flag never leaks into the Core or the next terminal

    tabs = tabs_by_id(core)
    assert tabs["tab-main"]["panes"]["0"]["session_id"] == home_cmd["session_id"]
    assert tabs["tab-main"]["panes"]["0"]["cwd"] == str(core.home.resolve())
    assert tabs["tab-2"]["panes"]["1"]["session_id"] == repo_ps["session_id"]
    assert tabs["tab-2"]["panes"]["1"]["cwd"] == str(core.repo.resolve())


# ---- R7 ----

def test_r7_typed_directory_with_spaces_is_authorized_and_used_as_is(core):
    spaced = core.tmp / "Meus Projetos" / "app com espaço"
    spaced.mkdir(parents=True)

    authorized = ok(rpc(core, "profile.authorize_project", {"project_path": f"  {spaced}  "}))
    assert authorized["project_path"] == str(spaced.resolve())
    open_tab(core, "tab-2", spaced)
    session = open_session(core, "tab-2", 1, spaced, "bash_git")
    assert session["cwd"] == str(spaced.resolve())
    assert FakeShell.spawned[-1].cwd == spaced.resolve()


def test_r7_wsl_keeps_a_directory_with_spaces_as_a_single_argument():
    wsl = Interpreter("bash_wsl", "Bash (WSL)", r"C:\Windows\System32\wsl.exe", cwd_kind="wsl")
    _, command, _, _ = resolve_interpreter("bash_wsl", r"D:\Meus Projetos\app", [wsl])
    assert command[1:3] == ["--cd", "/mnt/d/Meus Projetos/app"]
    assert '"/mnt/d/Meus Projetos/app"' in subprocess.list2cmdline(command)


@pytest.mark.parametrize(
    "spawn_error",
    [
        RuntimeError("CreateProcessW failed with Win32 error code 267"),  # ConPTY
        FileNotFoundError(2, "No such file or directory"),  # pipe backend (subprocess.Popen)
    ],
    ids=["conpty", "pipe"],
)
def test_r7_authorized_project_missing_from_disk_answers_a_clear_path_missing_without_the_path(core, spawn_error, monkeypatch):
    monkeypatch.setattr(FakeShell, "missing_dir_error", spawn_error)
    gone = core.tmp / "projeto-que-sumiu"
    gone.mkdir()
    core.profiles.authorize_project("default", gone)
    open_tab(core, "tab-main", core.home)
    alive = open_session(core, "tab-main", 0, core.home)
    open_tab(core, "tab-2", gone)
    gone.rmdir()

    for params in (
        {"tab_id": "tab-2", "pane_id": 1, "kind": "human_terminal", "cwd": str(gone)},
        {"tab_id": "tab-2", "pane_id": 1, "kind": "human_terminal"},  # the tab's own directory
    ):
        response = rpc(core, "terminal.create_session", params)
        assert response.error["code"] == -32602
        assert response.error["message"] == PROJECT_DIR_MISSING_MESSAGE
        assert response.error["data"] == {"field": "cwd", "reason": "path_missing"}
        assert "projeto-que-sumiu" not in json.dumps(response.error)

    assert tabs_by_id(core)["tab-2"]["panes"] == {}
    assert sorted(core.manager.sessions) == [alive["session_id"]]
    assert backend_of(core, alive["session_id"]).alive


def test_r7_a_spawn_failure_in_an_existing_directory_is_still_reported_as_start_failed(core, monkeypatch):
    open_tab(core, "tab-main", core.home)

    def refuse(self, cmd, cwd, env=None, max_memory_mb=256):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(FakeShell, "spawn", refuse)
    response = rpc(core, "terminal.create_session", {"tab_id": "tab-main", "pane_id": 0, "kind": "human_terminal"})
    assert response.error["message"] == "Terminal operation failed"
    assert response.error["data"]["reason"] == "start_failed"


def test_r7_each_terminal_keeps_its_own_history_while_other_projects_open_and_close(core):
    open_tab(core, "tab-main", core.home)
    open_tab(core, "tab-2", core.repo)
    first = open_session(core, "tab-main", 0, core.home)
    backend_of(core, first["session_id"]).emit("historico do primeiro\r\n")
    second = open_session(core, "tab-2", 1, core.repo)
    backend_of(core, second["session_id"]).emit("historico do segundo\r\n")

    def output(session_id: str, since: int = 0) -> dict:
        return ok(rpc(core, "terminal.get_output", {"session_id": session_id, "since_seq": since}))

    seen = output(first["session_id"])
    assert seen["raw"] == "historico do primeiro\r\n"

    third = open_session(core, "tab-main", 2, core.home)
    backend_of(core, third["session_id"]).emit("historico do terceiro\r\n")
    assert ok(rpc(core, "terminal.close_session", {"session_id": second["session_id"]}))["closed"] is True
    backend_of(core, first["session_id"]).emit("depois\r\n")

    assert output(first["session_id"])["raw"] == "historico do primeiro\r\ndepois\r\n"
    assert output(first["session_id"], seen["next_seq"])["raw"] == "depois\r\n"
    assert output(third["session_id"])["raw"] == "historico do terceiro\r\n"
    assert rpc(core, "terminal.get_output", {"session_id": second["session_id"], "since_seq": 0}).error is not None
