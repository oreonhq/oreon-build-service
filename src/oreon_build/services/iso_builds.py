# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from oreon_build.config import get_settings

_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_KS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,180}\.ks$")
_ISO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,180}\.iso$")
_ARCHES = {"x86_64", "aarch64"}


def kickstart_repo_slug(url: str | None = None) -> tuple[str, str]:
    raw = (url or get_settings().kickstart_git_url or "").strip()
    path = urlparse(raw).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("kickstart git url is not a github repo")
    return parts[0], parts[1]


def iso_filename_for_arch(raw: str, arch: str) -> str:
    name = (raw or "").strip()
    if name.lower().endswith(".iso"):
        name = name[:-4]
    if "{arch}" in name:
        name = name.replace("{arch}", arch)
    elif arch not in name.split("-"):
        name = f"{name}-{arch}"
    name = f"{name}.iso"
    if not _ISO_RE.fullmatch(name):
        raise ValueError(f"ISO name is not a safe filename: {name}")
    return name


def validate_iso_request(
    *,
    release_folder: str,
    arches: list[str],
    kickstarts: dict[str, str],
    iso_name: str,
    project: str,
    volid: str,
    releasever: str,
) -> list[dict[str, str]]:
    folder = (release_folder or "").strip()
    if not _FOLDER_RE.fullmatch(folder):
        raise ValueError("Release folder is not a safe name")
    clean_arches = []
    for arch in arches:
        a = (arch or "").strip()
        if a not in _ARCHES:
            raise ValueError(f"Unsupported arch: {a}")
        if a not in clean_arches:
            clean_arches.append(a)
    if not clean_arches:
        raise ValueError("Pick at least one arch")
    if len(clean_arches) > 1:
        missing = [a for a in clean_arches if not (kickstarts.get(a) or "").strip()]
        if missing:
            raise ValueError("Each arch needs its own kickstart: " + ", ".join(missing))
        used = [(kickstarts.get(a) or "").strip() for a in clean_arches]
        if len(set(used)) < len(used):
            raise ValueError("Each arch needs a different kickstart")
    rows = []
    for arch in clean_arches:
        ks = (kickstarts.get(arch) or "").strip()
        if not ks and len(clean_arches) == 1:
            only = [v.strip() for v in kickstarts.values() if v and v.strip()]
            ks = only[0] if len(only) == 1 else ""
        if not _KS_RE.fullmatch(ks):
            raise ValueError(f"Kickstart for {arch} is not a .ks file in that folder")
        rows.append(
            {
                "architecture": arch,
                "kickstart": ks,
                "iso_name": iso_filename_for_arch(iso_name, arch),
                "release_folder": folder,
                "project": (project or "Oreon").strip()[:64] or "Oreon",
                "volid": (volid or "").strip()[:32],
                "releasever": (releasever or "").strip()[:16],
            }
        )
        if not rows[-1]["volid"] or not rows[-1]["releasever"]:
            raise ValueError("volid and releasever are required")
    return rows


def mock_config_for_arch(config_name: str, arch: str) -> str:
    base = (config_name or "").strip()
    for suffix in ("-x86_64", "-aarch64"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}-{arch}" if base else arch


def _github_get(path: str) -> Any:
    settings = get_settings()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "oreon-build-service"}
    token = (settings.github_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{settings.github_api_url.rstrip('/')}/repos/{path}"
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def list_kickstart_tree(folder: str | None = None) -> dict[str, Any]:
    owner, repo = kickstart_repo_slug()
    branch = (get_settings().kickstart_git_branch or "main").strip() or "main"
    if folder:
        if not _FOLDER_RE.fullmatch(folder):
            raise ValueError("Release folder is not a safe name")
        data = _github_get(f"{owner}/{repo}/contents/{folder}?ref={branch}")
        if data is None:
            raise ValueError(f"Folder not found in kickstart repo: {folder}")
        if not isinstance(data, list):
            raise ValueError("Kickstart folder is not a directory")
        ks = sorted(item["name"] for item in data if item.get("type") == "file" and str(item.get("name") or "").endswith(".ks"))
        has_templates = any(item.get("type") == "dir" and item.get("name") == "lorax-templates" for item in data)
        return {"folder": folder, "kickstarts": ks, "lorax_templates": has_templates}
    data = _github_get(f"{owner}/{repo}/contents?ref={branch}")
    if data is None:
        return {"folders": [], "repo": f"{owner}/{repo}", "branch": branch, "missing_repo": True}
    if not isinstance(data, list):
        return {"folders": [], "repo": f"{owner}/{repo}", "branch": branch}
    folders = sorted(item["name"] for item in data if item.get("type") == "dir" and _FOLDER_RE.fullmatch(str(item.get("name") or "")))
    return {"folders": folders, "repo": f"{owner}/{repo}", "branch": branch}
