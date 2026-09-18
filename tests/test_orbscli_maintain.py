from pathlib import Path

from click.testing import CliRunner

from oreon_build.cli import orbscli as orbscli_mod
from oreon_build.cli.orbscli import cli


def test_maintain_is_on_standalone_cli() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["maintain", "--help"])
    assert result.exit_code == 0
    assert "package maintenance" in result.output.lower()


def test_orbscli_has_no_oreon_build_import() -> None:
    text = Path(orbscli_mod.__file__).read_text(encoding="utf-8")
    assert "import oreon_build" not in text
    assert "from oreon_build" not in text
