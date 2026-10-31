#!/usr/bin/env python3
"""SMART Sniffer App: the mounts the agent measures for disk space.

The agent measures disk space only for the filesystems its config lists. This
reads the container's mount table, keeps the block-device mounts that Home
Assistant gives the app (/data, and /media and /share with what is mounted
under them), one per device, and writes them as the agent's config:

  filesystems: [{path, uuid, device, fstype}, ...]
  token: the app's token option, when one is set

The token comes from the AGENT_TOKEN environment variable, so it is never on
a command line, and the file is readable by root only. The file is JSON,
which the agent's YAML parser reads as it is. It is always written, with an
empty list when nothing is found, since the agent refuses to start when
--config names a missing file. Each mount kept is printed as one line for
the app log.

Exit status: 0 written; 2 written without filesystems, as the mount table
could not be read (disk space is off, the token still applies); 1 not
written.

/data, /media and /share all sit on the data disk (the boot drive, or the
drive picked with Move data disk), so they collapse to /data.
Network shares, tmpfs and overlay mounts have no /dev/ device and are left out.

Drives added in Home Assistant under Settings > System > Storage > Local
disk (HA 2026.11) are systemd automounts. Each /media/<name> shows two
lines: the automount's autofs (source "systemd-1") and, once the disk is
mounted, the disk on top of it with its real device (systemd mounts it by
its canonical /dev path, never by a symlink). The Supervisor normally
mounts it before the App starts. A /media entry that shows only its autofs
line (the disk was not there at boot, or was unplugged and plugged back) is
woken once with statvfs, which mounts it (stat would not), within
TRIGGER_SECONDS, and the mount table read again. A disk added after the App
started shows after an App restart.

Run with:
  python3 /opt/mounts.py --out /data/agent-config.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

MOUNTINFO = "/proc/self/mountinfo"
BY_UUID = "/dev/disk/by-uuid"
# In this order: the first mount of a device is the one kept.
ROOTS = ("/data", "/media", "/share")
# How long all waking of automounts may take together, so a disk that is not
# there never holds up the App's start.
TRIGGER_SECONDS = 5.0


class Mount(NamedTuple):
    path: str
    device: str
    fstype: str


def _unescape(field: str) -> str:
    """The kernel writes space, tab, newline and backslash as octal (\\040)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _parse_line(line: str) -> Mount | None:
    """One mountinfo line: "id parent maj:min root mountpoint options
    [optional fields...] - fstype source superoptions"."""
    fields = line.split()
    if "-" not in fields:
        return None
    sep = fields.index("-")
    if sep < 6 or len(fields) < sep + 3:
        return None
    return Mount(_unescape(fields[4]), _unescape(fields[sep + 2]), fields[sep + 1])


def parse_mountinfo(text: str) -> list[Mount]:
    """Every well-formed line of /proc/self/mountinfo; the rest are skipped."""
    return [m for m in map(_parse_line, text.splitlines()) if m is not None]


def _rank(path: str) -> int | None:
    """The position of the root the path is, or is under; None for neither.
    /data counts only as itself."""
    return next((i for i, root in enumerate(ROOTS)
                 if path == root or (root != "/data" and path.startswith(root + "/"))), None)


def select_mounts(mounts: list[Mount]) -> list[Mount]:
    """The block-device mounts under the roots, one per device: /data first,
    then /media's, then /share's."""
    ranked = sorted(((rank, m) for m in mounts
                     if m.device.startswith("/dev/") and (rank := _rank(m.path)) is not None),
                    key=lambda ranked_mount: ranked_mount[0])
    firsts: dict[str, Mount] = {}
    for _, m in ranked:
        firsts.setdefault(m.device, m)
    return list(firsts.values())


def sleeping_automounts(mounts: list[Mount]) -> list[str]:
    """The /media/<name> mount points that show only an autofs mount: a
    systemd automount whose disk is not mounted yet."""
    mounted = {m.path for m in mounts if m.fstype != "autofs"}
    return list(dict.fromkeys(m.path for m in mounts
                              if m.fstype == "autofs" and m.path.startswith("/media/")
                              and m.path not in mounted))


def wake(path: str) -> None:
    """Ask for the filesystem at the path, which mounts an automount. statvfs,
    not stat: the kernel never automounts for stat. A missing disk is an
    error here, and is ignored."""
    try:
        os.statvfs(path)
    except OSError:
        pass


def wake_all(paths: list[str], timeout: float | None = None,
             waker: Callable[[str], None] | None = None) -> None:
    """Wake every path at once and wait at most timeout seconds in all; a
    wake still waiting then is left behind (its thread ends with the
    process)."""
    threads = [threading.Thread(target=waker or wake, args=(p,), daemon=True) for p in paths]
    for t in threads:
        t.start()
    deadline = time.monotonic() + (TRIGGER_SECONDS if timeout is None else timeout)
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))


def read_mounts(mountinfo: str, waker: Callable[[str], None] | None = None,
                timeout: float | None = None) -> list[Mount]:
    """The mount table, after waking any /media automount not mounted yet
    (and reading the table again when there was one)."""
    def read() -> list[Mount]:
        return parse_mountinfo(Path(mountinfo).read_text(encoding="utf-8", errors="replace"))

    mounts = read()
    sleeping = sleeping_automounts(mounts)
    if sleeping:
        wake_all(sleeping, timeout, waker)
        mounts = read()
    return mounts


def uuid_for(device: str, by_uuid_dir: str = BY_UUID) -> str:
    """The filesystem UUID whose /dev/disk/by-uuid link points at the device,
    or "" when there is none (the agent then names it by its mountpoint).
    Devices are compared by name, so /dev/mapper links resolve to their dm-N."""
    try:
        names = sorted(os.listdir(by_uuid_dir))
    except OSError:
        return ""
    target = os.path.basename(os.path.realpath(device))
    return next((n for n in names
                 if os.path.basename(os.path.realpath(os.path.join(by_uuid_dir, n))) == target), "")


def agent_config(mounts: list[Mount], uuid_lookup: Callable[[str], str] = uuid_for,
                 token: str = "") -> dict[str, Any]:
    """The agent's config for these mounts, and the token when there is one."""
    config: dict[str, Any] = {"filesystems": [{"path": m.path, "uuid": uuid_lookup(m.device),
                                               "device": m.device, "fstype": m.fstype}
                                              for m in mounts]}
    if token:
        config["token"] = token
    return config


def write_private(path: str, text: str) -> None:
    """Write the file readable by root only, also when it was there before."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the agent config listing the mounts to measure")
    parser.add_argument("--out", required=True, help="the agent config file to write")
    parser.add_argument("--mountinfo", default=MOUNTINFO, help=f"mount table to read (default {MOUNTINFO})")
    parser.add_argument("--by-uuid", default=BY_UUID, help=f"UUID links to read (default {BY_UUID})")
    args = parser.parse_args(argv)

    status = 0
    try:
        mounts = select_mounts(read_mounts(args.mountinfo))
    except OSError as err:
        print(err, file=sys.stderr)
        mounts, status = [], 2
    config = agent_config(mounts, lambda device: uuid_for(device, args.by_uuid),
                          os.environ.get("AGENT_TOKEN", ""))
    try:
        write_private(args.out, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    except OSError as err:
        print(err, file=sys.stderr)
        return 1
    for m in mounts:
        print(f"{m.path} on {m.device} ({m.fstype})")
    return status


if __name__ == "__main__":
    sys.exit(main())
