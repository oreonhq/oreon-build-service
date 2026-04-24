"""Topological build order from distgit spec files (GitHub)."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from oreon_build.config import get_settings

logger = logging.getLogger(__name__)

# Match BuildRequires name token (simplified, same spirit as dependencies_graph)
_BR_NAME = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9+._-]*)")


@dataclass
class DistgitBuildOrderResult:
    releasename: str
    distgit_url: str
    distgit_branch: str
    package_names: list[str]
    edges: list[dict[str, str]]  # { "from": consumer, "to": provider } consumer needs provider
    layers: list[list[str]]  # topological levels (parallel groups)
    errors: list[str]
    cycle_warning: bool


def _github_owner_repo_from_git_url(url: str) -> tuple[str, str] | None:
    s = (url or "").split("#", 1)[0].strip().rstrip("/")
    if s.lower().endswith(".git"):
        s = s[:-4]
    p = urlparse(s)
    if p.netloc not in ("github.com", "www.github.com") or not p.path:
        return None
    path = p.path.strip("/")
    parts = [x for x in path.split("/") if x]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _br_tokens_from_spec_file(path: str) -> list[str]:
    try:
        r = subprocess.run(
            ["rpmspec", "-q", "--buildrequires", path],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except FileNotFoundError:
        return _br_tokens_from_spec_file_fallback_read(path)
    if r.returncode != 0:
        logger.warning("rpmspec --buildrequires failed for %s: %s", path, (r.stderr or r.stdout)[:200])
        return _br_tokens_from_spec_file_fallback_read(path)
    names: list[str] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("("):
            continue
        m = _BR_NAME.match(line)
        if m:
            tok = m.group(1)
            if tok.startswith("rpmlib(") or tok.startswith("/"):
                continue
            names.append(tok)
    return names


def _br_tokens_from_spec_file_fallback_read(path: str) -> list[str]:
    names: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s.upper().startswith("BUILDREQUIRES:") and not s.upper().startswith("BUILDREQUIRES "):
                    continue
                if ":" in s:
                    val = s.split(":", 1)[1]
                else:
                    val = s
                m = _BR_NAME.match((val or "").strip())
                if m:
                    names.append(m.group(1))
    except OSError as e:
        logger.warning("fallback spec read %s: %s", path, e)
    return names


def _kahn_layers(
    names: set[str], deps: dict[str, set[str]], rdeps: dict[str, set[str]]
) -> tuple[list[list[str]], bool]:
    """deps[a] = predecessor packages a needs built first. Kahn; on cycle, peel one node per round."""
    remaining = set(names)
    in_deg = {n: len(deps.get(n, set())) for n in names}
    layers: list[list[str]] = []
    cycle = False
    while remaining:
        ready = sorted(n for n in remaining if in_deg.get(n, 0) == 0)
        if not ready:
            cycle = True
            ready = [sorted(remaining)[0]]
        layers.append(ready)
        for n in ready:
            remaining.discard(n)
            for d in rdeps.get(n, set()):
                if d in remaining:
                    in_deg[d] = max(0, in_deg.get(d, 0) - 1)
    return layers, cycle


async def compute_distgit_build_order(
    *,
    releasename: str,
    distgit_url: str = "https://github.com/oreonhq/rpm-specfiles.git",
    distgit_branch: str = "main",
) -> DistgitBuildOrderResult:
    """List packages under ``releasename/`` in the GitHub repo, parse each ``pkg/pkg.spec``, return topo order."""
    err: list[str] = []
    gr = _github_owner_repo_from_git_url(distgit_url)
    if not gr:
        return DistgitBuildOrderResult(
            releasename=releasename,
            distgit_url=distgit_url,
            distgit_branch=distgit_branch,
            package_names=[],
            edges=[],
            layers=[],
            errors=[f"distgit_url is not a supported github.com URL: {distgit_url!r}"],
            cycle_warning=False,
        )
    owner, repo = gr
    settings = get_settings()
    token = (getattr(settings, "github_token", None) or os.environ.get("GITHUB_TOKEN") or "").strip() or None
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    api_base = f"https://api.github.com/repos/{owner}/{repo}/contents/{releasename}"
    async with httpx.AsyncClient(timeout=120, headers=headers) as client:
        r = await client.get(api_base, params={"ref": distgit_branch})
        if r.status_code != 200:
            err.append(
                f"GitHub list {releasename} failed: HTTP {r.status_code} {r.text[:300]!r}. "
                "Set GITHUB_TOKEN in the environment if the repo is private or rate-limited."
            )
            return DistgitBuildOrderResult(
                releasename=releasename,
                distgit_url=distgit_url,
                distgit_branch=distgit_branch,
                package_names=[],
                edges=[],
                layers=[],
                errors=err,
                cycle_warning=False,
            )
        data = r.json()
        if not isinstance(data, list):
            err.append("Unexpected GitHub API response (not a list of directory items)")
            return DistgitBuildOrderResult(
                releasename=releasename,
                distgit_url=distgit_url,
                distgit_branch=distgit_branch,
                package_names=[],
                edges=[],
                layers=[],
                errors=err,
                cycle_warning=False,
            )
        subdirs: list[dict[str, Any]] = [x for x in data if x.get("type") == "dir" and x.get("name")]

    raw_urls: list[tuple[str, str]] = []
    async with httpx.AsyncClient(timeout=300, headers=headers) as client:
        for d in subdirs:
            pkg = d.get("name")
            if not isinstance(pkg, str) or not pkg:
                continue
            u = f"https://raw.githubusercontent.com/{owner}/{repo}/{distgit_branch}/{releasename}/{pkg}/{pkg}.spec"
            rb = await client.get(u)
            if rb.status_code == 200:
                raw_urls.append((pkg, rb.text))
            else:
                err.append(f"{pkg}: no {pkg}.spec at expected path (HTTP {rb.status_code})")

    if not raw_urls and not err:
        err.append(f"No packages with specs found under {releasename}/ on branch {distgit_branch}")

    names_set: set[str] = {a for a, _ in raw_urls}
    br_map: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="oreon-distgit-") as tmp:
        for pkg, spec_text in raw_urls:
            p = os.path.join(tmp, f"{pkg}.spec")
            with open(p, "w", encoding="utf-8") as f:
                f.write(spec_text)
            br_map[pkg] = _br_tokens_from_spec_file(p)

    edges: list[dict[str, str]] = []
    deps: dict[str, set[str]] = {n: set() for n in names_set}
    rdeps: dict[str, set[str]] = {n: set() for n in names_set}

    for cons, brs in br_map.items():
        for br in brs:
            if br in names_set and br != cons:
                # cons depends on br (br must be built first)
                deps[cons].add(br)
                rdeps[br].add(cons)
                edges.append({"from": cons, "to": br, "satisfied": False, "type": "distgit_br"})

    # Transitive trim for layer sort only
    layers, cyc = _kahn_layers(names_set, deps, rdeps)
    if cyc:
        err.append("Warning: a cycle exists in distgit BuildRequires; some parallel layers may be conservative.")

    return DistgitBuildOrderResult(
        releasename=releasename,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        package_names=sorted(names_set),
        edges=edges,
        layers=layers,
        errors=err,
        cycle_warning=cyc,
    )
