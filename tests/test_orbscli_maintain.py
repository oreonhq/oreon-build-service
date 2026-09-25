from pathlib import Path

from click.testing import CliRunner

from oreon_build.cli import orbscli as orbscli_mod
from oreon_build.cli.orbscli import cli


def test_bulk_bump_replaces_maintain() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["bulk-bump", "--help"])
    assert result.exit_code == 0
    assert "ordered build chain" in result.output
    assert "maintain" not in cli.commands


def test_orbscli_has_no_oreon_build_import() -> None:
    text = Path(orbscli_mod.__file__).read_text(encoding="utf-8")
    assert "import oreon_build" not in text
    assert "from oreon_build" not in text


def test_bulk_bump_chains_only_changed_specs(tmp_path, monkeypatch) -> None:
    root = tmp_path / "specs"
    for name in ("alpha", "beta"):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / f"{name}.spec").write_text(f"Name: {name}\nVersion: 1\n")
    calls = []

    def bump(**kwargs):
        calls.append(("bump", kwargs))
        (root / "alpha" / "alpha.spec").write_text("Name: alpha\nVersion: 2\n")

    def chain(**kwargs):
        calls.append(("chain", kwargs))

    monkeypatch.setattr(orbscli_mod.cmd_spec_bump, "callback", bump)
    monkeypatch.setattr(orbscli_mod.cmd_generate_chain, "callback", chain)
    result = CliRunner().invoke(cli, [
        "bulk-bump", "--release", "oreon-test", "--spec-root", str(root),
        "--packages", "alpha,beta",
    ])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == "bump"
    assert calls[1][1]["packages_blob"] == "alpha"
