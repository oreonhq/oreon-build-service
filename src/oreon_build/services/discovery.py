# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from oreon_build.models import BuildJob, BuildStatus, Package, RpmbuildPlan, RpmbuildPlanStatus
from oreon_build.services.build_pass import job_options_dict

_ATOM = re.compile(r"\s+")


def cap_atom(raw: str) -> str:
    return _ATOM.split((raw or "").strip(), 1)[0].strip()


def caps_overlap(needed: list[str], provided: list[str]) -> bool:
    have = {cap_atom(x).lower() for x in provided if cap_atom(x)}
    if not have:
        return False
    for item in needed:
        atom = cap_atom(item).lower()
        if atom and atom in have:
            return True
    return False


def name_level_cycles(blocked: list[tuple[str, list[str]]]) -> tuple[list[list[str]], int]:
    names = {n for n, _caps in blocked if n}
    deps: dict[str, set[str]] = {n: set() for n in names}
    for name, caps in blocked:
        if name not in deps:
            continue
        for cap in caps:
            atom = cap_atom(cap)
            if atom in names and atom != name:
                deps[name].add(atom)
    cycles: list[list[str]] = []
    seen: set[str] = set()

    def dfs(node: str, stack: list[str], instack: set[str]) -> None:
        seen.add(node)
        stack.append(node)
        instack.add(node)
        for nxt in sorted(deps.get(node) or ()):
            if nxt not in names:
                continue
            if nxt in instack:
                i = stack.index(nxt)
                cyc = stack[i:] + [nxt]
                if cyc not in cycles:
                    cycles.append(cyc)
                continue
            if nxt not in seen:
                dfs(nxt, stack, instack)
        stack.pop()
        instack.remove(node)

    for name in sorted(names):
        if name not in seen:
            dfs(name, [], set())
    cycle_members = {n for cyc in cycles for n in cyc[:-1]}
    downstream = 0
    for name, _caps in blocked:
        if name in cycle_members:
            continue
        if any(atom in cycle_members for atom in (cap_atom(c) for c in _caps)):
            downstream += 1
    return cycles, downstream


async def create_discovery_plan(
    db: Any,
    request: Any,
    *,
    packages: list[str],
    release_id: int,
    side_tag_id: int,
    architectures: list[str],
    priority: int = 0,
    preferred_mock_environment_id: int | None = None,
    distgit_url: str | None = None,
    distgit_branch: str | None = None,
    triggered_by_id: int | None = None,
) -> Any:
    from oreon_build.services.rpmbuild_order import create_manual_chain_plan

    names = [p.strip() for p in packages if p and p.strip()]
    if not names:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No packages for discovery")
    chain = " ".join(dict.fromkeys(names))
    out = await create_manual_chain_plan(
        db,
        request,
        chain=chain,
        release_id=release_id,
        side_tag_id=side_tag_id,
        architectures=architectures,
        priority=priority,
        preferred_mock_environment_id=preferred_mock_environment_id,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
        triggered_by_id=triggered_by_id,
    )
    plan = out.plan
    plan.seed_mode = "discovery"
    opts = dict(plan.options_json or {}) if isinstance(plan.options_json, dict) else {}
    opts["maintenance"] = True
    opts["discovery"] = True
    plan.options_json = opts
    for job in out.jobs:
        jopts = job_options_dict(job)
        jopts["discovery"] = True
        job.job_options = jopts
    await db.flush()
    return out


async def wake_blocked_jobs(db: Any, plan_id: int, provided: list[str]) -> int:
    if not provided:
        return 0
    rows = await db.execute(
        select(BuildJob).where(
            BuildJob.rpmbuild_plan_id == plan_id,
            BuildJob.status == BuildStatus.BLOCKED,
        )
    )
    woken = 0
    for job in rows.scalars().all():
        opts = job_options_dict(job)
        needed = [str(x) for x in (opts.get("missing_caps") or []) if str(x).strip()]
        if not needed:
            continue
        if not caps_overlap(needed, provided):
            continue
        opts.pop("missing_caps", None)
        job.job_options = opts
        job.status = BuildStatus.PENDING
        job.blocked_reason = None
        job.completed_at = None
        woken += 1
    if woken:
        await db.flush()
    return woken


async def mark_job_blocked(job: BuildJob, missing: list[str]) -> None:
    opts = job_options_dict(job)
    caps = [str(x).strip() for x in missing if str(x).strip()]
    opts["missing_caps"] = caps
    opts["discovery"] = True
    job.job_options = opts
    job.status = BuildStatus.BLOCKED
    job.blocked_reason = "waiting on " + ", ".join(caps[:12])
    if len(caps) > 12:
        job.blocked_reason += " ..."


async def apply_bootstrap_to_job(db: Any, job: BuildJob) -> None:
    opts = job_options_dict(job)
    defines = dict(opts.get("rpmbuild_defines") or {}) if isinstance(opts.get("rpmbuild_defines"), dict) else {}
    defines["bootstrap"] = "1"
    opts["build_profile"] = "bootstrap"
    opts["rpmbuild_defines"] = defines
    opts["next_profile"] = "full"
    opts.pop("missing_caps", None)
    job.job_options = opts
    job.status = BuildStatus.PENDING
    job.blocked_reason = None
    job.completed_at = None
    job.error_message = None
    await db.flush()


async def discovery_attention(db: Any, plan: RpmbuildPlan) -> dict[str, Any]:
    jobs = list(
        (
            await db.execute(
                select(BuildJob).options().where(BuildJob.rpmbuild_plan_id == plan.id)
            )
        ).scalars().all()
    )
    by_status: dict[str, int] = {}
    for job in jobs:
        key = job.status.value
        by_status[key] = by_status.get(key, 0) + 1
    live = by_status.get("pending", 0) + by_status.get("queued", 0) + by_status.get("running", 0)
    blocked_rows: list[tuple[str, list[str], int]] = []
    for job in jobs:
        if job.status != BuildStatus.BLOCKED:
            continue
        pkg = None
        if job.package_id:
            pkg_row = await db.get(Package, job.package_id)
            pkg = pkg_row.name if pkg_row is not None else None
        caps = [str(x) for x in (job_options_dict(job).get("missing_caps") or []) if str(x).strip()]
        blocked_rows.append((pkg or ("job-%s" % job.id), caps, job.id))
    cycles: list[list[str]] = []
    downstream = 0
    if live == 0 and blocked_rows:
        cycles, downstream = name_level_cycles([(n, c) for n, c, _i in blocked_rows])
    return {
        "plan_id": plan.id,
        "status": plan.status.value,
        "counts": by_status,
        "live": live,
        "blocked": [
            {"package": n, "missing_caps": caps, "job_id": jid} for n, caps, jid in blocked_rows
        ],
        "cycles": cycles,
        "downstream_blocked": downstream,
        "needs_attention": live == 0 and bool(blocked_rows),
    }


async def maybe_finish_discovery_plan(db: Any, plan: RpmbuildPlan) -> None:
    if plan.seed_mode != "discovery":
        return
    jobs = list(
        (await db.execute(select(BuildJob).where(BuildJob.rpmbuild_plan_id == plan.id))).scalars().all()
    )
    if not jobs:
        return
    if any(j.status in (BuildStatus.PENDING, BuildStatus.QUEUED, BuildStatus.RUNNING) for j in jobs):
        return
    if any(j.status == BuildStatus.BLOCKED for j in jobs):
        return
    if any(j.status == BuildStatus.FAILED for j in jobs):
        plan.status = RpmbuildPlanStatus.FAILED
        plan.error_message = "Discovery batch had failed jobs"
        return
    if all(j.status in (BuildStatus.SUCCESS, BuildStatus.SKIPPED, BuildStatus.CANCELLED) for j in jobs):
        plan.status = RpmbuildPlanStatus.SUCCESS
        plan.error_message = None

