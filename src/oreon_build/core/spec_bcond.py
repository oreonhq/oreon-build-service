from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from oreon_build.core.rpm_requires import rpm_requirement_tokens_from_value


class _ReqCheck(Protocol):
    def satisfies_requirement(self, dep: str) -> bool: ...


_RE_BCOND_WITH = re.compile(r"^%\s*bcond_with\s+(\S+)", re.IGNORECASE)
_RE_BCOND_WITHOUT = re.compile(r"^%\s*bcond_without\s+(\S+)", re.IGNORECASE)
_RE_GLOBAL = re.compile(r"^%\s*global\s+(\S+)\s+(.+)$", re.IGNORECASE)
_RE_DEFINE = re.compile(r"^%\s*define\s+(\S+)\s+(.+)$", re.IGNORECASE)
_RE_IF = re.compile(r"^%\s*if\b(.*)$", re.IGNORECASE)
_RE_IFARCH = re.compile(r"^%\s*ifarch\b(.*)$", re.IGNORECASE)
_RE_IFNARCH = re.compile(r"^%\s*ifnarch\b(.*)$", re.IGNORECASE)
_RE_ELSE = re.compile(r"^%\s*else\b", re.IGNORECASE)
_RE_ENDIF = re.compile(r"^%\s*endif\b", re.IGNORECASE)
_RE_BR_LINE = re.compile(r"^buildrequires\s*:\s*(.+)$", re.IGNORECASE)
_RE_REQ_LINE = re.compile(r"^requires\s*:\s*(.+)$", re.IGNORECASE)
_RE_WITH_SPACE = re.compile(r"%\{?\??with\s+([A-Za-z0-9_]+)\}?", re.IGNORECASE)
_RE_WITHOUT_SPACE = re.compile(r"%\{?\??without\s+([A-Za-z0-9_]+)\}?", re.IGNORECASE)
_RE_WITH_US = re.compile(r"%\{?\??with_([A-Za-z0-9_]+)\}?", re.IGNORECASE)
_RE_WITHOUT_US = re.compile(r"%\{?\??without_([A-Za-z0-9_]+)\}?", re.IGNORECASE)
_RE_DEFINED = re.compile(r"%\{defined\s+([A-Za-z0-9_]+)\}", re.IGNORECASE)
_RE_PY_DIST = re.compile(r"%\{(py\d*_?dist|python3_dist)\s+([^}]+)\}", re.IGNORECASE)
# rpm %{?name} / %{?name:body} / %{!?name:body}  (do before bare %{name})
_RE_COND_MACRO = re.compile(
    r"%\{(?P<bang>!?)\?(?P<name>[A-Za-z0-9_]+)(?::(?P<body>[^}]*))?\}",
    re.IGNORECASE,
)
_RE_MACRO = re.compile(r"%\{?\??([A-Za-z0-9_]+)\}?")
_RE_GATING_WITH = re.compile(
    r"%\{?\??with[_\s]+([A-Za-z0-9_]+)\}?|%\{?\??without[_\s]+([A-Za-z0-9_]+)\}?",
    re.IGNORECASE,
)


@dataclass
class SpecBcondInfo:
    defaults: dict[str, int] = field(default_factory=dict)
    conditional_brs: dict[str, set[str]] = field(default_factory=dict)
    unconditional_brs: set[str] = field(default_factory=set)
    unconditional_reqs: set[str] = field(default_factory=set)
    macros: dict[str, str] = field(default_factory=dict)


def _br_tokens(raw: str) -> list[str]:
    return rpm_requirement_tokens_from_value(raw)


def _expand_py_dist_macros(value: str) -> str:
    """%{py3_dist foo bar} -> python3dist(foo) python3dist(bar) (Fedora rpm macro)."""

    def repl(m: re.Match[str]) -> str:
        kind = (m.group(1) or "").lower().replace("_", "")
        names = [n for n in (m.group(2) or "").split() if n and "%" not in n]
        if not names:
            return ""
        if kind.startswith("py2"):
            prefix = "python2dist"
        else:
            prefix = "python3dist"
        return " ".join(f"{prefix}({n})" for n in names)

    return _RE_PY_DIST.sub(repl, value or "")


def _expand_defined_macros(
    text: str, macros: dict[str, str], bconds: dict[str, int]
) -> str:
    def repl(m: re.Match[str]) -> str:
        name = (m.group(1) or "").strip()
        if name in macros or name in bconds:
            return "1"
        return "0"

    return _RE_DEFINED.sub(repl, text or "")


def _macro_is_defined(name: str, macros: dict[str, str], bconds: dict[str, int]) -> bool:
    key = (name or "").strip()
    if not key:
        return False
    return key in macros or key in bconds


def _expand_conditional_macros(
    text: str, macros: dict[str, str], bconds: dict[str, int], *, max_rounds: int = 16
) -> str:
    """rpm %{?name} / %{?name:body} / %{!?name:body}"""

    out = text or ""
    for _ in range(max_rounds):
        def repl(m: re.Match[str]) -> str:
            name = (m.group("name") or "").strip()
            body = m.group("body")
            bang = bool(m.group("bang"))
            defined = _macro_is_defined(name, macros, bconds)
            take = (not defined) if bang else defined
            if not take:
                return ""
            if body is not None:
                return body
            if name in macros:
                return str(macros[name])
            if name in bconds:
                return str(bconds[name])
            return ""

        new = _RE_COND_MACRO.sub(repl, out)
        if new == out:
            break
        out = new
    return out


def _expand_br_macros(value: str, macros: dict[str, str], *, max_rounds: int = 16) -> str:
    out = _expand_py_dist_macros(value or "")
    out = _expand_conditional_macros(out, macros, {})
    for _ in range(max_rounds):
        changed = False

        def repl(match: re.Match[str]) -> str:
            nonlocal changed
            key = match.group(1)
            if key in macros:
                changed = True
                return str(macros[key])
            return match.group(0)

        new = _RE_MACRO.sub(repl, out)
        new = _expand_py_dist_macros(new)
        new = _expand_conditional_macros(new, macros, {})
        if not changed and new == out:
            break
        out = new
    return out


def default_distro_macros(releasename: str = "") -> dict[str, str]:
    out = {
        "oreon": "0",
        "rhel": "0",
        "fedora": "0",
        "el": "0",
        "centos": "0",
        "python3_pkgversion": "3",
        "python3_version": "3",
    }
    rel = (releasename or "").strip().lower()
    m = re.search(r"oreon[_-]?(\d+)", rel)
    if m:
        out["oreon"] = m.group(1)
    m = re.search(r"(?:^|[_\-])(?:el|rhel)(\d+)", rel)
    if m:
        out["rhel"] = m.group(1)
        out["el"] = m.group(1)
    m = re.search(r"(?:^|[_\-])f(?:edora)?[_-]?(\d+)", rel)
    if m and "oreon" not in rel:
        out["fedora"] = m.group(1)
    return out


def _macro_value(name: str, macros: dict[str, str], bconds: dict[str, int]) -> str:
    key = (name or "").strip()
    if not key:
        return "0"
    if key.startswith("with_"):
        if key in bconds:
            return str(bconds[key])
        if key in macros:
            return str(macros[key])
        return "0"
    if key.startswith("without_"):
        with_key = "with_" + key[len("without_") :]
        on = int(str(_macro_value(with_key, macros, bconds) or "0") or 0)
        return "0" if on else "1"
    if key in macros:
        return str(macros[key])
    if key in bconds:
        return str(bconds[key])
    return "0"


def _rewrite_with_forms(expr: str) -> str:
    text = expr or ""
    text = _RE_WITHOUT_SPACE.sub(lambda m: f"%{{without_{m.group(1)}}}", text)
    text = _RE_WITH_SPACE.sub(lambda m: f"%{{with_{m.group(1)}}}", text)
    return text


def _expand_expr(expr: str, macros: dict[str, str], bconds: dict[str, int]) -> str:
    text = _rewrite_with_forms(expr or "").strip()
    if not text:
        return "0"
    text = _expand_defined_macros(text, macros, bconds)
    text = _expand_py_dist_macros(text)
    # must run before ! -> not, or %{!?foo:1} becomes garbage
    text = _expand_conditional_macros(text, macros, bconds)

    def repl_without(m: re.Match[str]) -> str:
        return _macro_value(f"without_{m.group(1)}", macros, bconds)

    def repl_with(m: re.Match[str]) -> str:
        return _macro_value(f"with_{m.group(1)}", macros, bconds)

    def repl_macro(m: re.Match[str]) -> str:
        return _macro_value(m.group(1), macros, bconds)

    text = _RE_WITHOUT_US.sub(repl_without, text)
    text = _RE_WITH_US.sub(repl_with, text)
    text = _RE_MACRO.sub(repl_macro, text)
    text = text.replace("||", " or ").replace("&&", " and ")
    text = re.sub(r"(?i)\bnot\b", " not ", text)
    # rpm uses ! as logical not (!0%{?bootstrap}); python needs "not"
    text = re.sub(r"!\s*", " not ", text)
    text = re.sub(r"[^0-9A-Za-z_.<>=!()\s]", " ", text)
    return text


def _normalize_numeric_tokens(expr: str) -> str:
    def repl(m: re.Match[str]) -> str:
        try:
            return str(int(m.group(0)))
        except ValueError:
            return m.group(0)

    return re.sub(r"\b[0-9]+\b", repl, expr or "")


def _eval_condition(expr: str, macros: dict[str, str], bconds: dict[str, int]) -> bool:
    expanded = _normalize_numeric_tokens(_expand_expr(expr, macros, bconds))
    if not expanded.strip():
        return False
    cleaned = re.sub(r"[^0-9A-Za-z_.<>=!()\s]", " ", expanded)
    if not cleaned.strip():
        return False
    try:
        return bool(eval(cleaned, {"__builtins__": {}}, {}))
    except Exception:
        return False


def _arch_tokens(expr: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Za-z0-9_]+", expr or "") if t}


def _eval_ifarch(expr: str, target_arches: set[str] | None) -> bool:
    listed = _arch_tokens(expr)
    if not listed:
        return True
    if not target_arches:
        return True
    return bool(listed & {a.lower() for a in target_arches})


def _eval_ifnarch(expr: str, target_arches: set[str] | None) -> bool:
    listed = _arch_tokens(expr)
    if not listed:
        return True
    if not target_arches:
        return True
    targets = {a.lower() for a in target_arches}
    return bool(targets - listed)


def _expand_macro_value(raw: str, macros: dict[str, str], bconds: dict[str, int]) -> str:
    text = _rewrite_with_forms(raw or "").strip()
    if not text:
        return "0"

    def repl(m: re.Match[str]) -> str:
        return _macro_value(m.group(1), macros, bconds)

    prev = None
    for _ in range(8):
        if text == prev:
            break
        prev = text
        text = _expand_conditional_macros(text, macros, bconds)
        text = _RE_WITHOUT_US.sub(lambda m: _macro_value(f"without_{m.group(1)}", macros, bconds), text)
        text = _RE_WITH_US.sub(lambda m: _macro_value(f"with_{m.group(1)}", macros, bconds), text)
        text = _RE_MACRO.sub(repl, text)
    text = _normalize_numeric_tokens(text.strip())
    if re.fullmatch(r"[-+]?[0-9]+", text or ""):
        return str(int(text or "0"))
    low = text.lower()
    if low in ("true", "yes", "on"):
        return "1"
    if low in ("false", "no", "off"):
        return "0"
    try:
        cleaned = re.sub(r"[^0-9A-Za-z_.<>=!()\s]", " ", text)
        if not cleaned.strip():
            return text
        return "1" if bool(eval(cleaned, {"__builtins__": {}}, {})) else "0"
    except Exception:
        return text


def _gating_bconds_from_expr(expr: str) -> set[str]:
    out: set[str] = set()
    text = _rewrite_with_forms(expr or "")
    for m in _RE_GATING_WITH.finditer(text):
        with_name = m.group(1)
        without_name = m.group(2)
        if with_name:
            out.add(f"with_{with_name}")
        if without_name:
            out.add(f"with_{without_name}")
    return out


def parse_spec_bconds(
    spec_text: str,
    *,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
) -> SpecBcondInfo:
    info = SpecBcondInfo()
    macros: dict[str, str] = dict(distro_macros or {})
    info.macros = macros
    bconds: dict[str, int] = {}
    arches = {a.lower() for a in (target_arches or set()) if a}
    lines = (spec_text or "").splitlines()
    # each frame: (active_bool, gating_bcond_keys)
    stack: list[tuple[bool, frozenset[str]]] = []

    def active() -> bool:
        return all(fr[0] for fr in stack) if stack else True

    def gating() -> set[str]:
        out: set[str] = set()
        for fr in stack:
            if fr[0]:
                out.update(fr[1])
        return out

    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue

        m_w = _RE_BCOND_WITH.match(s)
        if m_w:
            if active():
                key = f"with_{m_w.group(1)}"
                bconds[key] = 0
                info.defaults[key] = 0
                macros[key] = "0"
            continue
        m_wo = _RE_BCOND_WITHOUT.match(s)
        if m_wo:
            if active():
                key = f"with_{m_wo.group(1)}"
                bconds[key] = 1
                info.defaults[key] = 1
                macros[key] = "1"
            continue

        m_g = _RE_GLOBAL.match(s) or _RE_DEFINE.match(s)
        if m_g:
            if active():
                name = m_g.group(1).strip()
                val = _expand_macro_value(m_g.group(2), macros, bconds)
                macros[name] = val
                if name.startswith("with_"):
                    try:
                        iv = int(val)
                        info.defaults[name] = iv
                        bconds[name] = iv
                    except ValueError:
                        pass
            continue

        m_ia = _RE_IFARCH.match(s)
        if m_ia:
            parent = active()
            ok = parent and _eval_ifarch(m_ia.group(1), arches or None)
            stack.append((ok, frozenset()))
            continue
        m_ina = _RE_IFNARCH.match(s)
        if m_ina:
            parent = active()
            ok = parent and _eval_ifnarch(m_ina.group(1), arches or None)
            stack.append((ok, frozenset()))
            continue
        m_if = _RE_IF.match(s)
        if m_if:
            parent = active()
            expr = m_if.group(1)
            ok = parent and _eval_condition(expr, macros, bconds)
            stack.append((ok, frozenset(_gating_bconds_from_expr(expr)) if ok else frozenset()))
            continue
        if _RE_ELSE.match(s):
            if stack:
                parent_ok = all(fr[0] for fr in stack[:-1]) if len(stack) > 1 else True
                was_active, _gates = stack[-1]
                stack[-1] = (parent_ok and (not was_active), frozenset())
            continue
        if _RE_ENDIF.match(s):
            if stack:
                stack.pop()
            continue

        if not active():
            continue

        if s.upper().startswith("NAME:"):
            val = (s.split(":", 1)[1] or "").strip()
            if val and "%" not in val:
                macros["name"] = val
            continue
        br_m = _RE_BR_LINE.match(s)
        req_m = _RE_REQ_LINE.match(s)
        if not (br_m or req_m):
            continue
        val = _expand_br_macros((br_m or req_m).group(1), macros)  # type: ignore[union-attr]
        gates = gating()
        for tok in _br_tokens(val):
            if "%" in tok:
                continue
            if gates:
                for key in gates:
                    info.conditional_brs.setdefault(key, set()).add(tok)
            elif br_m:
                info.unconditional_brs.add(tok)
            else:
                info.unconditional_reqs.add(tok)

    for key, val in bconds.items():
        info.defaults.setdefault(key, val)
    return info


def active_brs(info: SpecBcondInfo, defines: dict[str, str]) -> set[str]:
    out = set(info.unconditional_brs) | set(info.unconditional_reqs)
    merged = {**{k: str(v) for k, v in info.defaults.items()}, **defines}
    for bc, brs in info.conditional_brs.items():
        key = bc if bc.startswith("with_") else f"with_{bc}"
        val = merged.get(key, str(info.defaults.get(key, 0)))
        try:
            on = int(str(val).strip() or "0") != 0
        except ValueError:
            on = str(val).strip().lower() in ("1", "true", "yes", "on")
        if on:
            out.update(brs)
    return out


def active_build_brs(info: SpecBcondInfo, defines: dict[str, str]) -> set[str]:
    out = set(info.unconditional_brs)
    merged = {**{k: str(v) for k, v in info.defaults.items()}, **defines}
    for bc, brs in info.conditional_brs.items():
        key = bc if bc.startswith("with_") else f"with_{bc}"
        val = merged.get(key, str(info.defaults.get(key, 0)))
        try:
            on = int(str(val).strip() or "0") != 0
        except ValueError:
            on = str(val).strip().lower() in ("1", "true", "yes", "on")
        if on:
            out.update(brs)
    return out


def unsatisfied_brs(
    info: SpecBcondInfo,
    defines: dict[str, str],
    repo_caps: set[str] | _ReqCheck,
) -> set[str]:
    out: set[str] = set()
    for br in active_build_brs(info, defines):
        if "%" in br:
            continue
        if isinstance(repo_caps, set):
            if br not in repo_caps:
                out.add(br)
        elif not repo_caps.satisfies_requirement(br):
            out.add(br)
    return out


def default_build_defines(info: SpecBcondInfo) -> dict[str, str]:
    return {k: str(v) for k, v in info.defaults.items()}


def solve_bootstrap_defines(
    info: SpecBcondInfo,
    repo_caps: set[str] | _ReqCheck,
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
