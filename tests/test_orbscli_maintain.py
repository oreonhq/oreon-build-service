from click.testing import CliRunner

from oreon_build.cli.orbscli import cli
from oreon_build.cli.maintain.workflow import cmd_maintain


def test_maintain_is_registered() -> None:
    cli.add_command(cmd_maintain)
    runner = CliRunner()
    result = runner.invoke(cli, ["maintain", "--help"])
    assert result.exit_code == 0
    assert "package maintenance" in result.output.lower()
