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

import io
import json
import os

import pytest

import sambacc.config
import sambacc.samba_cmds
from sambacc import ctdb


def test_migrate_tdb(tmpdir, monkeypatch):
    src = tmpdir / "src"
    os.mkdir(src)
    dst = tmpdir / "dst"
    os.mkdir(dst)
    fake = tmpdir / "fake.sh"
    monkeypatch.setattr(ctdb, "_SRC_TDB_DIRS", [str(src)])
    monkeypatch.setattr(sambacc.samba_cmds, "_GLOBAL_PREFIX", [str(fake)])

    with open(fake, "w") as fh:
        fh.write("#!/bin/sh\n")
        fh.write('[ "$1" == "ltdbtool" ] || exit 1\n')
        fh.write('[ "$2" == "convert" ] || exit 1\n')
        fh.write('exec cp "$4" "$5"\n')
    os.chmod(fake, 0o755)

    with open(src / "registry.tdb", "w") as fh:
        fh.write("fake")
    with open(src / "passdb.tdb", "w") as fh:
        fh.write("fake")
    with open(src / "mango.tdb", "w") as fh:
        fh.write("fake")

    ctdb.migrate_tdb(None, str(dst))

    assert os.path.exists(dst / "registry.tdb.0")
    assert os.path.exists(dst / "passdb.tdb.0")
    assert not os.path.exists(dst / "mango.tdb.0")


def test_ensure_ctdbd_etc_files(tmpdir):
    src = tmpdir / "src"
    os.mkdir(src)
    dst = tmpdir / "dst"
    os.mkdir(dst)

    # this largely just creates a bunch of symlinks so it doesn't
    # need much fakery.
    ctdb.ensure_ctdbd_etc_files(etc_path=dst, src_path=src)
    assert os.path.islink(dst / "functions")
    assert os.path.islink(dst / "notify.sh")
    assert os.path.islink(dst / "events/legacy/00.ctdb.script")


def test_pnn_in_nodes(tmpdir):
    nodes_json = tmpdir / "nodes.json"
    real_path = tmpdir / "nodes"

    with pytest.raises(Exception):
        ctdb.pnn_in_nodes(0, nodes_json, real_path)

    with open(nodes_json, "w") as fh:
        fh.write("{}")
    result = ctdb.pnn_in_nodes(0, nodes_json, real_path)
    assert not result

    with open(nodes_json, "w") as fh:
        fh.write(
            """
            {"nodes": [
                {"node": "10.0.0.10", "pnn": 0, "in_nodes": true},
                {"node": "10.0.0.11", "pnn": 1, "in_nodes": false}
            ]}
        """
        )
    result = ctdb.pnn_in_nodes(0, nodes_json, real_path)
    assert result
    result = ctdb.pnn_in_nodes(1, nodes_json, real_path)
    assert not result


class _Stop(Exception):
    pass


def test_manage_nodes(tmpdir, monkeypatch):
    nodes_json = tmpdir / "nodes.json"
    real_path = tmpdir / "nodes"
    monkeypatch.setattr(sambacc.samba_cmds, "_GLOBAL_PREFIX", ["true"])

    def once():
        raise _Stop()

    with pytest.raises(FileNotFoundError):
        ctdb.manage_nodes(
            0, nodes_json=nodes_json, real_path=real_path, pause_func=once
        )

    # node not present - can not update
    with open(nodes_json, "w") as fh:
        fh.write(
            """
            {"nodes": [
            ]}
        """
        )
    with pytest.raises(_Stop):
        ctdb.manage_nodes(
            0, nodes_json=nodes_json, real_path=real_path, pause_func=once
        )

    # node present, not in nodes - can not update
    with open(nodes_json, "w") as fh:
        fh.write(
            """
            {"nodes": [
                {"node": "10.0.0.10", "pnn": 0, "in_nodes": false}
            ]}
        """
        )
    with pytest.raises(_Stop):
        ctdb.manage_nodes(
            0, nodes_json=nodes_json, real_path=real_path, pause_func=once
        )

    # node present, in nodes - nothing to do
    with open(nodes_json, "w") as fh:
        fh.write(
            """
            {"nodes": [
                {"node": "10.0.0.10", "pnn": 0, "in_nodes": true}
            ]}
        """
        )
    with open(real_path, "w") as fh:
        fh.write("10.0.0.10\n")
    with pytest.raises(_Stop):
        ctdb.manage_nodes(
            0, nodes_json=nodes_json, real_path=real_path, pause_func=once
        )

    # node present, in nodes - new node in json
    with open(nodes_json, "w") as fh:
        fh.write(
            """
            {"nodes": [
                {"node": "10.0.0.10", "pnn": 0, "in_nodes": true},
                {"node": "10.0.0.11", "pnn": 1, "in_nodes": false}
            ]}
        """
        )
    with open(real_path, "w") as fh:
        fh.write("10.0.0.10\n")
    with pytest.raises(_Stop):
        ctdb.manage_nodes(
            0, nodes_json=nodes_json, real_path=real_path, pause_func=once
        )
    with open(real_path, "r") as fh:
        lines = [x.strip() for x in fh.readlines()]
    assert "10.0.0.10" in lines
    assert "10.0.0.11" in lines

    # invalid state - nodes file and nodes json out of whack
    with open(nodes_json, "w") as fh:
        fh.write(
            """
            {"nodes": [
                {"node": "10.0.0.10", "pnn": 0, "in_nodes": true},
                {"node": "10.0.0.11", "pnn": 1, "in_nodes": false}
            ]}
        """
        )
    with open(real_path, "w") as fh:
        fh.write("10.0.0.10\n")
        fh.write("10.0.0.12\n")
        fh.write("10.0.0.13\n")
    with pytest.raises(ValueError):
        ctdb.manage_nodes(
            0, nodes_json=nodes_json, real_path=real_path, pause_func=once
        )

    # node present but json file shows update incomplete
    with open(nodes_json, "w") as fh:
        fh.write(
            """
            {"nodes": [
                {"node": "10.0.0.10", "pnn": 0, "in_nodes": true},
                {"node": "10.0.0.11", "pnn": 1, "in_nodes": false}
            ]}
        """
        )
    with open(real_path, "w") as fh:
        fh.write("10.0.0.10\n")
        fh.write("10.0.0.11\n")
    with pytest.raises(_Stop):
        ctdb.manage_nodes(
            0, nodes_json=nodes_json, real_path=real_path, pause_func=once
        )
    with open(real_path, "r") as fh:
        lines = [x.strip() for x in fh.readlines()]
    assert "10.0.0.10" in lines
    assert "10.0.0.11" in lines
    with open(nodes_json, "r") as fh:
        jdata = json.load(fh)
    assert jdata["nodes"][1]["node"] == "10.0.0.11"
    assert jdata["nodes"][1]["in_nodes"]


def test_manage_nodes_refresh_fails(tmpdir, monkeypatch):
    nodes_json = tmpdir / "nodes.json"
    real_path = tmpdir / "nodes"
    monkeypatch.setattr(sambacc.samba_cmds, "_GLOBAL_PREFIX", ["false"])

    def once():
        raise _Stop()

    # node needs to be added
    with open(nodes_json, "w") as fh:
        fh.write(
            """
            {"nodes": [
                {"node": "10.0.0.10", "pnn": 0, "in_nodes": true},
                {"node": "10.0.0.11", "pnn": 1, "in_nodes": false}
            ]}
        """
        )
    with open(real_path, "w") as fh:
        fh.write("10.0.0.10\n")
    with pytest.raises(Exception):
        ctdb.manage_nodes(
            0, nodes_json=nodes_json, real_path=real_path, pause_func=once
        )
    with open(real_path, "r") as fh:
        lines = [x.strip() for x in fh.readlines()]
    assert "10.0.0.10" in lines
    assert "10.0.0.11" in lines
    with open(nodes_json, "r") as fh:
        jdata = json.load(fh)
    assert jdata["nodes"][1]["node"] == "10.0.0.11"
    assert not jdata["nodes"][1]["in_nodes"]


def test_add_node_to_statefile(tmpdir):
    nodes_json = tmpdir / "nodes.json"

    ctdb.add_node_to_statefile(
        node="10.0.0.10", pnn=0, path=nodes_json, in_nodes=True
    )
    with open(nodes_json, "r") as fh:
        jdata = json.load(fh)
    assert jdata["nodes"][0]["node"] == "10.0.0.10"
    assert jdata["nodes"][0]["pnn"] == 0
    assert jdata["nodes"][0]["in_nodes"]

    with pytest.raises(ValueError):
        ctdb.add_node_to_statefile(
            node="10.0.0.11", pnn=0, path=nodes_json, in_nodes=False
        )

    ctdb.add_node_to_statefile(
        node="10.0.0.11", pnn=1, path=nodes_json, in_nodes=False
    )
    with open(nodes_json, "r") as fh:
        jdata = json.load(fh)
    assert jdata["nodes"][0]["node"] == "10.0.0.10"
    assert jdata["nodes"][0]["pnn"] == 0
    assert jdata["nodes"][0]["in_nodes"]
    assert jdata["nodes"][1]["node"] == "10.0.0.11"
    assert jdata["nodes"][1]["pnn"] == 1
    assert not jdata["nodes"][1]["in_nodes"]


def test_ensure_ctdb_node_present(tmpdir):
    real_path = tmpdir / "nodes"
    lpath = tmpdir / "nodes.lnk"

    assert not os.path.exists(real_path)

    ctdb.ensure_ctdb_node_present(
        node="10.0.0.10", expected_pnn=0, real_path=real_path, canon_path=lpath
    )
    assert os.path.islink(lpath)
    with open(real_path, "r") as fh:
        lines = [x.strip() for x in fh.readlines()]
    assert ["10.0.0.10"] == lines

    ctdb.ensure_ctdb_node_present(
        node="10.0.0.11", expected_pnn=1, real_path=real_path, canon_path=lpath
    )
    assert os.path.islink(lpath)
    with open(real_path, "r") as fh:
        lines = [x.strip() for x in fh.readlines()]
    assert ["10.0.0.10", "10.0.0.11"] == lines

    with pytest.raises(ValueError):
        ctdb.ensure_ctdb_node_present(
            node="10.0.0.11",
            expected_pnn=0,
            real_path=real_path,
            canon_path=lpath,
        )


def test_write_ctdb_conf(tmpdir):
    path = tmpdir / "ctdb.conf"

    params = {
        "log_level": "DEBUG",
        "recovery_lock": "/tmp/foo/lock",
    }
    with open(path, "w") as fh:
        ctdb.write_ctdb_conf(fh, params)
    with open(path, "r") as fh:
        data = fh.read()
    assert "DEBUG" in data
    assert "/tmp/foo/lock" in data


def test_ensure_ctdb_conf(tmpdir):
    from .test_config import ctdb_config1
    from sambacc.config import GlobalConfig

    cfg = GlobalConfig(io.StringIO(ctdb_config1))
    path = tmpdir / "ctdb.conf"

    ctdb.ensure_ctdb_conf(iconfig=cfg.get("ctdb1"), path=path)
    with open(path, "r") as fh:
        data = fh.read()
    assert "DEBUG" in data
    assert "/var/lib/ctdb/shared/RECOVERY" in data


def test_ensure_smb_conf(tmpdir):
    from .test_config import ctdb_config1
    from sambacc.config import GlobalConfig

    cfg = GlobalConfig(io.StringIO(ctdb_config1))
    path = tmpdir / "smb.conf"

    ctdb.ensure_smb_conf(iconfig=cfg.get("ctdb1"), path=path)
    with open(path, "r") as fh:
        data = fh.read()
    assert "clustering = yes" in data
    assert "include = registry" in data