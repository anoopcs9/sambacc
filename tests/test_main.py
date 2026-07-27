#
# sambacc: a samba container configuration tool
# Copyright (C) 2021  John Mulligan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>
#

from unittest import mock

import pytest

import sambacc.commands.cli
import sambacc.commands.main
from .test_netcmd_loader import config1


def run(*args):
    return sambacc.commands.main.main(args)


def test_no_id(capsys):
    with pytest.raises(sambacc.commands.cli.Fail):
        run("print-config")


def test_print_config(capsys, tmp_path):
    fname = tmp_path / "sample.json"
    with open(fname, "w") as fh:
        fh.write(config1)
    run("--identity", "foobar", "--config", str(fname), "print-config")
    out, err = capsys.readouterr()
    assert "[global]" in out
    assert "netbios name = GANDOLPH" in out
    assert "[share]" in out
    assert "path = /share" in out
    assert "[stuff]" in out
    assert "path = /mnt/stuff" in out


def test_print_config_env_vars(capsys, tmp_path, monkeypatch):
    fname = tmp_path / "sample.json"
    with open(fname, "w") as fh:
        fh.write(config1)
    monkeypatch.setenv("SAMBACC_CONFIG", str(fname))
    monkeypatch.setenv("SAMBA_CONTAINER_ID", "foobar")
    run("print-config")
    out, err = capsys.readouterr()
    assert "[global]" in out
    assert "netbios name = GANDOLPH" in out
    assert "[share]" in out
    assert "path = /share" in out
    assert "[stuff]" in out
    assert "path = /mnt/stuff" in out


@pytest.fixture(scope="function")
def clihack(tmp_path, monkeypatch):
    mm = mock.MagicMock()

    mm.tcmds = sambacc.commands.cli.CommandBuilder()

    @mm.tcmds.command("check")
    def _check(ctx):
        mm.check(ctx)

    mm.fname = tmp_path / "sample.json"
    with open(mm.fname, "w") as fh:
        fh.write(config1)

    def _run(args):
        for key, value in mm.test_env.items():
            monkeypatch.setenv(key, value)
        sambacc.commands.main.main_command(mm.tcmds, args)

    mm.run = _run
    return mm


def test_cli_simple(clihack):
    def _assert(ctx):
        assert ctx.cli.config == [str(clihack.fname)]
        assert ctx.cli.identity == "foobar"

    clihack.test_env = {
        "SAMBACC_CONFIG": str(clihack.fname),
        "SAMBA_CONTAINER_ID": "foobar",
    }
    clihack.check = _assert
    clihack.run(["check"])


def test_cli_opts_simple(clihack):
    def _assert(ctx):
        assert ctx.cli.config == [str(clihack.fname)]
        assert ctx.cli.identity == "foobar"

    clihack.test_env = {}
    clihack.check = _assert
    clihack.run(["--identity=foobar", f"--config={clihack.fname}", "check"])


def test_cli_altfiles_default(clihack):
    def _assert(ctx):
        assert ctx.cli.passwd_location.default_path == "/etc/passwd"
        assert not ctx.cli.passwd_location.altfiles
        assert ctx.cli.group_location.default_path == "/etc/group"
        assert not ctx.cli.group_location.altfiles

    clihack.test_env = {
        "SAMBACC_CONFIG": str(clihack.fname),
        "SAMBA_CONTAINER_ID": "foobar",
    }
    clihack.check = _assert
    clihack.run(["check"])


def test_cli_altfiles_opts(clihack):
    def _assert(ctx):
        assert ctx.cli.passwd_location.default_path == "/etc/passwd"
        assert ctx.cli.passwd_location.altfiles
        assert (
            ctx.cli.passwd_location.mutable_path == "/var/lib/samba/passwd.txt"
        )
        assert ctx.cli.passwd_location.link_path == "/lib/passwd"

        assert ctx.cli.group_location.default_path == "/etc/group"
        assert ctx.cli.group_location.altfiles
        assert (
            ctx.cli.group_location.mutable_path == "/var/lib/samba/group.txt"
        )
        assert ctx.cli.group_location.link_path == "/lib/group"

    clihack.test_env = {
        "SAMBACC_CONFIG": str(clihack.fname),
        "SAMBA_CONTAINER_ID": "foobar",
    }
    clihack.check = _assert
    clihack.run(
        [
            "--passwd-location",
            "/etc/passwd:/var/lib/samba/passwd.txt:/lib/passwd",
            "--group-location",
            "/etc/group:/var/lib/samba/group.txt:/lib/group",
            "check",
        ]
    )


def test_cli_altfiles_env(clihack):
    def _assert(ctx):
        assert ctx.cli.passwd_location.default_path == "/etc/passwd"
        assert ctx.cli.passwd_location.altfiles
        assert (
            ctx.cli.passwd_location.mutable_path == "/var/lib/samba/passwd.txt"
        )
        assert ctx.cli.passwd_location.link_path == "/lib/passwd"

        assert ctx.cli.group_location.default_path == "/etc/group"
        assert ctx.cli.group_location.altfiles
        assert (
            ctx.cli.group_location.mutable_path == "/var/lib/samba/group.txt"
        )
        assert ctx.cli.group_location.link_path == "/lib/group"

    clihack.test_env = {
        "SAMBACC_CONFIG": str(clihack.fname),
        "SAMBA_CONTAINER_ID": "foobar",
        "SAMBACC_PASSWD_LOCATION": (
            "/etc/passwd:/var/lib/samba/passwd.txt:/lib/passwd"
        ),
        "SAMBACC_GROUP_LOCATION": (
            "/etc/group:/var/lib/samba/group.txt:/lib/group"
        ),
    }
    clihack.check = _assert
    clihack.run(["check"])
