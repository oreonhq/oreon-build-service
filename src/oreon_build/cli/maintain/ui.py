from __future__ import annotations

import sys
from typing import Any

import click


def heading(title: str) -> None:
    click.echo("")
    click.echo(title)
    click.echo("")


def menu(title: str, options: list[str], *, default: int = 0) -> int:
    heading(title)
    for i, label in enumerate(options):
        mark = ">" if i == default else " "
        click.echo(" %s %s" % (mark, label))
    click.echo("")
    if not sys.stdin.isatty():
        return default
    raw = click.prompt("Choice", default=str(default + 1), show_default=True)
    try:
        idx = int(str(raw).strip()) - 1
    except ValueError:
        idx = default
    if idx < 0 or idx >= len(options):
        return default
    return idx


def confirm(prompt: str, *, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    return click.confirm(prompt, default=default)


def ask(prompt: str, default: str | None = None) -> str:
    if default is None:
        return click.prompt(prompt)
    return click.prompt(prompt, default=default)


def fmt_int(n: int) -> str:
    return f"{n:,}"


def echo_plan(layers: list[list[str]], arches: list[str]) -> None:
    pkgs = [p for layer in layers for p in layer]
    click.echo("Generating build plan...")
    click.echo("")
    click.echo("Packages:       %s" % fmt_int(len(pkgs)))
    click.echo("Architectures:  %s" % ", ".join(arches))
    click.echo("")
    for i, layer in enumerate(layers, 1):
        click.echo("Layer %d: %s packages" % (i, fmt_int(len(layer))))


def echo_cycle(cycles: list[list[str]], downstream: int) -> None:
    click.echo("Bootstrap cycle detected:")
    click.echo("")
    if not cycles:
        click.echo("  (blocked packages, no name-level cycle from missing caps)")
        click.echo("  Pick a package whose spec has a bootstrap bcond.")
        return
    for cyc in cycles[:5]:
        for name in cyc:
            click.echo("    %s" % name)
            if name != cyc[-1]:
                click.echo("    |")
                click.echo("    v")
        click.echo("")
    if downstream:
        click.echo("%s additional packages are blocked downstream." % fmt_int(downstream))


def load_json_list(data: dict[str, Any], key: str) -> list[Any]:
    raw = data.get(key)
    return list(raw) if isinstance(raw, list) else []
