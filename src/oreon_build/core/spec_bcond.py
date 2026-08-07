from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from oreon_build.core.rpm_requires import rpm_requirement_tokens_from_value


class _ReqCheck(Protocol):
    def satisfies_requirement(self, dep: str) -> bool: ...


_RE_BCOND_WITH = re.compile(r"^%\s*bcond_with\s+(\S+)", re.IGNORECASE)
_RE_BCOND_WITHOUT = re.compile(r"^%\s*bcond_without\s+(\S+)", re.IGNORECASE)
# rpm 4.17.1+: %bcond name default   (must be matched after bcond_with/without)
_RE_BCOND = re.compile(r"^%\s*bcond\s+(\S+)\s+(.+)$", re.IGNORECASE)
_RE_EXPR_BRACKET = re.compile(r"^%\[\s*(.+?)\s*\]$", re.DOTALL)
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
# body may nest %{...}; matched by _expand_conditional_macros brace scan
_RE_COND_MACRO_HEAD = re.compile(
    r"%\{(?P<bang>!?)\?(?P<name>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
# kept for tests / simple no-nest bodies
_RE_COND_MACRO = re.compile(
    r"%\{(?P<bang>!?)\?(?P<name>[A-Za-z0-9_]+)(?::(?P<body>(?:[^{}]|%\{[^}]*\})*))?\}",
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
    """rpm %{?name} / %{?name:body} / %{!?name:body}

    Body may nest macros like %{?up_prever:~%{up_prever}}.
    """

    def _one_pass(src: str) -> str:
        m = _RE_COND_MACRO_HEAD.search(src)
        if not m:
            return src
        start = m.start()
        name = (m.group("name") or "").strip()
        bang = bool(m.group("bang"))
        pos = m.end()
        body: str | None = None
        if pos < len(src) and src[pos] == "}":
            end = pos + 1
        elif pos < len(src) and src[pos] == ":":
            depth = 1
            j = pos + 1
            body_start = j
            while j < len(src) and depth:
                ch = src[j]
                if ch == "{" and j > 0 and src[j - 1] == "%":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:
                return src
            body = src[body_start:j]
            end = j + 1
        else:
            return src
        defined = _macro_is_defined(name, macros, bconds)
        take = (not defined) if bang else defined
        if not take:
            repl = ""
        elif body is not None:
            repl = body
        elif name in macros:
            repl = str(macros[name])
        elif name in bconds:
            repl = str(bconds[name])
        else:
            repl = ""
        return src[:start] + repl + src[end:]

    out = text or ""
    for _ in range(max_rounds):
        new = _one_pass(out)
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
    # only set the active distro id. sentinel fedora=0 made %{defined fedora}
    # true on oreon and wrongly enabled fedora-only bconds like scancode.
    out = {
        "python3_pkgversion": "3",
        "python3_version": "3",
    }
    rel = (releasename or "").strip().lower()
    m = re.search(r"oreon[_-]?(\d+)", rel)
    if m:
        out["oreon"] = m.group(1)
        return out
    m = re.search(r"(?:^|[_\-])(?:el|rhel)(\d+)", rel)
    if m:
        out["rhel"] = m.group(1)
        out["el"] = m.group(1)
        return out
    m = re.search(r"(?:^|[_\-])f(?:edora)?[_-]?(\d+)", rel)
    if m:
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
    # dotted versions like 2.6 / 15.0.7 stay literal (do not bool-eval)
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)+(?:[.~^].*)?", text or ""):
        return text
    low = text.lower()
    if low in ("true", "yes", "on"):
        return "1"
    if low in ("false", "no", "off"):
        return "0"
    try:
        cleaned = re.sub(r"[^0-9A-Za-z_.<>=!()\s]", " ", text)
        if not cleaned.strip():
            return text
        # only treat as a bool expression when operators are present
        if not re.search(r"[<>=!&|()]", cleaned):
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
    # only simple feature toggles like %if 0%{?hal:1} — not complex !0%{?bootstrap} && ...
    simple = (expr or "").strip()
    m = re.fullmatch(
        r"0?\s*%\{\?([A-Za-z0-9_]+)(?::1)?\}\s*",
        simple,
        re.IGNORECASE,
    )
    if m:
        name = (m.group(1) or "").strip()
        low = name.lower()
        if name and not low.startswith(("with_", "without_")) and low not in {
            "with",
            "without",
            "defined",
            "undefined",
            "expand",
        }:
            out.add(name)
    return out


def _eval_bcond_default(raw: str, macros: dict[str, str], bconds: dict[str, int]) -> int:
    """Evaluate %bcond name <default> to 0/1 (rpm numeric / %{defined} / %[expr])."""
    text = (raw or "").strip()
    if not text:
        return 0
    m = _RE_EXPR_BRACKET.match(text)
    if m:
        text = (m.group(1) or "").strip()
    if _eval_condition(text, macros, bconds):
        return 1
    expanded = _expand_macro_value(text, macros, bconds)
    try:
        return 1 if int(str(expanded).strip() or "0") != 0 else 0
    except ValueError:
        return 0


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
        m_b = _RE_BCOND.match(s)
        if m_b:
            if active():
                key = f"with_{m_b.group(1)}"
                iv = _eval_bcond_default(m_b.group(2), macros, bconds)
                bconds[key] = iv
                info.defaults[key] = iv
                macros[key] = str(iv)
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
    merged = {
        **dict(info.macros or {}),
        **{k: str(v) for k, v in info.defaults.items()},
        **defines,
    }
    for bc, brs in info.conditional_brs.items():
        key = bc if bc.startswith("with_") else bc
        val = merged.get(key, "0")
        try:
            on = int(str(val).strip() or "0") != 0
        except ValueError:
            on = str(val).strip().lower() in ("1", "true", "yes", "on")
        if on:
            out.update(brs)
    return out


def active_build_brs(info: SpecBcondInfo, defines: dict[str, str]) -> set[str]:
    out = set(info.unconditional_brs)
    merged = {
        **dict(info.macros or {}),
        **{k: str(v) for k, v in info.defaults.items()},
        **defines,
    }
    for bc, brs in info.conditional_brs.items():
        key = bc if bc.startswith("with_") else bc
        val = merged.get(key, "0")
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


_FALSEY = frozenset({"0", "no", "false", "off"})
_TRUTHY = frozenset({"1", "yes", "true", "on"})


def rpm_bcond_define_args(defines: dict[str, str] | None) -> list[str]:
    """Translate planner with_*=0/1 into rpm --with/--without defines.

    `--define 'with_check 0'` still *defines* with_check, so %{with check} becomes
    true and cargo_generate_buildrequires adds --with-check (dev-deps). Use
    `_without_check` / `_with_check` instead, matching mock CLI.
    """
    out: list[str] = []
    if not isinstance(defines, dict):
        return out
    for key in sorted(defines.keys()):
        k = str(key).strip()
        if not k:
            continue
        v = str(defines.get(key) or "").strip()
        if k.startswith("with_"):
            feature = k[5:]
            if not feature:
                continue
            low = v.lower()
            if low in _FALSEY:
                out += ["--define", f"_without_{feature} --without-{feature}"]
                continue
            if low in _TRUTHY:
                out += ["--define", f"_with_{feature} --with-{feature}"]
                continue
        out += ["--define", f"{k} {v}"]
    return out


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
