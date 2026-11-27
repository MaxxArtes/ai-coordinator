"""Exercise the eight legacy tools and the separate read-only G0 contract over MCP."""
import asyncio

import pytest

pytest.importorskip("mcp")
from mcp.client import Client

from ai_coordinator.api import Coordinator
from ai_coordinator.dag import PlanNode
from ai_coordinator.gateway import GATEWAY_CONTRACTS, create_gateway_server
from ai_coordinator.mcp_server import create_server


LEGACY = {
    "bootstrap_project": "project_path",
    "list_active_tasks": "project_path",
    "create_task": "goal",
    "resume_task": "task_id",
    "save_checkpoint": "revision",
    "list_routes": None,
    "plan_worker": "category",
    "delegate_to_worker": "input_text",
}


def test_legacy_mcp_tool_contracts_and_errors(tmp_path, monkeypatch):
    root = tmp_path / "state"
    project = str(tmp_path)
    delegation = []

    def fake_delegate(self, task_id, category, input_text, **options):
        assert self.store.task(task_id)["project"] == project
        delegation.append((task_id, category, input_text, options))
        return {"status": "synthetic"}

    monkeypatch.setattr(Coordinator, "delegate", fake_delegate)

    async def verify():
        async with Client(create_server(root)) as client:
            tools = {item.name: item.input_schema for item in (await client.list_tools()).tools}
            assert tools.keys() == LEGACY.keys()
            for name, field in LEGACY.items():
                assert field is None or field in tools[name]["required"]
            args = {"project_path": project}
            async def ok(name, values=None):
                result = await client.call_tool(name, values)
                assert not result.is_error, (name, result.content)
                return result.structured_content

            assert (await ok("bootstrap_project", args))["active_tasks"] == []
            task_id = (await ok("create_task", {**args, "goal": "P9 fixture"}))["id"]
            assert (await ok("list_active_tasks", args))["result"][0]["id"] == task_id
            state = await ok("resume_task", {"task_id": task_id})
            revision = state["task"]["revision"]
            checkpoint = {"task_id": task_id, "revision": revision, "patch": {"summary": "P9 checkpoint"}}
            saved = await ok("save_checkpoint", checkpoint)
            assert saved["revision"] == revision + 1
            assert "code" in (await ok("list_routes"))
            assert await ok("plan_worker", {"category": "code"})
            delegated = {"task_id": task_id, "category": "code", "input_text": "synthetic offline fixture"}
            assert (await ok("delegate_to_worker", delegated))["status"] == "synthetic"
            for name, values in [
                ("bootstrap_project", {"project_path": str(tmp_path / "missing")}),
                ("list_active_tasks", {}),
                ("create_task", {**args, "goal": ""}),
                ("resume_task", {"task_id": "bad"}),
                ("save_checkpoint", checkpoint),
                ("plan_worker", {"category": "unknown"}),
                ("delegate_to_worker", {**delegated, "max_attempts": 99}),
            ]:
                assert (await client.call_tool(name, values)).is_error, name
            assert len(delegation) == 1

    asyncio.run(verify())


def test_p9_g0_discovery_scope_and_refusal_of_mutations(tmp_path):
    root = tmp_path / "state"
    foreign = tmp_path / "separate"
    foreign.mkdir()
    with Coordinator(root) as coordinator:
        anchor = coordinator.new_task(tmp_path, "P9 mission")["id"]
        other = coordinator.new_task(foreign, "Separate project")["id"]
        coordinator.store.register_mission(anchor, "Mission")
        coordinator.store.register_mission(other, "Separate")
        coordinator.store.create_plan_revision(anchor, [PlanNode("a", "Step A")], [], "P9 revision")
    db = root / "coordination.sqlite"
    before = db.read_bytes()

    async def verify():
        async with Client(create_gateway_server(tmp_path, root)) as client:
            tools = {item.name: item.input_schema for item in (await client.list_tools()).tools}
            assert tools.keys() == GATEWAY_CONTRACTS.keys()
            for name, required in GATEWAY_CONTRACTS.items():
                assert tools[name].get("required", []) == required
            for name, args in [
                ("capabilities", {}), ("version", {}), ("contracts", {}),
                ("missions_list", {}), ("missions_get", {"mission_id": anchor}),
                ("tasks_get", {"task_id": anchor}), ("dag_get", {"mission_id": anchor}),
                ("dag_ready", {"mission_id": anchor}),
            ]:
                result = await client.call_tool(name, args)
                assert not result.is_error, (name, result.content)
                if name == "dag_ready":
                    assert result.structured_content["result"] == ["a"]
            for name, args in [
                ("missions_get", {"mission_id": other}),
                ("tasks_get", {"task_id": other}),
                ("dag_get", {"mission_id": other}),
                ("dag_ready", {}),
                ("create_task", {"project_path": str(foreign), "goal": "Outside scope"}),
                ("save_checkpoint", {"task_id": other, "revision": 1, "patch": {}}),
                ("delegate_to_worker", {"task_id": other, "category": "code", "input_text": "blocked"}),
            ]:
                assert (await client.call_tool(name, args)).is_error, name

    asyncio.run(verify())
    assert db.read_bytes() == before
