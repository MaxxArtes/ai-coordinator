"""Marco 4D.3 — barra de título e ícone do instalador (ajustes da logo autorizados pelo operador em 16/11).

Contrato estático (o Verify não compila Rust; a CI "Build Desktop Alpha (Windows)" compila):

- A barra de título da janela "main" é pintada com #071017 (cor medida na barra do print de referência do
  operador) via DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR = 35, &COLORREF, 4); COLORREF é 0x00BBGGRR.
- Todo o código DWM fica sob #[cfg(windows)], sem dependência nova (FFI direto com dwmapi) e sem caminho de pânico:
  falha (Windows 10 devolve E_INVALIDARG) é ignorada e a abertura segue.
- O instalador e o desinstalador NSIS usam o ícone do app (bundle.windows.nsis.installerIcon e uninstallerIcon
  -> .ico existente) e a janela mantém "theme": "Dark".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
_tauri_dir = _repo_root / "desktop" / "src-tauri"
_main_rs = (_tauri_dir / "src" / "main.rs").read_text(encoding="utf-8")
_conf = json.loads((_tauri_dir / "tauri.conf.json").read_text(encoding="utf-8"))

_DWM_SYMBOLS = (
    "MAIN_WINDOW_CAPTION_COLOR",
    "DWMWA_CAPTION_COLOR",
    "DwmSetWindowAttribute",
    "apply_main_window_caption_color",
)


def _colorref(hex_rgb: str) -> int:
    """#RRGGBB -> COLORREF 0x00BBGGRR."""
    r, g, b = (int(hex_rgb[i : i + 2], 16) for i in (1, 3, 5))
    return (b << 16) | (g << 8) | r


def _attributes_before(code: str, start: int) -> list[str]:
    """Atributos `#[...]` em sequência imediatamente acima da linha que começa em `start`."""
    lines = code[:start].rstrip("\n").split("\n")
    attrs: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped.startswith("#["):
            break
        attrs.append(stripped)
    return attrs


def _item_end(code: str, start: int) -> int:
    """Fim do item/declaração que começa em `start`: até o `;` ou até fechar o primeiro bloco `{...}`."""
    depth = 0
    for i in range(start, len(code)):
        ch = code[i]
        if ch == ";" and depth == 0:
            return i + 1
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise AssertionError("item sem fim em main.rs")


def _cfg_windows_spans(code: str) -> list[tuple[int, int]]:
    """Trechos de main.rs que só existem no Windows (itens e instruções precedidos de #[cfg(windows)])."""
    spans = []
    for m in re.finditer(r"^[ \t]*#\[cfg\(windows\)\]\n", code, re.MULTILINE):
        pos = m.end()
        # Pula outros atributos do mesmo item (ex.: #[allow(...)], #[link(...)]).
        while True:
            nxt = re.match(r"[ \t]*#\[[^\n]*\]\n", code[pos:])
            if not nxt:
                break
            pos += nxt.end()
        spans.append((m.start(), _item_end(code, pos)))
    return spans


def _item_start(pattern: str) -> int:
    m = re.search(pattern, _main_rs, re.MULTILINE)
    assert m, f"padrão ausente em main.rs: {pattern}"
    return m.start()


def test_caption_color_is_reference_bar_colorref() -> None:
    m = re.search(r"const MAIN_WINDOW_CAPTION_COLOR: u32 = (0x[0-9A-Fa-f]{8});", _main_rs)
    assert m, "MAIN_WINDOW_CAPTION_COLOR ausente"
    assert m.group(1) == "0x00171007"
    assert int(m.group(1), 16) == _colorref("#071017")
    # O azul da logo (#001c44) fica só como alternativa documentada, não como valor em uso.
    assert int(m.group(1), 16) != _colorref("#001c44")
    assert re.search(r"const DWMWA_CAPTION_COLOR: u32 = 35;", _main_rs)


def test_dwm_block_exists_only_on_windows() -> None:
    for pattern in (
        r"^const MAIN_WINDOW_CAPTION_COLOR\b",
        r"^const DWMWA_CAPTION_COLOR\b",
        r'^extern "system" \{',
        r"^fn apply_main_window_caption_color\b",
        r"^[ \t]+apply_main_window_caption_color\(",
    ):
        attrs = _attributes_before(_main_rs, _item_start(pattern))
        assert "#[cfg(windows)]" in attrs, f"{pattern} precisa de #[cfg(windows)] logo acima: {attrs}"

    extern_attrs = _attributes_before(_main_rs, _item_start(r'^extern "system" \{'))
    assert '#[link(name = "dwmapi")]' in extern_attrs

    # Nenhuma menção ao código DWM fora de um trecho #[cfg(windows)] (comentários não contam).
    spans = _cfg_windows_spans(_main_rs)
    for symbol in _DWM_SYMBOLS:
        hits = [m.start() for m in re.finditer(rf"\b{symbol}\b", _main_rs)]
        assert hits, symbol
        for pos in hits:
            line = _main_rs[_main_rs.rfind("\n", 0, pos) + 1 : _main_rs.find("\n", pos)]
            if line.lstrip().startswith("//"):
                continue
            assert any(a <= pos < b for a, b in spans), f"{symbol} fora de #[cfg(windows)]: {line.strip()}"


def test_dwm_call_contract_and_no_new_dependency() -> None:
    start = _item_start(r'^extern "system" \{')
    decl = _main_rs[start : _item_end(_main_rs, start)]
    assert re.search(r"fn DwmSetWindowAttribute\(\s*hwnd: isize,\s*dw_attribute: u32,\s*"
                     r"pv_attribute: \*const std::ffi::c_void,\s*cb_attribute: u32,?\s*\) -> i32;", decl)

    start = _item_start(r"^fn apply_main_window_caption_color\b")
    body = _main_rs[start : _item_end(_main_rs, start)]
    assert 'app.get_webview_window("main")' in body
    assert "use tauri::Manager;" in body
    assert "window.hwnd()" in body
    # Converte HWND(*mut c_void) e HWND(isize) do crate windows sem citar o tipo.
    assert "hwnd.0 as isize" in body
    call = body[body.index("DwmSetWindowAttribute(") :]
    args = [a.strip() for a in call[call.index("(") + 1 : call.index(")\n")].split(",") if a.strip()]
    assert args[1] == "DWMWA_CAPTION_COLOR"
    assert args[2] == "&color as *const u32 as *const std::ffi::c_void"
    assert args[3] == "std::mem::size_of::<u32>() as u32"  # 4 bytes, o tamanho do COLORREF
    # Falha ignorada: nada de pânico nem propagação de erro que aborte a abertura.
    for forbidden in (".unwrap()", ".expect(", "panic!", "?;", "?\n"):
        assert forbidden not in body, forbidden

    # O hook .setup só chama o helper e sempre devolve Ok(()).
    setup = _main_rs[_main_rs.index(".setup(|") : _main_rs.index(".on_window_event(")]
    assert "apply_main_window_caption_color(" in setup
    assert "Ok(())" in setup
    assert "?" not in setup and ".unwrap()" not in setup

    cargo = (_tauri_dir / "Cargo.toml").read_text(encoding="utf-8")
    for dep in ("windows", "windows-sys", "raw-window-handle", "window-vibrancy"):
        assert not re.search(rf"^{re.escape(dep)}\s*=", cargo, re.MULTILINE), f"dependência nova: {dep}"


def test_nsis_installer_and_uninstaller_icons_are_the_app_ico() -> None:
    nsis = _conf["bundle"]["windows"]["nsis"]
    for key in ("installerIcon", "uninstallerIcon"):
        icon = nsis[key]
        assert icon.endswith(".ico"), key
        path = _tauri_dir / icon
        assert path.is_file(), f"{key} aponta para arquivo inexistente: {path}"
        assert path.read_bytes()[:4] == b"\x00\x00\x01\x00", f"{key} não é um arquivo ICO"
        assert icon in _conf["bundle"]["icon"], f"{key} deve usar o mesmo ícone do app"


def test_main_window_keeps_dark_theme() -> None:
    main = [w for w in _conf["app"]["windows"] if w.get("label") == "main"]
    assert len(main) == 1
    assert main[0]["theme"] == "Dark"
