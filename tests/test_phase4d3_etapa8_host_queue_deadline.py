"""Marco 4D.3, Etapa 8 — achado #1: o prazo de L2 conta a espera pela vez no fluxo do Core.

Antes, o prazo de uma chamada só começava depois que ela pegava a vez (FifoLock::lock): com o Core travado, cada
chamada na fila esperava a própria vez e mais 30 s, e uma ação do operador feita atrás do polling de terminal podia
ficar horas sem erro. Agora call() e call_structured() tomam o prazo antes de pedir a vez (FifoLock::lock_until): quem
não recebe a vez até o prazo abandona a senha (o turno a pula) e devolve o erro de prazo sem enviar nada; exchange
espera a resposta só até esse mesmo prazo.

O Verify não compila Rust: estes testes leem desktop/src-tauri/src/main.rs como texto (tests/_desktop_static.py). O
comportamento é coberto pelos testes Rust do módulo ipc_transport_tests de main.rs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _desktop_static as static  # noqa: E402

_main_rs = static.MAIN_RS.read_text(encoding="utf-8")
_masked = static.mask_rust(_main_rs)
_functions = static.rust_functions(_main_rs)


def _function(name: str) -> static.RustFn:
    found = [fn for fn in _functions if fn.name == name and fn.body_start >= 0]
    assert len(found) == 1, f"esperada uma única fn {name} com corpo em main.rs, encontradas {len(found)}"
    return found[0]


def _impl_block(header: str) -> str:
    start = re.search(header, _masked)
    assert start, f"bloco ausente: {header}"
    open_idx = _masked.index("{", start.end())
    return _masked[open_idx : static.matching(_masked, open_idx) + 1]


def test_ipc_calls_take_their_deadline_before_waiting_for_the_turn() -> None:
    forwarded = {"call": r"call_locked_until\(\s*method\s*,\s*params\s*,\s*deadline\s*\)",
                 "call_structured": r"exchange\(\s*method\s*,\s*params\s*,\s*deadline\s*\)"}
    for name, forward in forwarded.items():
        body = _function(name).masked_body
        taken = re.search(r"let\s+deadline\s*=\s*ipc_deadline_for\(\s*method\s*\)", body)
        turn = re.search(r"io_lock\s*\.\s*lock_until\(\s*deadline\s*\)", body)
        assert taken and turn and taken.start() < turn.start(), f"{name}: o prazo precisa começar antes da vez"
        assert not re.search(r"io_lock\s*\.\s*lock\(\s*\)", body), f"{name}: espera sem prazo pela vez"
        assert re.search(forward, body), f"{name}: o prazo da chamada não chega à troca com o Core"


def test_the_deadline_covers_the_method_wait_in_all() -> None:
    body = _function("ipc_deadline_for").masked_body
    assert re.search(r"Instant::now\(\)\s*\+\s*ipc_timeout_for\(\s*method\s*\)", body)


def test_exchange_waits_until_the_callers_deadline_and_sends_nothing_after_it() -> None:
    exchange = _function("exchange")
    assert ("deadline", "Instant") in exchange.params
    body = exchange.masked_body
    assert not re.search(r"Instant::now\(\)\s*\+", body), "exchange não pode recomeçar o prazo depois da vez"
    late = re.search(r"if\s+Instant::now\(\)\s*>=\s*deadline\b", body)
    sent = re.search(r"\btx\s*\.\s*send\(", body)
    assert late and sent and late.start() < sent.start(), "o pedido sai mesmo com o prazo vencido"
    assert re.search(r"recv_timeout\(\s*deadline\s*\.\s*saturating_duration_since\(", body)


def test_a_queue_timeout_is_the_contract_error() -> None:
    """Prazo vencido na fila: o mesmo erro de L2 (Timeout com o prazo do método, ou Disconnected com o Core fora)."""
    for name, conversion in (("call", "message"), ("call_structured", "to_structured")):
        body = _function(name).masked_body
        assert re.search(rf"else\s*\{{\s*return\s+Err\(\s*self\s*\.\s*deadline_failure\(\s*method\s*\)\s*\.\s*"
                         rf"{conversion}\(\)\s*\)", body), name
    failure = _function("deadline_failure").masked_body
    assert re.search(r"IpcFailure::Timeout\(\s*ipc_timeout_for\(\s*method\s*\)\s*\.\s*as_secs\(\)\s*\)", failure)
    assert "core_runtime_down()" in failure and "IpcFailure::Disconnected" in failure


def test_fifo_lock_abandons_late_tickets_and_the_turn_skips_them() -> None:
    lock_until = _function("lock_until")
    assert lock_until.params == [("deadline", "Instant")]
    assert lock_until.returns == "Option<FifoGuard<'_>>"
    body = lock_until.masked_body
    mine = body.find("now_serving == ticket")
    abandon = body.find("abandoned.insert(ticket)")
    assert 0 <= mine < abandon, "a senha só pode ser abandonada depois de conferir que a vez não chegou"
    assert "wait_timeout(" in body
    assert re.search(r"abandoned\s*:\s*HashSet<u64>", _impl_block(r"struct\s+FifoState\b"))
    advance = _function("advance").masked_body
    assert re.search(r"while\s+self\s*\.\s*abandoned\s*\.\s*remove\(\s*&\s*self\s*\.\s*now_serving\s*\)", advance)
    assert re.search(r"\.\s*advance\(\s*\)", _impl_block(r"impl\s+Drop\s+for\s+FifoGuard\b"))


def test_only_the_update_freeze_waits_for_the_turn_without_a_deadline() -> None:
    """acquire_update_freeze espera com lock(): todos à sua frente devolvem a vez dentro do próprio prazo."""
    freeze = _function("acquire_update_freeze")
    unbounded = [m.start() for m in re.finditer(r"io_lock\s*\.\s*lock\(\s*\)", _masked)]
    assert unbounded, "acquire_update_freeze deveria segurar a vez durante o congelamento"
    outside = [_main_rs.count("\n", 0, pos) + 1 for pos in unbounded if not freeze.body_start < pos < freeze.body_end]
    assert not outside, f"espera sem prazo pela vez fora do congelamento: main.rs linhas {outside}"


def test_rust_tests_of_the_queue_deadline_stay_in_place() -> None:
    module = re.search(r"#\[cfg\(test\)\]\s*mod\s+ipc_transport_tests\s*\{", _main_rs)
    assert module, "módulo de testes do transporte ausente"
    tests = set(re.findall(r"#\[test\]\s*fn\s+(\w+)", _main_rs[module.start() :]))
    assert {"a_stuck_call_does_not_add_the_deadlines_of_the_calls_ahead",
            "abandoned_tickets_are_skipped_when_the_turn_moves_on",
            "nothing_is_sent_once_the_deadline_has_passed"} <= tests, sorted(tests)
