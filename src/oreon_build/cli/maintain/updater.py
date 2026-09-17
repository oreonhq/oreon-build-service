from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from oreon_build.cli.orbscli import (
    _iter_bounded,
    _nvchecker_resolve_entry,
    _nvchecker_run_batched,
    _spec_bump_compute,
    _status_line,
    _status_nl,
    find_pkg_specs,
)


def bump_packages(names: list[str], spec_root: Path) -> dict[str, list[str]]:
    root = spec_root.expanduser().resolve()
    jobs: list[tuple[str, Path]] = []
    fail: list[str] = []
    for pkg in names:
        specs = find_pkg_specs(root, pkg)
        if len(specs) != 1:
            fail.append(pkg)
            click.echo("%s  no unique spec" % pkg, err=True)
            continue
        jobs.append((pkg, specs[0]))
    work: list[tuple[str, Path]] = []
    entries: dict[str, dict[str, Any]] = {}

    def _load(item: tuple[str, Path]) -> tuple[str, Path, dict[str, Any] | None, str | None]:
        pkg, spec_path = item
        try:
            text = spec_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return pkg, spec_path, None, str(exc)
        return pkg, spec_path, _nvchecker_resolve_entry(pkg, spec_path, text), None

    for pkg, spec_path, entry, err in _iter_bounded(8, _load, jobs, inflight=8):
        if err or not entry:
            fail.append(pkg)
            if err:
                click.echo("%s  %s" % (pkg, err), err=True)
            continue
        work.append((pkg, spec_path))
        entries[pkg] = entry
    latest_map = _nvchecker_run_batched(entries) if entries else {}
    ok: list[str] = []
    skip: list[str] = []

    def _one(item: tuple[str, Path]) -> tuple[str, str, str]:
        pkg, spec_path = item
        text = spec_path.read_text(encoding="utf-8", errors="replace")
        latest = latest_map.get(pkg) or ""
        status, payload, old, new = _spec_bump_compute(pkg, spec_path, text, latest)
        if status == "ok":
            spec_path.write_text(payload, encoding="utf-8")
            return pkg, "ok", "%s -> %s" % (old or "?", new)
        return pkg, status, payload

    done = 0
    todo = len(work)
    for pkg, status, msg in _iter_bounded(8, _one, work, inflight=8):
        done += 1
        _status_line("[%d/%d] %s" % (done, todo, status))
        if status == "ok":
            ok.append(pkg)
        elif status == "skip":
            skip.append(pkg)
        else:
            fail.append(pkg)
            _status_nl()
            click.echo("%s  %s" % (pkg, msg), err=True)
    _status_nl()
    click.echo("bumped=%s already=%s failed=%s" % (len(ok), len(skip), len(fail)))
    return {"ok": ok, "skip": skip, "fail": fail}
