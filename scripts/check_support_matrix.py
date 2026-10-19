"""Validate the published support claims and integration artifacts."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_STATUSES = {"supported", "packaged", "documented", "experimental", "not-claimed"}
ALLOWED_VERIFICATION = {
    "ci-matrix", "ci-smoke", "ci-extra", "ci-artifact", "manual-host-check", "none"
}


def load_json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def check() -> list[str]:
    errors: list[str] = []
    matrix = load_json("docs/support-matrix.json")
    if matrix.get("schema_version") != 1:
        errors.append("docs/support-matrix.json precisa de schema_version=1")
    entries = matrix.get("platforms", []) + matrix.get("integrations", [])
    identifiers: set[str] = set()
    for entry in entries:
        ident = entry.get("id")
        if not isinstance(ident, str) or ident in identifiers:
            errors.append(f"id ausente ou duplicado na matriz: {ident!r}")
        identifiers.add(ident)
        if entry.get("status") not in ALLOWED_STATUSES:
            errors.append(f"status invalido em {ident}")
        if entry.get("verification") not in ALLOWED_VERIFICATION:
            errors.append(f"tipo de verificacao invalido em {ident}")
        for relative in entry.get("files", []):
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                errors.append(f"caminho fora do repositorio em {ident}: {relative}")
            elif not (ROOT / path).is_file():
                errors.append(f"arquivo de evidencia ausente em {ident}: {relative}")

    expected = {
        "python-library-cli", "mcp-stdio", "agent-skill", "claude-code-plugin",
        "opencode-v2-plugin", "codex-generic-mcp-skill", "agy-generic-mcp-skill",
        "opencode-v1-plugin",
    }
    if expected - identifiers:
        errors.append("matriz sem entradas: " + ", ".join(sorted(expected - identifiers)))

    skill_paths = [
        ROOT / "skills/ai-coordinator/SKILL.md",
        ROOT / "plugins/claude-code/skills/ai-coordinator/SKILL.md",
        ROOT / "plugins/opencode/skills/ai-coordinator/SKILL.md",
    ]
    skill_contents = [path.read_bytes() for path in skill_paths]
    if len(set(skill_contents)) != 1:
        errors.append("As tres copias de SKILL.md precisam permanecer identicas")

    for relative in (
        ".claude-plugin/marketplace.json",
        "plugins/claude-code/.claude-plugin/plugin.json",
        "plugins/claude-code/.mcp.json",
        "plugins/opencode/package.json",
        "src/ai_coordinator/routes.example.json",
    ):
        try:
            load_json(relative)
        except (OSError, ValueError) as exc:
            errors.append(f"JSON invalido em {relative}: {exc}")

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = project.get("project", {})
    version = metadata.get("version")
    if metadata.get("license") != "PolyForm-Noncommercial-1.0.0":
        errors.append("pyproject.toml precisa declarar a licenca PolyForm-Noncommercial-1.0.0")
    if not {"LICENSE", "NOTICE"}.issubset(set(metadata.get("license-files", []))):
        errors.append("pyproject.toml precisa empacotar LICENSE e NOTICE")
    if not (ROOT / "LICENSE").is_file() or not (ROOT / "NOTICE").is_file():
        errors.append("LICENSE e NOTICE precisam existir")
    for relative in (
        "plugins/claude-code/LICENSE", "plugins/opencode/LICENSE",
    ):
        if (ROOT / relative).is_file() and (ROOT / relative).read_bytes() != (ROOT / "LICENSE").read_bytes():
            errors.append(f"Licenca divergente em {relative}")
    for relative in (
        "plugins/claude-code/NOTICE", "plugins/opencode/NOTICE",
    ):
        if (ROOT / relative).is_file() and (ROOT / relative).read_bytes() != (ROOT / "NOTICE").read_bytes():
            errors.append(f"Aviso legal divergente em {relative}")
    if version:
        claude = load_json("plugins/claude-code/.claude-plugin/plugin.json")
        opencode = load_json("plugins/opencode/package.json")
        marketplace = load_json(".claude-plugin/marketplace.json")
        marketplace_plugin = (marketplace.get("plugins") or [{}])[0]
        if any(item.get("version") != version for item in (claude, opencode, marketplace_plugin)):
            errors.append("Versoes Python, Claude plugin, OpenCode plugin e marketplace divergem")
        if opencode.get("license") != "PolyForm-Noncommercial-1.0.0":
            errors.append("package.json do OpenCode precisa declarar a mesma licenca")
    if '"mode": "allowlist"' not in (ROOT / "src/ai_coordinator/routes.example.json").read_text(encoding="utf-8"):
        errors.append("routes.example.json deve iniciar com ambiente de worker em allowlist")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "docs/support-matrix.md" not in readme:
        errors.append("README.md deve apontar para docs/support-matrix.md")
    return errors


if __name__ == "__main__":
    failures = check()
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        raise SystemExit(1)
    print("Support matrix and package artifacts are consistent.")
