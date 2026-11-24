"""Phase 4D.4 r2 - AI agents behind wrappers (node, bun, python, cmd, PowerShell) are identified, not "Filhos genéricos".

- `detect_agent_kind(name, cmdline)` against a table of real-shaped command lines, positive and negative.
- The command line is read once per `(pid, create_time_ns)`, again after PID reuse, never for an injected process list,
  and an unreadable command line leaves the kind unknown without raising.
- The Rust host (desktop/src-tauri/src/main.rs) declares the same tables as the Core and reads `cmd` only for wrappers.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from desktop.ipc import collector as collector_module
from desktop.ipc.collector import (
    ProcessTreeCollector,
    RawProcessSample,
    agent_needs_cmdline,
    detect_agent_kind,
    read_process_cmdline,
)

ROOT = Path(__file__).resolve().parents[1]
MAIN_RS = ROOT / "desktop" / "src-tauri" / "src" / "main.rs"

NPM = "C:\\Users\\u\\AppData\\Roaming\\npm"

POSITIVE_CASES: List[Tuple[str, Optional[Sequence[str]], str]] = [
    ("claude.exe", None, "claude"),
    ("codex-code-mode-host.exe", None, "codex"),
    ("opencode-cli.exe", None, "opencode"),
    ("Antigravity.exe", None, "agy"),
    ("gemini", None, "gemini"),
    ("aider.exe", None, "aider"),
    ("ai-coordinator.exe", None, "ai-coordinator"),
    ("node.exe", ["node.exe", f"{NPM}\\node_modules\\@anthropic-ai\\claude-code\\cli.js", "--resume"], "claude"),
    ("node", ["/usr/bin/node", "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"], "claude"),
    (
        "node.exe",
        ["node", "--no-warnings", "--max-old-space-size=8192", f"{NPM}\\node_modules\\@openai\\codex\\bin\\codex.js", "exec"],
        "codex",
    ),
    ("NODE.EXE", ["NODE.EXE", "D:\\npm-global\\node_modules\\opencode-ai\\bin\\opencode", "run"], "opencode"),
    (
        "node.exe",
        ["node", "-r", "C:\\hooks\\preload.js", f"{NPM}\\node_modules\\@google\\gemini-cli\\dist\\index.js"],
        "gemini",
    ),
    ("node.exe", ["node", "C:\\tools\\agy.js"], "agy"),
    ("node", ["node", "/home/u/.npm/_npx/1a2b/node_modules/@anthropic-ai/claude-code/cli.js"], "claude"),
    ("bun.exe", ["bun", "x", "@anthropic-ai/claude-code@latest"], "claude"),
    ("bunx", ["bunx", "@openai/codex"], "codex"),
    ("bun", ["bun", "run", "/home/u/.bun/install/global/node_modules/@google/gemini-cli/dist/index.js"], "gemini"),
    ("python.exe", ["python.exe", "-m", "aider", "--model", "x"], "aider"),
    ("python3.12", ["python3.12", "-X", "utf8", "-m", "ai_coordinator.cli", "plan"], "ai-coordinator"),
    ("py.exe", ["py", "-3.12", "-m", "aider"], "aider"),
    ("pythonw.exe", ["pythonw.exe", "-maider"], "aider"),
    ("python.exe", ["python.exe", "C:\\venv\\Scripts\\ai-coordinator.exe", "plan"], "ai-coordinator"),
    ("python3", ["python3", "/home/u/.local/bin/aider"], "aider"),
    # cmd.exe /d /s /c ""...\claude.cmd" --version", as CommandLineToArgvW splits it (sysinfo uses the same API).
    ("cmd.exe", ["C:\\WINDOWS\\system32\\cmd.exe", "/d", "/s", "/c", f"{NPM}\\claude.cmd --version"], "claude"),
    ("cmd.exe", ["cmd.exe", "/c", "codex", "exec", "--json"], "codex"),
    ("CMD.EXE", ["CMD.EXE", "/C", "\"C:\\Program Files\\nodejs\\gemini.cmd\" -p oi"], "gemini"),
    (
        "pwsh.exe",
        ["pwsh.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", f"{NPM}\\claude.ps1", "-p", "oi"],
        "claude",
    ),
    ("powershell.exe", ["powershell.exe", "-NoLogo", "-Command", "& 'C:\\npm\\opencode.ps1' run"], "opencode"),
    ("pwsh", ["pwsh", "-c", "codex", "exec"], "codex"),
]

NEGATIVE_CASES: List[Tuple[str, Optional[Sequence[str]]]] = [
    ("", None),
    ("node.exe", None),
    ("node.exe", ["node.exe"]),
    ("node.exe", ["node", ""]),
    ("node.exe", ["node", "D:\\notes\\claude-notes.txt"]),
    ("node.exe", ["node", "server.js", "--port", "3000"]),
    ("node.exe", ["node", "prettier.js", "--write", "claude.js"]),
    ("node.exe", ["node", "-e", "require('@anthropic-ai/claude-code')"]),
    ("node.exe", ["node", "D:\\projects\\opencode-ai\\build.js"]),
    ("node.exe", ["node", "C:\\x\\node_modules\\@openai\\codex-sdk\\dist\\index.js"]),
    ("node.exe", ["node", "C:\\x\\node_modules\\claude-code-router\\cli.js"]),
    ("node.exe", ["node", "claude_helper.js"]),
    ("node", ["node", "run", "claude.js"]),
    ("python.exe", ["python.exe", "gemini_web.py", "--port", "8765"]),
    ("python.exe", ["python.exe", "-m", "pip", "install", "aider-chat"]),
    ("python.exe", ["python.exe", "-c", "import aider"]),
    ("python.exe", ["python.exe", "D:\\docs\\claude.md"]),
    ("python.exe", ["python.exe", "-I", "D:\\app\\desktop\\ipc\\server_main.py"]),
    ("python.exe", ["python.exe", "-m"]),
    ("cmd.exe", ["cmd.exe"]),
    ("cmd.exe", ["cmd.exe", "/c", "dir", "claude"]),
    ("cmd.exe", ["cmd.exe", "/c", "type", "claude.cmd"]),
    ("cmd.exe", ["cmd.exe", "/c", "echo", "C:\\npm\\claude.cmd"]),
    ("pwsh.exe", ["pwsh.exe", "-NoLogo"]),
    ("pwsh.exe", ["pwsh.exe", "-File"]),
    ("pwsh.exe", ["pwsh.exe", "-File", "D:\\scripts\\build.ps1", "claude"]),
    ("pwsh.exe", ["pwsh.exe", "-Command", "Get-Content claude.md"]),
    ("Code.exe", ["Code.exe", "claude.js"]),
    ("notepad.exe", ["notepad.exe", f"{NPM}\\claude.cmd"]),
    ("vim", ["vim", "codex.js"]),
    ("git.exe", ["git.exe", "commit", "-m", "claude"]),
    ("ai-coordinator-desktop.exe", None),
    ("claude-notes.txt", None),
    ("python-config", ["python-config", "aider"]),
]


@pytest.mark.parametrize(("name", "cmdline", "expected"), POSITIVE_CASES)
def test_agents_behind_wrappers_are_identified(name: str, cmdline: Optional[Sequence[str]], expected: str) -> None:
    assert detect_agent_kind(name, cmdline) == expected


@pytest.mark.parametrize(("name", "cmdline"), NEGATIVE_CASES)
def test_no_obvious_false_positives(name: str, cmdline: Optional[Sequence[str]]) -> None:
    assert detect_agent_kind(name, cmdline) is None


def test_only_wrappers_need_a_command_line() -> None:
    for name in ("node.exe", "NODE", "bun", "bunx.exe", "python3.12", "pythonw.exe", "py.exe", "cmd.exe", "pwsh.exe",
                 "powershell.exe"):
        assert agent_needs_cmdline(name), name
    for name in ("claude.exe", "codex", "git.exe", "Code.exe", "notepad.exe", "python-config", "pythonista", ""):
        assert not agent_needs_cmdline(name), name


class _Reader:
    def __init__(self, table: Dict[int, Optional[Sequence[str]]], error: Optional[BaseException] = None) -> None:
        self.table = table
        self.error = error
        self.calls: List[int] = []

    def __call__(self, pid: int) -> Optional[Sequence[str]]:
        self.calls.append(pid)
        if self.error is not None:
            raise self.error
        return self.table.get(pid)


def _collector(samples: List[RawProcessSample], reader: Optional[_Reader], clock: List[int]) -> ProcessTreeCollector:
    return ProcessTreeCollector(
        core_pid=200,
        process_lister=lambda _roots: list(samples),
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 2,
        system_sampler=lambda _e, _c: None,
        cmdline_reader=reader,
    )


def _tree(*extra: RawProcessSample) -> List[RawProcessSample]:
    return [
        RawProcessSample(100, 1, "ai-coordinator-desktop.exe", 10, 40_000_000, 1.0, private_ram_bytes=30_000_000),
        RawProcessSample(200, 100, "python.exe", 20, 30_000_000, 0.5, private_ram_bytes=20_000_000),
        RawProcessSample(300, 200, "pwsh.exe", 30, 25_000_000, 0.3, private_ram_bytes=18_000_000),
        *extra,
    ]


def test_wrapped_agent_is_counted_as_agent_and_read_once_per_identity() -> None:
    clock = [1_000_000_000]
    node = RawProcessSample(400, 300, "node.exe", 40, 150_000_000, 2.0, private_ram_bytes=120_000_000)
    git = RawProcessSample(500, 300, "git.exe", 50, 10_000_000, 0.1, private_ram_bytes=7_000_000)
    samples = _tree(node, git)
    reader = _Reader({
        300: ["pwsh.exe", "-NoLogo"],
        400: ["node.exe", f"{NPM}\\node_modules\\@anthropic-ai\\claude-code\\cli.js"],
    })
    col = _collector(samples, reader, clock)

    snap = col.collect(host_pid=100, core_pid=200, terminal_pids={300: 30})
    by_pid = {n.pid: n for n in snap.nodes}
    assert by_pid[300].role == "terminal" and by_pid[300].agent_kind is None
    assert by_pid[400].role == "agent" and by_pid[400].agent_kind == "claude"
    assert by_pid[500].role == "child"
    assert snap.categories["agent"].process_count == 1
    assert snap.categories["agent"].private_ram_bytes == 120_000_000
    # Host and Core are never inspected; git.exe is not a wrapper.
    assert sorted(reader.calls) == [300, 400]

    for _ in range(3):
        clock[0] += 1_000_000_000
        again = col.collect(host_pid=100, core_pid=200, terminal_pids={300: 30})
        assert {n.pid: n.agent_kind for n in again.nodes}[400] == "claude"
    assert sorted(reader.calls) == [300, 400], "command lines must not be re-read on every poll"


def test_pid_reuse_reads_the_new_process_command_line() -> None:
    clock = [1_000_000_000]
    samples = _tree(RawProcessSample(400, 300, "node.exe", 40, 150_000_000, 2.0))
    reader = _Reader({400: ["node.exe", "C:\\x\\node_modules\\@openai\\codex\\bin\\codex.js"]})
    col = _collector(samples, reader, clock)
    assert {n.pid: n.agent_kind for n in col.collect(host_pid=100, core_pid=200).nodes}[400] == "codex"

    samples[-1] = RawProcessSample(400, 300, "node.exe", 41, 60_000_000, 0.1)
    reader.table[400] = ["node.exe", "D:\\app\\server.js"]
    clock[0] += 1_000_000_000
    by_pid = {n.pid: n for n in col.collect(host_pid=100, core_pid=200).nodes}
    assert by_pid[400].role == "child" and by_pid[400].agent_kind is None
    assert reader.calls.count(400) == 2


@pytest.mark.parametrize("error", [PermissionError("access denied"), OSError(5, "denied"), RuntimeError("boom")])
def test_unreadable_command_line_fails_closed_without_error(error: BaseException) -> None:
    clock = [1_000_000_000]
    samples = _tree(RawProcessSample(400, 300, "node.exe", 40, 150_000_000, 2.0))
    reader = _Reader({}, error=error)
    col = _collector(samples, reader, clock)
    by_pid = {n.pid: n for n in col.collect(host_pid=100, core_pid=200).nodes}
    assert by_pid[400].role == "child" and by_pid[400].agent_kind is None
    clock[0] += 1_000_000_000
    col.collect(host_pid=100, core_pid=200)
    assert reader.calls.count(400) == 1, "a failed read is not retried every poll"


def test_missing_command_line_or_identity_stays_unknown() -> None:
    clock = [1_000_000_000]
    samples = _tree(
        RawProcessSample(400, 300, "node.exe", 40, 150_000_000, 2.0),
        RawProcessSample(600, 300, "node.exe", None, 50_000_000, 0.2),
    )
    reader = _Reader({400: None, 600: ["node.exe", "C:\\x\\node_modules\\@openai\\codex\\bin\\codex.js"]})
    col = _collector(samples, reader, clock)
    by_pid = {n.pid: n for n in col.collect(host_pid=100, core_pid=200).nodes}
    assert by_pid[400].agent_kind is None and by_pid[400].role == "child"
    # Without a creation time the identity cannot be told from a reused PID: not read at all.
    assert by_pid[600].agent_kind is None and 600 not in reader.calls


def test_injected_process_list_never_reads_real_command_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(_pid: int) -> None:
        raise AssertionError("a fixture process list must not read live command lines")

    monkeypatch.setattr(collector_module, "read_process_cmdline", forbidden)
    clock = [1_000_000_000]
    col = ProcessTreeCollector(
        core_pid=200,
        process_lister=lambda _roots: _tree(RawProcessSample(400, 300, "node.exe", 40, 150_000_000, 2.0)),
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 2,
        system_sampler=lambda _e, _c: None,
    )
    by_pid = {n.pid: n for n in col.collect(host_pid=100, core_pid=200).nodes}
    assert by_pid[400].role == "child" and by_pid[400].agent_kind is None


def test_session_hint_keeps_its_kind_and_the_wrapper_fills_a_missing_one() -> None:
    clock = [1_000_000_000]
    samples = _tree(
        RawProcessSample(400, 300, "node.exe", 40, 150_000_000, 2.0),
        RawProcessSample(410, 300, "node.exe", 41, 150_000_000, 2.0),
    )
    reader = _Reader({
        400: ["node.exe", "C:\\x\\node_modules\\@openai\\codex\\bin\\codex.js"],
        410: ["node.exe", "C:\\x\\node_modules\\@google\\gemini-cli\\dist\\index.js"],
    })
    col = _collector(samples, reader, clock)
    snap = col.collect(
        host_pid=100,
        core_pid=200,
        agent_pids={400: (40, "codex", "sess-1"), 410: (41, None, "sess-2")},
    )
    by_pid = {n.pid: n for n in snap.nodes}
    assert (by_pid[400].agent_kind, by_pid[400].session_id) == ("codex", "sess-1")
    assert (by_pid[410].agent_kind, by_pid[410].session_id) == ("gemini", "sess-2")


def test_live_os_wrapped_script_is_identified(tmp_path: Path) -> None:
    script = tmp_path / "aider.py"
    script.write_bytes(b"import time\ntime.sleep(4)\n")
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.4)
        argv = read_process_cmdline(proc.pid)
        assert argv is not None and any(arg.endswith("aider.py") for arg in argv)
        col = ProcessTreeCollector(core_pid=os.getpid(), min_sample_interval_ms=200)
        snap = col.collect()
        by_pid = {n.pid: n for n in snap.nodes}
        assert proc.pid in by_pid
        assert by_pid[proc.pid].role == "agent" and by_pid[proc.pid].agent_kind == "aider"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_live_os_reader_fails_closed() -> None:
    assert read_process_cmdline(0) is None
    assert read_process_cmdline(-1) is None
    assert read_process_cmdline(0x7FFFFFF0) is None


# ---------------------------------------------------------------------------------------------------------------------
# Rust host: same tables, same rule, command line only for wrappers
# ---------------------------------------------------------------------------------------------------------------------

_RUST_CONST = re.compile(r"const (AGENT_[A-Z_]+): \[(\(&str, &str\)|&str); (\d+)\] = \[(.*?)\];", re.S)
_RUST_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _rust_agent_tables() -> Dict[str, Tuple]:
    tables: Dict[str, Tuple] = {}
    for name, elem, count, body in _RUST_CONST.findall(MAIN_RS.read_text(encoding="utf-8")):
        values = [v.replace('\\"', '"').replace("\\\\", "\\") for v in _RUST_STR.findall(body)]
        if elem.startswith("("):
            assert len(values) % 2 == 0, name
            table: Tuple = tuple(zip(values[0::2], values[1::2]))
        else:
            table = tuple(values)
        assert len(table) == int(count), f"{name}: declared {count}, found {len(table)}"
        tables[name] = table
    return tables


def test_rust_and_python_declare_the_same_tables() -> None:
    rust = _rust_agent_tables()
    python = {
        name[1:]: value
        for name, value in vars(collector_module).items()
        if name.startswith("_AGENT_") and isinstance(value, tuple)
    }
    assert len(python) == 16
    assert set(rust) == set(python)
    for name, value in python.items():
        assert rust[name] == value, name


def test_rust_rule_mirrors_the_python_helpers() -> None:
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    helpers = {
        "normalize_agent_token": "_normalize_agent_token",
        "strip_agent_extension": "_strip_agent_extension",
        "agent_kind_from_token": "_agent_kind_from_token",
        "agent_kind_from_package": "_agent_kind_from_package",
        "agent_kind_from_script": "_agent_kind_from_script",
        "agent_kind_from_module": "_agent_kind_from_module",
        "first_command_token": "_first_command_token",
        "agent_kind_from_js_args": "_agent_kind_from_js_args",
        "agent_kind_from_python_args": "_agent_kind_from_python_args",
        "agent_kind_from_cmd_args": "_agent_kind_from_cmd_args",
        "agent_kind_from_powershell_args": "_agent_kind_from_powershell_args",
        "is_python_launcher": "_is_python_launcher",
        "agent_launcher_stem": "_agent_launcher_stem",
        "agent_needs_cmdline": "agent_needs_cmdline",
        "detect_agent_kind": "detect_agent_kind",
    }
    for rust_fn, python_fn in helpers.items():
        assert len(re.findall(rf"\bfn {rust_fn}\(", main_rs)) == 1, rust_fn
        assert callable(getattr(collector_module, python_fn)), python_fn
    assert "fn detect_agent_kind(name: &str, cmd: &[String]) -> Option<&'static str>" in main_rs
    # Same matching details on both sides.
    for fragment in ('format!("{prefix}-")', 'format!("{prefix}_")', 'format!("{marker}@")', 'format!("{marker}/")',
                     'format!("node_modules/{marker}/")', 'strip_prefix("python")', "strip_suffix('w')",
                     'stem.starts_with("bun")', 'arg.starts_with("-m") && arg.len() > 2', 'arg == "-c" || arg == "-"'):
        assert fragment in main_rs, fragment
    # The name-only detector of 4D.4.1 is gone; the role gets the kind computed from name + command line.
    assert "Self::detect_agent_kind" not in main_rs
    assert "Self::classify_role(pid_u32, ppid, proc.name(), agent_kind, host_pid_u32, core_pid)" in main_rs


def test_rust_reads_the_command_line_only_for_wrappers_once_per_identity() -> None:
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    assert "use sysinfo::{Disks, Networks, Pid, ProcessRefreshKind, System, UpdateKind};" in main_rs
    # The polling refresh of every process stays without `cmd`.
    assert "let refresh_kind = ProcessRefreshKind::new().with_memory().with_cpu();" in main_rs
    assert main_rs.count("with_cmd(") == 1
    assert "cmdline_agent_kinds: HashMap<(u32, u64), Option<&'static str>>," in main_rs
    collect = main_rs[main_rs.index("    fn collect(&mut self) -> ProcessTreeMetrics {"):]
    pass_start = collect.index("let mut cmdline_agent_kinds")
    pass_end = collect.index("self.cmdline_agent_kinds = cmdline_agent_kinds;")
    cmdline_pass = collect[pass_start:pass_end]
    assert "self.cmdline_agent_kinds.get(&identity)" in cmdline_pass
    assert "agent_needs_cmdline(proc.name())" in cmdline_pass
    assert ".refresh_process_specifics(pid, ProcessRefreshKind::new().with_cmd(UpdateKind::OnlyIfNotSet))" in cmdline_pass
    assert "detect_agent_kind(proc.name(), proc.cmd())" in cmdline_pass
    # The pass runs before the nodes are built, so no `self.sys` borrow is alive while it refreshes.
    assert pass_end < collect.index("let mut nodes: Vec<ProcessNodeMetrics>")
