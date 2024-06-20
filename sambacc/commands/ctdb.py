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

import argparse
import logging
import os
import socket
import typing

from sambacc import ctdb
from sambacc import jfile
from sambacc import rados_opener
from sambacc.simple_waiter import Sleeper, Waiter

from .cli import best_waiter, commands, Context, Fail

_logger = logging.getLogger(__name__)

# Rather irritatingly, k8s does not have a simple method for passing the
# ordinal index of a stateful set down to the containers. This has been
# proposed but not implemented yet. See:
#  https://github.com/kubernetes/kubernetes/issues/40651
# While I find putting any k8s specific knowledge in sambacc distasteful
# all we're really doing is teaching sambacc how to extract the node
# number from the host name, an operation that's not k8s specific.
# That isn't *too* dirty. Just a smudge really. :-)
_AFTER_LAST_DASH = "after-last-dash"


def _ctdb_ok():
    sambacc_ctdb = os.environ.get("SAMBACC_CTDB")
    gate = "ctdb-is-experimental"
    if sambacc_ctdb == gate:
        return
    print("Using CTDB with samba-container (sambacc) is experimental.")
    print("If you are developing or testing features for sambacc please")
    print("set the environment variable SAMBACC_CTDB to the value:")
    print("    ", gate)
    print("before continuing and try again.")
    print()
    raise Fail(gate)


def _ctdb_migrate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dest-dir",
        default=ctdb.DB_DIR,
        help="Specify where CTDB database files will be written.",
    )


def _ctdb_general_node_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hostname",
        help="Specify the host name for the CTDB node",
    )
    parser.add_argument(
        "--node-number",
        type=int,
        help="Expected node number",
    )
    # This is a choice with a single acceptable param, rather than an on/off
    # bool, # in the case that other container orchs have a similar but not
    # quite the same issue and we want to support a different scheme someday.
    parser.add_argument(
        "--take-node-number-from-hostname",
        choices=(_AFTER_LAST_DASH,),
        help=(
            "Take the node number from the given host name following"
            " the specified policy."
        ),
    )
    parser.add_argument(
        "--persistent-path",
        help="Path to a persistent path for storing nodes file",
    )
    parser.add_argument(
        "--metadata-source",
        help=(
            "Specify location of cluster metadata state-tracking object."
            " This can be a file path or a URI-style identifier."
        ),
    )


def _ctdb_set_node_args(parser: argparse.ArgumentParser) -> None:
    _ctdb_general_node_args(parser)
    parser.add_argument(
        "--ip",
        help="Specify node by IP",
    )


class NodeParams:
    _ctx: Context
    node_number: typing.Optional[int] = None
    hostname: typing.Optional[str] = None
    persistent_path: str = ""
    _nodes_json: str = ""
    _cluster_meta_uri: str = ""
    _ip_addr: typing.Optional[str] = None
    _cluster_meta_obj: typing.Optional[ctdb.ClusterMeta] = None
    _waiter_obj: typing.Optional[Waiter] = None

    def __init__(self, ctx: Context):
        self._ctx = ctx
        ccfg = ctx.instance_config.ctdb_config()

        # stuff that many of the commands use
        self.persistent_path = ctx.cli.persistent_path
        if self.persistent_path is None:
            self.persistent_path = ccfg["nodes_path"]
        # nodes_json will now only be in the ctdb config section if it has been
        # specified by the user.
        self._nodes_json = ccfg.get("nodes_json") or ""
        # cluster_meta_uri can be a uri-ish string or path. It will be set with
        # a default value by the config even if there's no user supplied value.
        self._cluster_meta_uri = ccfg.get("cluster_meta_uri") or ""

        self.hostname = ctx.cli.hostname
        if ctx.cli.node_number is not None:
            if ctx.cli.node_number < 0:
                raise ValueError(f"invalid node number: {ctx.cli.node_number}")
            self.node_number = ctx.cli.node_number
        elif ctx.cli.take_node_number_from_hostname == _AFTER_LAST_DASH:
            if not self.hostname:
                raise ValueError(
                    "--hostname required if taking node number from host name"
                )
            if "-" not in self.hostname:
                raise ValueError(
                    f"invalid hostname for node number: {self.hostname}"
                )
            self.node_number = int(self.hostname.rsplit("-")[-1])
        else:
            self.node_number = None

    @property
    def node_ip_addr(self) -> str:
        if self._ip_addr is None:
            cli = self._ctx.cli
            if getattr(cli, "ip", None):
                self._ip_addr = cli.ip
            elif cli.hostname:
                self._ip_addr = _lookup_hostname(cli.hostname)
            else:
                raise ValueError("can not determine node ip")
        return self._ip_addr

    @property
    def identity(self) -> str:
        # this could be extended to use something like /etc/machine-id
        # or whatever in the future.
        if self.hostname:
            return self.hostname
        elif self.node_number:
            return f"node-{self.node_number}"
        else:
            # the dashes make this an invalid dns name
            return "-unknown-"

    @property
    def cluster_meta_uri(self) -> str:
        """Return a cluster meta uri value."""
        values = (
            # cli takes highest precedence
            self._ctx.cli.metadata_source,
            # _nodes_json should only be set if user set it using the old key
            self._nodes_json,
            # default or customized value on current key
            self._cluster_meta_uri,
        )
        for uri in values:
            if uri:
                return uri
        raise ValueError("failed to determine cluster_meta_uri")

    def _cluster_meta_init(self) -> None:
        uri = self.cluster_meta_uri
        # it'd be nice to re-use the opener infrastructure here but openers
        # don't do file modes the way we need for JSON state file or do
        # writable file types in the url_opener (urllib wrapper). For now, just
        # manually handle the string.
        if rados_opener.is_rados_uri(uri):
            self._cluster_meta_obj = (
                rados_opener.ClusterMetaRADOSObject.create_from_uri(uri)
            )
            self._waiter_obj = Sleeper()
            return
        if uri.startswith("file:"):
            path = uri.split(":", 1)[-1]
        else:
            path = uri
        if path.startswith("/"):
            path = "/" + path.rstrip("/")  # ensure one leading /
        self._cluster_meta_obj = jfile.ClusterMetaJSONFile(path)
        self._waiter_obj = best_waiter(path)

    def cluster_meta(self) -> ctdb.ClusterMeta:
        if self._cluster_meta_obj is None:
            self._cluster_meta_init()
        assert self._cluster_meta_obj is not None
        return self._cluster_meta_obj

    def cluster_meta_waiter(self) -> Waiter:
        if self._waiter_obj is None:
            self._cluster_meta_init()
        assert self._waiter_obj is not None
        return self._waiter_obj


@commands.command(name="ctdb-migrate", arg_func=_ctdb_migrate_args)
def ctdb_migrate(ctx: Context) -> None:
    """Migrate standard samba databases to CTDB databases."""
    _ctdb_ok()
    ctdb.migrate_tdb(ctx.instance_config, ctx.cli.dest_dir)


def _lookup_hostname(hostname):
    # XXX this is a nasty little hack.
    ips = socket.gethostbyname_ex(hostname)[2]
    addr = [ip for ip in ips if ip != "127.0.0.1"][0]
    _logger.info(f"Determined address for {hostname}: {addr}")
    return addr


@commands.command(name="ctdb-set-node", arg_func=_ctdb_set_node_args)
def ctdb_set_node(ctx: Context) -> None:
    """Set up the current node in the ctdb and sambacc nodes files."""
    _ctdb_ok()
    np = NodeParams(ctx)
    expected_pnn = np.node_number

    try:
        ctdb.refresh_node_in_cluster_meta(
            cmeta=np.cluster_meta(),
            identity=np.identity,
            node=np.node_ip_addr,
            pnn=int(expected_pnn or 0),
        )
        return
    except ctdb.NodeNotPresent:
        pass

    ctdb.add_node_to_cluster_meta(
        cmeta=np.cluster_meta(),
        identity=np.identity,
        node=np.node_ip_addr,
        pnn=int(expected_pnn or 0),
        in_nodes=(expected_pnn == 0),
    )
    if expected_pnn == 0:
        ctdb.ensure_ctdb_node_present(
            node=np.node_ip_addr,
            expected_pnn=expected_pnn,
            real_path=np.persistent_path,
        )


@commands.command(name="ctdb-manage-nodes", arg_func=_ctdb_general_node_args)
def ctdb_manage_nodes(ctx: Context) -> None:
    """Run a long lived procees to monitor the node state file for new nodes.
    When a new node is found, if the current node is in the correct state, this
    node will add it to CTDB.
    """
    _ctdb_ok()
    np = NodeParams(ctx)
    expected_pnn = np.node_number or 0
    waiter = np.cluster_meta_waiter()

    errors = 0
    while True:
        try:
            ctdb.monitor_cluster_meta_updates(
                cmeta=np.cluster_meta(),
                pnn=expected_pnn,
                real_path=np.persistent_path,
                pause_func=waiter.wait,
            )
            errors = 0
        except KeyboardInterrupt:
            raise
        except Exception as err:
            _logger.error(
                f"error during manage_nodes: {err}, count={errors}",
                exc_info=True,
            )
            errors += 1
            if errors > 10:
                _logger.error(f"too many retries ({errors}). giving up")
                raise
            waiter.wait()


@commands.command(name="ctdb-must-have-node", arg_func=_ctdb_general_node_args)
def ctdb_must_have_node(ctx: Context) -> None:
    """Block until the current node is present in the ctdb nodes file."""
    _ctdb_ok()
    np = NodeParams(ctx)
    expected_pnn = np.node_number or 0
    waiter = np.cluster_meta_waiter()

    while True:
        if ctdb.pnn_in_cluster_meta(
            cmeta=np.cluster_meta(),
            pnn=expected_pnn,
        ):
            return
        _logger.info("node not yet ready")
        waiter.wait()
