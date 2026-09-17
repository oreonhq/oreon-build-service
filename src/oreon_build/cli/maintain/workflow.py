from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import click

from oreon_build.cli.maintain import builds, ui
from oreon_build.cli.maintain.dependencies import generate_main_chain, layers_from_chain
from oreon_build.cli.maintain.repodata import load_oreon_repodata, unbuilt_packages
from oreon_build.cli.maintain.state import clear_state, default_repo_base, load_state, save_state
from oreon_build.cli.maintain.updater import bump_packages
from oreon_build.cli.orbscli import (
    _git_changed_spec_paths,
    _spec_dirs_with_specs,
    parse_package_blob,
    resolve_release_id,
)


def _spec_root_for(release_ref: str, spec_root: Path | None) -> Path:
    if spec_root is not None:
        return spec_root.expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / "fedora-rpms" / release_ref).resolve()


def _pick_release() -> tuple[str, int, list[str]]:
    items = builds.list_releases()
    active = [x for x in items if x.get("is_active", True)]
    if not active:
        raise click.ClickException("No releases on the API")
    labels = []
    for item in active:
        arches = item.get("architectures") or item.get("arch_list") or []
        if isinstance(arches, str):
            arches = [a.strip() for a in arches.split(",") if a.strip()]
        labels.append("%s" % item.get("releasename"))
    idx = 0 if len(active) == 1 else ui.menu("Release", labels)
    item = active[idx]
    name = str(item.get("releasename") or "")
    rid = int(item["id"])
    arches = item.get("architectures") or item.get("arch_list") or ["x86_64", "aarch64"]
    if isinstance(arches, str):
        arches = [a.strip() for a in arches.split(",") if a.strip()]
    if not arches:
        arches = ["x86_64", "aarch64"]
    return name, rid, [str(a) for a in arches]


def _pick_packages(root: Path, mode: int) -> list[str]:
    if mode == 0:
        return [n for n, _p in _spec_dirs_with_specs(root)]
    if mode == 2:
        git_root = root
        try:
            import subprocess

            found = subprocess.run(
                ("git", "rev-parse", "--show-toplevel"),
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if found:
                git_root = Path(found)
        except Exception:
            pass
        return [p.parent.name for p in _git_changed_spec_paths(git_root, root, False)]
    blob = ui.ask("Package names (spaces or commas)")
    return parse_package_blob(blob)


def _pick_side_tag(release_id: int) -> int:
    tags = builds.list_side_tags(release_id)
    labels = ["Create new side tag"] + [
        "%s (%s)" % (t.get("name"), t.get("id")) for t in tags
    ]
    idx = ui.menu("Side tag", labels)
    if idx == 0:
        name = ui.ask("New side tag name")
        created = builds.create_side_tag(release_id, name.strip())
        return int(created["id"])
    return int(tags[idx - 1]["id"])


def _poll_plan(plan_id: int, discovery: bool) -> dict[str, Any]:
    while True:
        data = builds.discovery_status(plan_id) if discovery else builds.plan_status(plan_id)
        counts = data.get("counts") or {}
        live = int(counts.get("pending") or 0) + int(counts.get("queued") or 0) + int(counts.get("running") or 0)
        ok = int(counts.get("success") or 0)
        failed = int(counts.get("failed") or 0)
        blocked = int(counts.get("blocked") or 0)
        click.echo(
            "status=%s success=%s failed=%s blocked=%s live=%s"
            % (data.get("status"), ok, failed, blocked, live)
        )
        if discovery:
            if data.get("needs_attention"):
                return data
            if live == 0:
                return data
        st = (data.get("status") or "").lower()
        if st in {"success", "failed", "cancelled"} and live == 0:
            return data
        if not discovery and live == 0 and st in {"waiting_settle", "running"}:
            time.sleep(8)
            continue
        if live == 0 and not discovery:
            return data
        time.sleep(8)


def _handle_attention(data: dict[str, Any], state: dict[str, Any]) -> None:
    ui.heading("Maintenance operation requires attention.")
    cycles = data.get("cycles") or []
    ui.echo_cycle(cycles, int(data.get("downstream_blocked") or 0))
    blocked = data.get("blocked") or []
    labels = []
    jobs = []
    seen: set[str] = set()
    for row in blocked:
        name = str(row.get("package") or "")
        if name in seen:
            continue
        seen.add(name)
        labels.append(name)
        jobs.append(int(row["job_id"]))
    labels.append("Skip for now")
    idx = ui.menu("Select a package to bootstrap", labels)
    if idx >= len(jobs):
        click.echo("Left blocked jobs on the server. Re-run orbscli maintain to resume.")
        return
    builds.bootstrap_job(jobs[idx])
    click.echo("Submitted %s with bootstrap=1. Full rebuild is queued after it publishes." % labels[idx])
    disc_id = int(state.get("discovery_plan_id") or 0)
    if disc_id:
        _poll_plan(disc_id, True)


def _resume_existing(state: dict[str, Any]) -> bool:
    ops = builds.active_maintenance()
    disc_id = state.get("discovery_plan_id")
    chain_id = state.get("chain_plan_id")
    if not ops and not disc_id and not chain_id:
        return False
    pkg_n = len(state.get("packages") or [])
    done = state.get("complete_hint") or "?"
    idx = ui.menu(
        "Existing maintenance operation found:\n\n%s package maintenance\n%s/%s packages complete"
        % (state.get("release") or "Oreon", done, pkg_n or "?"),
        ["Resume", "View failures", "View build status", "Abort"],
    )
    plan_id = int(disc_id or chain_id or 0)
    if ops:
        plan_id = int(ops[0].get("id") or plan_id)
    if idx == 3:
        if plan_id:
            builds.cancel_plan(plan_id)
        clear_state()
        click.echo("Aborted.")
        return True
    if not plan_id:
        click.echo("No plan id stored.")
        return True
    data = builds.discovery_status(plan_id) if disc_id else builds.plan_status(plan_id)
    if idx == 1:
        for job in data.get("jobs") or []:
            click.echo("job %s %s %s" % (job.get("job_id"), job.get("package_name"), job.get("status")))
        return True
    if idx == 2:
        click.echo(data.get("counts") or {})
        return True
    if data.get("needs_attention"):
        _handle_attention(data, state)
        return True
    _poll_plan(plan_id, bool(disc_id))
    return True


@click.command("maintain")
@click.option("--spec-root", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--release", "release_ref", default=None)
@click.option("--arch", default=None, help="Comma list. Default comes from the release.")
@click.option(
    "--repo-base",
    envvar="ORBSCLI_REPO_BASE",
    default=None,
)
@click.option("--distgit-url", envvar="ORBSCLI_DISTGIT_URL", default="https://github.com/oreonhq/rpm-specfiles.git")
@click.option("--distgit-branch", envvar="ORBSCLI_DISTGIT_BRANCH", default="main")
def cmd_maintain(
    spec_root: Path | None,
    release_ref: str | None,
    arch: str | None,
    repo_base: str | None,
    distgit_url: str,
    distgit_branch: str,
) -> None:
    """Interactive Oreon package maintenance (update, discover, chain, submit)."""
    state = load_state()
    if state.get("phase") in {"discovery", "chain", "attention"}:
        if _resume_existing(state):
            return
    if release_ref:
        api = os.environ.get("ORBSCLI_API_URL") or ""
        from oreon_build.cli.orbscli import get_api_url, get_token

        rid = resolve_release_id(get_api_url(), get_token(), release_ref)
        name = release_ref
        arches = [a.strip() for a in (arch or "x86_64,aarch64").split(",") if a.strip()]
    else:
        name, rid, arches = _pick_release()
        if arch:
            arches = [a.strip() for a in arch.split(",") if a.strip()]
    root = _spec_root_for(name, spec_root)
    if not root.is_dir():
        raise click.ClickException("spec root is not a directory: %s" % root)
    ui.heading("Oreon Package Maintenance")
    click.echo("Target: %s" % name)
    click.echo("Architectures: %s" % ", ".join(arches))
    mode = ui.menu(
        "What would you like to maintain?",
        ["All outdated packages", "Specific packages", "Changed specs"],
    )
    names = _pick_packages(root, mode)
    if not names:
        click.echo("No packages selected.")
        return
    click.echo("Selected %s packages" % ui.fmt_int(len(names)))
    stats = bump_packages(names, root)
    if stats["fail"]:
        click.echo("%s failed to update." % ui.fmt_int(len(stats["fail"])))
        if not ui.confirm("Continue with packages that are ready?", default=True):
            return
    ready = list(dict.fromkeys(stats["ok"] + stats["skip"] + [n for n in names if n not in stats["fail"]]))
    if not ready:
        click.echo("Nothing left to build.")
        return
    side_tag_id = _pick_side_tag(rid)
    base = (repo_base or default_repo_base()).rstrip("/")
    tags = builds.list_side_tags(rid)
    tag_name = None
    for t in tags:
        if int(t.get("id") or 0) == side_tag_id:
            tag_name = t.get("name")
            break
    click.echo("Analyzing Oreon repodata...")
    present = load_oreon_repodata(
        repo_base=base,
        release_ref=name,
        arches=arches,
        side_tag_ref=str(tag_name or side_tag_id),
    )
    missing = unbuilt_packages(ready, present)
    built = [n for n in ready if n not in set(missing)]
    state = {
        "phase": "discovery" if missing else "chain",
        "release": name,
        "release_id": rid,
        "arches": arches,
        "packages": ready,
        "unbuilt": missing,
        "side_tag_id": side_tag_id,
    }
    save_state(state)
    if missing:
        click.echo("%s packages have no Oreon repodata. Submitting discovery batch." % ui.fmt_int(len(missing)))
        disc = builds.trigger_discovery(
            packages=missing,
            release_id=rid,
            side_tag_id=side_tag_id,
            arches=arches,
            distgit_url=distgit_url,
            distgit_branch=distgit_branch,
        )
        state["discovery_plan_id"] = disc.get("id")
        save_state(state)
        click.echo("discovery plan %s queued_jobs=%s" % (disc.get("id"), disc.get("queued_jobs")))
        result = _poll_plan(int(disc["id"]), True)
        if result.get("needs_attention"):
            state["phase"] = "attention"
            save_state(state)
            _handle_attention(result, state)
            result = builds.discovery_status(int(disc["id"]))
            if result.get("needs_attention"):
                return
        st = (result.get("status") or "").lower()
        if st == "failed":
            click.echo("Discovery finished with failures. Fix those, then re-run maintain.")
            return
        click.echo("Refreshing repodata after discovery...")
        present = load_oreon_repodata(
            repo_base=base,
            release_ref=name,
            arches=arches,
            side_tag_ref=str(tag_name or side_tag_id),
        )
        still = unbuilt_packages(ready, present)
        if still:
            click.echo("Still no repodata for: %s" % " ".join(still[:40]))
            if not ui.confirm("Generate the main chain without those packages?", default=False):
                return
            built = [n for n in ready if n not in set(still)]
        else:
            built = ready
    if not built:
        click.echo("No packages with Oreon repodata for the main chain.")
        clear_state()
        return
    host_arch = os.uname().machine
    chain, selected, cyclic, layers = generate_main_chain(
        root,
        built,
        present,
        host_arch=host_arch if host_arch in present else next(iter(present)),
        repo_urls=None,
    )
    ui.echo_plan(layers, arches)
    click.echo("")
    click.echo("Dependency graph resolved.")
    click.echo("")
    click.echo("%s packages" % ui.fmt_int(len(selected)))
    click.echo("%s build layers" % ui.fmt_int(len(layers)))
    if cyclic:
        click.echo("Authoritative layers skipped cyclic names (see fedora-44 mock list above).")
    if not ui.confirm("Execute build plan?", default=True):
        state["phase"] = "chain"
        state["chain_text"] = chain
        save_state(state)
        click.echo("Plan stored. Re-run orbscli maintain to submit.")
        return
    out = builds.trigger_chain(
        chain=chain,
        release_id=rid,
        side_tag_id=side_tag_id,
        arches=arches,
        distgit_url=distgit_url,
        distgit_branch=distgit_branch,
    )
    state["phase"] = "chain"
    state["chain_plan_id"] = out.get("id")
    save_state(state)
    click.echo(
        "chain_id=%s status=%s groups=%s queued_jobs=%s"
        % (out.get("id"), out.get("status"), out.get("total_layers"), out.get("queued_jobs"))
    )
    final = _poll_plan(int(out["id"]), False)
    click.echo("Finished: %s %s" % (final.get("status"), final.get("counts")))
    if (final.get("status") or "").lower() == "success":
        clear_state()
