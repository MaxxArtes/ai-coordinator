import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_coordinator import cli


@pytest.fixture
def dispatch(monkeypatch, tmp_path):
    task_id = "1210ff494600475c"
    task = {
        "id": task_id,
        "goal": "isolated fixture",
        "revision": 113,
        "snapshot": {},
        "ai_memory_workstream_id": None,
    }
    store = SimpleNamespace(root=tmp_path, task=lambda ident: task.copy())
    start = Mock(return_value=0)
    scope = Mock(return_value={"workspace": "lab", "project": "fixture"})
    monkeypatch.setattr(cli.subprocess, "call", start)
    monkeypatch.setattr(cli, "launcher", lambda *_: ["synthetic-codex"])
    monkeypatch.setattr(cli, "scope", scope)

    def launch(stored, requested):
        task["project"] = str(stored)
        return cli.launch(store, {}, "codex", str(requested), task_id)

    return launch, start, scope


@pytest.mark.skipif(os.name != "nt", reason="Windows path spellings")
def test_legacy_drive_case_and_forward_slashes_are_same_project(dispatch, tmp_path):
    project = tmp_path / "MixedCaseRepo"
    project.mkdir()
    launch, start, scope = dispatch
    assert launch(project, project.as_posix().lower() + "/.") == 0
    assert os.path.samefile(start.call_args.kwargs["cwd"], project)
    assert os.path.samefile(scope.call_args.args[0], project)
    assert start.call_args.kwargs["env"]["AI_COORDINATOR_TASK_ID"] == "1210ff494600475c"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_junction_can_open_its_own_task(dispatch, tmp_path):
    project = tmp_path / "Project"
    project.mkdir()
    alias = tmp_path / "alias"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(project)],
                   check=True, capture_output=True)
    launch, start, _ = dispatch
    assert launch(alias, project) == 0
    assert os.path.samefile(start.call_args.kwargs["cwd"], project)


def test_foreign_directory_with_copied_scope_is_still_denied(dispatch, tmp_path):
    own = tmp_path / "project"
    other = tmp_path / "project-other"
    for folder in (own, other):
        folder.mkdir()
        (folder / ".ai-memory.toml").write_text('workspace = "lab"\nproject = "fixture"\n')
    launch, start, scope = dispatch
    with pytest.raises(ValueError, match="Tarefa pertence a outro projeto"):
        launch(own, other)
    scope.assert_not_called()
    start.assert_not_called()


def test_removed_project_fails_closed(dispatch, tmp_path):
    project = tmp_path / "new-project"
    project.mkdir()
    launch, start, scope = dispatch
    with pytest.raises(ValueError, match="Tarefa pertence a outro projeto"):
        launch(tmp_path / "missing", project)
    scope.assert_not_called()
    start.assert_not_called()


def test_normalization_cannot_switch_directories(dispatch, tmp_path, monkeypatch):
    requested = tmp_path / "requested"
    other = tmp_path / "other"
    requested.mkdir()
    other.mkdir()
    monkeypatch.setattr(cli, "project_path", lambda _: str(other))
    launch, start, scope = dispatch
    with pytest.raises(ValueError, match="Normalizacao do projeto"):
        launch(requested, requested)
    scope.assert_not_called()
    start.assert_not_called()
