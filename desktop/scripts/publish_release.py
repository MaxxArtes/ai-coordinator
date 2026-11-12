#!/usr/bin/env python3
"""Publish AI Coordinator Desktop Alpha Release to GitHub Releases and update manifest.

1. Identifies release version from tauri.conf.json.
2. Locates NSIS installer (.exe) and portable package (.zip).
3. Ensures valid Minisign / Ed25519 signature (.sig).
4. Creates / updates GitHub Pre-Release via GitHub Releases API.
5. Uploads release assets (.exe, .zip, .sig).
6. Updates desktop/updates/alpha.json with permanent GitHub Release URLs and real cryptographic signature.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Strip Authorization header on redirect to prevent Azure 401."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        r = super().redirect_request(req, fp, code, msg, headers, newurl)
        if "Authorization" in r.headers:
            del r.headers["Authorization"]
        return r


def get_github_token() -> str:
    """Retrieve GitHub token from env or git credential helper."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        p = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n",
            capture_output=True,
            text=True,
            check=True,
        )
        data = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
        token = data.get("password")
        if token:
            return token
    except Exception as e:
        print(f"Warning: Could not get git credentials: {e}")
    raise RuntimeError("No GitHub token available. Set GITHUB_TOKEN or authenticate git.")


def get_app_version() -> str:
    tauri_conf = _repo_root / "desktop" / "src-tauri" / "tauri.conf.json"
    data = json.loads(tauri_conf.read_text(encoding="utf-8"))
    return data.get("version", "0.6.1-alpha.48")


def sign_file(file_path: Path, app_version: str) -> str:
    """Sign file using local Minisign key or return existing .sig content."""
    sig_file = file_path.parent / f"{file_path.name}.sig"
    if sig_file.exists():
        return sig_file.read_text(encoding="utf-8").strip()

    key_path = Path.home() / ".tauri" / "ai_coordinator_updater.key"
    if not key_path.exists():
        raise FileNotFoundError(f"Cannot sign: private key not found at {key_path}")

    print(f"Signing {file_path.name} with Minisign...")
    npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"
    cmd = [
        npx_cmd,
        "@tauri-apps/cli",
        "signer",
        "sign",
        "-f",
        str(key_path),
        "--app-version",
        app_version,
        str(file_path),
    ]
    subprocess.run(cmd, cwd=str(_repo_root / "desktop"), check=True)
    if not sig_file.exists():
        raise RuntimeError(f"Signer did not produce {sig_file}")
    return sig_file.read_text(encoding="utf-8").strip()


def github_api_call(url: str, token: str, method: str = "GET", data: bytes | None = None, content_type: str = "application/json") -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "AI-Coordinator-Desktop-Publisher",
        "Accept": "application/vnd.github+json",
    }
    if data is not None:
        headers["Content-Type"] = content_type

    opener = urllib.request.build_opener(NoAuthRedirect)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with opener.open(req) as resp:
        if resp.status == 204:
            return {}
        return json.loads(resp.read().decode("utf-8"))


def create_or_get_release(repo: str, tag: str, name: str, body: str, token: str, target_commitish: Optional[str] = None) -> dict:
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    try:
        release = github_api_call(url, token, method="GET")
        print(f"Found existing release for tag {tag}: ID {release['id']}")
        return release
    except Exception:
        pass

    # Create new release
    print(f"Creating new GitHub Pre-Release: {tag} ({name})...")
    create_url = f"https://api.github.com/repos/{repo}/releases"
    payload_dict = {
        "tag_name": tag,
        "name": name,
        "body": body,
        "draft": False,
        "prerelease": True,
    }
    if target_commitish:
        payload_dict["target_commitish"] = target_commitish
    payload = json.dumps(payload_dict).encode("utf-8")
    return github_api_call(create_url, token, method="POST", data=payload)


def upload_release_asset(repo: str, release_id: int, file_path: Path, token: str) -> str:
    """Upload asset to release, replacing if already exists."""
    name = file_path.name
    # List existing assets
    assets_url = f"https://api.github.com/repos/{repo}/releases/{release_id}/assets"
    existing = github_api_call(assets_url, token, method="GET")
    for a in existing:
        if a["name"] == name:
            print(f"Deleting existing asset {name} (ID: {a['id']})...")
            del_url = f"https://api.github.com/repos/{repo}/releases/assets/{a['id']}"
            github_api_call(del_url, token, method="DELETE")

    print(f"Uploading asset {name} ({file_path.stat().st_size:,} bytes)...")
    upload_url = f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={urllib.parse.quote(name)}"
    data = file_path.read_bytes()
    res = github_api_call(upload_url, token, method="POST", data=data, content_type="application/octet-stream")
    download_url = res.get("browser_download_url") or f"https://github.com/{repo}/releases/download/v{release_id}/{name}"
    print(f"Uploaded: {download_url}")
    return download_url


def update_manifest(version: str, notes: str, download_url: str, signature: str) -> None:
    manifest_path = _repo_root / "desktop" / "updates" / "alpha.json"
    manifest = {
        "version": version,
        "notes": notes,
        "pub_date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": {
            "windows-x86_64": {
                "signature": signature,
                "url": download_url,
            }
        }
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated manifest at {manifest_path}: version={version}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish Alpha Release and Manifest")
    parser.add_argument("--version", default=None)
    parser.add_argument("--repo", default="MaxxArtes/ai-coordinator")
    parser.add_argument("--installer", type=Path, default=None)
    parser.add_argument("--portable", type=Path, default=None)
    parser.add_argument("--notes", default="AI Coordinator Desktop Alpha Release com auto-atualizador e terminais ConPTY.")
    args = parser.parse_args()

    version = args.version or get_app_version()
    tag = f"v{version}"
    token = get_github_token()

    try:
        target_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=str(_repo_root)).strip()
    except Exception:
        target_sha = None

    print(f"=== Publishing AI Coordinator Desktop Alpha: {version} (target: {target_sha}) ===")
    release = create_or_get_release(
        repo=args.repo,
        tag=tag,
        name=f"AI Coordinator Desktop Alpha v{version}",
        body=args.notes,
        token=token,
        target_commitish=target_sha,
    )
    release_id = release["id"]

    # Locate installer
    installer = args.installer
    if not installer:
        candidates = list((_repo_root / "desktop" / "src-tauri" / "target" / "release" / "bundle" / "nsis").glob("*.exe"))
        if candidates:
            installer = candidates[0]

    if not installer or not installer.exists():
        print(f"Error: Installer not found in target/release/bundle/nsis. Please supply --installer.")
        return 1

    # Sign installer
    signature = sign_file(installer, version)

    # Upload installer
    installer_url = upload_release_asset(args.repo, release_id, installer, token)

    # Upload signature file
    sig_file = installer.parent / f"{installer.name}.sig"
    if sig_file.exists():
        upload_release_asset(args.repo, release_id, sig_file, token)

    # Upload portable if present
    portable = args.portable
    if not portable:
        p_cand = _repo_root / "desktop" / "src-tauri" / "target" / "release" / "ai-coordinator-desktop-alpha-portable.zip"
        if p_cand.exists():
            portable = p_cand
    if portable and portable.exists():
        upload_release_asset(args.repo, release_id, portable, token)

    # Update manifest
    update_manifest(version, args.notes, installer_url, signature)

    print("=== Release Published and Manifest Updated Successfully ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
