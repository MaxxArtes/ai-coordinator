"""Static readers of the Desktop sources for the contract tests (Marco 4D.3).

The Verify suite does not compile Rust nor run the interface; these helpers read desktop/src-tauri/src/main.rs and
desktop/src/**/*.ts(x) as text so that the tests can check the contract between the three layers (interface ->
Tauri host -> Core IPC). Not a test module (no test_ prefix): imported by the tests that need it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
MAIN_RS = ROOT / "desktop" / "src-tauri" / "src" / "main.rs"
INTERFACE_SRC = ROOT / "desktop" / "src"

# Bridge methods that perform a blocking round trip with the Core process (L1).
IPC_SEED = frozenset({"exchange", "call_locked", "call", "call_structured"})
# Parameters injected by Tauri, never sent by the interface.
INJECTED_TYPE = re.compile(r"^(?:tauri::)?(?:State|AppHandle|Window|WebviewWindow|Webview)\b")
OPTION_TYPE = re.compile(r"^(?:std::option::)?Option\s*<")
_PAIRS = {"(": ")", "[": "]", "{": "}"}


# ---------------------------------------------------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------------------------------------------------

def mask_rust(code: str) -> str:
    """Same length as `code`: comments blanked, the inside of string and char literals replaced by "_".

    Brackets, keywords and names can then be found in the masked text, and the original text sliced at the same
    positions when a literal itself matters.
    """
    out = list(code)
    n = len(code)

    def blank(start: int, end: int, fill: str) -> None:
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = fill

    raw = re.compile(r'b?r(#*)"')
    char = re.compile(r"""'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|.)|[^\\'\n])'""")
    i = 0
    while i < n:
        if code.startswith("//", i):
            end = code.find("\n", i)
            end = n if end < 0 else end
            blank(i, end, " ")
            i = end
            continue
        if code.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if code.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif code.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j, " ")
            i = j
            continue
        prev_ident = i > 0 and (code[i - 1].isalnum() or code[i - 1] == "_")
        m = raw.match(code, i)
        if m and not prev_ident:
            closing = '"' + m.group(1)
            end = code.find(closing, m.end())
            end = n if end < 0 else end
            blank(m.end(), end, "_")
            i = end + len(closing)
            continue
        if code[i] == '"':
            j = i + 1
            while j < n and code[j] != '"':
                j += 2 if code[j] == "\\" else 1
            blank(i + 1, j, "_")
            i = j + 1
            continue
        if code[i] == "'":
            m = char.match(code, i)
            if m:
                blank(i + 1, m.end() - 1, "_")
                i = m.end()
                continue
        i += 1
    return "".join(out)


def matching(masked: str, start: int) -> int:
    """Index of the bracket closing the one at `start` (round, square or curly), in masked text."""
    stack = [_PAIRS[masked[start]]]
    for j in range(start + 1, len(masked)):
        ch = masked[j]
        if ch in _PAIRS:
            stack.append(_PAIRS[ch])
        elif ch in ")]}":
            if stack.pop() != ch:
                raise AssertionError(f"unbalanced bracket at offset {j}")
            if not stack:
                return j
    raise AssertionError(f"bracket at offset {start} is never closed")


def split_top_level(text: str, sep: str = ",") -> List[str]:
    """Split masked text on `sep` outside (), [], {} and <> nesting ("->" is not a closing angle)."""
    parts, depth, current = [], 0, []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "<":
            depth += 1
        elif ch == ">" and not (i > 0 and text[i - 1] in "-="):
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return [p for p in (part.strip() for part in parts) if p]


@dataclass
class RustFn:
    name: str
    start: int  # line start of the item (attributes excluded)
    attrs: List[str]
    is_async: bool
    params: List[Tuple[str, str]]  # (name, type) in declaration order; `self` receivers omitted
    returns: str
    body_start: int = -1  # index of "{", or -1 for a declaration without body
    body_end: int = -1  # index of the matching "}"
    source: str = field(default="", repr=False)
    masked: str = field(default="", repr=False)

    @property
    def is_command(self) -> bool:
        return any(a.startswith("#[tauri::command") for a in self.attrs)

    @property
    def rename_all(self) -> Optional[str]:
        for attr in self.attrs:
            m = re.search(r'rename_all\s*=\s*"([a-zA-Z_]+)"', attr)
            if m:
                return m.group(1)
        return None

    @property
    def body(self) -> str:
        return self.source[self.body_start : self.body_end + 1] if self.body_start >= 0 else ""

    @property
    def masked_body(self) -> str:
        return self.masked[self.body_start : self.body_end + 1] if self.body_start >= 0 else ""

    def interface_params(self) -> List[Tuple[str, str]]:
        """Parameters the interface sends (Tauri-injected State/AppHandle/Window excluded)."""
        return [(n, t) for n, t in self.params if not INJECTED_TYPE.match(t)]

    def calls(self) -> List[Tuple[str, int]]:
        """(name, offset in body) of every function or method call in the body (macros excluded)."""
        found = []
        for m in re.finditer(r"(?<![\w])(\w+)\s*(?:::\s*<[^<>()]*>)?\s*\(", self.masked_body):
            before = self.masked_body[: m.start()].rstrip()
            if before.endswith("fn") and not before[-3:-2].isalnum():
                continue
            if m.group(1) in {"if", "while", "for", "match", "return", "move", "in", "as", "loop"}:
                continue
            found.append((m.group(1), m.start(1)))
        return found

    def spans(self, callee: str = "spawn_blocking") -> List[Tuple[int, int]]:
        """Body-relative (open, close) of the argument lists of `callee(...)`."""
        spans = []
        for m in re.finditer(rf"(?<![\w]){callee}\s*(?:::\s*<[^<>()]*>)?\s*\(", self.masked_body):
            open_idx = m.end() - 1
            spans.append((open_idx, matching(self.masked_body, open_idx)))
        return spans


def _attributes_above(code: str, start: int) -> List[str]:
    lines = code[:start].rstrip("\n").split("\n")
    attrs: List[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped.startswith("#["):
            break
        attrs.append(stripped)
    return list(reversed(attrs))


def rust_functions(code: str) -> List[RustFn]:
    masked = mask_rust(code)
    items = []
    pattern = re.compile(r"(?m)^[ \t]*(?:pub(?:\([^)\n]*\))?\s+)?(?:const\s+)?(async\s+)?(?:unsafe\s+)?"
                         r"(?:extern\s+\"_*\"\s+)?fn\s+(\w+)")
    for m in pattern.finditer(masked):
        name = m.group(2)
        pos = m.end()
        # Generic parameters: skip a balanced <...> (no "->" inside a generic list).
        rest = masked[pos:]
        lead = len(rest) - len(rest.lstrip())
        pos += lead
        if masked[pos] == "<":
            depth = 0
            while True:
                if masked[pos] == "<":
                    depth += 1
                elif masked[pos] == ">" and masked[pos - 1] != "-":  # "FnOnce() -> T" inside a bound
                    depth -= 1
                    if depth == 0:
                        pos += 1
                        break
                pos += 1
            while masked[pos].isspace():
                pos += 1
        if masked[pos] != "(":
            continue
        close = matching(masked, pos)
        params = []
        for part in split_top_level(masked[pos + 1 : close]):
            if re.fullmatch(r"&?\s*(?:'\w+\s+)?(?:mut\s+)?self", part) or part.startswith("self:"):
                continue
            pm = re.fullmatch(r"(?:mut\s+)?(\w+)\s*:\s*(.+)", part, re.DOTALL)
            if pm:
                params.append((pm.group(1), " ".join(pm.group(2).split())))
        j = close + 1
        while j < len(masked) and masked[j] not in "{;":
            j += 1
        returns = masked[close + 1 : j].strip()
        returns = re.sub(r"^->\s*", "", returns)
        returns = re.split(r"\bwhere\b", returns)[0].strip()
        fn = RustFn(name=name, start=m.start(), attrs=_attributes_above(code, m.start()),
                    is_async=bool(m.group(1)), params=params, returns=" ".join(returns.split()),
                    source=code, masked=masked)
        if j < len(masked) and masked[j] == "{":
            fn.body_start, fn.body_end = j, matching(masked, j)
        items.append(fn)
    return items


def _inside(offset: int, spans: List[Tuple[int, int]]) -> bool:
    return any(a < offset < b for a, b in spans)


@dataclass
class IpcAnalysis:
    functions: List[RustFn]
    blocking: Set[str]  # functions that run a Core round trip on the calling thread
    offloaded: Set[str]  # helpers whose every round trip runs inside spawn_blocking
    commands: Dict[str, RustFn]
    wrappers: Set[str] = field(default_factory=set)  # helpers that call spawn_blocking (may receive a closure)

    def offload_spans(self, fn: RustFn) -> List[Tuple[int, int]]:
        """Argument lists that run off the calling thread: spawn_blocking(...) and calls of spawn_blocking wrappers."""
        spans = fn.spans()
        for wrapper in self.wrappers:
            spans += fn.spans(wrapper)
        return spans

    def calls_outside_offload(self, fn: RustFn) -> List[Tuple[str, int]]:
        spans = self.offload_spans(fn)
        return [(n, p) for n, p in self.blocking_calls(fn) if not _inside(p, spans)]

    def helpers_of(self, fn: RustFn) -> List[RustFn]:
        """Non-command functions of main.rs called (transitively) from `fn`."""
        by_name: Dict[str, List[RustFn]] = {}
        for item in self.functions:
            if not item.is_command and item.body_start >= 0:
                by_name.setdefault(item.name, []).append(item)
        seen: Set[str] = set()
        pending = [name for name, _ in fn.calls()]
        out: List[RustFn] = []
        while pending:
            name = pending.pop()
            if name in seen or name in {"drop", "main"} or name not in by_name:
                continue
            seen.add(name)
            for item in by_name[name]:
                out.append(item)
                pending.extend(n for n, _ in item.calls())
        return out

    def expanded_body(self, fn: RustFn) -> str:
        return "\n".join([fn.body] + [h.body for h in self.helpers_of(fn)])

    def blocking_calls(self, fn: RustFn) -> List[Tuple[str, int]]:
        return [(n, p) for n, p in fn.calls() if n in self.blocking]

    def offloaded_calls(self, fn: RustFn) -> List[Tuple[str, int]]:
        return [(n, p) for n, p in fn.calls() if n in self.offloaded]

    def uses_ipc(self, fn: RustFn) -> bool:
        return bool(self.blocking_calls(fn) or self.offloaded_calls(fn))


def analyze_ipc(code: str) -> IpcAnalysis:
    functions = rust_functions(code)
    helpers = [f for f in functions if not f.is_command and f.body_start >= 0 and f.name not in {"drop", "main"}]
    wrappers = {f.name for f in helpers if f.spans()}
    blocking = set(IPC_SEED)
    while True:
        grown = set(blocking)
        for f in helpers:
            if f.name in IPC_SEED:
                continue
            spans = f.spans()
            for wrapper in wrappers:
                spans += f.spans(wrapper)
            if any(n in blocking and not _inside(p, spans) for n, p in f.calls()):
                grown.add(f.name)
        if grown == blocking:
            break
        blocking = grown
    offloaded: Set[str] = set()
    while True:
        grown = set(offloaded)
        for f in helpers:
            if f.name in blocking:
                continue
            if any(n in blocking or n in offloaded for n, _ in f.calls()):
                grown.add(f.name)
        if grown == offloaded:
            break
        offloaded = grown
    offloaded |= wrappers - blocking
    commands = {f.name: f for f in functions if f.is_command}
    return IpcAnalysis(functions=functions, blocking=blocking, offloaded=offloaded, commands=commands,
                       wrappers=wrappers - blocking)


def camel(name: str) -> str:
    """Tauri 2's default argument rename (snake_case -> camelCase)."""
    return re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), name)


def expected_keys(fn: RustFn) -> Dict[str, Tuple[str, bool]]:
    """Interface key -> (Rust parameter, optional) for a command."""
    keys = {}
    for name, typ in fn.interface_params():
        key = name if fn.rename_all == "snake_case" else camel(name)
        keys[key] = (name, bool(OPTION_TYPE.match(typ)))
    return keys


# ---------------------------------------------------------------------------------------------------------------------
# TypeScript (interface)
# ---------------------------------------------------------------------------------------------------------------------

JS_CALL_PATTERNS = (
    re.compile(r"""\bcallTauri\(\s*(["'])([a-z_0-9]+)\1"""),
    re.compile(r"""\binvoke(?:<[^>()]*>)?\(\s*(["'])([a-z_0-9]+)\1"""),
    re.compile(r"""\(\s*callTauri\s*,\s*(["'])([a-z_0-9]+)\1"""),
)


def _skip_js_literal(text: str, i: int) -> int:
    """Index just past the string, template literal or comment starting at `i`; `i` itself when there is none."""
    ch = text[i]
    if text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return len(text) if end < 0 else end + 2
    if ch in "'\"":
        j = i + 1
        while j < len(text) and text[j] != ch and text[j] != "\n":
            j += 2 if text[j] == "\\" else 1
        return j + 1
    if ch == "`":
        j = i + 1
        while j < len(text) and text[j] != "`":
            if text[j] == "\\":
                j += 2
            elif text.startswith("${", j):
                j = js_balanced(text, j + 1) + 1
            else:
                j += 1
        return j + 1
    return i


def js_balanced(text: str, start: int) -> int:
    """Index of the bracket closing the one at `start`, skipping strings, templates and comments."""
    depth, i = 0, start
    while i < len(text):
        nxt = _skip_js_literal(text, i)
        if nxt != i:
            i = nxt
            continue
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError(f"unbalanced bracket at offset {start}")


def _js_split(text: str) -> List[str]:
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        nxt = _skip_js_literal(text, i)
        if nxt != i:
            i = nxt
            continue
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _strip_js_comments(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        nxt = _skip_js_literal(text, i)
        if nxt != i:
            if not text.startswith(("//", "/*"), i):
                out.append(text[i:nxt])
            i = nxt
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


@dataclass
class ObjectKeys:
    keys: Set[str] = field(default_factory=set)  # always sent
    conditional: Set[str] = field(default_factory=set)  # sent only by a conditional spread
    unresolved: List[str] = field(default_factory=list)  # entries whose key cannot be read statically


def js_object_keys(literal: str) -> ObjectKeys:
    """Keys of a JS object literal `{...}`. Keys inside spreads (`...(cond ? { a } : {})`) are conditional."""
    result = ObjectKeys()
    inner = literal.strip()
    assert inner.startswith("{") and inner.endswith("}"), literal
    for entry in _js_split(inner[1:-1]):
        entry = _strip_js_comments(entry).strip()
        if not entry:
            continue
        if entry.startswith("..."):
            spread = entry[3:]
            found = False
            i = 0
            while i < len(spread):
                nxt = _skip_js_literal(spread, i)
                if nxt != i:
                    i = nxt
                    continue
                if spread[i] == "{":
                    end = js_balanced(spread, i)
                    nested = js_object_keys(spread[i : end + 1])
                    result.conditional |= nested.keys | nested.conditional
                    result.unresolved += nested.unresolved
                    found = True
                    i = end + 1
                    continue
                i += 1
            if not found:
                result.unresolved.append(entry)
            continue
        m = re.match(r"""(?:(["'])(?P<q>[^"'\\]+)\1|(?P<id>[A-Za-z_$][\w$]*))\s*(?P<rest>:[\s\S]*)?$""", entry)
        if not m or (m.group("id") and m.group("rest") is None and not re.fullmatch(r"[A-Za-z_$][\w$]*", entry)):
            result.unresolved.append(entry)
            continue
        result.keys.add(m.group("q") or m.group("id"))
    return result


@dataclass
class CallSite:
    file: str
    line: int
    command: str
    has_args: bool
    args: ObjectKeys

    def where(self) -> str:
        return f"{self.file}:{self.line}"


def interface_sources() -> Iterator[Path]:
    for source in sorted(INTERFACE_SRC.rglob("*.ts*")):
        if source.suffix not in (".ts", ".tsx") or re.search(r"\.(test|spec)\.tsx?$", source.name):
            continue
        yield source


def interface_call_sites() -> List[CallSite]:
    """Every call of a host command with a literal name in the interface (tests excluded), with its argument keys."""
    sites = []
    for source in interface_sources():
        text = source.read_text(encoding="utf-8")
        rel = source.relative_to(ROOT).as_posix()
        for pattern in JS_CALL_PATTERNS:
            for m in pattern.finditer(text):
                i = m.end()
                while i < len(text):
                    nxt = _skip_js_literal(text, i)
                    if nxt != i and text.startswith(("//", "/*"), i):
                        i = nxt
                    elif text[i].isspace():
                        i += 1
                    else:
                        break
                args = ObjectKeys()
                has_args = False
                if text[i] == ",":
                    i += 1
                    while text[i].isspace():
                        i += 1
                    if text[i] == "{":
                        end = js_balanced(text, i)
                        args, has_args = js_object_keys(text[i : end + 1]), True
                    elif text[i] != ")":
                        has_args = True
                        args.unresolved.append(text[i : text.find("\n", i)].strip())
                sites.append(CallSite(file=rel, line=text.count("\n", 0, m.start()) + 1, command=m.group(2),
                                      has_args=has_args, args=args))
    return sites
