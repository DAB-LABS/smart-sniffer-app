#!/usr/bin/env python3
"""Dev harness for the Control Center page. Not used by the app.

Serves this directory and fakes every route the page calls, so the page can
be opened and tested without the add-on, the Go agent, the mock agent or
Home Assistant. Python 3.9+, stdlib only.

    python3 devserver.py                       # http://127.0.0.1:8099/
    LAB=connected HA=loaded python3 devserver.py --port 8123

Switches, from the environment or from the page's query string (for example
http://127.0.0.1:8099/?LAB=off&HA=unreachable). The page's own requests
carry no query string, so the server reads the flags from the Referer.

    LAB     off | running | connected     Test Lab state (default running)
    HA      unreachable | no_integration | loaded            (default loaded)
    AGENT   up | down                     the real Go agent  (default up)
    RULES   ok | missing                  /evaluate answers 503 when missing
    DRIVES  0 | 1 | 2                     real drives        (default 2)
    AGENT_ENTRY  1 | 0                    Drive Agent added in HA (default 1)
    CONNECT ok | error                    error: /ha/connect fails with flow_failed
    POOL_DEVICES  1 | 0                   1: pools carry a full "devices" list
    LAB_ENTRY 0 | 1                       with LAB=off: the Test Lab entry is still in HA
    SLUG    <slug> | none                 the app's slug (default local_smart_sniffer_agent);
                                          none: the Supervisor could not tell, slug is null
    HOSTNAME <host>                       the agent host (default: the slug with dashes)
    MDNS_NAME <name> | none               the agent's mDNS name; none (or empty) for none
                                          (default smartha-homeassistant)
    HA_VERSION <version> | none           Home Assistant's version (default 2026.7.2); 2025.x
                                          and earlier give the /hassio/... link paths, none:
                                          Core could not tell, ha_version is null
    VDISK   0 | 1 | zero                  1: the agent also sees a QEMU SCSI virtual disk with no
                                          SMART data and no temperature (UNSUPPORTED); zero: the
                                          same disk with smartctl's 0 C stand-in temperature

HOSTNAME is read from the environment like the rest, and some shells and
containers export it already, so set it on the command line when it matters:

    SLUG=local_smart_sniffer_agent HOSTNAME=local-smart-sniffer-agent python3 devserver.py

The repo's alpine.min.js and tailwind.min.js are placeholders that the image
build replaces. Download the real ones (URLs are in the placeholders) into a
folder and set VENDOR_DIR to it; the server then serves those instead.

The page is also served under /api/hassio_ingress/<token>/ to check that
every request stays relative to the page, as it must behind ingress.

/evaluate runs the integration's real rules when it can find them: set
SS_RULES_DIR to a directory holding attention.py, devstat.py and extract.py
(for example a smart-sniffer checkout's custom_components/smart_sniffer), or
build the image layout at /opt/ssrules. Otherwise a rough stand-in answers,
good enough to exercise the page and nothing more.

Buttons that change state (Turn on, Connect, Disconnect) override the flags
until the server restarts. Turn on and off fake a six second restart.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import mimetypes
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEB_DIR = Path(__file__).resolve().parent
AGENT_PORT = 9099
MOCK_PORT = 9100
APP_VERSION = "0.3.0-dev"
AGENT_VERSION = "0.8.1"
RULES_VERSION_FALLBACK = "v0.8.1 (stand-in)"

mimetypes.add_type("application/javascript", ".js")

DEFAULTS = {
    "LAB": "running",
    "HA": "loaded",
    "AGENT": "up",
    "RULES": "ok",
    "DRIVES": "2",
    "POOL_DEVICES": "1",
    "LAB_ENTRY": "0",
    "AGENT_ENTRY": "1",
    "CONNECT": "ok",
    "SLUG": "local_smart_sniffer_agent",
    "HOSTNAME": "",
    "MDNS_NAME": "smartha-homeassistant",
    "HA_VERSION": "2026.7.2",
    "VDISK": "0",
}

LOCK = threading.Lock()
OVERRIDES: dict[str, str] = {}
RESTART_UNTIL = 0.0
STARTED = time.time()


# ── Rules ────────────────────────────────────────────────────────────────────

def load_rules():
    candidates = []
    if os.environ.get("SS_RULES_DIR"):
        candidates.append(Path(os.environ["SS_RULES_DIR"]))
    candidates.append(Path("/opt/ssrules/custom_components/smart_sniffer"))
    for directory in candidates:
        if not (directory / "attention.py").is_file():
            continue
        cc = types.ModuleType("custom_components")
        cc.__path__ = []  # type: ignore[attr-defined]
        pkg = types.ModuleType("custom_components.smart_sniffer")
        pkg.__path__ = [str(directory)]  # type: ignore[attr-defined]
        sys.modules.setdefault("custom_components", cc)
        sys.modules["custom_components.smart_sniffer"] = pkg
        try:
            attention = importlib.import_module("custom_components.smart_sniffer.attention")
        except Exception as err:  # noqa: BLE001
            print(f"[dev] could not import rules from {directory}: {err}")
            continue
        version = "unknown"
        vfile = directory.parent.parent / "VERSION"
        manifest = directory / "manifest.json"
        if vfile.is_file():
            version = vfile.read_text().strip()
        elif manifest.is_file():
            version = "v" + json.loads(manifest.read_text()).get("version", "?")
        print(f"[dev] rules from {directory} ({version})")
        return attention, version
    print("[dev] integration rules not found, using the stand-in")
    return None, RULES_VERSION_FALLBACK


RULES, RULES_VERSION = load_rules()


def evaluate(drive: dict) -> dict:
    if RULES is not None:
        # As the coordinator does: merge this poll's Device Statistics onto the
        # drive as _devstat (nothing held) before evaluating, or the gap-fill
        # readings are never seen.
        devstat = importlib.import_module("custom_components.smart_sniffer.devstat")
        drive = dict(drive)
        drive[devstat.DEVSTAT_KEY], _ = devstat.merge_devstat(drive, None)
        state, severity, reasons, accepted = RULES.evaluate_attention(drive)
        return {
            "state": state,
            "severity": severity,
            "reasons": reasons,
            "accepted": accepted,
            "readings": RULES.current_readings(drive),
            "reasons_text": RULES.compose_reasons_text(state, reasons, accepted),
            "rules_version": RULES_VERSION,
        }
    return stand_in_evaluate(drive)


def stand_in_evaluate(drive: dict) -> dict:
    """A rough copy of the rules, only so the page has something to show."""
    sd = drive.get("smart_data") or {}
    crit: list[str] = []
    warn: list[str] = []
    readings: dict[str, int] = {}
    if (sd.get("smart_status") or {}).get("passed") is False:
        crit.append("SMART overall status: FAILED")
    log = sd.get("nvme_smart_health_information_log")
    table = (sd.get("ata_smart_attributes") or {}).get("table") or []
    if not log and not table and "passed" not in (sd.get("smart_status") or {}):
        return {"state": "UNSUPPORTED", "severity": "none", "reasons": [], "accepted": [],
                "readings": {}, "reasons_text": "No usable SMART data",
                "rules_version": RULES_VERSION}
    if log:
        # A field the drive leaves out is not a reading of 0.
        for label, key in (("NVMe Media Errors", "media_errors"),
                           ("NVMe Spare Warn Below", "available_spare"),
                           ("SSD Wear Percent Used", "percentage_used")):
            if log.get(key) is not None:
                readings[label] = log[key]
        if log.get("critical_warning"):
            crit.append(f"NVMe critical warning flag set (0x{log['critical_warning']:02x})")
        if log.get("media_errors"):
            crit.append(f"NVMe media errors: {log['media_errors']} (expected 0)")
        if log.get("available_spare", 100) < 20:
            warn.append(f"NVMe available spare low: {log['available_spare']}% remaining")
    labels = {"Reallocated_Sector_Ct": ("Reallocated Sector Count", 0, crit),
              "Current_Pending_Sector": ("Current Pending Sector Count", 0, crit),
              "Offline_Uncorrectable": ("Offline Uncorrectable Errors", 0, crit),
              "Reported_Uncorrect": ("Reported Uncorrectable Errors", 0, crit),
              "Reallocated_Event_Count": ("Reallocated Event Count", 0, warn),
              "Spin_Retry_Count": ("Spin Retry Count", 0, warn),
              "Command_Timeout": ("Command Timeout", 100, warn)}
    for attr in table:
        if attr["name"] in labels:
            label, limit, bucket = labels[attr["name"]]
            value = (attr.get("raw") or {}).get("value")
            if value is None:
                continue
            readings[label] = value
            if value > limit:
                bucket.append(f"{label}: {value} (expected 0)" if not limit
                              else f"{label}: {value} (threshold {limit})")
    unc = devstat_unc(drive)
    if unc is not None and "Reported Uncorrectable Errors" not in readings:
        readings["Reported Uncorrectable Errors"] = unc
        if unc:
            crit.append(f"Reported Uncorrectable Errors: {unc} (expected 0; from device statistics)")
    if crit:
        state, severity, reasons = "YES", "critical", crit + warn
    elif warn:
        state, severity, reasons = "MAYBE", "warning", warn
    else:
        state, severity, reasons = "NO", "none", []
    return {"state": state, "severity": severity, "reasons": reasons, "accepted": [],
            "readings": readings,
            "reasons_text": "; ".join(reasons) if reasons else "No issues detected",
            "rules_version": RULES_VERSION}


# ── Drive builders ───────────────────────────────────────────────────────────

def iso(seconds_ago: float = 0) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def attr(aid, name, raw, value=100, worst=100, thresh=0, raw_string=None):
    return {"id": aid, "name": name, "value": value, "worst": worst, "thresh": thresh,
            "when_failed": "", "raw": {"value": raw, "string": raw_string or str(raw)}}


def nvme_payload(drive_id, model, serial, path, log, writes_units, reads_units):
    sd = {
        "device": {"name": path, "type": "nvme", "protocol": "NVMe"},
        "model_name": model,
        "serial_number": serial,
        "smart_status": {"passed": True},
        "temperature": {"current": log["temperature"]},
        "power_on_time": {"hours": log["power_on_hours"]},
        "power_cycle_count": log["power_cycles"],
        "nvme_smart_health_information_log": dict(log, data_units_written=writes_units,
                                                  data_units_read=reads_units),
    }
    return {
        "id": drive_id, "model": model, "serial": serial, "protocol": "NVMe",
        "device_path": path, "readable": True, "in_standby": False,
        "last_updated": iso(12), "smart_data": sd,
        "device_statistics": {"status": "not_applicable"},
        "derived": {"host_writes": {"bytes": writes_units * 512000, "source": "nvme"},
                    "host_reads": {"bytes": reads_units * 512000, "source": "nvme"}},
    }


def nvme_log(**kw):
    log = {"critical_warning": 0, "temperature": 41, "available_spare": 100,
           "available_spare_threshold": 10, "percentage_used": 2, "data_units_read": 0,
           "data_units_written": 0, "host_reads": 102_334_221, "host_writes": 88_120_431,
           "controller_busy_time": 1_204, "power_cycles": 11, "power_on_hours": 2210,
           "unsafe_shutdowns": 4, "media_errors": 0, "num_err_log_entries": 0,
           "warning_temp_time": 0, "critical_comp_time": 0}
    log.update(kw)
    return log


def devstat_pages(unc: int, writes_sectors: int, reads_sectors: int, poh: int) -> list:
    pages = _devstat_pages(unc, writes_sectors, reads_sectors, poh)
    for page in pages:
        for entry in page["table"]:
            entry["flags"] = {"value": 192, "string": "V--- ", "valid": True, "normalized": False,
                              "supports_dsn": False, "monitored_condition_met": False}
    return pages


def _devstat_pages(unc: int, writes_sectors: int, reads_sectors: int, poh: int) -> list:
    return [
        {"number": 1, "name": "General Statistics", "revision": 1, "table": [
            {"offset": 8, "name": "Lifetime Power-On Resets", "size": 4, "value": 10},
            {"offset": 16, "name": "Power-on Hours", "size": 4, "value": poh},
            {"offset": 24, "name": "Logical Sectors Written", "size": 6, "value": writes_sectors},
            {"offset": 40, "name": "Logical Sectors Read", "size": 6, "value": reads_sectors},
        ]},
        {"number": 4, "name": "General Errors Statistics", "revision": 1, "table": [
            {"offset": 8, "name": "Number of Reported Uncorrectable Errors", "size": 4, "value": unc},
            {"offset": 16, "name": "Resets Between Cmd Acceptance and Completion", "size": 4, "value": 0},
        ]},
    ]


def devstat_unc(drive: dict):
    ds = drive.get("device_statistics") or {}
    for page in ds.get("pages") or []:
        for entry in page.get("table") or []:
            if "Reported Uncorrectable" in entry.get("name", ""):
                return entry.get("value")
    return None


def wdc_payload(drive_id, serial, path, unc=0, writes_tb=107.83):
    table = [
        attr(1, "Raw_Read_Error_Rate", 0, 100, 100, 1),
        attr(2, "Throughput_Performance", 96, 138, 138, 54),
        attr(3, "Spin_Up_Time", 30089675135, 81, 81, 1, "383 (Average 380)"),
        attr(4, "Start_Stop_Count", 10, 100, 100, 0),
        attr(5, "Reallocated_Sector_Ct", 0, 100, 100, 1),
        attr(7, "Seek_Error_Rate", 0, 100, 100, 1),
        attr(8, "Seek_Time_Performance", 18, 140, 140, 20),
        attr(9, "Power_On_Hours", 27694, 97, 97, 0),
        attr(10, "Spin_Retry_Count", 0, 100, 100, 1),
        attr(12, "Power_Cycle_Count", 10, 100, 100, 0),
        attr(22, "Helium_Level", 100, 100, 100, 25),
        attr(192, "Power-Off_Retract_Count", 431, 100, 100, 0),
        attr(193, "Load_Cycle_Count", 431, 100, 100, 0),
        attr(194, "Temperature_Celsius", 34, 60, 46, 0, "34 (Min/Max 18/54)"),
        attr(196, "Reallocated_Event_Count", 0, 100, 100, 0),
        attr(197, "Current_Pending_Sector", 0, 100, 100, 0),
        attr(198, "Offline_Uncorrectable", 0, 100, 100, 0),
        attr(199, "UDMA_CRC_Error_Count", 0, 200, 200, 0),
    ]
    writes_sectors = int(writes_tb * 1e12 / 512)
    reads_sectors = 1_546_494_077_801
    model = "WDC  WUH721414ALE604"
    sd = {"device": {"name": path, "type": "sat", "protocol": "ATA"},
          "model_name": model, "serial_number": serial, "logical_block_size": 512,
          "smart_status": {"passed": True}, "temperature": {"current": 34},
          "power_on_time": {"hours": 27694}, "power_cycle_count": 10,
          "ata_smart_attributes": {"revision": 16, "table": table}}
    return {
        "id": drive_id, "model": model, "serial": serial, "protocol": "ATA",
        "device_path": path, "readable": True, "in_standby": False,
        "last_updated": iso(40), "smart_data": sd,
        "device_statistics": {"status": "present", "complete": True, "exit_status": 0,
                              "logical_block_size": 512,
                              "pages": devstat_pages(unc, writes_sectors, reads_sectors, 27694)},
        "derived": {"host_writes": {"bytes": writes_sectors * 512, "source": "ata_device_statistics"},
                    "host_reads": {"bytes": reads_sectors * 512, "source": "ata_device_statistics"}},
    }


def scsi_vdisk_payload(zero_temperature: bool) -> dict:
    """A QEMU SCSI virtual disk as the agent reports it: smartctl finds the
    device but no SMART support, so the integration calls it UNSUPPORTED. Some
    smartctl builds still write a temperature of 0 for it."""
    model, serial, path = "QEMU HARDDISK", "drive-scsi0", "/dev/sdb"
    sd = {"device": {"name": path, "info_name": path, "type": "scsi", "protocol": "SCSI"},
          "scsi_vendor": "QEMU", "scsi_product": "QEMU HARDDISK", "scsi_model_name": "QEMU QEMU HARDDISK",
          "model_name": model, "serial_number": serial, "logical_block_size": 512}
    if zero_temperature:
        sd["temperature"] = {"current": 0, "drive_trip": 0}
    return {
        "id": "sdb", "model": model, "serial": serial, "protocol": "SCSI",
        "device_path": path, "readable": True, "in_standby": False,
        "last_updated": iso(25), "smart_data": sd,
        "device_statistics": {"status": "not_applicable"},
        "derived": {"host_writes_omitted": "no_source", "host_reads_omitted": "no_source"},
    }


def real_drives(count: int, vdisk: str = "0") -> list:
    drives = [nvme_payload("nvme0", "Samsung SSD 980 PRO 1TB", "S5GXNX0DEMO412A", "/dev/nvme0",
                           nvme_log(), 9_121_388, 18_250_114)]
    if count >= 2:
        hdd = wdc_payload("sda", "FIXTURE-m01-sdl", "/dev/sda")
        hdd["in_standby"] = True
        hdd["last_updated"] = iso(3 * 3600 + 120)
        drives.append(hdd)
    drives = drives[:count]
    if vdisk in ("1", "zero"):
        drives.append(scsi_vdisk_payload(vdisk == "zero"))
    return drives


# ── Mock store ───────────────────────────────────────────────────────────────

PRESETS = {
    "sata_hdd": "SATA HDD (Seagate Barracuda 2TB)",
    "sata_ssd": "SATA SSD (Samsung 870 EVO 500GB)",
    "nvme": "NVMe SSD (Samsung 980 PRO 1TB)",
    "nvme_usb": "NVMe USB-C Enclosure (Sabrent)",
    "usb_blocked": "USB External, SMART Blocked (WD Elements)",
    "virtual_disk": "Virtual Disk (QEMU VirtIO)",
    "sas_enterprise": "Enterprise SAS (Seagate Exos 10E2400)",
    "sata_hdd_devstat": "SATA HDD with Device Statistics (WDC Ultrastar 14TB)",
    "zfs_pool": "ZFS pool (two-disk mirror)",
}

COUNTER = [0]


def _next(prefix: str) -> str:
    COUNTER[0] += 1
    return f"{prefix}{COUNTER[0]:02d}"


def mock_drive(preset: str) -> dict:
    serial_tail = _next("")
    if preset == "sata_hdd":
        table = [attr(5, "Reallocated_Sector_Ct", 0), attr(10, "Spin_Retry_Count", 0),
                 attr(188, "Command_Timeout", 0), attr(196, "Reallocated_Event_Count", 0),
                 attr(197, "Current_Pending_Sector", 0), attr(198, "Offline_Uncorrectable", 0),
                 attr(194, "Temperature_Celsius", 36), attr(9, "Power_On_Hours", 8760),
                 attr(12, "Power_Cycle_Count", 142), attr(241, "Total_LBAs_Written", 3_906_250_000)]
        return _ata("Seagate Barracuda ST2000DM008", f"WFL3MOCK{serial_tail}", "/dev/sda", table,
                    {"status": "absent"},
                    {"host_writes": {"bytes": 2_000_000_000_000, "source": "ata_attribute",
                                     "attribute_id": 241, "attribute_name": "Total_LBAs_Written"},
                     "host_reads_omitted": "no_source"})
    if preset == "sata_ssd":
        table = [attr(5, "Reallocated_Sector_Ct", 0), attr(196, "Reallocated_Event_Count", 0),
                 attr(198, "Offline_Uncorrectable", 0), attr(177, "Wear_Leveling_Count", 2, 98, 98, 0),
                 attr(188, "Command_Timeout", 0), attr(194, "Temperature_Celsius", 31),
                 attr(9, "Power_On_Hours", 4200), attr(12, "Power_Cycle_Count", 315)]
        return _ata("Samsung SSD 870 EVO 500GB", f"S4ENMOCK{serial_tail}", "/dev/sdb", table,
                    {"status": "absent"},
                    {"host_writes": {"bytes": 12_400_000_000_000, "source": "ata_attribute",
                                     "attribute_id": 241, "attribute_name": "Total_LBAs_Written"},
                     "host_reads_omitted": "no_source"})
    if preset in ("nvme", "nvme_usb"):
        model = "Samsung 980 PRO 1TB" if preset == "nvme" else "Sabrent Rocket NVMe 500GB (USB)"
        serial = ("S5GXMOCK" if preset == "nvme" else "SB50MOCK") + serial_tail
        d = nvme_payload(serial.lower(), model, serial, "/dev/nvme0" if preset == "nvme" else "/dev/nvme1",
                         nvme_log(temperature=38, power_on_hours=2100, power_cycles=87, percentage_used=3),
                         9_121_388, 18_250_114)
        d["last_updated"] = iso(2)
        return d
    if preset in ("usb_blocked", "virtual_disk"):
        model = "WD Elements 2TB (USB)" if preset == "usb_blocked" else "QEMU HARDDISK"
        serial = ("WX72MOCK" if preset == "usb_blocked" else "QM00MOCK") + serial_tail
        ds = ({"status": "unavailable", "reason": "usb_bridge"} if preset == "usb_blocked"
              else {"status": "absent"})
        derived = {"host_writes_omitted": "device_statistics_unavailable" if preset == "usb_blocked"
                   else "no_source", "host_reads_omitted": "no_source"}
        return {"id": serial.lower(), "model": model, "serial": serial, "protocol": "ATA",
                "device_path": "/dev/sdc", "readable": True, "in_standby": False,
                "last_updated": iso(2), "smart_data": {}, "device_statistics": ds, "derived": derived}
    if preset == "sas_enterprise":
        serial = f"WFK0MOCK{serial_tail}"
        return {"id": serial.lower(), "model": "Seagate Exos 10E2400 ST1200MM0129", "serial": serial,
                "protocol": "SCSI", "device_path": "/dev/sg0", "readable": True, "in_standby": False,
                "last_updated": iso(2),
                "smart_data": {"smart_status": {"passed": True}, "scsi_grown_defect_list": 0},
                "device_statistics": {"status": "not_applicable"},
                "derived": {"host_writes_omitted": "no_source", "host_reads_omitted": "no_source"}}
    if preset == "sata_hdd_devstat":
        d = wdc_payload(f"fixture-m{serial_tail}-sdl", f"FIXTURE-m{serial_tail}-sdl", "/dev/sdd", 0, 107.83)
        d["last_updated"] = iso(2)
        return d
    raise KeyError(preset)


def _ata(model, serial, path, table, ds, derived):
    return {"id": serial.lower(), "model": model, "serial": serial, "protocol": "ATA",
            "device_path": path, "readable": True, "in_standby": False, "last_updated": iso(2),
            "smart_data": {"device": {"name": path, "type": "sat", "protocol": "ATA"},
                           "model_name": model, "serial_number": serial,
                           "smart_status": {"passed": True},
                           "ata_smart_attributes": {"table": table}},
            "device_statistics": ds, "derived": derived}


def pool_tank() -> dict:
    return {"name": "tank", "state": "ONLINE", "status": "", "action": "",
            "read_errors": 0, "write_errors": 0, "checksum_errors": 0, "data_errors": 0,
            "scan_function": "SCRUB", "scan_state": "FINISHED", "scrub_in_progress": False,
            "last_scrub_end": iso(4 * 86400), "last_scrub_repaired": 0, "last_scrub_errors": 0,
            "problem_vdevs": [],
            "devices": [{"name": "sda", "state": "ONLINE", "read": 0, "write": 0, "cksum": 0},
                        {"name": "sdb", "state": "ONLINE", "read": 0, "write": 0, "cksum": 0}]}


class Mock:
    def __init__(self):
        self.drives: dict[str, dict] = {}
        self.pools: dict[str, dict] = {}
        self.vanished: set[str] = set()
        for preset, scenario in (("sata_hdd", "failing"), ("nvme", "early"),
                                 ("sata_hdd_devstat", None), ("usb_blocked", None)):
            d = mock_drive(preset)
            if scenario == "failing":
                self.patch_smart(d, {"Reallocated_Sector_Ct": 24, "Current_Pending_Sector": 3})
            if scenario == "early":
                self.patch_smart(d, {"available_spare": 15})
            self.drives[d["id"]] = d
        self.pools["tank"] = pool_tank()

    def add(self, preset):
        if preset == "zfs_pool":
            name = "tank" if "tank" not in self.pools else _next("tank")
            p = pool_tank()
            p["name"] = name
            self.pools[name] = p
            return name
        d = mock_drive(preset)
        self.drives[d["id"]] = d
        return d["id"]

    @staticmethod
    def patch_smart(d, updates):
        sd = d["smart_data"]
        if "smart_passed" in updates:
            sd.setdefault("smart_status", {})["passed"] = bool(updates.pop("smart_passed"))
        log = sd.get("nvme_smart_health_information_log")
        if log is not None:
            for k, v in updates.items():
                if k in log:
                    log[k] = v
        for a in (sd.get("ata_smart_attributes") or {}).get("table") or []:
            if a["name"] in updates:
                a["raw"]["value"] = updates[a["name"]]
                a["raw"]["string"] = str(updates[a["name"]])
        d["last_updated"] = iso(0)

    def summaries(self):
        return [{k: d[k] for k in ("id", "device_path", "model", "serial", "protocol")}
                for d in self.drives.values()]

    def pool_list(self):
        out = []
        for name, p in self.pools.items():
            if name in self.vanished:
                continue
            p = copy.deepcopy(p)
            bad = [dev for dev in p["devices"] if dev["state"] != "ONLINE"
                   or dev["read"] or dev["write"] or dev["cksum"]]
            p["read_errors"] = sum(dev["read"] for dev in p["devices"])
            p["write_errors"] = sum(dev["write"] for dev in p["devices"])
            p["checksum_errors"] = sum(dev["cksum"] for dev in p["devices"])
            p["problem_vdevs"] = ([{"name": "mirror-0", "type": "mirror", "state": p["state"],
                                    "read_errors": 0, "write_errors": 0, "checksum_errors": 0}]
                                  if p["state"] != "ONLINE" else []) + [
                {"name": dev["name"], "type": "disk", "state": dev["state"],
                 "read_errors": dev["read"], "write_errors": dev["write"],
                 "checksum_errors": dev["cksum"]} for dev in bad]
            out.append(p)
        return out


MOCK = Mock()


# ── Flags ────────────────────────────────────────────────────────────────────

def flags_for(handler) -> dict[str, str]:
    flags = {k: os.environ.get(k, v) for k, v in DEFAULTS.items()}
    for source in (urlparse(handler.headers.get("Referer", "")).query, urlparse(handler.path).query):
        for k, v in parse_qs(source).items():
            if k.upper() in DEFAULTS and v:
                flags[k.upper()] = v[0]
    with LOCK:
        flags.update(OVERRIDES)
    return flags


def app_slug(flags) -> str | None:
    slug = flags["SLUG"].strip()
    return None if slug.lower() in ("", "none", "null") else slug


def agent_host(flags) -> str:
    # The Supervisor names the container after the slug, with dashes.
    host = flags["HOSTNAME"].strip()
    return host or (app_slug(flags) or "smart-sniffer-agent").replace("_", "-")


def mdns_name(flags) -> str | None:
    name = flags["MDNS_NAME"].strip()
    return None if name.lower() in ("", "none", "null") else name


def ha_version(flags) -> str | None:
    # The proxy asks Core, so there is no version when Core is out of reach.
    version = flags["HA_VERSION"].strip()
    if flags["HA"] == "unreachable" or version.lower() in ("", "none", "null"):
        return None
    return version


def ha_targets(flags) -> dict:
    agent_on = flags["AGENT_ENTRY"] == "1"
    lab_on = flags["LAB"] == "connected" or flags["LAB_ENTRY"] == "1"
    host = agent_host(flags)
    return {
        "agent": {"host": host, "port": AGENT_PORT, "entry_id": "01J9DEVAGENT" if agent_on else None,
                  "state": "loaded" if agent_on else None},
        "testlab": {"host": host, "port": MOCK_PORT, "entry_id": "01J9DEVTESTLAB" if lab_on else None,
                    "state": "loaded" if lab_on else None},
    }


# ── Handler ──────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("QUIET"):
            return
        sys.stderr.write("[dev] " + (fmt % args) + "\n")

    # Plumbing
    def _send(self, code, data=None, ctype="application/json", body=None):
        if body is None:
            body = json.dumps(data if data is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def _route(self):
        path = urlparse(self.path).path
        if path.startswith("/api/hassio_ingress/"):
            parts = path.split("/", 4)
            path = "/" + (parts[4] if len(parts) > 4 else "")
        return path.lstrip("/")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        route = self._route()
        dynamic = route.split("/")[0] in ("api", "mock", "app", "evaluate", "ha", "lab")
        if not dynamic:
            if method != "GET":
                return self._send(405, {"error": "method not allowed"})
            return self._static(route)
        if time.time() < RESTART_UNTIL:
            return self._send(502, {"error": "app restarting"})
        flags = flags_for(self)
        time.sleep(float(os.environ.get("LATENCY", "0.02")))
        try:
            with LOCK:
                self._api(method, route, flags)
        except BrokenPipeError:
            pass

    def _static(self, route):
        rel = route or "index.html"
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR not in target.parents and target != WEB_DIR:
            return self._send(403, {"error": "forbidden"})
        if not target.is_file():
            if "." in rel.split("/")[-1]:
                return self._send(404, body=b"not found", ctype="text/plain")
            target = WEB_DIR / "index.html"
        body = target.read_bytes()
        if body.startswith(b"// PLACEHOLDER") and os.environ.get("VENDOR_DIR"):
            # The repo carries placeholders for Alpine and Tailwind; the image
            # downloads them. Point VENDOR_DIR at a folder with the real files.
            real = Path(os.environ["VENDOR_DIR"]) / target.name
            if real.is_file():
                body = real.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, body=body, ctype=ctype)

    def _api(self, method, route, flags):
        parts = route.strip("/").split("/")
        head = parts[0]

        if head == "app" and parts[1:] == ["info"]:
            return self._send(200, {"app_version": APP_VERSION,
                                    "rules_version": None if flags["RULES"] == "missing" else RULES_VERSION,
                                    "agent_port": AGENT_PORT, "mock_port": MOCK_PORT,
                                    "mock_enabled": flags["LAB"] != "off",
                                    "ha_api": flags["HA"] != "unreachable",
                                    "hostname": agent_host(flags),
                                    "slug": app_slug(flags),
                                    "mdns_name": mdns_name(flags),
                                    "ha_version": ha_version(flags)})

        if head == "api":
            if flags["AGENT"] == "down":
                return self._send(502, {"error": "Backend unavailable on port 9099"})
            drives = real_drives(int(flags["DRIVES"]), flags["VDISK"])
            if parts[1:] == ["health"]:
                return self._send(200, {"status": "ok", "version": AGENT_VERSION, "os": "linux",
                                        "uptime_seconds": int(time.time() - STARTED) + 3600,
                                        "endpoints": ["/api/health", "/api/drives", "/api/drives/{id}"],
                                        "drives": len(drives), "filesystems": 0})
            if parts[1:] == ["drives"]:
                return self._send(200, [{k: d[k] for k in ("id", "device_path", "model", "serial", "protocol")}
                                        for d in drives])
            if len(parts) == 3 and parts[1] == "drives":
                for d in drives:
                    if d["id"] == parts[2]:
                        return self._send(200, d)
                return self._send(404, {"error": "drive not found"})
            return self._send(404, {"error": "not found"})

        if head == "mock":
            if flags["LAB"] == "off":
                return self._send(502, {"error": "Backend unavailable on port 9100"})
            return self._mock(method, parts[1:], flags)

        if head == "evaluate" and method == "POST":
            if flags["RULES"] == "missing":
                return self._send(503, {"error": "rules not available"})
            return self._send(200, evaluate(self._body()))

        if head == "ha":
            return self._ha(method, parts[1:], flags)

        if head == "lab" and method == "POST" and parts[1:] in (["on"], ["off"]):
            global RESTART_UNTIL  # noqa: PLW0603
            if parts[1] == "on":
                OVERRIDES["LAB"] = "connected" if flags["LAB_ENTRY"] == "1" else "running"
                OVERRIDES["LAB_ENTRY"] = "0"
            else:
                # Turning the lab off leaves its Home Assistant entry in place.
                OVERRIDES["LAB_ENTRY"] = "1" if flags["LAB"] == "connected" else flags["LAB_ENTRY"]
                OVERRIDES["LAB"] = "off"
            RESTART_UNTIL = time.time() + 6
            return self._send(200, {"result": "restarting"})

        return self._send(404, {"error": "not found"})

    def _mock(self, method, parts, flags):
        if parts == ["health"] and method == "GET":
            return self._send(200, {"status": "ok", "version": "0.8.1-mock", "os": "linux",
                                    "uptime_seconds": int(time.time() - STARTED),
                                    "endpoints": ["/api/health", "/api/drives", "/api/drives/{id}", "/api/pools"],
                                    "drives": len(MOCK.drives), "filesystems": 0,
                                    "pools": len(MOCK.pool_list()), "pools_status": "ok"})
        if parts == ["drives"]:
            if method == "GET":
                return self._send(200, MOCK.summaries())
            if method == "POST":
                preset = self._body().get("preset")
                if preset not in PRESETS:
                    return self._send(400, {"error": f"unknown preset: {preset}"})
                return self._send(201, {"id": MOCK.add(preset)})
        if len(parts) >= 2 and parts[0] == "drives":
            d = MOCK.drives.get(parts[1])
            if d is None:
                return self._send(404, {"error": "drive not found"})
            if len(parts) == 2 and method == "GET":
                return self._send(200, d)
            if len(parts) == 2 and method == "DELETE":
                del MOCK.drives[parts[1]]
                return self._send(200, {"ok": True})
            if len(parts) == 3 and method == "PATCH":
                body = self._body()
                if parts[2] == "smart":
                    MOCK.patch_smart(d, body)
                    return self._send(200, {"ok": True})
                if parts[2] == "devstat":
                    if d.get("device_statistics", {}).get("status") != "present":
                        return self._send(400, {"error": "no device statistics on this drive"})
                    for page in d["device_statistics"]["pages"]:
                        for e in page["table"]:
                            if "Reported Uncorrectable" in e["name"] and "reported_uncorrectable" in body:
                                e["value"] = int(body["reported_uncorrectable"])
                    d["last_updated"] = iso(0)
                    return self._send(200, {"ok": True})
                if parts[2] == "derived":
                    tb = float(body.get("host_writes_tb", 0))
                    old = (d.get("derived") or {}).get("host_writes") or {}
                    source = old.get("source") or ("nvme" if d["protocol"] == "NVMe" else "ata_attribute")
                    d.setdefault("derived", {})["host_writes"] = dict(old, bytes=int(tb * 1e12), source=source)
                    d["derived"].pop("host_writes_omitted", None)
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        if parts == ["pools"] and method == "GET":
            pools = MOCK.pool_list()
            if flags["POOL_DEVICES"] != "1":
                # The agent's own shape: only problem_vdevs, no full disk list.
                for p in pools:
                    p.pop("devices", None)
            return self._send(200, pools)
        if len(parts) >= 2 and parts[0] == "pools":
            p = MOCK.pools.get(parts[1])
            if p is None:
                return self._send(404, {"error": "pool not found"})
            if len(parts) == 2 and method == "PATCH":
                body = self._body()
                if "state" in body:
                    p["state"] = body["state"]
                if isinstance(body.get("devices"), list):
                    by_name = {dev["name"]: dev for dev in p["devices"]}
                    for dev in body["devices"]:
                        if dev.get("name") in by_name:
                            by_name[dev["name"]].update({k: dev[k] for k in ("state", "read", "write", "cksum") if k in dev})
                return self._send(200, {"ok": True})
            if len(parts) == 3 and method == "POST" and parts[2] in ("vanish", "restore"):
                (MOCK.vanished.add if parts[2] == "vanish" else MOCK.vanished.discard)(parts[1])
                return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def _ha(self, method, parts, flags):
        # Shapes and status codes follow the proxy (web/proxy.py on app-proxy).
        reachable = flags["HA"] != "unreachable"
        loaded = flags["HA"] == "loaded"
        unreachable = {"result": "error", "message": "unreachable"}
        if parts == ["status"] and method == "GET":
            host = agent_host(flags)
            targets = ha_targets(flags) if loaded else {
                "agent": {"host": host, "port": AGENT_PORT, "entry_id": None, "state": None},
                "testlab": {"host": host, "port": MOCK_PORT, "entry_id": None, "state": None}}
            return self._send(200, {"reachable": reachable, "integration_loaded": loaded and reachable,
                                    "targets": targets})
        if parts == ["connect"] and method == "POST":
            target = self._body().get("target")
            if target not in ("agent", "testlab"):
                return self._send(400, {"error": 'target must be "agent" or "testlab"'})
            if not reachable:
                return self._send(502, unreachable)
            if not loaded:
                return self._send(502, {"result": "error", "message": "integration_not_loaded"})
            if flags["CONNECT"] == "error":
                return self._send(502, {"result": "error", "message": "flow_failed"})
            if target == "agent":
                if flags["AGENT_ENTRY"] == "1":
                    return self._send(200, {"result": "already_connected", "entry_id": "01J9DEVAGENT"})
                OVERRIDES["AGENT_ENTRY"] = "1"
                return self._send(200, {"result": "created", "entry_id": "01J9DEVAGENT"})
            if flags["LAB"] == "connected":
                return self._send(200, {"result": "already_connected", "entry_id": "01J9DEVTESTLAB"})
            if flags["LAB"] == "off":
                # The config flow's form comes back with cannot_connect.
                return self._send(200, {"result": "error", "message": "cannot_connect"})
            OVERRIDES["LAB"] = "connected"
            return self._send(200, {"result": "created", "entry_id": "01J9DEVTESTLAB"})
        if parts == ["disconnect"] and method == "POST":
            if self._body().get("target") != "testlab":
                return self._send(400, {"error": "only the Test Lab can be disconnected here"})
            if not reachable:
                return self._send(502, unreachable)
            if flags["LAB"] != "connected" and flags["LAB_ENTRY"] != "1":
                return self._send(200, {"result": "not_connected"})
            OVERRIDES["LAB_ENTRY"] = "0"
            if flags["LAB"] == "connected":
                OVERRIDES["LAB"] = "running"
            return self._send(200, {"result": "removed", "entry_id": "01J9DEVTESTLAB"})
        if parts == ["verdict"] and method == "GET":
            serial = (parse_qs(urlparse(self.path).query).get("serial") or [""])[0]
            if not serial:
                return self._send(400, {"error": "serial is required"})
            if not reachable:
                return self._send(502, {"error": "home assistant not reachable"})
            none = {"found": False, "entity_id": None, "state": None, "severity": None, "reasons": None}
            if not loaded:
                return self._send(200, none)
            for d in list(MOCK.drives.values()) + real_drives(int(flags["DRIVES"])):
                if d["serial"] != serial:
                    continue
                is_mock = d["id"] in MOCK.drives
                if (is_mock and flags["LAB"] != "connected") or (not is_mock and flags["AGENT_ENTRY"] != "1"):
                    break
                ev = evaluate(d)
                slug = "_".join(filter(None, "".join(c if c.isalnum() else " " for c in serial.lower()).split()))
                return self._send(200, {"found": True, "entity_id": f"sensor.{slug}_attention_needed",
                                        "state": ev["state"], "severity": ev["severity"],
                                        "reasons": ev["reasons"]})
            return self._send(200, none)
        return self._send(404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8099)))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[dev] Control Center dev harness on http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
