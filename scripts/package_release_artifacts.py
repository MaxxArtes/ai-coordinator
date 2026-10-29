"""Build and validate release candidate artifacts for ai-coordinator 0.5.0.

Produces:
1. dist/ai_coordinator-0.5.0-py3-none-any.whl
2. dist/ai_coordinator-0.5.0.tar.gz
3. dist/ai-coordinator-skill-0.5.0.zip
4. dist/ai-coordinator-claude-plugin-0.5.0.zip
5. dist/maxxartes-ai-coordinator-opencode-0.5.0.tgz
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
VERSION = os.environ.get("RELEASE_VERSION")
if not VERSION:
    import tomllib
    pyproj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    VERSION = pyproj["project"]["version"]



def make_zip(source_dir: Path, out_zip: Path, extra_files: list[Path] = None):
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                p = Path(root) / file
                arc = p.relative_to(source_dir)
                zf.write(p, arc)
        if extra_files:
            for ef in extra_files:
                zf.write(ef, ef.name)


def main():
    DIST.mkdir(parents=True, exist_ok=True)
    license_file = ROOT / "LICENSE"
    notice_file = ROOT / "NOTICE"

    print(f"--- Packaging ai-coordinator v{VERSION} Release Candidate Artifacts ---")

    # 1. Skill ZIP
    with tempfile.TemporaryDirectory() as tmp_skill:
        skill_stage = Path(tmp_skill)
        shutil.copytree(ROOT / "skills" / "ai-coordinator", skill_stage / "ai-coordinator")
        shutil.copy2(license_file, skill_stage / "LICENSE")
        shutil.copy2(notice_file, skill_stage / "NOTICE")
        skill_zip = DIST / f"ai-coordinator-skill-{VERSION}.zip"
        if skill_zip.exists():
            skill_zip.unlink()
        make_zip(skill_stage, skill_zip)
        print(f"[OK] Created {skill_zip.name} ({skill_zip.stat().st_size} bytes)")

    # 2. Claude Plugin ZIP
    with tempfile.TemporaryDirectory() as tmp_claude:
        claude_stage = Path(tmp_claude)
        for item in (ROOT / "plugins" / "claude-code").iterdir():
            if item.is_dir():
                shutil.copytree(item, claude_stage / item.name)
            else:
                shutil.copy2(item, claude_stage / item.name)
        shutil.copy2(license_file, claude_stage / "LICENSE")
        shutil.copy2(notice_file, claude_stage / "NOTICE")
        claude_zip = DIST / f"ai-coordinator-claude-plugin-{VERSION}.zip"
        if claude_zip.exists():
            claude_zip.unlink()
        make_zip(claude_stage, claude_zip)
        print(f"[OK] Created {claude_zip.name} ({claude_zip.stat().st_size} bytes)")

    # 3. OpenCode npm tarball
    opencode_src = ROOT / "plugins" / "opencode"
    cmd = ["npm", "pack", str(opencode_src), "--pack-destination", str(DIST)]
    res = subprocess.run(cmd, capture_output=True, text=True, shell=os.name == "nt")
    if res.returncode != 0:
        print(f"[FAIL] npm pack failed: {res.stderr}", file=sys.stderr)
        sys.exit(1)
    tgz_name = f"maxxartes-ai-coordinator-opencode-{VERSION}.tgz"
    opencode_tgz = DIST / tgz_name
    if not opencode_tgz.exists():
        print(f"[FAIL] {opencode_tgz} not found after npm pack", file=sys.stderr)
        sys.exit(1)
    print(f"[OK] Created {opencode_tgz.name} ({opencode_tgz.stat().st_size} bytes)")

    # 4. Verification of All 5 Artifacts
    print("\n--- Verifying All Artifact Contents ---")

    # 4.1 Wheel
    whl = DIST / f"ai_coordinator-{VERSION}-py3-none-any.whl"
    assert whl.exists(), f"Missing {whl}"
    with zipfile.ZipFile(whl, "r") as zf:
        namelist = zf.namelist()
        assert any("ai_coordinator/__init__.py" in n for n in namelist)
        assert any("ai_coordinator/fs_sandbox.py" in n for n in namelist)
        assert any("ai_coordinator/tool_broker.py" in n for n in namelist)
        assert any("ai_coordinator/worker_audit.py" in n for n in namelist)
        metadata = [n for n in namelist if n.endswith("METADATA")][0]
        meta_txt = zf.read(metadata).decode("utf-8")
        assert f"Version: {VERSION}" in meta_txt
        assert "PolyForm-Noncommercial-1.0.0" in meta_txt
    print(f"[OK] Wheel verified: {whl.name} ({len(namelist)} files)")

    # 4.2 Sdist
    sdist = DIST / f"ai_coordinator-{VERSION}.tar.gz"
    assert sdist.exists(), f"Missing {sdist}"
    with tarfile.open(sdist, "r:gz") as tf:
        names = tf.getnames()
        assert any("pyproject.toml" in n for n in names)
        assert any("LICENSE" in n for n in names)
        assert any("NOTICE" in n for n in names)
        assert any("src/ai_coordinator/__init__.py" in n for n in names)
    print(f"[OK] Sdist verified: {sdist.name} ({len(names)} files)")

    # 4.3 Skill ZIP
    with zipfile.ZipFile(skill_zip, "r") as zf:
        namelist = zf.namelist()
        assert "ai-coordinator/SKILL.md" in namelist
        assert "LICENSE" in namelist
        assert "NOTICE" in namelist
    print(f"[OK] Skill ZIP verified: {skill_zip.name} ({len(namelist)} files)")

    # 4.4 Claude Plugin ZIP
    with zipfile.ZipFile(claude_zip, "r") as zf:
        namelist = zf.namelist()
        assert ".claude-plugin/plugin.json" in namelist
        assert "skills/ai-coordinator/SKILL.md" in namelist
        assert "LICENSE" in namelist
        assert "NOTICE" in namelist
        plug_json = json.loads(zf.read(".claude-plugin/plugin.json").decode("utf-8"))
        assert plug_json["version"] == VERSION
    print(f"[OK] Claude Code Plugin ZIP verified: {claude_zip.name} ({len(namelist)} files)")

    # 4.5 OpenCode Tarball
    with tarfile.open(opencode_tgz, "r:gz") as tf:
        names = tf.getnames()
        assert "package/package.json" in names
        assert "package/skills/ai-coordinator/SKILL.md" in names
        assert "package/LICENSE" in names
        assert "package/NOTICE" in names
        f = tf.extractfile("package/package.json")
        pkg_json = json.loads(f.read().decode("utf-8"))
        assert pkg_json["version"] == VERSION
    print(f"[OK] OpenCode npm tarball verified: {opencode_tgz.name} ({len(names)} files)")

    print("\n[SUCCESS] All 5 release candidate artifacts packaged and verified successfully!")


if __name__ == "__main__":
    main()
