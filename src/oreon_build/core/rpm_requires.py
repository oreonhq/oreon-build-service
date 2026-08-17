from __future__ import annotations

import re
from typing import Any

_RE_VERSION_TRIM = re.compile(r"\s+(?=[<>=])")
_RE_ELF_MARKER = re.compile(r"\(\)\((?:64bit|32bit|none)\)$")
_RE_VERSIONED = re.compile(r"\s[<>=]")
_RE_VERSIONED_REQ = re.compile(
    r"^(?P<name>.+?)\s*(?P<op><=|>=|==|!=|<|>|=)\s*(?P<ver>\S+)\s*$"
)
_RE_VERSION_ONLY_TOKEN = re.compile(r"^[\d]+([.-][\d]+)*$")
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


def is_spurious_solver_miss(cap: str) -> bool:
    """junk caps from bad macro split / version-only solver jobs."""
    c = (cap or "").strip()
    if not c:
        return True
    if _RE_VERSION_ONLY_TOKEN.match(c):
        return True
    if len(c) == 1 and c in "<>=!":
        return True
    if c in {">", "<", ">=", "<=", "==", "!="}:
        return True
    return False


def is_chain_plan_requirement(cap: str) -> bool:
    c = (cap or "").strip()
    if not c or "%" in c:
        return False
    if c.startswith("(") and c.endswith(")"):
        return False
    if is_spurious_solver_miss(c):
        return False
    name, _op, _ver = parse_versioned_requirement(c)
    check = name or c
    if is_spurious_solver_miss(check):
        return False
    if is_low_level_runtime_requirement(check):
        return False
    if is_arch_qualified_rpm_dependency(check):
        return False
    # shell / sed / flag fragments wrongly split out of macros
    if check.startswith("-"):
        return False
    if any(ch in check for ch in ("'", '"', "\\", "`", "$", "|", ";", "{", "}")):
        return False
    if re.match(r"^\d+/", check):
        return False
    if check in {"echo", "bad", "grep", "sed", "awk", "true", "false", "test", "then", "else", "fi"}:
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


def _providers_from_maps(
    cap: str,
    provider_maps: list[dict[str, set[str]]],
    *,
    exact: bool = True,
) -> set[str]:
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
        if exact:
            for sk in soname_keys:
                hit = pmap.get(sk)
                if hit:
                    out.update(hit)
            if need_pkgconfig_glob:
                for key, srcs in pmap.items():
                    if key == "__lower__":
                        continue
                    if _pkgconfig_glob_match(c, key):
                        out.update(srcs)
            continue
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



def sources_providing_requirement(
    requirement: str,
    repo: Any,
    *,
    provider_maps: list[dict[str, set[str]]] | None = None,
    exact: bool = True,
) -> set[str]:
    """Sources that Provide a capability, even when the provide already exists but is uninstallable."""
    maps = [m for m in (provider_maps or []) if m]
    out: set[str] = set()
    owner_fn = getattr(repo, "owner_sources_for_capability", None)
    for cap in requirement_check_capabilities(requirement):
        if callable(owner_fn):
            for src in owner_fn(cap) or ():
                if src and is_distgit_source_name(src):
                    out.add(src)
        else:
            for src in repo.distgit_sources_for_capability(cap):
                if src and is_distgit_source_name(src):
                    out.add(src)
        for src in _providers_from_maps(cap, maps, exact=exact):
            if not src or not is_distgit_source_name(src):
                continue
            out.add(src)
    return out


def sources_for_requirement(
    requirement: str,
    repo: Any,
    *,
    provider_maps: list[dict[str, set[str]]] | None = None,
    exact: bool = True,
    include_built_sources: bool = False,
) -> set[str]:
    del include_built_sources
    if repo.satisfies_requirement(requirement):
        return set()
    return sources_providing_requirement(
        requirement, repo, provider_maps=provider_maps, exact=exact
    )


def similar_sources_for_requirement(
    requirement: str,
    repo: Any,
    *,
    provider_maps: list[dict[str, set[str]]] | None = None,
) -> set[str]:
    return sources_for_requirement(
        requirement, repo, provider_maps=provider_maps, exact=False
    )


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
    base = _RE_ELF_MARKER.sub("", base)
    return base


def parse_versioned_requirement(requirement: str) -> tuple[str | None, str | None, str | None]:
    """Return (name, op, version). op/version are None when unversioned."""
    raw = (requirement or "").strip()
    if not raw or (raw.startswith("(") and raw.endswith(")")):
        return None, None, None
    m = _RE_VERSIONED_REQ.match(raw)
    if not m:
        primary = rpm_requirement_primary(raw)
        return primary, None, None
    name = (m.group("name") or "").strip()
    op = (m.group("op") or "").strip()
    ver = (m.group("ver") or "").strip()
    if (
        not name
        or "%" in name
        or not ver
        or "%" in ver
        or is_spurious_solver_miss(name)
        or name in {">", "<", "=", "!"}
    ):
        primary = rpm_requirement_primary(raw)
        return primary, None, None
    if op == "==":
        op = "="
    return name, op, ver


def split_requirement_evr(version: str) -> tuple[str, str, str]:
    """Parse epoch:version-release or version into (epoch, ver, rel)."""
    raw = (version or "").strip()
    if not raw:
        return "0", "", ""
    epoch = "0"
    rest = raw
    if ":" in raw:
        left, right = raw.split(":", 1)
        if left.isdigit():
            epoch = left
            rest = right
    ver = rest
    rel = ""
    if "-" in rest:
        # only split release if it looks like NEVRA release (avoid busting versions with dashes carefully)
        # RPM versions rarely have dashes; releases do. Use rsplit once.
        ver, rel = rest.rsplit("-", 1)
    return epoch, ver, rel


def rpm_evr_compare(a: tuple[str, str, str], b: tuple[str, str, str]) -> int:
    import rpm

    return int(rpm.labelCompare(tuple(a), tuple(b)))


def evr_satisfies(op: str, provide_evr: tuple[str, str, str], need_evr: tuple[str, str, str]) -> bool:
    # version-only req (no release): compare epoch+version only, like dnf for "pkg >= 1.48"
    if need_evr[2] == "":
        provide_evr = (provide_evr[0], provide_evr[1], "")
        need_evr = (need_evr[0], need_evr[1], "")
    cmp = rpm_evr_compare(provide_evr, need_evr)
    if op in ("=", "==", "eq"):
        return cmp == 0
    if op in (">=", "ge"):
        return cmp >= 0
    if op in (">", "gt"):
        return cmp > 0
    if op in ("<=", "le"):
        return cmp <= 0
    if op in ("<", "lt"):
        return cmp < 0
    if op in ("!=", "ne"):
        return cmp != 0
    return False


def rpm_requirement_tokens_from_value(value: str) -> list[str]:
    """Requirement tokens kept with version operators when present."""
    caps: list[str] = []
    seen: set[str] = set()
    for chunk in _requirement_chunks(value):
        tok = " ".join((chunk or "").split())
        # spec line continuations leave trailing '\'
        tok = tok.rstrip("\\").strip().strip("{}")
        if not tok or tok in seen:
            continue
        if len(tok) == 1 and not tok.isalnum():
            continue
        primary = rpm_requirement_primary(tok)
        if not primary:
            continue
        seen.add(tok)
        caps.append(tok)
    return caps


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
    # expand inners that are useful provider aliases
    if kind in ("pkgconfig", "cmake", "mvn") or kind.startswith("pkgconfig"):
        out.append(inner)
    return out


def _neutralize_shell_macros(value: str) -> str:
    """Collapse %(...) to a single token. rpm runs it through a shell, we cannot.

    Left as-is, %(echo %{version} | cut -d. -f1,2) gets split on its own comma and
    hands back "2)" as a requirement. Nothing ever provides that, so the package
    fails to resolve and never gets a build attempt.
    """
    text = value or ""
    if "%(" not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "%" and i + 1 < n and text[i + 1] == "(":
            depth = 0
            j = i + 1
            while j < n:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            out.append("%__expr__")
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _requirement_chunks(value: str) -> list[str]:
    chunks: list[str] = []
    for part in re.split(r"[,;\n]", _neutralize_shell_macros(value or "")):
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


_RE_VIRTUAL_RPM_CAP = re.compile(
    r"^(?:"
    r"crate|python3(?:\.\d+)?dist|golang|pkgconfig|perl|gem|npm|mvn|cmake|"
    r"ghc-devel|ghc-prof|tex|font|mono|mingw(?:32|64)|ocaml|rubygem"
    r")\(",
    re.IGNORECASE,
)


def is_virtual_rpm_capability(cap: str) -> bool:
    c = (cap or "").strip()
    if not c:
        return False
    primary = rpm_requirement_primary(c) or c
    return bool(_RE_VIRTUAL_RPM_CAP.match(primary))


def requirement_check_capabilities(requirement: str) -> list[str]:
    caps: list[str] = []
    seen: set[str] = set()

    def add(cap: str) -> None:
        c = (cap or "").strip()
        if not c or c in seen:
            return
        seen.add(c)
        caps.append(c)

    def add_leaf(cap: str) -> None:
        add(cap)
        arch_base = rpm_arch_qualified_base(cap)
        if arch_base:
            add(arch_base)
        if cap.startswith("mvn(") and cap.endswith(")"):
            add(cap[4:-1].strip())
            return
        if ":" in cap and not cap.startswith(("/", "pkgconfig(", "mvn(")):
            add(f"mvn({cap})")
        if cap.startswith("/") and not cap.endswith("/"):
            base = cap.rsplit("/", 1)[-1].strip()
            if base and base not in {".", ".."} and "%" not in base:
                add(base)
        m = re.match(r"^(python3(?:\.\d+)?dist)\(([^)]+)\)$", cap, re.IGNORECASE)
        if m:
            inner = (m.group(2) or "").strip()
            if inner and "%" not in inner:
                dashed = inner.replace("_", "-")
                unders = inner.replace("-", "_")
                for name in (f"python3-{dashed}", f"python-{dashed}", f"python3-{unders}", f"python-{unders}"):
                    add(name)
        # crate(foo/default) also resolves via rust-foo Provide crate(foo)
        cm = re.match(r"^crate\(([^)/]+)(?:/[^)]+)?\)$", cap)
        if cm:
            crate = (cm.group(1) or "").strip()
            if crate and "%" not in crate:
                add(f"crate({crate})")
                add(f"rust-{crate}")

    raw = (requirement or "").strip()
    if raw.startswith("(") and raw.endswith(")"):
        from oreon_build.services.repo_capability_index import _rich_dep_actionable_leaves

        for leaf in _rich_dep_actionable_leaves(raw):
            add_leaf(leaf)
        return caps

    for cap in rpm_requirement_capabilities(requirement):
        add_leaf(cap)
    return caps


def install_check_capabilities(requirement: str) -> list[str]:
    """Installability caps only (no discovery source-name aliases)."""
    raw = (requirement or "").strip()
    if not raw:
        return []
    caps = requirement_check_capabilities(raw)
    if not any(is_virtual_rpm_capability(c) for c in caps) and not is_virtual_rpm_capability(raw):
        return caps
    out: list[str] = []
    seen: set[str] = set()
    for cap in caps:
        if not is_virtual_rpm_capability(cap):
            continue
        if cap in seen:
            continue
        seen.add(cap)
        out.append(cap)
    if out:
        return out
    primary = rpm_requirement_primary(raw) or raw
    return [primary] if primary else []
