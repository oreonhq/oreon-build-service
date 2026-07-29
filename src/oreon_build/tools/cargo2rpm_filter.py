"""cargo2rpm wrapper that drops foreign-target crate BuildRequires."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _real_cargo2rpm() -> str:
    env = (os.environ.get("OREON_REAL_CARGO2RPM") or "").strip()
    if env and Path(env).is_file():
        return env
    for cand in (
        "/usr/bin/cargo2rpm.real",
        "/usr/bin/cargo2rpm",
        shutil.which("cargo2rpm") or "",
    ):
        p = Path(cand) if cand else None
        if not p or not p.is_file():
            continue
        if p.resolve() == Path(sys.argv[0]).resolve():
            continue
        return str(p)
    return "/usr/bin/cargo2rpm"


def _host_arches() -> set[str]:
    raw = (os.environ.get("OREON_CARGO_TARGET_ARCHES") or "").strip()
    if raw:
        return {a.strip() for a in raw.split(",") if a.strip()}
    try:
        out = subprocess.check_output(["uname", "-m"], text=True, timeout=5)
        arch = (out or "").strip()
        if arch:
            return {arch}
    except Exception:
        pass
    return {"x86_64"}


def _filter_buildrequires(lines: list[str], argv: list[str]) -> list[str]:
    from oreon_build.services.cargo_target_brs import filter_foreign_target_crate_brs

    manifest = Path("Cargo.toml")
    for i, arg in enumerate(argv):
        if arg == "--path" and i + 1 < len(argv):
            manifest = Path(argv[i + 1])
            break
        if arg.startswith("--path="):
            manifest = Path(arg.split("=", 1)[1])
            break
    if not manifest.is_file():
        return lines
    return filter_foreign_target_crate_brs(
        lines,
        cargo_tomls=[manifest],
        arches=_host_arches(),
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    real = _real_cargo2rpm()
    proc = subprocess.run([real, *args], capture_output=True, text=True, check=False)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    out = proc.stdout or ""
    if proc.returncode != 0:
        if out:
            sys.stdout.write(out)
        return int(proc.returncode)
    if "buildrequires" in args:
        filtered = _filter_buildrequires(out.splitlines(), args)
        sys.stdout.write("\n".join(filtered) + ("\n" if filtered else ""))
        return 0
    if out:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
