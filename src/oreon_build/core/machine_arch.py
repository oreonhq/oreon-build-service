from __future__ import annotations

def normalize_machine_arch(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "x86_64"
    if s in ("amd64", "x64"):
        return "x86_64"
    if s == "arm64":
        return "aarch64"
    return s

def sql_arch_match_values(raw: str | None) -> frozenset[str]:
    s = (raw or "").strip().lower()
    if not s:
        return frozenset()
    canon = normalize_machine_arch(raw)
    out = {canon, s}
    if canon == "aarch64":
        out.add("arm64")
    if canon == "x86_64":
        out.add("amd64")
        out.add("x64")
    return frozenset(out)
