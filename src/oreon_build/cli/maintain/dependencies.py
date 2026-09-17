from __future__ import annotations

from pathlib import Path

import click

from oreon_build.cli.orbscli import _generate_chain_from_specs


def layers_from_chain(chain: str) -> list[list[str]]:
    out: list[list[str]] = []
    for part in (chain or "").split(":"):
        names = [p for p in part.split() if p]
        if names:
            out.append(names)
    return out


def generate_main_chain(
    spec_root: Path,
    seeds: list[str],
    present: dict,
    *,
    host_arch: str | None,
    repo_urls: list[str] | None,
) -> tuple[str, list[str], str, list[list[str]]]:
    chain, selected, cyclic = _generate_chain_from_specs(
        spec_root,
        seeds,
        present,
        repo_urls=repo_urls,
        repo_arch=host_arch,
    )
    layers = layers_from_chain(chain)
    if cyclic:
        click.echo(
            "Unresolved cycle (not ordered into layers): %s" % " ".join(cyclic.split()),
            err=True,
        )
    return chain, selected, cyclic, layers
