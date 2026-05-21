"""Published-repo EVR helpers (used by dependencies_graph primary.xml indexing)."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

_RE_BR_LINE = re.compile(
    r"^([A-Za-z0-9_][A-Za-z0-9+._-]*)"
    r"(?:\s*([<>=!]+)\s*([0-9][0-9a-zA-Z._+~^:-]*))?"
    r"\s*$"
)


@dataclass(frozen=True, order=True)
class RpmEvr:
    epoch: int
    version: str
    release: str

    def canonical(self) -> str:
        return f"{self.epoch}:{self.version}-{self.release}"

    @staticmethod
    def from_canonical(s: str) -> RpmEvr:
        raw = (s or "").strip()
        if not raw:
            return RpmEvr(0, "0", "0")
        epoch = 0
        rest = raw
        if ":" in rest:
            ep, rest = rest.split(":", 1)
            try:
                epoch = int(ep.strip() or "0")
            except ValueError:
                epoch = 0
        if "-" in rest:
            ver, rel = rest.split("-", 1)
        else:
            ver, rel = rest, "0"
        return RpmEvr(epoch, ver.strip() or "0", rel.strip() or "0")


@dataclass
class RepoSatisfactionIndex:
    provides_index: dict[str, set[str]]
    known_sources: set[str]
    provide_best_evr: dict[str, RpmEvr]
    source_best_evr: dict[str, RpmEvr]


def _vercmp(a: RpmEvr, b: RpmEvr) -> int:
    ta = (a.epoch, a.version, a.release)
    tb = (b.epoch, b.version, b.release)
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    if (os.getenv("OREON_CHAIN_USE_RPM_VERCMP") or "").strip().lower() not in ("1", "true", "yes"):
        return 0
    sa, sb = a.canonical(), b.canonical()
    try:
        proc = subprocess.run(
            ["rpm", "--eval", f"%{{vercmp:{sa},{sb}}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip().lstrip("-").isdigit():
            return int((proc.stdout or "0").strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return 0


def _evr_meets_constraint(installed: RpmEvr, op: str | None, want: RpmEvr) -> bool:
    if not op:
        return True
    op = op.strip()
    c = _vercmp(installed, want)
    if op == ">=":
        return c >= 0
    if op == "<=":
        return c <= 0
    if op == ">":
        return c > 0
    if op == "<":
        return c < 0
    if op == "=" or op == "==":
        return c == 0
    if op == "!=":
        return c != 0
    return True


def _parse_br_requirement(line: str) -> tuple[str, str | None, RpmEvr | None]:
    s = (line or "").strip()
    if not s:
        return "", None, None
    m = _RE_BR_LINE.match(s.split()[0] if " " in s and not _RE_BR_LINE.match(s) else s)
    if not m and " " in s:
        m = _RE_BR_LINE.match(s)
    if not m:
        tok = s.split()[0] if s.split() else ""
        return tok, None, None
    name = m.group(1)
    op = m.group(2)
    ver_tok = m.group(3)
    want = None
    if ver_tok:
        if "-" in ver_tok and ":" not in ver_tok:
            want = RpmEvr(0, ver_tok.split("-", 1)[0], ver_tok.split("-", 1)[1])
        else:
            want = RpmEvr.from_canonical(ver_tok)
    return name, op, want
