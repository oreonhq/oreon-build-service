from __future__ import annotations

from typing import Any

import click

from oreon_build.cli.orbscli import (
    _MIN_REPO_RPMS,
    _RepoDetail,
    _dnf_repo_maps,
    _parse_repo_arches,
    _repo_repomd_ok,
    _repo_url,
    _side_tag_repo_dirs,
)


def load_oreon_repodata(
    *,
    repo_base: str,
    release_ref: str,
    arches: list[str],
    side_tag_ref: str | None,
) -> dict[str, _RepoDetail]:
    tags = ["testing"]
    side_dirs: list[str] = []
    if side_tag_ref:
        side_dirs = _side_tag_repo_dirs(side_tag_ref)
        for name in side_dirs:
            if name not in tags:
                tags.append(name)
    present: dict[str, _RepoDetail] = {}
    for arch in arches:
        urls: list[str] = []
        seen: set[str] = set()
        got_side = False
        for tag in tags:
            url = _repo_url(repo_base, release_ref, tag, arch)
            if url in seen:
                continue
            if not _repo_repomd_ok(url):
                if tag == "testing":
                    raise click.ClickException("no dnf repo at %s" % url)
                continue
            seen.add(url)
            urls.append(url)
            if tag in side_dirs:
                got_side = True
        if side_dirs and not got_side:
            raise click.ClickException("no dnf repo for side tag %s on %s" % (side_tag_ref, arch))
        if not urls:
            raise click.ClickException("no dnf repo for %s" % arch)
        click.echo("querying dnf %s" % arch, err=True)
        provides, requires, provides_evr, srpms = _dnf_repo_maps(urls, arch, with_solver=False)
        click.echo(
            "dnf %s %d rpms %d srpms %d caps"
            % (arch, len(requires), len(srpms), len(provides)),
            err=True,
        )
        if len(requires) < _MIN_REPO_RPMS:
            click.echo("skipping %s, dnf only saw %d rpms" % (arch, len(requires)), err=True)
            continue
        present[arch] = _RepoDetail(set(provides), provides, requires, provides_evr, srpms=srpms)
    if not present:
        raise click.ClickException("no usable dnf repos")
    return present


def unbuilt_packages(names: list[str], present: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for name in names:
        hit = False
        for detail in present.values():
            srpms = getattr(detail, "srpms", None) or set()
            reqs = getattr(detail, "requires", None) or {}
            if name in srpms or name in reqs:
                hit = True
                break
        if not hit:
            out.append(name)
    return out
