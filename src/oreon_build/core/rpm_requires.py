from __future__ import annotations

import re
from typing import Any

_RE_VERSION_TRIM = re.compile(r"\s+(?=[<>=])")
_RE_VERSIONED = re.compile(r"\s[<>=]")
_RE_RPM_ARCH_QUAL = re.compile(
    r"^(.+)\((?:x86-64|aarch-64|aarch64|i686|noarch|ppc64le|s390x)\)$",
    re.IGNORECASE,
)


def rpm_arch_qualified_base(cap: str) -> str | None:
    m = _RE_RPM_ARCH_QUAL.match((cap or "").strip())
    if not m:
        return None
    base = (m.group(1) or "").strip()
    return base or None


def is_arch_qualified_rpm_dependency(cap: str) -> bool:
    return rpm_arch_qualified_base(cap) is not None


def is_low_level_runtime_requirement(cap: str) -> bool:
    c = (cap or "").strip()
    if not c:
        return True
    low = c.lower()
    if low.startswith("rpmlib("):
        return True
    if low.startswith("rtld(") or low.startswith("ld-linux"):
        return True
    if "()(64bit)" in low or ")(64bit)" in low:
        return True
    if ".so" in low and "(" in c:
        return True
    if low.startswith(("libc.so", "libm.so", "libresolv.so", "libgcc_s.so", "libpthread.so")):
        return True
    return False


def is_chain_plan_requirement(cap: str) -> bool:
    c = (cap or "").strip()
    if not c or "%" in c:
        return False
    if c.startswith("(") and c.endswith(")"):
        return False
    if is_low_level_runtime_requirement(c):
        return False
    if is_arch_qualified_rpm_dependency(c):
        return False
    return True


def is_distgit_source_name(name: str) -> bool:
    n = (name or "").strip()
    if not n or "%" in n or " " in n:
        return False
    low = n.lower()
    if low.startswith(("config(", "mvn(", "rpmlib(", "pkgconfig(")):
        return False
    if n.startswith("/") or low.startswith("bundled("):
        return False
    if ".so" in n or "()(64bit)" in n or ")(64bit)" in n:
        return False
    if any(ch in n for ch in ("<", ">", "=", ",")):
        return False
    return True


def _pkgconfig_glob_match(cap: str, key: str) -> bool:
    """Match when the requirement has a glob (pkgconfig(Qt5*) vs pkgconfig(Qt5Core))."""
    c = (cap or "").strip().lower()
    k = (key or "").strip().lower()
    if not (c.startswith("pkgconfig(") and c.endswith(")")):
        return False
    if not (k.startswith("pkgconfig(") and k.endswith(")")):
        return False
    cinner = c[len("pkgconfig(") : -1]
    kinner = k[len("pkgconfig(") : -1]
    if "*" in kinner:
        return False
    if "*" not in cinner:
        return cinner == kinner
    if cinner == "*":
        return False
    prefix, _, suffix = cinner.partition("*")
    if "*" in suffix:
        return False
    return bool(kinner) and kinner.startswith(prefix) and kinner.endswith(suffix)


def _soname_prefix_match(cap: str, key: str) -> bool:
    """libIL.so.1()(64bit) matches a files provide of libIL.so (literal stem from %files)."""
    c = (cap or "").strip().lower().split("(", 1)[0]
    k = (key or "").strip().lower().split("(", 1)[0]
    if ".so" not in c or ".so" not in k:
        return False
    return c == k or c.startswith(k + ".") or k.startswith(c + ".")


def _soname_lookup_keys(cap: str) -> list[str]:
    """Exact keys to try for a soname requirement. No inventing package names."""
    c = (cap or "").strip()
    if not c or ".so" not in c:
        return []
    base = c.split("(", 1)[0].strip()
    out: list[str] = []
    seen: set[str] = set()

    def add(tok: str) -> None:
        t = (tok or "").strip()
        if not t or t.lower() in seen:
            return
        seen.add(t.lower())
        out.append(t)

    add(c)
    add(base)
    # libIL.so.1 -> libIL.so
    m = re.match(r"^(lib[A-Za-z0-9_+.-]+\.so)(?:\.\d+)+$", base)
    if m:
        add(m.group(1))
    return out


def _providers_from_maps(cap: str, provider_maps: list[dict[str, set[str]]]) -> set[str]:
    c = (cap or "").strip()
    if not c:
        return set()
    out: set[str] = set()
    cl = c.lower()
    soname_keys = _soname_lookup_keys(c)
    need_pkgconfig_glob = (
        cl.startswith("pkgconfig(") and cl.endswith(")") and "*" in cl[len("pkgconfig(") : -1]
    )
    for pmap in provider_maps:
        if not pmap:
            continue
        hit = pmap.get(c)
        if hit:
            out.update(hit)
        lower_index = pmap.get("__lower__")
        if isinstance(lower_index, dict):
            hit = lower_index.get(cl)
            if hit:
                out.update(hit)
            for sk in soname_keys:
                hit = pmap.get(sk) or lower_index.get(sk.lower())
                if hit:
                    out.update(hit)
            if need_pkgconfig_glob:
                for key, srcs in pmap.items():
                    if key == "__lower__":
                        continue
                    if _pkgconfig_glob_match(c, key):
                        out.update(srcs)
            continue
        # legacy maps without lower index: exact + targeted only
        for sk in soname_keys:
            hit = pmap.get(sk)
            if hit:
                out.update(hit)
        if need_pkgconfig_glob or (not out and cl != c):
            for key, srcs in pmap.items():
                if key.lower() == cl or _pkgconfig_glob_match(c, key) or _soname_prefix_match(c, key):
                    out.update(srcs)
    return out


def index_provider_map(pmap: dict[str, set[str]] | None) -> dict[str, set[str]]:
    """Attach a lower-case lookup index. Keeps exact keys for soname/pkgconfig forms."""
    if not pmap:
        return {"__lower__": {}}  # type: ignore[dict-item]
    if "__lower__" in pmap and isinstance(pmap.get("__lower__"), dict):
        return pmap
    lower: dict[str, set[str]] = {}
    out: dict[str, set[str]] = {}
    for k, srcs in pmap.items():
        if k == "__lower__":
            continue
        out[k] = set(srcs)
        lower.setdefault(k.lower(), set()).update(srcs)
    out["__lower__"] = lower  # type: ignore[assignment]
    return out


def merge_provider_maps(*maps: dict[str, set[str]] | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for pmap in maps:
        if not pmap:
            continue
        for cap, srcs in pmap.items():
            if cap == "__lower__":
                continue
            out.setdefault(cap, set()).update(srcs)
    return index_provider_map(out)


def add_capability_providers(
    pmap: dict[str, set[str]],
    capability: str,
    sources: set[str] | list[str] | tuple[str, ...],
) -> None:
    cap = (capability or "").strip()
    if not cap or cap == "__lower__":
        return
    srcs = {s for s in sources if s}
    if not srcs:
        return
    bucket = pmap.setdefault(cap, set())
    bucket.update(srcs)
    lower = pmap.get("__lower__")
    if isinstance(lower, dict):
        lower.setdefault(cap.lower(), set()).update(srcs)
        return
    raw = {k: set(v) for k, v in pmap.items() if k != "__lower__"}
    pmap.clear()
    pmap.update(index_provider_map(raw))



def sources_for_requirement(
    requirement: str,
    repo: Any,
    *,
    provider_maps: list[dict[str, set[str]]] | None = None,
) -> set[str]:
    if repo.satisfies_requirement(requirement):
        return set()
    maps = [m for m in (provider_maps or []) if m]
    out: set[str] = set()
    for cap in requirement_check_capabilities(requirement):
        for src in repo.distgit_sources_for_capability(cap):
            if src and is_distgit_source_name(src) and not repo.satisfies_source(src):
                out.add(src)
        for src in _providers_from_maps(cap, maps):
            if src and is_distgit_source_name(src) and not repo.satisfies_source(src):
                out.add(src)
    return out


def rpm_requirement_primary(requirement: str) -> str | None:
    raw = (requirement or "").strip()
    if not raw:
        return None
    if raw.startswith("(") and raw.endswith(")"):
        return raw
    base = _RE_VERSION_TRIM.split(raw, 1)[0].strip()
    if not base or base.lower().startswith("rpmlib("):
        return None
    if "%" in base:
        return None
    return base


def rpm_requirement_capabilities(requirement: str) -> list[str]:
    base = rpm_requirement_primary(requirement)
    if not base:
        return []
    out: list[str] = [base]
    m = re.match(r"^([^(]+)\(([^)]*)\)$", base)
    if not m:
        return out
    kind = (m.group(1) or "").strip().lower()
    inner = (m.group(2) or "").strip()
    if not inner or "%" in inner:
        return out
    # only expand inners that are useful provider aliases, never python3dist/rpm_macro
    if kind in ("pkgconfig", "cmake", "mvn") or kind.startswith("pkgconfig"):
        out.append(inner)
    return out


def _requirement_chunks(value: str) -> list[str]:
    chunks: list[str] = []
    for part in re.split(r"[,;\n]", value or ""):
        part = part.strip()
        if not part:
            continue
        chunks.extend(_split_requirement_part(part))
    return chunks


def _split_requirement_part(part: str) -> list[str]:
    text = (part or "").strip()
    if not text:
        return []
    # RPM: "foo bar >= 1.0" is foo + (bar >= 1.0), not one token
    return _split_keeping_rich_deps(text)


def _split_keeping_rich_deps(text: str) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        if text[i] == "(":
            depth = 0
            j = i
            while j < n:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            out.append(text[i:j].strip())
            i = j
            continue
        j = i
        while j < n and not text[j].isspace():
            j += 1
        # keep "name >= 1.0" together when version ops follow
        tok = text[i:j]
        k = j
        while k < n and text[k].isspace():
            k += 1
        if k < n and text[k] in "<>=":
            while k < n and not text[k].isspace():
                k += 1
            while k < n and text[k].isspace():
                k += 1
            while k < n and not text[k].isspace() and text[k] != "(":
                k += 1
            out.append(text[i:k].strip())
            i = k
            continue
        out.append(tok)
        i = j
    return [c for c in out if c]


def rpm_requirement_capabilities_from_value(value: str) -> list[str]:
    caps: list[str] = []
    seen: set[str] = set()
    for chunk in _requirement_chunks(value):
        for cap in rpm_requirement_capabilities(chunk):
            if cap not in seen:
                seen.add(cap)
                caps.append(cap)
    return caps


def rpm_requirement_primary_capabilities_from_value(value: str) -> list[str]:
    caps: list[str] = []
    seen: set[str] = set()
    for chunk in _requirement_chunks(value):
        primary = rpm_requirement_primary(chunk)
        if not primary or primary in seen:
            continue
        seen.add(primary)
        caps.append(primary)
    return caps


def requirement_check_capabilities(requirement: str) -> list[str]:
    caps: list[str] = []
    seen: set[str] = set()

    def add(cap: str) -> None:
        c = (cap or "").strip()
        if not c or c in seen:
            return
        seen.add(c)
        caps.append(c)

    for cap in rpm_requirement_capabilities(requirement):
        add(cap)
        arch_base = rpm_arch_qualified_base(cap)
        if arch_base:
            add(arch_base)
        if cap.startswith("mvn(") and cap.endswith(")"):
            add(cap[4:-1].strip())
            continue
        if ":" in cap and not cap.startswith(("/", "pkgconfig(", "mvn(")):
            add(f"mvn({cap})")
    return caps
