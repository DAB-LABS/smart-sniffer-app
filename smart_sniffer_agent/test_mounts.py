"""Tests for mounts.py: which mounts the agent measures for disk space.

Each case is a mount table written out inline; the first is a Raspberry Pi
running HAOS with a USB drive, as the Terminal & SSH app saw it.

Run with:
  python3 -m pytest smart_sniffer_agent/test_mounts.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import mounts
from mounts import Mount

# A Pi 4 on HAOS, SD card, USB drive auto-mounted as /media/batcave. /share,
# /media and /data are binds of the data partition (mmcblk0p8).
REAL_PI = """\
1541 891 0:65 / / rw,relatime - overlay overlay rw,lowerdir=/mnt/data/docker/overlay2/l/QULQAOW4YS2OL2AJ5UOOQF4EPQ:/mnt/data/docker/overlay2/l/W5KWKFEUSE4UUSAF2T54IXMYOT:/mnt/data/docker/overlay2/l/IYADALNPK3Q4A55IBWDTOJBDHY:/mnt/data/docker/overlay2/l/VAEZLCAXP6FH4FYWALPMIMSS3G:/mnt/data/docker/overlay2/l/B7MCM3HM52E5MXFEEDWQD4BDP5:/mnt/data/docker/overlay2/l/2LDMKWW6TNT6U6Y3ZLGB5TQT4G:/mnt/data/docker/overlay2/l/23CNNGPFGJ4BBUGLC2NMRQNVPC:/mnt/data/docker/overlay2/l/7TMIN5G36XZWG35K4IO5PA4TVI:/mnt/data/docker/overlay2/l/UNA7232TZUEMNOAPOYUYWQENCJ:/mnt/data/docker/overlay2/l/HM6U2HGTPM4Q6FFT3KH7EQSN7J,upperdir=/mnt/data/docker/overlay2/deeeac2dcee728f6ea6102fdef3038a0a87574939dd4122bc85bd9a24da81d87/diff,workdir=/mnt/data/docker/overlay2/deeeac2dcee728f6ea6102fdef3038a0a87574939dd4122bc85bd9a24da81d87/work
1546 1541 179:8 /supervisor/share /share rw,relatime master:118 - ext4 /dev/mmcblk0p8 rw,commit=30
1549 1541 179:8 /supervisor/media /media rw,relatime master:118 - ext4 /dev/mmcblk0p8 rw,commit=30
1550 1549 8:1 / /media/batcave rw,relatime master:863 - ext4 /dev/sda1 rw
1553 1541 179:8 /supervisor/apps/data/a0d7b954_ssh /data rw,relatime - ext4 /dev/mmcblk0p8 rw,commit=30
"""

REAL_PI_UUIDS = {
    "047b9c91-6b1c-4940-a1d1-424eb0eaae26": "sda1",
    "6aad20a6-61c7-4733-b8c7-4219c25f0280": "mmcblk0p8",
    "76982c7e-75a7-41b4-bbaa-1fe4d3bca203": "mmcblk0p3",
    "E9B9-ED26": "mmcblk0p1",
    "f0b24eab-bcd2-436c-8b3a-a6eb18ae3740": "mmcblk0p5",
    "fbf076b6-bb5e-4cab-8d9a-072b399f19e5": "mmcblk0p7",
}


def line(n: int, path: str, fstype: str, source: str, optional: str = "") -> str:
    """One mountinfo line; optional is the propagation fields, e.g. master:1."""
    return f"{n} 1 8:{n} / {path} rw,relatime {optional} - {fstype} {source} rw".replace("  ", " ")


@pytest.fixture
def by_uuid(tmp_path):
    """A /dev/disk/by-uuid of the real Pi's links, relative as udev makes them."""
    d = tmp_path / "disk" / "by-uuid"
    d.mkdir(parents=True)
    for uuid, name in REAL_PI_UUIDS.items():
        os.symlink(f"../../{name}", d / uuid)
    return str(d)


def selected(text: str) -> list[tuple[str, str]]:
    return [(m.path, m.device) for m in mounts.select_mounts(mounts.parse_mountinfo(text))]


# ── parse_mountinfo ─────────────────────────────────────────────────────────

def test_parse_reads_mountpoint_device_and_fstype():
    assert mounts.parse_mountinfo(REAL_PI)[3] == Mount("/media/batcave", "/dev/sda1", "ext4")


def test_parse_decodes_octal_escapes():
    text = line(1, r"/media/My\040Passport", "exfat", "/dev/sdb1")
    assert mounts.parse_mountinfo(text) == [Mount("/media/My Passport", "/dev/sdb1", "exfat")]


@pytest.mark.parametrize("text", ["", "\n", "garbage", "1 2 3 4 5 6 7 8", "1 2 8:1 / /data rw - ext4"])
def test_parse_skips_empty_and_garbled_lines(text):
    assert mounts.parse_mountinfo(text) == []


# ── select_mounts ───────────────────────────────────────────────────────────

def test_real_pi_keeps_data_and_the_usb_drive():
    assert selected(REAL_PI) == [("/data", "/dev/mmcblk0p8"), ("/media/batcave", "/dev/sda1")]


def test_external_data_disk_is_measured_as_data():
    text = "\n".join([
        line(1, "/share", "ext4", "/dev/sda8"),
        line(2, "/media", "ext4", "/dev/sda8"),
        line(3, "/data", "ext4", "/dev/sda8"),
    ])
    assert selected(text) == [("/data", "/dev/sda8")]


def test_network_shares_tmpfs_and_overlay_are_dropped():
    text = "\n".join([
        line(1, "/", "overlay", "overlay"),
        line(2, "/data", "ext4", "/dev/nvme0n1p8"),
        line(3, "/media/nas", "cifs", "//nas.local/media"),
        line(4, "/share/nfs", "nfs4", "nas.local:/export"),
        line(5, "/media/tmp", "tmpfs", "tmpfs"),
    ])
    assert selected(text) == [("/data", "/dev/nvme0n1p8")]


def test_duplicate_binds_collapse_to_the_first_root():
    text = "\n".join([
        line(1, "/share/usb", "ext4", "/dev/sdb1"),
        line(2, "/media/usb", "ext4", "/dev/sdb1"),
        line(3, "/media/usb-again", "ext4", "/dev/sdb1"),
    ])
    assert selected(text) == [("/media/usb", "/dev/sdb1")]


def test_mounts_outside_the_roots_are_dropped():
    text = "\n".join([
        line(1, "/config", "ext4", "/dev/sda8"),
        line(2, "/data/sub", "ext4", "/dev/sdc1"),
        line(3, "/mediaX", "ext4", "/dev/sdd1"),
        line(4, "/etc/hosts", "ext4", "/dev/sda8"),
    ])
    assert selected(text) == []


# ── uuid_for ────────────────────────────────────────────────────────────────

def test_uuid_for_follows_the_by_uuid_links(by_uuid):
    assert mounts.uuid_for("/dev/sda1", by_uuid) == "047b9c91-6b1c-4940-a1d1-424eb0eaae26"
    assert mounts.uuid_for("/dev/mmcblk0p8", by_uuid) == "6aad20a6-61c7-4733-b8c7-4219c25f0280"


def test_uuid_for_unknown_device_or_missing_dir_is_empty(by_uuid, tmp_path):
    assert mounts.uuid_for("/dev/sdz9", by_uuid) == ""
    assert mounts.uuid_for("/dev/sda1", str(tmp_path / "missing")) == ""


# ── agent_config and main ───────────────────────────────────────────────────

def test_agent_config_lists_each_mount_with_its_uuid():
    config = mounts.agent_config([Mount("/data", "/dev/sda8", "ext4")], lambda device: f"uuid-of-{device}")
    assert config == {"filesystems": [
        {"path": "/data", "uuid": "uuid-of-/dev/sda8", "device": "/dev/sda8", "fstype": "ext4"},
    ]}


def test_main_writes_the_real_pi_config_and_logs_each_mount(tmp_path, by_uuid, capsys):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(REAL_PI)
    out = tmp_path / "agent-config.yaml"

    assert mounts.main(["--out", str(out), "--mountinfo", str(mountinfo), "--by-uuid", by_uuid]) == 0

    assert json.loads(out.read_text()) == {"filesystems": [
        {"path": "/data", "uuid": "6aad20a6-61c7-4733-b8c7-4219c25f0280",
         "device": "/dev/mmcblk0p8", "fstype": "ext4"},
        {"path": "/media/batcave", "uuid": "047b9c91-6b1c-4940-a1d1-424eb0eaae26",
         "device": "/dev/sda1", "fstype": "ext4"},
    ]}
    assert capsys.readouterr().out.splitlines() == [
        "/data on /dev/mmcblk0p8 (ext4)",
        "/media/batcave on /dev/sda1 (ext4)",
    ]


def test_main_writes_an_empty_list_when_nothing_is_found(tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(line(1, "/", "overlay", "overlay"))
    out = tmp_path / "agent-config.yaml"

    assert mounts.main(["--out", str(out), "--mountinfo", str(mountinfo)]) == 0
    assert json.loads(out.read_text()) == {"filesystems": []}


def test_main_fails_with_one_line_when_a_file_cannot_be_read_or_written(tmp_path, capsys, monkeypatch):
    """An unreadable mount table still writes the config, with no
    filesystems (2: disk space is off, the token applies); a config that
    cannot be written is 1, and run.sh starts the agent without --config."""
    monkeypatch.setenv("AGENT_TOKEN", "secret")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(REAL_PI)
    out = tmp_path / "agent-config.yaml"

    assert mounts.main(["--out", str(out), "--mountinfo", str(tmp_path / "missing")]) == 2
    assert json.loads(out.read_text()) == {"filesystems": [], "token": "secret"}
    assert mounts.main(["--out", str(tmp_path / "no-dir" / "agent-config.yaml"),
                        "--mountinfo", str(mountinfo)]) == 1
    errors = capsys.readouterr().err.splitlines()
    assert len(errors) == 2 and all("No such file or directory" in e for e in errors)


# ── the token ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("token", ["s3cret", "two words * and $HOME ?", "p\u00e4ss\"w'rd"])
def test_main_writes_the_token_from_the_environment_root_only(tmp_path, monkeypatch, token):
    monkeypatch.setenv("AGENT_TOKEN", token)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(REAL_PI)
    out = tmp_path / "agent-config.yaml"
    out.write_text("{}")
    out.chmod(0o644)  # as an older version left it

    assert mounts.main(["--out", str(out), "--mountinfo", str(mountinfo)]) == 0
    config = json.loads(out.read_text(encoding="utf-8"))
    assert config["token"] == token and len(config["filesystems"]) == 2
    assert out.stat().st_mode & 0o777 == 0o600


def test_main_writes_no_token_when_none_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(REAL_PI)
    out = tmp_path / "agent-config.yaml"
    assert mounts.main(["--out", str(out), "--mountinfo", str(mountinfo)]) == 0
    assert "token" not in json.loads(out.read_text())
    assert out.stat().st_mode & 0o777 == 0o600


# ── run.sh and config.yaml ──────────────────────────────────────────────────

RUN_SH = (Path(__file__).resolve().parent / "run.sh").read_text(encoding="utf-8")
CONFIG_YAML = (Path(__file__).resolve().parent / "config.yaml").read_text(encoding="utf-8")


def test_token_option_is_a_password():
    schema = CONFIG_YAML.split("\nschema:\n", 1)[1]
    assert re.search(r'^  token: "password\?"$', schema, re.M)
    assert re.search(r'^  token: ""$', CONFIG_YAML.split("\nschema:\n", 1)[0], re.M)


def test_run_sh_never_puts_the_token_on_a_command_line():
    assert "--token" not in RUN_SH
    # The token reaches mounts.py and proxy.py in the environment only.
    uses = [ln.strip() for ln in RUN_SH.splitlines()
            if "${TOKEN}" in ln and not ln.strip().startswith("#")]
    assert all(ln.startswith(("if bashio::var.has_value", "bashio::var.has_value",
                              'MOUNT_LINES="$(AGENT_TOKEN="${TOKEN}" python3 /opt/mounts.py ',
                              'AGENT_TOKEN="${TOKEN}" python3 /opt/web/proxy.py '))
               for ln in uses), uses
    assert sum('AGENT_TOKEN="${TOKEN}"' in ln for ln in uses) == 2


def test_run_sh_quotes_its_argument_arrays():
    assert RUN_SH.startswith("#!/usr/bin/with-contenv bashio\n")
    assert '/usr/bin/smartha-agent "${AGENT_ARGS[@]}" &' in RUN_SH
    assert 'python3 /opt/web/proxy.py "${PROXY_ARGS[@]}" &' in RUN_SH
    assert not re.search(r"\$\{?(AGENT|PROXY)_ARGS\}?(?!\[@\])", RUN_SH.replace('"${AGENT_ARGS[@]}"', "")
                         .replace('"${PROXY_ARGS[@]}"', ""))
