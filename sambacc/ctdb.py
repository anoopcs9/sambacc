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

import os
import subprocess
import typing

from sambacc import config
from sambacc import jfile
from sambacc import samba_cmds
from sambacc.netcmd_loader import template_config


DB_DIR = "/var/lib/ctdb/persistent"
ETC_DIR = "/etc/ctdb"
SHARE_DIR = "/usr/share/ctdb"

CTDB_CONF: str = "/etc/ctdb/ctdb.conf"
CTDB_NODES: str = "/etc/ctdb/nodes"


def ensure_smb_conf(
    iconfig: config.InstanceConfig, path: str = config.SMB_CONF
) -> None:
    """Ensure that the smb.conf on disk is ctdb and registry enabled."""
    with open(path, "w") as fh:
        write_smb_conf(fh, iconfig)


def write_smb_conf(fh: typing.IO, iconfig: config.InstanceConfig) -> None:
    """Write an smb.conf style output enabling ctdb and samba registry."""
    template_config(fh, iconfig.ctdb_smb_config())


def ensure_ctdb_conf(
    iconfig: config.InstanceConfig, path: str = CTDB_CONF
) -> None:
    """Ensure that the ctdb.conf on disk matches our desired state."""
    with open(path, "w") as fh:
        write_ctdb_conf(fh, iconfig.ctdb_config())


def write_ctdb_conf(fh: typing.IO, ctdb_params: typing.Dict, enc=str) -> None:
    """Write a ctdb.conf style output."""

    def _write_param(fh: typing.IO, name: str, key: str) -> None:
        value = ctdb_params.get(key)
        if value is None:
            return
        fh.write(enc(f"{name} = {value}\n"))

    fh.write(enc("[logging]\n"))
    _write_param(fh, "log level", "log_level")
    fh.write(enc("\n"))
    fh.write(enc("[cluster]\n"))
    _write_param(fh, "recovery lock", "recovery_lock")
    fh.write(enc("\n"))
    fh.write(enc("[legacy]\n"))
    _write_param(fh, "realtime scheduling", "realtime_scheduling")
    _write_param(fh, "script log level", "script_log_level")
    fh.write(enc("\n"))


def ensure_ctdb_nodes(
    ctdb_nodes: typing.List[str], real_path: str, canon_path: str = CTDB_NODES
) -> None:
    """Ensure a real nodes file exists, containing the specificed content,
    and has a symlink in the proper place for ctdb.
    """
    try:
        os.unlink(canon_path)
    except FileNotFoundError:
        pass
    os.symlink(real_path, canon_path)
    # XXX: add locking?
    with open(real_path, "w") as fh:
        write_nodes_file(fh, ctdb_nodes)


def write_nodes_file(
    fh: typing.IO, ctdb_nodes: typing.List[str], enc=str
) -> None:
    """Write the ctdb nodes file."""
    for node in ctdb_nodes:
        fh.write(enc(f"{node}\n"))


def read_nodes_file(fh: typing.IO) -> typing.List[str]:
    """Read content from an open ctdb nodes file."""
    entries = []
    for line in fh:
        entries.append(line.strip())
    return entries


def read_ctdb_nodes(path: str = CTDB_NODES) -> typing.List[str]:
    """Read the content of the ctdb nodes file."""
    try:
        with open(path, "r") as fh:
            entries = read_nodes_file(fh)
    except FileNotFoundError:
        return []
    return entries


def ensure_ctdb_node_present(
    node: str,
    real_path: str,
    canon_path: str = CTDB_NODES,
    expected_pnn: typing.Optional[int] = None,
) -> None:
    """Ensure that the ctdb nodes file is populated with at least the
    node given. The optional `expect_pnn` can be provided to ensure that
    the node occupies the correct position in the nodes file.
    """
    nodes = read_ctdb_nodes(real_path)
    if node not in nodes:
        nodes.append(node)
    if expected_pnn is not None:
        try:
            found_pnn = nodes.index(node)
        except ValueError:
            found_pnn = -1
        if expected_pnn != found_pnn:
            raise ValueError(f"expected pnn {expected_pnn} is not {found_pnn}")
    ensure_ctdb_nodes(nodes, real_path=real_path, canon_path=canon_path)


def add_node_to_statefile(
    node: str, pnn: int, path: str, in_nodes: bool = False
) -> None:
    """Add the given node (IP) at the line for the given PNN to
    the ctdb nodes file located at path.
    """
    with jfile.open(path, jfile.OPEN_RW) as fh:
        jfile.flock(fh)
        data = jfile.load(fh, {})
        _update_statefile(data, node, pnn, in_nodes=in_nodes)
        jfile.dump(data, fh)


def _update_statefile(
    data, node: str, pnn: int, in_nodes: bool = False
) -> None:
    data.setdefault("nodes", [])
    for entry in data["nodes"]:
        if pnn == entry["pnn"]:
            raise ValueError("duplicate pnn")
    data["nodes"].append(
        {
            "node": node,
            "pnn": pnn,
            "in_nodes": in_nodes,
        }
    )


def pnn_in_nodes(pnn: int, nodes_json: str, real_path: str) -> bool:
    """Returns true if the specified pnn has an entry in the nodes json
    file.
    """
    with jfile.open(nodes_json, jfile.OPEN_RO) as fh:
        jfile.flock(fh)
        json_data = jfile.load(fh, {})
        current_nodes = json_data.get("nodes", [])
        for entry in current_nodes:
            if pnn == entry["pnn"] and entry["in_nodes"]:
                return True
    return False


def manage_nodes(
    pnn: int, nodes_json: str, real_path: str, pause_func
) -> None:
    """Monitor nodes json for updates, reflecting those changes into ctdb."""
    while True:
        print("checking if node is able to make updates")
        if _node_check(pnn, nodes_json, real_path):
            print("checking for node updates")
            if _node_update(nodes_json, real_path):
                print("updated nodes")
        else:
            print("node can not make updates")
        pause_func()


def _node_check(pnn: int, nodes_json: str, real_path: str) -> bool:
    with jfile.open(nodes_json, jfile.OPEN_RO) as fh:
        jfile.flock(fh)
        desired = jfile.load(fh, {}).get("nodes", [])
    ctdb_nodes = read_ctdb_nodes(real_path)
    # first: check to see if the current node is in the nodes file
    try:
        my_desired = [e for e in desired if e.get("pnn") == pnn][0]
    except IndexError:
        # no entry found for this node
        print(f"PNN {pnn} not found in json state file")
        return False
    if my_desired["node"] not in ctdb_nodes:
        # this current node is not in the nodes file.
        # it is ineligible to make changes to the nodes file
        return False
    # this node is already in the nodes file!
    return True


def _node_update_check(json_data, nodes_json: str, real_path: str):
    desired = json_data.get("nodes", [])
    ctdb_nodes = read_ctdb_nodes(real_path)
    new_nodes = []
    need_reload = []
    for entry in desired:
        pnn = entry["pnn"]
        try:
            matched = entry["node"] == ctdb_nodes[pnn]
        except IndexError:
            matched = False
        if matched and entry["in_nodes"]:
            # everything's fine. skip this entry
            continue
        if not entry["in_nodes"]:
            need_reload.append(entry)
            continue
        if len(ctdb_nodes) > entry["pnn"]:
            msg = f'unexpected pnn {entry["pnn"]} for nodes {ctdb_nodes}'
            raise ValueError(msg)
        new_nodes.append(entry["node"])
        need_reload.append(entry)
    return ctdb_nodes, new_nodes, need_reload


def _node_update(nodes_json: str, real_path: str) -> bool:
    # open r/o so that we don't initailly open for write.  we do a probe and
    # decide if anything needs to be updated if we are wrong, its not a
    # problem, we'll "time out" and reprobe later
    with jfile.open(nodes_json, jfile.OPEN_RO) as fh:
        jfile.flock(fh)
        json_data = jfile.load(fh, {})
        _, test_new_nodes, test_need_reload = _node_update_check(
            json_data, nodes_json, real_path
        )
        if not test_new_nodes and not test_need_reload:
            print("examined nodes state - no changes")
            return False
    # we probably need to make a change. but we recheck our state again
    # under lock, with the data file open r/w
    # update the nodes file and make changes to ctdb
    with jfile.open(nodes_json, jfile.OPEN_RW) as fh:
        jfile.flock(fh)
        json_data = jfile.load(fh, {})
        ctdb_nodes, new_nodes, need_reload = _node_update_check(
            json_data, nodes_json, real_path
        )
        if not new_nodes and not need_reload:
            print("reexamined nodes state - no changes")
            return False
        print("writing updates to ctdb nodes file")
        all_nodes = ctdb_nodes + new_nodes
        with open(real_path, "w") as nffh:
            write_nodes_file(nffh, all_nodes)
            nffh.flush()
            os.fsync(nffh)
        print("running: ctdb reloadnodes")
        subprocess.check_call(list(samba_cmds.ctdb["reloadnodes"]))
        for entry in need_reload:
            entry["in_nodes"] = True
        jfile.dump(json_data, fh)
        fh.flush()
        os.fsync(fh)
    return True


def ensure_ctdbd_etc_files(
    etc_path: str = ETC_DIR, src_path: str = SHARE_DIR
) -> None:
    """Ensure certain files that ctdbd expects to exist in its etc dir
    do exist.
    """
    functions_src = os.path.join(src_path, "functions")
    functions_dst = os.path.join(etc_path, "functions")
    notify_src = os.path.join(src_path, "notify.sh")
    notify_dst = os.path.join(etc_path, "notify.sh")
    legacy_scripts_src = os.path.join(src_path, "events/legacy")
    legacy_scripts_dst = os.path.join(etc_path, "events/legacy")
    link_legacy_scripts = ["00.ctdb.script"]

    os.makedirs(etc_path, exist_ok=True)
    try:
        os.unlink(functions_dst)
    except FileNotFoundError:
        pass
    os.symlink(functions_src, functions_dst)

    try:
        os.unlink(notify_dst)
    except FileNotFoundError:
        pass
    os.symlink(notify_src, notify_dst)

    os.makedirs(legacy_scripts_dst, exist_ok=True)
    for legacy_script_name in link_legacy_scripts:
        lscript_src = os.path.join(legacy_scripts_src, legacy_script_name)
        lscript_dst = os.path.join(legacy_scripts_dst, legacy_script_name)
        try:
            os.unlink(lscript_dst)
        except FileNotFoundError:
            pass
        os.symlink(lscript_src, lscript_dst)


_SRC_TDB_FILES = [
    "account_policy.tdb",
    "group_mapping.tdb",
    "passdb.tdb",
    "registry.tdb",
    "secrets.tdb",
    "share_info.td",
    "winbindd_idmap.tdb",
]

_SRC_TDB_DIRS = [
    "/var/lib/samba",
    "/var/lib/samba/private",
]


def migrate_tdb(
    iconfig: config.InstanceConfig, dest_dir: str, pnn: int = 0
) -> None:
    """Migrate TDB files into CTDB."""
    # TODO: these paths should be based on our instance config, not hard coded
    for tdbfile in _SRC_TDB_FILES:
        for parent in _SRC_TDB_DIRS:
            tdb_path = os.path.join(parent, tdbfile)
            if _has_tdb_file(tdb_path):
                _convert_tdb_file(tdb_path, dest_dir, pnn=pnn)


def _has_tdb_file(tdb_path: str) -> bool:
    # TODO: It would be preferable to handle errors from the convert
    # function only, but it if ltdbtool is missing it raises FileNotFoundError
    # and its not simple to disambiguate between the command missing and the
    # tdb file missing.
    print(f"Checking for {tdb_path}")
    return os.path.isfile(tdb_path)


def _convert_tdb_file(tdb_path: str, dest_dir: str, pnn: int = 0) -> None:
    orig_name = os.path.basename(tdb_path)
    opath = os.path.join(dest_dir, f"{orig_name}.{pnn}")
    print(f"Converting {tdb_path} to {opath} ...")
    cmd = samba_cmds.ltdbtool["convert", "-s0", tdb_path, opath]
    subprocess.check_call(list(cmd))
