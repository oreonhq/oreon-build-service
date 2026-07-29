"""Drop crate BuildRequires that only apply to foreign rustc targets."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_RE_CRATE = re.compile(r"crate\(([^)/\s]+)(?:/[^)]*)?\)", re.IGNORECASE)

_ARCH_TO_TRIPLE = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "amd64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
    "arm64": "aarch64-unknown-linux-gnu",
    "i686": "i686-unknown-linux-gnu",
    "i386": "i686-unknown-linux-gnu",
    "ppc64le": "powerpc64le-unknown-linux-gnu",
    "s390x": "s390x-unknown-linux-gnu",
    "riscv64": "riscv64gc-unknown-linux-gnu",
}


def arches_to_rust_triples(arches: set[str] | None) -> list[str]:
    if not arches:
        return ["x86_64-unknown-linux-gnu"]
    out: list[str] = []
    seen: set[str] = set()
    for raw in sorted(arches):
        a = (raw or "").strip().lower()
        if not a:
            continue
        triple = _ARCH_TO_TRIPLE.get(a)
        if not triple:
            if "-unknown-linux-" in a or a.count("-") >= 2:
                triple = a
            else:
                triple = f"{a}-unknown-linux-gnu"
        if triple not in seen:
            seen.add(triple)
            out.append(triple)
    return out or ["x86_64-unknown-linux-gnu"]


@lru_cache(maxsize=32)
def _rustc_cfg(triple: str) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    try:
        proc = subprocess.run(
            ["rustc", "--print", "cfg", "--target", triple],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        logger.warning("rustc --print cfg failed for %s", triple, exc_info=True)
        return frozenset(), frozenset()
    if proc.returncode != 0:
        logger.warning(
            "rustc --print cfg rc=%s for %s: %s",
            proc.returncode,
            triple,
            (proc.stderr or "")[:200],
        )
        return frozenset(), frozenset()
    flags: set[str] = set()
    kvs: set[tuple[str, str]] = set()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            kvs.add((key.strip(), val.strip().strip('"')))
        else:
            flags.add(line)
    return frozenset(flags), frozenset(kvs)


def _split_cfg_args(inner: str) -> list[str]:
    args: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in inner:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            part = "".join(cur).strip()
            if part:
                args.append(part)
            cur = []
        else:
            cur.append(ch)
    part = "".join(cur).strip()
    if part:
        args.append(part)
    return args


def _eval_cfg_expr(
    expr: str,
    flags: frozenset[str],
    kvs: frozenset[tuple[str, str]],
) -> bool | None:
    """True/False if decidable. None if feature/unknown (keep dep)."""
    expr = (expr or "").strip()
    if not expr:
        return None
    if expr.startswith("all(") and expr.endswith(")"):
        parts = _split_cfg_args(expr[4:-1])
        if not parts:
            return True
        vals = [_eval_cfg_expr(p, flags, kvs) for p in parts]
        if any(v is None for v in vals):
            return None
        return all(vals)
    if expr.startswith("any(") and expr.endswith(")"):
        parts = _split_cfg_args(expr[4:-1])
        if not parts:
            return False
        vals = [_eval_cfg_expr(p, flags, kvs) for p in parts]
        if any(v is True for v in vals):
            return True
        if any(v is None for v in vals):
            return None
        return False
    if expr.startswith("not(") and expr.endswith(")"):
        inner = _eval_cfg_expr(expr[4:-1], flags, kvs)
        if inner is None:
            return None
        return not inner
    m = re.match(r'^([A-Za-z0-9_]+)\s*=\s*"([^"]*)"$', expr)
    if m:
        key, val = m.group(1), m.group(2)
        if key == "feature":
            return None
        return (key, val) in kvs
    if re.match(r"^[A-Za-z0-9_]+$", expr):
        return expr in flags
    return None


def target_matches_platform(target: str, triple: str) -> bool | None:
    """Whether a cargo dependency target applies to rustc triple.

    None means unknown (keep the dependency).
    """
    raw = (target or "").strip()
    if not raw:
        return True
    if raw.startswith("cfg(") and raw.endswith(")"):
        flags, kvs = _rustc_cfg(triple)
        if not flags and not kvs:
            return None
        return _eval_cfg_expr(raw[4:-1], flags, kvs)
    return raw == triple


def foreign_crate_names_from_metadata(
    metadata: dict,
    triples: list[str],
) -> set[str]:
    """Crate names that only appear under target cfgs matching none of triples."""
    packages = metadata.get("packages") or []
    if not packages:
        return set()
    needed: set[str] = set()
    foreign: set[str] = set()
    for pkg in packages:
        for dep in pkg.get("dependencies") or []:
            name = (dep.get("rename") or dep.get("name") or "").strip()
            if not name:
                continue
            target = dep.get("target")
            if not target:
                needed.add(name)
                continue
            matched = False
            unknown = False
            for triple in triples:
                hit = target_matches_platform(str(target), triple)
                if hit is True:
                    matched = True
                    break
                if hit is None:
                    unknown = True
            if matched or unknown:
                needed.add(name)
            else:
                foreign.add(name)
    return foreign - needed


def load_cargo_metadata(manifest: Path) -> dict | None:
    try:
        proc = subprocess.run(
            [
                "cargo",
                "metadata",
                "--format-version",
                "1",
                "--no-deps",
                "--offline",
                "--manifest-path",
                str(manifest),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception:
        logger.warning("cargo metadata failed for %s", manifest, exc_info=True)
        return None
    if proc.returncode != 0:
        logger.warning(
            "cargo metadata rc=%s for %s: %s",
            proc.returncode,
            manifest,
            (proc.stderr or "")[:300],
        )
        return None
    try:
        return json.loads(proc.stdout or "")
    except Exception:
        logger.warning("cargo metadata json parse failed for %s", manifest, exc_info=True)
        return None


def crate_names_in_requirement(requirement: str) -> set[str]:
    return {m.group(1).strip() for m in _RE_CRATE.finditer(requirement or "") if m.group(1)}


def filter_foreign_target_crate_brs(
    brs: list[str],
    *,
    cargo_tomls: list[Path],
    arches: set[str] | None,
) -> list[str]:
    """Remove crate() BRs that cargo only needs for non-matching target cfgs."""
    triples = arches_to_rust_triples(arches)
    drop: set[str] = set()
    for manifest in cargo_tomls:
        if not manifest or not manifest.is_file():
            continue
        meta = load_cargo_metadata(manifest)
        if not meta:
            continue
        drop |= foreign_crate_names_from_metadata(meta, triples)
    if not drop:
        return list(brs)
    out: list[str] = []
    for br in brs:
        names = crate_names_in_requirement(br)
        if names and names <= drop:
            continue
        if names and (names & drop) and not (names - drop):
            continue
        out.append(br)
    logger.info(
        "filtered foreign-target crate BRs drop=%s kept=%s/%s triples=%s",
        sorted(drop),
        len(out),
        len(brs),
        triples,
    )
    return out


def find_cargo_tomls(build_root: Path) -> list[Path]:
    if not build_root.is_dir():
        return []
    return sorted(p for p in build_root.rglob("Cargo.toml") if p.is_file())
