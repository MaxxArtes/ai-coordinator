"""Regression tests for the Windows mailbox pin bypass (candidate patch v0.6.1).

_PinnedDirectory.pin() returns without a handle when the directory is missing
at that instant. Before the fix, process_mailbox on Windows did not check the
result, so a mailbox directory renamed away between the existence checks and
pin() stayed unpinned. A junction swapped in after the last path validation
then received the response file outside the sandbox.

These tests drive the race deterministically through hooks around pin() and
validate_contained_path(), and always inspect the victim directory, also when
the cycle raises.
"""

from __future__ import annotations

import gc
import json
import os
from pathlib import Path

if os.name == "nt":
    import _winapi
else:
    _winapi = None  # type: ignore[assignment]

import pytest

from ai_coordinator import tool_broker as tb
from ai_coordinator.fs_sandbox import ReparseEscapeError
from ai_coordinator.storage import Store
from ai_coordinator.tool_broker import ToolDefinition, ToolPolicyMode, _PinnedDirectory

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows handle pinning and directory junctions")

REQUEST = "req_pin_race.json"
RESPONSE = "res_req_pin_race.json"


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    sandbox = base / "sandbox"
    outside = base / "outside_victim"
    project = base / "project"
    for folder in (sandbox / ".broker" / "requests", sandbox / ".broker" / "responses", outside, project):
        folder.mkdir(parents=True)
    store = Store(root=base / "coord", supervisor_key="test_sup_secret_key_v061")
    broker = store.tool_broker
    broker.registry.register(ToolDefinition(
        name="echo_tool", description="Echo payload",
        handler=lambda ctx, params: {"echo": params.get("msg")}, policy_mode=ToolPolicyMode.ALLOW,
    ))
    token = broker.token_manager.issue_token(
        task_id="task_v061_pin", run_id="run_v061_pin", worker_pid=os.getpid(),
        project_root=project, allowed_tools=["echo_tool"],
    )
    (sandbox / ".broker" / "requests" / REQUEST).write_text(json.dumps({
        "token": token, "tool": "echo_tool", "parameters": {"msg": "PIN_RACE"}, "caller_pid": os.getpid(),
    }), encoding="utf-8")

    hooks = {}
    original_pin = _PinnedDirectory.pin
    original_validate = tb.validate_contained_path

    def pin(self):
        before = hooks.get(("pre_pin", self.raw_path.name))
        if before:
            before()
        try:
            return original_pin(self)
        finally:
            after = hooks.get(("post_pin", self.raw_path.name))
            if after:
                after()

    def validate(path, root):
        result = original_validate(path, root)
        after = hooks.get(("post_validate", f"{Path(path).name}|{Path(root).name}"))
        if after:
            after()
        return result

    monkeypatch.setattr(_PinnedDirectory, "pin", pin)
    monkeypatch.setattr(tb, "validate_contained_path", validate)

    def outside_files():
        return [p for p in outside.rglob("*")]

    yield {"sandbox": sandbox, "outside": outside, "broker": broker, "hooks": hooks, "outside_files": outside_files}

    monkeypatch.undo()
    store.db.close()
    gc.collect()
    for root, dirs, _ in os.walk(base, topdown=True):
        for name in list(dirs):
            if os.path.isjunction(os.path.join(root, name)):
                os.rmdir(os.path.join(root, name))
                dirs.remove(name)


def _swap_responses_for_junction(sandbox, outside):
    res = sandbox / ".broker" / "responses"
    if res.exists() and not os.path.isjunction(res):
        os.rename(res, res.with_name("responses_parked"))
    _winapi.CreateJunction(str(outside), str(res))


def _arm_write_swap(mb):
    mb["hooks"][("post_validate", f"{RESPONSE}|responses")] = lambda: _swap_responses_for_junction(mb["sandbox"], mb["outside"])


def _process(mb, raise_on_fatal):
    return mb["broker"].process_mailbox(mb["sandbox"], expected_worker_pid=os.getpid(), raise_on_fatal=raise_on_fatal)


@pytest.mark.parametrize("raise_on_fatal", [True, False])
def test_responses_renamed_before_pin_fails_closed(mailbox, raise_on_fatal):
    res = mailbox["sandbox"] / ".broker" / "responses"
    away = res.with_name("responses_away")
    mailbox["hooks"][("pre_pin", "responses")] = lambda: os.rename(res, away)
    mailbox["hooks"][("post_pin", "responses")] = lambda: os.rename(away, res)
    _arm_write_swap(mailbox)
    try:
        if raise_on_fatal:
            with pytest.raises(ReparseEscapeError, match="nao pode ser fixado"):
                _process(mailbox, True)
        else:
            results = _process(mailbox, False)
            assert [r["error"] for r in results] == ["SymlinkSecurityViolation"]
    finally:
        assert mailbox["outside_files"]() == []
    assert not list(res.glob("res_*.json"))


def test_new_responses_dir_renamed_between_mkdir_and_pin_fails_closed(mailbox):
    res = mailbox["sandbox"] / ".broker" / "responses"
    res.rmdir()
    away = res.with_name("responses_away")
    mailbox["hooks"][("pre_pin", "responses")] = lambda: os.rename(res, away)
    mailbox["hooks"][("post_pin", "responses")] = lambda: os.rename(away, res)
    _arm_write_swap(mailbox)
    try:
        with pytest.raises(ReparseEscapeError, match="nao pode ser fixado"):
            _process(mailbox, True)
    finally:
        assert mailbox["outside_files"]() == []


@pytest.mark.parametrize("component", [".broker", "requests"])
def test_ancestor_renamed_before_pin_fails_closed(mailbox, component):
    broker_dir = mailbox["sandbox"] / ".broker"
    target = broker_dir if component == ".broker" else broker_dir / "requests"
    away = target.with_name(target.name + "_away")
    mailbox["hooks"][("pre_pin", component)] = lambda: os.rename(target, away)
    mailbox["hooks"][("post_pin", component)] = lambda: os.rename(away, target)
    _arm_write_swap(mailbox)
    try:
        with pytest.raises(ReparseEscapeError, match="nao pode ser fixado"):
            _process(mailbox, True)
    finally:
        assert mailbox["outside_files"]() == []


def test_failed_createfile_is_not_taken_as_a_pinned_handle(mailbox, monkeypatch):
    """responses vanishes after the attribute check and before CreateFileW inside pin().

    Without ctypes prototypes CreateFileW returned -1 on failure, which never matched
    INVALID_HANDLE_VALUE, so the cycle went on with a bogus handle and no pin.
    """
    res = mailbox["sandbox"] / ".broker" / "responses"
    away = res.with_name("responses_away")
    real_create = tb.kernel32.CreateFileW
    fired = []

    def create(path, *args):
        if not (str(path).endswith("responses") and not fired):
            return real_create(path, *args)
        fired.append(path)
        # The directory is gone only while CreateFileW runs; it is back before the
        # attribute re-check, as in the escape observed with a separate-process attacker.
        os.rename(res, away)
        try:
            return real_create(path, *args)
        finally:
            os.rename(away, res)

    monkeypatch.setattr(tb.kernel32, "CreateFileW", create)
    _arm_write_swap(mailbox)
    try:
        with pytest.raises(ReparseEscapeError):
            _process(mailbox, True)
    finally:
        assert mailbox["outside_files"]() == []
    assert fired


def test_junction_swapped_in_before_pin_is_rejected(mailbox):
    mailbox["hooks"][("pre_pin", "responses")] = lambda: _swap_responses_for_junction(mailbox["sandbox"], mailbox["outside"])
    try:
        with pytest.raises(ReparseEscapeError):
            _process(mailbox, True)
    finally:
        assert mailbox["outside_files"]() == []


def test_pinned_responses_cannot_be_swapped_after_validation(mailbox):
    """With the handle held, the swap attempted after validation is refused by the OS."""
    errors = []

    def attempt():
        try:
            _swap_responses_for_junction(mailbox["sandbox"], mailbox["outside"])
        except OSError as exc:
            errors.append(exc)

    mailbox["hooks"][("post_validate", f"{RESPONSE}|responses")] = attempt
    try:
        results = _process(mailbox, True)
    finally:
        assert mailbox["outside_files"]() == []
    assert [r["status"] for r in results] == ["success"]
    assert errors and all(isinstance(e, PermissionError) for e in errors)
    written = mailbox["sandbox"] / ".broker" / "responses" / RESPONSE
    assert json.loads(written.read_text(encoding="utf-8"))["response"]["result"]["echo"] == "PIN_RACE"


def test_unattacked_cycle_still_succeeds(mailbox):
    results = _process(mailbox, True)
    assert [r["status"] for r in results] == ["success"]
    assert (mailbox["sandbox"] / ".broker" / "responses" / RESPONSE).exists()
    assert mailbox["outside_files"]() == []
