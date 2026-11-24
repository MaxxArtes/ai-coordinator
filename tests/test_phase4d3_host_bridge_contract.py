"""Marco 4D.3, Etapa 8 — contrato estático do host Tauri com o IpcBridge (L1-L4).

O Verify não compila Rust (o build nativo do Windows compila); estes testes leem desktop/src-tauri/src/main.rs como
texto, com comentários e literais mascarados (tests/_desktop_static.py), e conferem:

- L1: todo #[tauri::command] que faz ida e volta com o Core (call, call_structured, exchange ou funções que os usam,
  como count_active_sessions) é async fn, recebe State<'_, Arc<IpcBridge>> e faz cada chamada bloqueante dentro de
  tauri::async_runtime::spawn_blocking (ou de um auxiliar que a use), com um clone do Arc e aguardando o resultado.
  Comandos que não falam com o Core (ping, métricas, canal e reinício do atualizador) ficam fora.
- L2: o bridge lê as respostas numa thread leitora dedicada que entrega cada linha por std::sync::mpsc; exchange espera
  com recv_timeout (30 s; 120 s para terminal.create_tab, terminal.create_session e terminal.restore_sessions), com as
  mensagens de prazo e de desconexão do contrato, sem registrar parâmetros nem conteúdo de resposta.
- L3: profile_get_active e profile_authorize_project usam call_structured; o segundo repassa confirm_broad.
- L4: vault_delete_secret repassa expected_revision.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _desktop_static as static  # noqa: E402

_main_rs = static.MAIN_RS.read_text(encoding="utf-8")
_masked = static.mask_rust(_main_rs)
_analysis = static.analyze_ipc(_main_rs)
_commands = _analysis.commands

# Commands that reach the Core today (directly or through count_active_sessions / acquire_update_freeze).
IPC_COMMANDS = {
    "get_runtime_status", "profile_get_active", "profile_authorize_project", "profile_list_authorized_projects",
    "terminal_create_tab", "terminal_create_session", "terminal_list_interpreters", "terminal_validate_interpreter",
    "terminal_write", "terminal_resize", "terminal_get_output",
    "terminal_close_session", "terminal_save_sessions", "terminal_list_tabs", "updater_check",
    "updater_download_and_install", "settings_get", "settings_update", "vault_status", "vault_list_keys",
    "vault_set_secret", "vault_delete_secret", "profile_revoke_project", "profile_remember_project",
    "security_status", "discovery_agents", "providers_routes", "providers_telemetry",
}
NOT_IPC_COMMANDS = {"ping", "get_process_tree_metrics", "updater_get_channel_info", "updater_restart_app"}
LONG_METHODS = ("terminal.create_tab", "terminal.create_session", "terminal.restore_sessions")
CONTENT_NAMES = {"params", "req", "request", "resp", "response", "line", "resp_line", "raw", "payload", "reply"}
LOG_MACRO = re.compile(r"\b(?:e?println|e?print|log::\w+|tracing::\w+|info|warn|error|debug|trace)!\s*\(")


def _line(fn: static.RustFn, offset: int) -> int:
    return _main_rs.count("\n", 0, fn.body_start + offset) + 1


def _function(name: str) -> static.RustFn:
    found = [fn for fn in _analysis.functions if fn.name == name and fn.body_start >= 0]
    assert len(found) == 1, f"esperada uma única fn {name} com corpo em main.rs, encontradas {len(found)}"
    return found[0]


def _thread_closures() -> list:
    """(open, close) of the arguments of every thread spawn: thread::spawn(...) or Builder::new()...spawn(...)."""
    spans = []
    for m in re.finditer(r"(?:thread::|\.)spawn\s*\(", _masked):
        spans.append((m.end() - 1, static.matching(_masked, m.end() - 1)))
    return spans


def _reader_closures() -> list:
    """Thread closures that read the Core's stdout and send each line through a channel."""
    return [(a, b) for a, b in _thread_closures()
            if ".send(" in _masked[a:b] and re.search(r"read_line\(|read_until\(|\.lines\(\)", _masked[a:b])]


def _consts() -> dict:
    return dict(re.findall(r"\bconst\s+(\w+)\s*:[^=;]+=\s*([^;]+);", _masked))


def _with_consts(text: str) -> str:
    """`text` followed by the values of the constants it names (two levels: a constant built from another)."""
    consts = _consts()
    out = text
    for _ in range(2):
        out += "\n" + "\n".join(value for name, value in consts.items() if re.search(rf"\b{name}\b", out))
    return out


SAMPLE = r'''
impl IpcBridge {
    pub fn call(&self, method: &str, params: Value) -> Result<Value, String> { self.exchange(method, params) }
}
fn count(bridge: &IpcBridge) -> usize { bridge.call("terminal.list_tabs", json!({})).map(|_| 1).unwrap_or(0) }
async fn offload<T: Send + 'static, F: FnOnce(&IpcBridge) -> T + Send + 'static>(b: Arc<IpcBridge>, f: F) -> T {
    tauri::async_runtime::spawn_blocking(move || f(&b)).await.unwrap()
}
#[tauri::command]
fn blocking_cmd(bridge: State<Arc<IpcBridge>>) -> Result<Value, String> {
    // bridge.call("x") in a comment is not a call
    bridge.call("vault.status", json!({ "s": "spawn_blocking(" }))
}
#[tauri::command]
async fn direct_cmd(key: String, bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call("vault.status", json!({ "key": key })))
        .await
        .map_err(|_| String::new())?
}
#[tauri::command]
async fn wrapped_cmd(bridge: State<'_, Arc<IpcBridge>>) -> Result<usize, String> {
    Ok(offload(bridge.inner().clone(), |b| count(b)).await)
}
#[tauri::command]
async fn half_cmd(bridge: State<'_, Arc<IpcBridge>>) -> Result<usize, String> {
    let n = count(&bridge);
    Ok(n + offload(bridge.inner().clone(), |b| count(b)).await)
}
#[tauri::command]
fn plain_cmd() -> &'static str { "pong" }
'''


def test_static_reader_tells_blocking_from_offloaded_commands() -> None:
    sample = static.analyze_ipc(SAMPLE)
    assert {"call", "count"} <= sample.blocking and "offload" in sample.wrappers
    outside = {name: [c for c, _ in sample.calls_outside_offload(fn)] for name, fn in sample.commands.items()}
    assert outside == {"blocking_cmd": ["call"], "direct_cmd": [], "wrapped_cmd": [], "half_cmd": ["count"],
                       "plain_cmd": []}
    assert {name for name, fn in sample.commands.items() if sample.uses_ipc(fn)} == {
        "blocking_cmd", "direct_cmd", "wrapped_cmd", "half_cmd"}
    assert [fn.is_async for fn in sample.commands.values()] == [False, True, True, True, False]
    assert sample.commands["direct_cmd"].interface_params() == [("key", "String")]


def test_commands_are_classified_as_the_contract_expects() -> None:
    ipc = {name for name, fn in _commands.items() if _analysis.uses_ipc(fn)}
    assert IPC_COMMANDS <= ipc, sorted(IPC_COMMANDS - ipc)
    assert NOT_IPC_COMMANDS.isdisjoint(ipc), sorted(NOT_IPC_COMMANDS & ipc)
    assert {"exchange", "call", "call_structured", "count_active_sessions"} <= _analysis.blocking


def test_commands_that_reach_the_core_run_the_round_trip_off_the_main_thread() -> None:
    """L1: a synchronous command runs on the main (UI) thread; a Core that takes long froze the whole window."""
    problems = []
    for name, fn in sorted(_commands.items()):
        if not _analysis.uses_ipc(fn):
            continue
        if not fn.is_async:
            problems.append(f"{name}: precisa ser async fn")
        for callee, offset in _analysis.calls_outside_offload(fn):
            problems.append(f"{name} (main.rs:{_line(fn, offset)}): {callee}(...) bloqueia fora de spawn_blocking")
        bridge = [(n, t) for n, t in fn.params if "IpcBridge" in t]
        for param, typ in bridge:
            if not re.fullmatch(r"(?:tauri::)?State<'_,\s*Arc<IpcBridge>>", typ):
                problems.append(f"{name}: {param}: {typ} (esperado State<'_, Arc<IpcBridge>>)")
        if not bridge and "state::<Arc<IpcBridge>>" not in fn.masked_body.replace(" ", ""):
            problems.append(f"{name}: não recebe o bridge (State<'_, Arc<IpcBridge>>)")
        expanded = _analysis.expanded_body(fn)
        if not re.search(r"\.inner\(\)\s*\.clone\(\)|Arc::clone\(", expanded):
            problems.append(f"{name}: a thread de bloqueio precisa de um clone do Arc (bridge.inner().clone())")
    assert not problems, "\n".join(problems)


def test_blocking_work_uses_the_tauri_runtime_and_is_awaited() -> None:
    assert "spawn_blocking" in _masked, "nenhum spawn_blocking em main.rs"
    assert "tokio::task::spawn_blocking" not in _masked
    prefixes = set(re.findall(r"([\w:]*?)spawn_blocking\s*(?:::\s*<[^<>()]*>)?\s*\(", _masked))
    for prefix in prefixes:
        if prefix in ("tauri::async_runtime::", "async_runtime::"):
            continue
        assert prefix == "" and re.search(r"use\s+tauri::async_runtime::(?:\{[^}]*\bspawn_blocking\b|spawn_blocking\b)",
                                          _masked), f"spawn_blocking de origem inesperada: {prefix!r}"
    if "async_runtime::" in prefixes:
        assert re.search(r"use\s+tauri::(?:\{[^}]*\b)?async_runtime\b", _masked), "async_runtime sem import de tauri"
    for fn in _analysis.functions:
        spans = fn.spans()
        if not spans:
            continue
        assert fn.is_async, f"{fn.name}: spawn_blocking fora de uma fn async"
        # The JoinHandle must be awaited (a detached task would answer before the Core does).
        assert fn.masked_body.count(".await") >= len(spans), f"{fn.name}: spawn_blocking sem .await"


def test_every_argument_of_a_core_command_reaches_its_body() -> None:
    for name, fn in sorted(_commands.items()):
        if not _analysis.uses_ipc(fn):
            continue
        for param, _typ in fn.interface_params():
            assert re.search(rf"\b{param}\b", fn.masked_body), f"{name}: parâmetro {param} não é usado nem repassado"


def test_exchange_waits_with_a_deadline_on_a_dedicated_reader_thread() -> None:
    """L2 (R57-4): a read_line that never returns kept io_lock forever; every later call hung behind it."""
    exchange = _function("exchange")
    body = _analysis.expanded_body(exchange)
    assert "recv_timeout(" in body, "exchange precisa esperar com recv_timeout"
    assert "read_line(" not in exchange.masked_body, "exchange não pode ler o stdout do Core diretamente"
    assert re.search(r"\bmpsc\b", _masked) and re.search(r"\bstd::sync::(?:\{[^}]*\bmpsc\b|mpsc\b)", _masked)
    readers = [_main_rs[a:b] for a, b in _reader_closures()]
    assert readers, "nenhuma thread leitora entrega as linhas do Core por mpsc"
    # A response for another request (late answer of a call that timed out, or a null id) is discarded: the id of
    # the response is compared with the request's, in exchange or in the reader.
    assert re.search(r"""get\(\s*"id"\s*\)|\[\s*"id"\s*\]""", "\n".join([body, *readers])), \
        "o id da resposta não é conferido"


def test_deadlines_are_30_s_and_120_s_for_terminal_creation() -> None:
    candidates = []
    for fn in _analysis.functions:
        if fn.body_start < 0 or not all(f'"{method}"' in fn.body for method in LONG_METHODS):
            continue
        expanded = _with_consts(fn.masked_body)
        if re.search(r"\b120(?:_?000)?\b", expanded):
            candidates.append((fn.name, expanded))
    assert candidates, "nenhuma fn associa terminal.create_tab/create_session/restore_sessions a 120 s"
    default_ok = any(re.search(r"\b30(?:_?000)?\b", expanded) for _name, expanded in candidates) or re.search(
        r"\b30(?:_?000)?\b", _with_consts(_function("exchange").masked_body))
    assert default_ok, "prazo padrão de 30 s não encontrado"


def test_timeout_and_disconnection_errors_are_the_contract_messages() -> None:
    assert re.search(r"O Core não respondeu em \{\w*\} s\. A operação pode ter sido concluída: confira antes de "
                     r"repetir\.", _main_rs), "mensagem de prazo esgotado ausente ou diferente"
    assert "O Core encerrou a comunicação. Reinicie o aplicativo." in _main_rs
    assert re.search(r"-32007\b", _main_rs), "código -32007 ausente"
    for literal in ('"ipc"', '"timeout"', '"disconnected"'):
        assert literal in _main_rs, literal


def test_bridge_never_logs_params_or_response_content() -> None:
    assert not re.search(r"\bdbg!\s*\(", _masked), "dbg! imprime valores"
    regions = []
    for name in ("exchange", "call", "call_locked", "call_structured"):
        fn = _function(name)
        regions.append((fn.body_start, fn.body_end))
        regions += [(h.body_start, h.body_end) for h in _analysis.helpers_of(fn)]
    regions += _reader_closures()
    problems = []
    for start, end in regions:
        segment = _masked[start:end]
        for m in LOG_MACRO.finditer(segment):
            open_idx = start + m.end() - 1
            close = static.matching(_masked, open_idx)
            parts = static.split_top_level(_masked[open_idx + 1 : close])
            fmt = re.search(r'"(?:[^"\\]|\\.)*"', _main_rs[open_idx + 1 : close])
            inline = set(re.findall(r"\{(\w+)", fmt.group(0) if fmt else "")) & CONTENT_NAMES
            leaked = [p for p in parts[1:] if set(re.findall(r"\b\w+\b", p)) & CONTENT_NAMES
                      and not re.search(r"\.len\(\)\s*$", p)]
            if inline or leaked:
                problems.append(f"main.rs:{_main_rs.count(chr(10), 0, open_idx) + 1}: {sorted(inline) or leaked}")
    assert not problems, "o bridge registra conteúdo do pedido ou da resposta:\n" + "\n".join(problems)


def test_profile_commands_report_structured_errors() -> None:
    """L3: the interface shows the reason of a refused authorization (broad_scope, path_missing) from {code, message,
    data}; a plain String error lost it."""
    for name in ("profile_get_active", "profile_authorize_project"):
        fn = _commands[name]
        assert fn.returns == "Result<Value, Value>", (name, fn.returns)
        assert re.search(r"\bcall_structured\s*\(", _analysis.expanded_body(fn)), f"{name} não usa call_structured"
        assert "call" not in {callee for callee, _ in fn.calls()}, f"{name} ainda usa call() com erro em texto"
        assert '"' + name.replace("_", ".", 1) + '"' in fn.body


def test_authorize_forwards_confirm_broad() -> None:
    fn = _commands["profile_authorize_project"]
    assert dict(fn.interface_params()) == {"project_path": "String", "confirm_broad": "Option<bool>"}
    assert re.search(r'"project_path"\s*:\s*project_path\b', fn.body)
    assert re.search(r'"confirm_broad"\s*:\s*confirm_broad\b', fn.body)


def test_vault_delete_forwards_expected_revision() -> None:
    fn = _commands["vault_delete_secret"]
    assert dict(fn.interface_params()) == {"key": "String", "expected_revision": "Option<String>"}
    assert fn.returns == "Result<Value, Value>"
    assert re.search(r'"key"\s*:\s*key\b', fn.body)
    assert re.search(r'"expected_revision"\s*:\s*expected_revision\b', fn.body)
