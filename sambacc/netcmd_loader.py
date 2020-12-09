import subprocess


class LoaderError(Exception):
    pass


def _utf8(s):
    return s.encode("utf8")


def template_config(fh, iconfig, enc=str):
    fh.write(enc("[global]\n"))
    for gkey, gval in iconfig.global_options():
        fh.write(enc(f"\t{gkey} = {gval}\n"))

    for share in iconfig.shares():
        fh.write(enc("\n[{}]\n".format(share.name)))
        for skey, sval in share.share_options():
            fh.write(enc(f"\t{skey} = {sval}\n"))


class NetCmdLoader:
    cmd_prefix = ["net", "conf"]

    def _netcmd(self, *args, **kwargs):
        cmd = list(self.cmd_prefix)
        cmd.extend(args)
        return cmd, subprocess.Popen(cmd, **kwargs)

    def _check(self, cli, proc):
        ret = proc.wait()
        if ret != 0:
            raise LoaderError("failed to run {}".format(cli))

    def import_config(self, iconfig):
        """Import to entire instance config to samba config.
        """
        cli, proc = self._netcmd("import", "/dev/stdin", stdin=subprocess.PIPE)
        template_config(proc.stdin, iconfig, enc=_utf8)
        proc.stdin.close()
        self._check(cli, proc)

    def dump(self, out):
        """Dump the current smb config in an smb.conf format.
        Writes the dump to `out`.
        """
        cli, proc = self._netcmd("list", stdout=out)
        self._check(cli, proc)

    def current_shares(self):
        """Returns a list of current shares.
        """
        cli, proc = self._netcmd("listshares", stdout=subprocess.PIPE)
        # read and parse shares list
        self._check(cli, proc)

    def set(self, section, param, value):
        """Set an individual config parameter.
        """
        cli, proc = self._netcmd("steparm", section, param, value)
        self._check(cli, proc)
