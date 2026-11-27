"""Mission read model, parity and authenticated Desktop access."""
from contextlib import closing
import json
from pathlib import Path
import random
import sqlite3
from types import SimpleNamespace

import pytest

from ai_coordinator.api import mission_reader
from ai_coordinator.cli import main
from ai_coordinator.dag import PlanEdge, PlanNode, PlanRevision, get_ready_nodes
from ai_coordinator.mission import mission_dag
from ai_coordinator.storage import Store
from desktop.auth.authorizer import SecurityAuthorizationError
from desktop.ipc.server import SecureIPCServer


@pytest.fixture
def mission_fixture(tmp_path):
    project = tmp_path / "project"
    foreign = tmp_path / "foreign"
    project.mkdir()
    foreign.mkdir()
    root = tmp_path / "store"
    store = Store(root)
    try:
        first = store.new(project, "Mission anchor")["id"]
        task = store.new(project, "Review result")["id"]
        other = store.new(foreign, "Foreign task")["id"]
        with pytest.raises(ValueError, match="pertencer"):
            store.register_mission(first, "Isolated mission", [task, other])
        assert mission_reader(project, root).list() == []
        store.register_mission(first, "Isolated mission", [task])
        store.register_mission(first, "Isolated mission", [task])
        store.register_mission(other, "Foreign mission")
        store.create_plan_revision(
            first,
            [PlanNode("start", "Start", status="completed"), PlanNode("finish", "Finish")],
            [PlanEdge("start", "finish")],
            "Reference",
        )
        yield store, root, project, foreign, first, task, other
    finally:
        store.db.close()


def test_read_model_matches_cli_and_reads_updated_nodes(mission_fixture, capsys, monkeypatch):
    store, root, project, foreign, anchor, task, other = mission_fixture
    reader = mission_reader(project, root)
    assert [item["id"] for item in reader.list()] == [anchor]
    detail = reader.get(anchor)
    assert {item["id"] for item in detail["tasks"]} == {anchor, task}
    dag = detail["dag"]
    assert dag["ready_node_ids"] == ["finish"]
    assert dag["node_order"] == ["start", "finish"]
    monkeypatch.setattr("sys.argv", ["ai-coordinator", "--root", str(root), "dag-revision", "--task", anchor, "--show"])
    assert main() == 0
    reference = json.loads(capsys.readouterr().out)
    assert {key: dag[key] for key in reference} == reference
    monkeypatch.setattr("sys.argv", ["ai-coordinator", "--root", str(root), "mission", "--project", str(project), "--id", anchor])
    assert main() == 0
    assert json.loads(capsys.readouterr().out) == detail
    store.update_node_status(anchor, 1, "finish", "completed")
    assert reader.get(anchor)["dag"]["ready_node_ids"] == []
    for read in (reader.get, reader.task):
        with pytest.raises(ValueError, match="encontrada"):
            read(other)
    assert mission_reader(foreign, root).get(other)["dag"] is None


def test_readers_do_not_initialize_missing_or_legacy_store(tmp_path):
    root = tmp_path / "coordinator"
    assert mission_reader(tmp_path, root).list() == []
    assert not root.exists()
    root.mkdir()
    database = root / "coordination.sqlite"
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE old_state (id TEXT)")
    before = database.read_bytes()
    assert mission_reader(tmp_path, root).list() == []
    assert database.read_bytes() == before


def test_advisory_ready_parity_on_1000_random_dags():
    rng = random.Random(417531)
    for trial in range(1000):
        order = list(range(rng.randint(1, 24)))
        rng.shuffle(order)
        nodes = {
            str(node): PlanNode(str(node), f"Node {node}", status=rng.choice(
                ["pending", "pending", "ready", "completed", "in_progress", "blocked", "skipped"]
            ))
            for node in order
        }
        edges = [
            PlanEdge(str(source), str(target))
            for i, source in enumerate(order)
            for target in order[i + 1:] if rng.random() < 0.19
        ]
        revision = PlanRevision(f"{trial:016x}", 1, None, "Random graph", nodes=nodes, edges=edges)
        advisory = mission_dag(revision)
        expected = sorted(
            node_id for node_id, node in nodes.items() if node.status == "pending"
            and all(nodes[edge.from_node].status == "completed" for edge in edges if edge.to_node == node_id)
        )
        assert advisory["ready_node_ids"] == get_ready_nodes(nodes, edges) == expected
        indexes = {node: position for position, node in enumerate(advisory["node_order"])}
        assert all(indexes[edge.from_node] < indexes[edge.to_node] for edge in edges)


def test_authenticated_desktop_reads_only_authorized_project(mission_fixture, monkeypatch):
    _, root, project, foreign, anchor, _, other = mission_fixture

    def authorize(token, target):
        if token != "session-token" or Path(target) != project:
            raise SecurityAuthorizationError("denied")

    monkeypatch.setenv("AI_COORDINATOR_HOME", str(root))
    manager = SimpleNamespace(authorizer=SimpleNamespace(authorize_project_access=authorize))
    server = SecureIPCServer(SimpleNamespace(log_diagnostic=lambda message: None), "session-token", manager)

    def call(method, params, token="session-token"):
        return server.handle_raw_payload(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params, "session_token": token,
        }))

    args = {"project_path": str(project)}
    assert call("mission.list", args).result[0]["id"] == anchor
    assert call("mission.get", {**args, "mission_id": anchor}).result["dag"]["ready_node_ids"] == ["finish"]
    assert call("mission.get", {**args, "mission_id": other}).error is not None
    assert call("mission.get", {**args, "project_path": str(foreign), "mission_id": other}).error is not None
    assert call("mission.list", args, token="invalid").error is not None
    assert call("mission.update", args).error is not None
