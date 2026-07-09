from __future__ import annotations

import re
from dataclasses import dataclass, field

_RE_BCOND_WITH = re.compile(r"^%\s*bcond_with\s+(\S+)", re.IGNORECASE)
_RE_BCOND_WITHOUT = re.compile(r"^%\s*bcond_without\s+(\S+)", re.IGNORECASE)
_RE_IF_WITH = re.compile(r"%if\s+.*?%\{?\??with_([a-zA-Z0-9_]+)", re.IGNORECASE)
_RE_BR_LINE = re.compile(r"^buildrequires\s*:\s*(.+)$", re.IGNORECASE)
_RE_REQ_LINE = re.compile(r"^requires\s*:\s*(.+)$", re.IGNORECASE)
_RE_BR_NAME = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9+._-]*)")


@dataclass
class SpecBcondInfo:
    defaults: dict[str, int] = field(default_factory=dict)
    conditional_brs: dict[str, set[str]] = field(default_factory=dict)
    unconditional_brs: set[str] = field(default_factory=set)
    unconditional_reqs: set[str] = field(default_factory=set)


def _br_tokens(raw: str) -> list[str]:
    out: list[str] = []
    for chunk in (raw or "").replace("\n", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        if "=" in token:
            token = token.split("=", 1)[0].strip()
        m = _RE_BR_NAME.match(token)
        if m:
            name = m.group(1)
            if not name.startswith(("rpmlib(", "/", "config(")):
                out.append(name)
    return out


def parse_spec_bconds(spec_text: str) -> SpecBcondInfo:
    info = SpecBcondInfo()
    lines = (spec_text or "").splitlines()
    depth = 0
    active_bconds: set[str] = set()

    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        low = s.lower()
        m_w = _RE_BCOND_WITH.match(s)
        if m_w:
            info.defaults[f"with_{m_w.group(1)}"] = 1
            continue
        m_wo = _RE_BCOND_WITHOUT.match(s)
        if m_wo:
            info.defaults[f"with_{m_wo.group(1)}"] = 0
            continue
        if low.startswith("%if"):
            depth += 1
            active_bconds = set()
            for m in _RE_IF_WITH.findall(s):
                active_bconds.add(m if m.startswith("with_") else f"with_{m}")
            continue
        if low.startswith("%else"):
            continue
        if low.startswith("%endif"):
            depth = max(0, depth - 1)
            if depth == 0:
                active_bconds = set()
            continue
        br_m = _RE_BR_LINE.match(s)
        req_m = _RE_REQ_LINE.match(s)
        if br_m or req_m:
            val = (br_m or req_m).group(1)  # type: ignore[union-attr]
            for tok in _br_tokens(val):
                if depth > 0 and active_bconds:
                    for bc in active_bconds:
                        key = bc if bc.startswith("with_") else f"with_{bc}"
                        info.conditional_brs.setdefault(key, set()).add(tok)
                elif br_m:
                    info.unconditional_brs.add(tok)
                else:
                    info.unconditional_reqs.add(tok)
    return info


def active_brs(info: SpecBcondInfo, defines: dict[str, str]) -> set[str]:
    out = set(info.unconditional_brs) | set(info.unconditional_reqs)
    merged = {**{k: str(v) for k, v in info.defaults.items()}, **defines}
    for bc, brs in info.conditional_brs.items():
        key = bc if bc.startswith("with_") else f"with_{bc}"
        val = merged.get(key, str(info.defaults.get(key, 1)))
        try:
            on = int(str(val).strip() or "0") != 0
        except ValueError:
            on = str(val).strip().lower() in ("1", "true", "yes", "on")
        if on:
            out.update(brs)
    return out


def unsatisfied_brs(info: SpecBcondInfo, defines: dict[str, str], repo_caps: set[str]) -> set[str]:
    return {br for br in active_brs(info, defines) if br not in repo_caps}


def solve_bootstrap_defines(
    info: SpecBcondInfo,
    repo_caps: set[str],
) -> tuple[str, dict[str, str]]:
    defines: dict[str, str] = {k: str(v) for k, v in info.defaults.items()}
    missing = unsatisfied_brs(info, defines, repo_caps)
    if not missing:
        return "full", {}

    toggled: dict[str, str] = {}
    for _ in range(len(info.conditional_brs) + 2):
        progress = False
        for bc in sorted(info.conditional_brs.keys()):
            key = bc if bc.startswith("with_") else f"with_{bc}"
            if toggled.get(key) == "0":
                continue
            gated = info.conditional_brs.get(bc, set())
            if not gated & missing:
                continue
            toggled[key] = "0"
            defines[key] = "0"
            progress = True
            missing = unsatisfied_brs(info, defines, repo_caps)
            if not missing:
                return "bootstrap", toggled
        if not progress:
            break

    still = unsatisfied_brs(info, defines, repo_caps)
    hard = still & info.unconditional_brs
    if hard:
        return "blocked", toggled
    if still:
        return "bootstrap", toggled
    return "bootstrap", toggled
