"""Tests for CLI check command."""

import pytest
from click.testing import CliRunner


@pytest.mark.parametrize(
    "command,args",
    [
        ("crucible", ["--help"]),
        ("check", ["--help"]),
    ],
)
def test_cli_help(command, args):
    """Test that CLI help commands execute."""
    from crucible.cli import main
    from crucible.cli.check import check

    runner = CliRunner()
    if command == "crucible":
        result = runner.invoke(main, args)
    else:
        result = runner.invoke(check, args)

    assert result.exit_code == 0
    assert "Usage:" in result.output or "check" in result.output


def test_check_config_command():
    """Test that check config command executes."""
    from crucible.cli.check import config

    runner = CliRunner()
    result = runner.invoke(config, [])

    assert result.exit_code == 0
    assert "Phoenix Configuration" in result.output
