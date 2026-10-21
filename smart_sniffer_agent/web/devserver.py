#!/usr/bin/env python3
"""Dev harness for the SMART Sniffer App page. Not used by the app.

Serves this directory and fakes every route the page calls, so the page can
be opened and tested without the app, the Go agent or Home Assistant.
Python 3.9+, standard library only.

    python3 devserver.py                         # http://127.0.0.1:8099/
    INTEGRATION=missing python3 devserver.py --port 8123

Switches come from the environment or from the page's query string (for
example http://127.0.0.1:8099/?INTEGRATION=update&DRIVES=2). The page's own
requests carry no query string, so the server reads the switches from the
Referer.

    AGENT           up | down                 this app's agent; down: the HAOS cards keep
                                              their last readings, not answering
    DRIVES          board | mixed | 0 | 1 | 2 HAOS drives. board: a healthy NVMe with / at
                                              41% and /boot at 12%, and a SATA drive at
                                              Attention; mixed: the NVMe, that drive with
                                              four problem rows, and a WD Red at Maybe; 0:
                                              none; 1: the NVMe; 2: the NVMe and a WDC with
                                              Device Statistics
    VDISK           0 | 1                     1: add a QEMU virtual disk
    POOL            0 | online | degraded     a ZFS pool on this box, or none
    RULES           ok | missing              missing: no verdicts, the rules line says so
    INTEGRATION     loaded | no_entries | restart | panel_failed | update | missing |
                    unreachable               the integration as /ha/integration reports it:
                                              v0.9+ loaded; v0.9+ with no machine yet;
                                              v0.9+ downloaded, not restarted; v0.9+ loaded
                                              but its page failed; 0.8.x; none; Home
                                              Assistant not answering
    HAOS_ENTRY      1 | 0                     with loaded: whether HAOS is a machine in it
                                              (0 shows the Create line)
    PANEL_PATH      path                      the panel's path from smart_sniffer/info
                                              (default /smart-sniffer)
    HACS            installed | missing       with missing: which install link the card has
    ADD             ok | error                /ha/add succeeds or answers flow_failed
    MDNS            on | off                  the agent's mDNS name in /app/info, or none
    LATENCY         seconds                   added to every route (default 0.02)
    SELF_DELAY      seconds                   added to fleet/hosts/self/drives only (default 0):
                                              the page before this machine's first answer
    SLUG, HOSTNAME, HA_VERSION                the app's slug, its agent host name, Home
                                              Assistant's version (2025.x and earlier
                                              give the /hassio/... link paths)

Writes (a closed card, HAOS created in the integration) live in memory until
the server stops. Each set of the switches that shape them has its own copy;
a POST to /dev/reset with the switches in its query string starts that copy
afresh, as the page checks do before each page they open.

The repo's alpine.min.js and tailwind.min.js are placeholders that the image
build replaces. Download the real ones (URLs are in the placeholders) into a
folder and set VENDOR_DIR to it; the server then serves those instead.

The page is also served under /api/hassio_ingress/<token>/ to check that
every request stays relative to the page, as it must behind ingress.

Verdicts: the server imports drive_view, pool_verdict and Rules from
proxy.py when they are there. Rules need the integration's modules: set
SS_RULES_DIR to a smart-sniffer checkout's custom_components directory, or
build the image layout at /opt/ssrules. Without them a rough stand-in
answers, good enough to exercise the page and nothing more.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import mimetypes
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

WEB_DIR = Path(__file__).resolve().parent
AGENT_PORT = 9099
APP_VERSION = "0.4.0-dev"
AGENT_VERSION = "0.8.5"
RULES_VERSION_FALLBACK = "v0.8.4"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

DEFAULTS = {
    "AGENT": "up",
    "SELF_DELAY": "0",
    "DRIVES": "board",
    "VDISK": "0",
    "POOL": "0",
    "RULES": "ok",
    "INTEGRATION": "loaded",
    "HAOS_ENTRY": "1",
    "PANEL_PATH": "/smart-sniffer",
    "HACS": "installed",
    "ADD": "ok",
    "SLUG": "local_smart_sniffer_agent",
    "HOSTNAME": "",
    "HA_VERSION": "2026.10.1",
    "MDNS": "on",
}
# The switches that shape the in-memory world; a change rebuilds it.
WORLD_KEYS = ("INTEGRATION", "HAOS_ENTRY", "DRIVES", "VDISK", "POOL")

LOCK = threading.RLock()


# ── Time ─────────────────────────────────────────────────────────────────────

def iso(seconds_ago: float = 0) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


DAY = 86400


# ── Verdicts: proxy.py when it has them, else a stand-in ─────────────────────

def _load_proxy():
    path = WEB_DIR / "proxy.py"
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("ss_proxy", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    except Exception as err:  # noqa: BLE001
        print(f"[dev] could not import proxy.py: {err}")
        return None


PROXY = _load_proxy()


def _load_rules():
    if PROXY is None or not hasattr(PROXY, "Rules"):
        return None
    candidates = []
    if os.environ.get("SS_RULES_DIR"):
        d = Path(os.environ["SS_RULES_DIR"])
        # Either the custom_components directory or smart_sniffer inside it.
        candidates.append(d.parent if (d / "attention.py").is_file() else d)
    candidates.append(Path("/opt/ssrules/custom_components"))
    for d in candidates:
        if (d / "smart_sniffer" / "attention.py").is_file():
            rules = PROXY.Rules(str(d))
            if rules.load():
                if not rules.version:
                    manifest = d / "smart_sniffer" / "manifest.json"
                    if manifest.is_file():
                        rules.version = "v" + json.loads(manifest.read_text()).get("version", "?")
                print(f"[dev] rules from {d} ({rules.version})")
                return rules
    print("[dev] integration rules not found, using the stand-in")
    return None


RULES = _load_rules()
RULES_VERSION = (RULES.version if RULES else None) or RULES_VERSION_FALLBACK
HAVE_DRIVE_VIEW = PROXY is not None and callable(getattr(PROXY, "drive_view", None))
HAVE_POOL_VERDICT = PROXY is not None and callable(getattr(PROXY, "pool_verdict", None))
HAVE_POOL_VIEW = PROXY is not None and callable(getattr(PROXY, "pool_view", None))
print(f"[dev] drive_view from {'proxy.py' if HAVE_DRIVE_VIEW else 'the stand-in'}")


def evaluate(drive: dict, overrides: dict | None = None) -> dict:
    if RULES is not None:
        return RULES.evaluate(copy.deepcopy(drive), overrides or None)
    return stand_in_evaluate(drive)


def _attrs(drive: dict) -> dict:
    table = ((drive.get("smart_data") or {}).get("ata_smart_attributes") or {}).get("table") or []
    return {a.get("name"): a for a in table}


def devstat_unc(drive: dict):
    ds = drive.get("device_statistics") or {}
    for page in ds.get("pages") or []:
        for entry in page.get("table") or []:
            if "Reported Uncorrectable" in entry.get("name", ""):
                return entry.get("value")
    return None


def stand_in_evaluate(drive: dict) -> dict:
    """A rough copy of the rules, only so the page has something to show."""
    sd = drive.get("smart_data") or {}
    crit: list[str] = []
    warn: list[str] = []
    readings: dict[str, int] = {}
    log = sd.get("nvme_smart_health_information_log")
    table = (sd.get("ata_smart_attributes") or {}).get("table") or []
    grown = sd.get("scsi_grown_defect_list")
    if not log and not table and grown is None:
        return {"state": "UNSUPPORTED", "severity": "none", "reasons": [], "accepted": [],
                "readings": {}, "reasons_text": "No usable SMART data",
                "rules_version": RULES_VERSION}
    if (sd.get("smart_status") or {}).get("passed") is False:
        crit.append("SMART overall status: FAILED")
    if log:
        if log.get("critical_warning"):
            crit.append(f"NVMe critical warning flag set (0x{log['critical_warning']:02x})")
        if log.get("media_errors"):
            crit.append(f"NVMe media errors: {log['media_errors']} (expected 0)")
        spare, floor = log.get("available_spare"), log.get("available_spare_threshold", 10)
        if spare is not None and spare <= floor:
            crit.append(f"NVMe available spare ({spare}%) at or below drive threshold ({floor}%)")
        elif spare is not None and spare < 20:
            warn.append(f"NVMe available spare low: {spare}% remaining")
        if (log.get("percentage_used") or 0) >= 90:
            warn.append(f"NVMe drive wear at {log['percentage_used']}% of rated life")
    if grown:
        crit.append(f"Grown Defect List: {grown} (expected 0)")
    labels = {"Reallocated_Sector_Ct": ("Reallocated Sector Count", 0, crit),
              "Current_Pending_Sector": ("Current Pending Sector Count", 0, crit),
              "Offline_Uncorrectable": ("Offline Uncorrectable Errors", 0, crit),
              "Reported_Uncorrect": ("Reported Uncorrectable Errors", 0, crit),
              "Reallocated_Event_Count": ("Reallocated Event Count", 0, warn),
              "Spin_Retry_Count": ("Spin Retry Count", 0, warn),
              "Command_Timeout": ("Command Timeout", 100, warn)}
    for a in table:
        if a["name"] in labels:
            label, limit, bucket = labels[a["name"]]
            value = (a.get("raw") or {}).get("value")
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
            crit.append(f"Reported Uncorrectable Errors: {unc} (expected 0) from device statistics")
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


# The stand-in for drive_view (spec 4.7), same output shape.
_ATA_ROWS = [
    ("reallocated", "Reallocated sectors", "Reallocated_Sector_Ct"),
    ("pending", "Pending sectors", "Current_Pending_Sector"),
    ("offline_unc", "Offline uncorrectable", "Offline_Uncorrectable"),
    ("reported_unc", "Reported uncorrectable", "Reported_Uncorrect"),
    ("cmd_timeout", "Command timeout", "Command_Timeout"),
    ("realloc_events", "Reallocated events", "Reallocated_Event_Count"),
    ("spin_retry", "Spin retries", "Spin_Retry_Count"),
    ("wear", "Wear used", "Wear_Leveling_Count"),
]
_REASON_KEYS = [
    ("Reallocated Sector Count", "reallocated"),
    ("Current Pending Sector", "pending"),
    ("Offline Uncorrectable", "offline_unc"),
    ("Reported Uncorrectable", "reported_unc"),
    ("Command Timeout", "cmd_timeout"),
    ("Reallocated Event Count", "realloc_events"),
    ("Spin Retry Count", "spin_retry"),
    ("NVMe critical warning", "critical_warning"),
    ("NVMe media errors", "media_errors"),
    ("NVMe available spare", "available_spare"),
    ("NVMe drive wear", "wear"),
    ("SSD wear", "wear"),
    ("Grown Defect", "grown_defects"),
]
_WARN_KEYS = {"cmd_timeout", "realloc_events", "spin_retry", "wear"}


def _short_expect(key: str, reason: str) -> str | None:
    if key == "available_spare":
        return "warns below 20%" if "low" in reason else "expected above the drive's threshold"
    if key == "wear":
        return "warns at 90%"
    m = re.search(r"\((.*?)\)", reason)
    text = m.group(1) if m else ""
    devstat = "device statistics" in reason.lower()
    if text.startswith("threshold "):
        text = "warns above " + text.split(" ", 1)[1]
    elif text.startswith("expected"):
        text = text.split(";")[0]
    if devstat:
        text = (text or "expected 0") + ", from device statistics"
    return text or None


def stub_drive_view(drive: dict, verdict: dict | None) -> dict:
    verdict = verdict or {}
    sd = drive.get("smart_data") or {}
    state = verdict.get("state")
    header = {"YES": "attention", "MAYBE": "maybe", "NO": "ok"}.get(state, "unknown")
    marks: dict[str, tuple[str, str | None]] = {}
    for reason in verdict.get("reasons") or []:
        for prefix, key in _REASON_KEYS:
            if reason.startswith(prefix):
                if key == "reported_unc" and "device statistics" in reason.lower():
                    key = "reported_unc_devstat"
                sev = "warn" if key in _WARN_KEYS or reason.startswith("NVMe available spare low") else "crit"
                marks[key] = (sev, _short_expect(key, reason))
                break
    rows = []

    def row(key, name, value, note=None, unknown=False):
        mark, expect = marks.get(key, (None, None))
        if unknown:
            mark, value = "unk", "not known"
        rows.append({"key": key, "name": name, "value": value, "mark": mark,
                     "expect": expect, "note": note})

    log = sd.get("nvme_smart_health_information_log")
    attrs = _attrs(drive)
    if state == "UNSUPPORTED":
        if drive.get("protocol") == "NVMe":
            row("critical_warning", "Critical warning", None, unknown=True)
            row("media_errors", "Media errors", None, unknown=True)
        else:
            row("reallocated", "Reallocated sectors", None, unknown=True)
            row("pending", "Pending sectors", None, unknown=True)
    elif log:
        if "critical_warning" in log:
            row("critical_warning", "Critical warning", f"0x{int(log['critical_warning']):02x}")
        if "media_errors" in log:
            row("media_errors", "Media errors", f"{log['media_errors']:,}")
        if "available_spare" in log:
            row("available_spare", "Available spare", f"{log['available_spare']}%")
        if "percentage_used" in log:
            row("wear", "Wear used", f"{log['percentage_used']}%")
    elif attrs:
        unc = devstat_unc(drive)
        for key, name, attr_name in _ATA_ROWS:
            a = attrs.get(attr_name)
            if a is None:
                if key == "reported_unc" and unc is not None:
                    row(key, name, None, note="no attribute 187", unknown=True)
                    row("reported_unc_devstat", "Uncorrectable, page 4", f"{unc:,}")
                continue
            raw = (a.get("raw") or {}).get("value")
            if key == "wear":
                row(key, name, f"{100 - int(a.get('value', 100))}%")
            else:
                row(key, name, f"{raw:,}" if isinstance(raw, int) else str(raw))
    elif sd.get("scsi_grown_defect_list") is not None:
        row("grown_defects", "Grown defects", f"{sd['scsi_grown_defect_list']:,}")
    for key, (sev, expect) in marks.items():
        if not any(r["key"] == key for r in rows):
            rows.append({"key": "other", "name": key, "value": "", "mark": sev,
                         "expect": expect, "note": None})

    derived = drive.get("derived") or {}

    def volume(which):
        v = derived.get(f"host_{which}")
        if isinstance(v, dict) and v.get("bytes") is not None:
            source = v.get("source") or ""
            out = {"bytes": v["bytes"],
                   "source": "attribute" if "attribute" in source
                   else "nvme" if "nvme" in source else "devstat"}
            if v.get("attribute_id"):
                out["attribute_id"] = v["attribute_id"]
            return out
        return {"omitted": derived.get(f"host_{which}_omitted") or "no_source"}

    ds = drive.get("device_statistics") or {}
    dstatus = ds.get("status") or "absent"
    if dstatus in ("off", "unavailable") and ds.get("reason"):
        dstatus = f"{dstatus}: {ds['reason']}"
    if log:
        report = {"kind": "nvme", "count": len(log)}
    elif attrs:
        report = {"kind": "ata", "count": len(attrs)}
    else:
        report = {"kind": "none", "count": 0}
    return {
        "header": header,
        "health_failed": (sd.get("smart_status") or {}).get("passed") is False,
        "stats": {"temp_c": (sd.get("temperature") or {}).get("current"),
                  "power_on_hours": (sd.get("power_on_time") or {}).get("hours"),
                  "power_cycles": sd.get("power_cycle_count")},
        "rows": rows,
        "volumes": {"written": volume("writes"), "read": volume("reads")},
        "devstat": dstatus,
        "report": report,
        "can_save": state not in ("UNSUPPORTED", None),
    }


def drive_view(drive: dict, verdict: dict | None) -> dict:
    if HAVE_DRIVE_VIEW and verdict is not None:
        try:
            return PROXY.drive_view(drive, verdict)
        except Exception as err:  # noqa: BLE001
            print(f"[dev] proxy.drive_view failed, using the stand-in: {err}")
    return stub_drive_view(drive, verdict)


def pool_verdict(pool: dict) -> dict:
    if HAVE_POOL_VERDICT:
        try:
            return PROXY.pool_verdict(pool)
        except Exception as err:  # noqa: BLE001
            print(f"[dev] proxy.pool_verdict failed, using the stand-in: {err}")
    problems = []
    if pool.get("state") != "ONLINE":
        problems.append(f"State: {pool.get('state')}")
    for dev in pool.get("devices") or []:
        if dev.get("state") != "ONLINE" or dev.get("read") or dev.get("write") or dev.get("cksum"):
            problems.append(f"{dev['name']}: {dev.get('state')}")
    for v in pool.get("problem_vdevs") or []:
        problems.append(f"{v.get('name')}: {v.get('state')}")
    return {"state": "YES" if problems else "NO", "problems": problems}


# ── Drive and pool builders ──────────────────────────────────────────────────

def attr(aid, name, raw, value=100, worst=100, thresh=0, raw_string=None):
    return {"id": aid, "name": name, "value": value, "worst": worst, "thresh": thresh,
            "when_failed": "", "raw": {"value": raw, "string": raw_string or str(raw)}}


FILLER = [(1, "Raw_Read_Error_Rate", 109361208, 80, 64, 6), (3, "Spin_Up_Time", 0, 97, 96, 0),
          (4, "Start_Stop_Count", 1238, 99, 99, 20), (7, "Seek_Error_Rate", 688412001, 88, 60, 45),
          (183, "Runtime_Bad_Block", 0, 100, 100, 0), (184, "End-to-End_Error", 0, 100, 100, 99),
          (189, "High_Fly_Writes", 0, 100, 100, 0), (190, "Airflow_Temperature_Cel", 38, 62, 48, 40),
          (191, "G-Sense_Error_Rate", 0, 100, 100, 0), (192, "Power-Off_Retract_Count", 112, 100, 100, 0),
          (193, "Load_Cycle_Count", 4120, 98, 98, 0), (195, "Hardware_ECC_Recovered", 109361208, 80, 64, 0),
          (240, "Head_Flying_Hours", 31012, 100, 253, 0)]

# smartctl's flags on a Device Statistics entry; the rules read only valid ones.
DEVSTAT_VALID = {"value": 192, "string": "V--- ", "valid": True, "normalized": False,
                 "supports_dsn": False, "monitored_condition_met": False}

ATA_COUNTERS = {5: "Reallocated_Sector_Ct", 197: "Current_Pending_Sector", 198: "Offline_Uncorrectable",
                187: "Reported_Uncorrect", 188: "Command_Timeout", 196: "Reallocated_Event_Count",
                10: "Spin_Retry_Count"}


def ata_drive(drive_id, model, serial, path, *, temp, poh, cycles, counters=None,
              written_tb=None, read_tb=None, written_from="attribute", read_from=None,
              passed=True, devstat_unc_count=None, filler=0, dtype="sat", last=12,
              firmware=None, capacity=None, link=None):
    """An ATA drive as the agent serves it. counters maps attribute id to raw.
    firmware, capacity (bytes) and link ((current, max) SATA speeds) are
    what smartctl says about the drive itself; left out when None."""
    counters = counters if counters is not None else {5: 0, 197: 0, 198: 0, 187: 0, 188: 0}
    table = [attr(aid, ATA_COUNTERS[aid], raw) for aid, raw in counters.items()]
    table += [attr(9, "Power_On_Hours", poh, 65, 65, 0), attr(12, "Power_Cycle_Count", cycles, 99, 99, 20),
              attr(194, "Temperature_Celsius", temp, temp, 52, 0, f"{temp} (0 18 0 0 0)")]
    if written_tb is not None and written_from == "attribute":
        lbas = int(written_tb * 1e12 / 512)
        table.append(attr(241, "Total_LBAs_Written", lbas, 100, 253, 0))
    table += [attr(*f) for f in FILLER[:filler]]
    table.sort(key=lambda a: a["id"])
    sd = {"device": {"name": path, "type": dtype, "protocol": "ATA"},
          "model_name": model, "serial_number": serial, "logical_block_size": 512,
          "smart_status": {"passed": passed}, "temperature": {"current": temp},
          "power_on_time": {"hours": poh}, "power_cycle_count": cycles,
          "ata_smart_attributes": {"revision": 16, "table": table}}
    if firmware:
        sd["firmware_version"] = firmware
    if capacity:
        sd["user_capacity"] = {"blocks": capacity // 512, "bytes": capacity}
    if link:
        sd["interface_speed"] = {"max": {"string": link[1]}, "current": {"string": link[0]}}
    derived: dict = {}
    if written_tb is not None:
        if written_from == "attribute":
            derived["host_writes"] = {"bytes": int(written_tb * 1e12), "source": "ata_attribute",
                                      "attribute_id": 241, "attribute_name": "Total_LBAs_Written"}
        else:
            derived["host_writes"] = {"bytes": int(written_tb * 1e12), "source": "ata_device_statistics"}
    else:
        derived["host_writes_omitted"] = "no_source"
    if read_tb is not None:
        derived["host_reads"] = {"bytes": int(read_tb * 1e12),
                                 "source": read_from or "ata_device_statistics"}
    else:
        derived["host_reads_omitted"] = "no_source"
    if devstat_unc_count is not None or written_from == "devstat" or read_from == "ata_device_statistics":
        ws = int((written_tb or 0) * 1e12 / 512)
        rs = int((read_tb or 0) * 1e12 / 512)
        def entry(offset, name, size, value):
            return {"offset": offset, "name": name, "size": size, "value": value, "flags": dict(DEVSTAT_VALID)}

        ds = {"status": "present", "complete": True, "logical_block_size": 512, "pages": [
            {"number": 1, "name": "General Statistics", "revision": 1, "table": [
                entry(8, "Lifetime Power-On Resets", 4, cycles),
                entry(16, "Power-on Hours", 4, poh),
                entry(24, "Logical Sectors Written", 6, ws),
                entry(40, "Logical Sectors Read", 6, rs)]},
            {"number": 4, "name": "General Errors Statistics", "revision": 1, "table": [
                entry(8, "Number of Reported Uncorrectable Errors", 4, devstat_unc_count or 0),
                entry(16, "Resets Between Cmd Acceptance and Completion", 4, 0)]},
            {"number": 5, "name": "Temperature Statistics", "revision": 1, "table": [
                entry(8, "Current Temperature", 1, temp),
                entry(32, "Highest Temperature", 1, temp + 13),
                entry(40, "Lowest Temperature", 1, 19),
                entry(80, "Time in Over-Temperature", 4, 0)]},
            {"number": 6, "name": "Transport Statistics", "revision": 1, "table": [
                entry(8, "Number of Hardware Resets", 4, cycles + 3),
                entry(24, "Number of Interface CRC Errors", 4, 0)]}]}
    else:
        ds = {"status": "absent"}
    return {"id": drive_id, "model": model, "serial": serial, "protocol": "ATA",
            "device_path": path, "readable": True, "in_standby": False,
            "last_updated": iso(last), "smart_data": sd, "device_statistics": ds, "derived": derived}


def nvme_drive(drive_id, model, serial, path, *, temp, poh, cycles, written_tb, read_tb=None,
               spare=100, used=2, media=0, cw=0, last=12, firmware=None, capacity=None):
    log = {"critical_warning": cw, "temperature": temp, "available_spare": spare,
           "available_spare_threshold": 10, "percentage_used": used,
           "data_units_read": int((read_tb or 0) * 1e12 / 512000),
           "data_units_written": int(written_tb * 1e12 / 512000),
           "host_reads": 102_334_221, "host_writes": 88_120_431, "controller_busy_time": 1_204,
           "power_cycles": cycles, "power_on_hours": poh, "unsafe_shutdowns": 4,
           "media_errors": media, "num_err_log_entries": 0, "warning_temp_time": 0,
           "critical_comp_time": 0, "temperature_sensors": [temp, temp + 6], "thm_temp1_trans_count": 0}
    sd = {"device": {"name": path, "type": "nvme", "protocol": "NVMe"},
          "model_name": model, "serial_number": serial, "smart_status": {"passed": True},
          "temperature": {"current": temp}, "power_on_time": {"hours": poh},
          "power_cycle_count": cycles, "nvme_smart_health_information_log": log}
    if firmware:
        sd["firmware_version"] = firmware
    if capacity:
        sd["nvme_total_capacity"] = capacity
    derived = {"host_writes": {"bytes": int(written_tb * 1e12), "source": "nvme"}}
    if read_tb is not None:
        derived["host_reads"] = {"bytes": int(read_tb * 1e12), "source": "nvme"}
    else:
        derived["host_reads_omitted"] = "no_source"
    return {"id": drive_id, "model": model, "serial": serial, "protocol": "NVMe",
            "device_path": path, "readable": True, "in_standby": False, "last_updated": iso(last),
            "smart_data": sd, "device_statistics": {"status": "not_applicable"}, "derived": derived}


def unsupported_drive(drive_id, model, serial, path, protocol="ATA", dtype="ata"):
    return {"id": drive_id, "model": model, "serial": serial, "protocol": protocol,
            "device_path": path, "readable": True, "in_standby": False, "last_updated": iso(6),
            "smart_data": {"device": {"name": path, "type": dtype, "protocol": protocol},
                           "model_name": model, "serial_number": serial},
            "device_statistics": {"status": "absent"},
            "derived": {"host_writes_omitted": "no_source", "host_reads_omitted": "no_source"}}


LAYOUT_WORDS = {1: "single disk", 2: "two-disk", 3: "three-disk", 4: "four-disk"}


def pool(name, layout_type, disks, *, state="ONLINE", scrub_days=4):
    devices = [{"name": d, "state": "ONLINE", "read": 0, "write": 0, "cksum": 0} for d in disks]
    return {"name": name, "state": state, "status": "", "action": "",
            "read_errors": 0, "write_errors": 0, "checksum_errors": 0, "data_errors": 0,
            "scan_function": "SCRUB", "scan_state": "FINISHED", "scrub_in_progress": False,
            "last_scrub_end": iso(scrub_days * DAY), "last_scrub_repaired": 0,
            "last_scrub_errors": 0, "problem_vdevs": [],
            "vdev": {"name": f"{layout_type}-0", "type": layout_type}, "devices": devices}


def pool_layout(p: dict) -> str | None:
    vdev, devices = p.get("vdev"), p.get("devices")
    if not vdev or not devices:
        return None
    n = len(devices)
    if n == 1:
        return "single disk"
    words = LAYOUT_WORDS.get(n, f"{n}-disk")
    return f"{words} {vdev.get('type')}"


def refresh_pool(p: dict) -> dict:
    """Error totals and problem vdevs from the disk list, as the agent reports them."""
    devs = p.get("devices") or []
    p["read_errors"] = sum(d["read"] for d in devs)
    p["write_errors"] = sum(d["write"] for d in devs)
    p["checksum_errors"] = sum(d["cksum"] for d in devs)
    bad = [d for d in devs if d["state"] != "ONLINE" or d["read"] or d["write"] or d["cksum"]]
    vdev = p.get("vdev") or {}
    p["problem_vdevs"] = ([{"name": vdev.get("name"), "type": vdev.get("type"), "state": p["state"],
                            "read_errors": 0, "write_errors": 0, "checksum_errors": 0}]
                          if p["state"] != "ONLINE" else []) + [
        {"name": d["name"], "type": "disk", "state": d["state"], "read_errors": d["read"],
         "write_errors": d["write"], "checksum_errors": d["cksum"]} for d in bad]
    return p


# ── This machine ─────────────────────────────────────────────────────────────

def self_drives(flags) -> list:
    which = flags["DRIVES"]
    nvme = nvme_drive("nvme0", "Samsung SSD 980 PRO 1TB", "S5GXNF0R412233A", "/dev/nvme0n1",
                      temp=41, poh=2210, cycles=11, written_tb=4.67, read_tb=9.34,
                      firmware="5B2QGXA7", capacity=1_000_204_886_016)
    out = []
    if which == "board":
        bc = ata_drive("sda", "Seagate BarraCuda 2TB ST2000DM008", "ZFL0K7Q2", "/dev/sda",
                       temp=38, poh=31502, cycles=1204, counters={5: 24, 197: 3, 198: 0, 187: 0, 188: 0},
                       written_tb=61.20, filler=13, last=4, firmware="0001", capacity=2_000_398_934_016,
                       link=("6.0 Gb/s", "6.0 Gb/s"))
        bc["device_statistics"] = {"status": "present", "complete": True, "pages": []}
        out = [nvme, bc]
    elif which == "mixed":
        bc = ata_drive("sda", "Seagate BarraCuda 2TB ST2000DM008", "ZFL0K7Q2", "/dev/sda",
                       temp=38, poh=31502, cycles=1204, counters={5: 24, 197: 3, 198: 2, 187: 4, 188: 0},
                       written_tb=61.20, filler=13, last=4, firmware="0001", capacity=2_000_398_934_016,
                       link=("6.0 Gb/s", "6.0 Gb/s"))
        red = ata_drive("sdb", "WDC WD40EFRX-68N32N0", "WD-WCC7K1234567", "/dev/sdb",
                        temp=36, poh=40211, cycles=88, counters={5: 0, 197: 0, 198: 0, 187: 0, 188: 120},
                        written_tb=12.4, filler=4, last=6, firmware="82.00A82", capacity=4_000_787_030_016,
                        link=("3.0 Gb/s", "6.0 Gb/s"))
        out = [nvme, bc, red]
    elif which in ("1", "2"):
        out = [nvme]
        if which == "2":
            out.append(ata_drive("sdb", "WDC  WUH721414ALE604", "9JG0X1AB", "/dev/sdb", temp=34,
                                 poh=27694, cycles=10, counters={5: 0, 197: 0, 198: 0, 196: 0, 10: 0},
                                 written_tb=107.83, written_from="devstat", read_tb=791.80,
                                 devstat_unc_count=0, firmware="LDGNW400", capacity=14_000_519_643_136,
                                 link=("6.0 Gb/s", "6.0 Gb/s")))
    if flags["VDISK"] == "1":
        out.append(unsupported_drive("sdc", "QEMU HARDDISK", "QM00005", "/dev/sdc"))
    return out


def fs(fs_id, mount, device, pct, total_tb, fstype="ext4", status="ok"):
    """One filesystem as the agent's /api/filesystems lists it."""
    total = int(total_tb * 1e12)
    used = int(total * pct / 100) if status == "ok" else 0
    return {"id": fs_id, "uuid": "", "mountpoint": mount, "device": device, "fstype": fstype,
            "total_bytes": total if status == "ok" else 0, "used_bytes": used,
            "available_bytes": total - used if status == "ok" else 0,
            "use_percent": float(pct) if status == "ok" else 0.0, "status": status}


def self_filesystems(flags) -> list:
    if flags["DRIVES"] == "0":
        return []
    return [fs("fs-6a1f03c2", "/", "/dev/nvme0n1p8", 41, 0.98),
            fs("fs-0b77e1d4", "/boot", "/dev/nvme0n1p1", 12, 0.000033, "vfat")]




def self_pools(flags) -> list:
    which = flags["POOL"]
    if which not in ("online", "degraded"):
        return []
    p = pool("tank", "mirror", ["sdd", "sde"])
    if which == "degraded":
        p["state"] = "DEGRADED"
        p["devices"][1].update(state="FAULTED", read=3, write=0, cksum=12)
    return [refresh_pool(p)]


# ── The world: what the page changed ─────────────────────────────────────────

class World:
    def __init__(self, flags: dict):
        self.started = time.time()
        self.self_cache: list | None = None
        self.self_last_answer = time.time() - 3 * 3600
        self.dismissed = {"install": False, "update": False}
        self.haos_created = False
        self.adds: list = []


WORLDS: dict[tuple, World] = {}


def world_for(flags, fresh: bool = False) -> World:
    key = tuple(flags[k] for k in WORLD_KEYS)
    if fresh or key not in WORLDS:
        WORLDS[key] = World(flags)
    return WORLDS[key]


# ── Responses ────────────────────────────────────────────────────────────────

def drive_item(drive: dict, flags) -> dict:
    verdict = None if flags["RULES"] == "missing" else evaluate(drive)
    return {"type": "drive", "id": drive["id"], "drive": drive, "verdict": verdict,
            "view": drive_view(drive, verdict), "lab": None, "thresholds_source": "app"}


def pool_item(p: dict, flags) -> dict:
    """A pool as the proxy answers it for a real agent: no disk layout."""
    verdict = None if flags["RULES"] == "missing" else pool_verdict(p)
    if HAVE_POOL_VIEW:
        view = PROXY.pool_view(p, verdict, None)
    else:
        state = (verdict or {}).get("state")
        view = {"header": "unknown" if state is None else ("attention" if state == "YES" else "ok"),
                "layout": None, "state": p.get("state"), "disks": None, "vanished": False}
    return {"type": "pool", "id": f"pool.{p['name']}", "pool": p, "verdict": verdict,
            "view": view, "lab": None}


def summarize(items: list, status: str) -> dict:
    if PROXY is not None and callable(getattr(PROXY, "summarize", None)):
        return PROXY.summarize(items, status == "ok")
    return {"header": "unknown", "attention": 0, "maybe": 0, "unsupported": 0,
            "drives": sum(1 for i in items if i["type"] == "drive"),
            "pools": sum(1 for i in items if i["type"] == "pool")}


def card_key(item: dict) -> str:
    if item["type"] == "pool":
        return f"pool:{item['pool']['name']}"
    serial = str(item["drive"].get("serial") or "").strip()
    return serial or f"id:{item['id']}"


def with_cards(items: list, filesystems: list) -> tuple[list, list]:
    """Card ids, each filesystem on its drive or pool, the rest as FILESYSTEM
    cards: as the proxy answers."""
    for item in items:
        item["card_id"] = f"self|{card_key(item)}"
    filesystems = copy.deepcopy(filesystems)
    if PROXY is not None and callable(getattr(PROXY, "attach_filesystems", None)):
        fs_cards = PROXY.attach_filesystems("self", items, filesystems)
    else:
        for item in items:
            item["filesystems"] = []
        fs_cards = [{"type": "filesystem", "id": f["id"], "card_id": f"self|fs:{f['id']}",
                     "filesystem": f} for f in filesystems]
    return items, fs_cards


def agent_host(flags) -> str:
    host = flags["HOSTNAME"].strip()
    slug = flags["SLUG"].strip()
    if slug.lower() in ("", "none", "null"):
        slug = "smart_sniffer_agent"
    return host or slug.replace("_", "-")


def self_response(flags, world: World) -> dict:
    drives = self_drives(flags)
    pools = self_pools(flags)
    status = "unreachable" if flags["AGENT"] == "down" else "ok"
    if status == "ok":
        items = [drive_item(d, flags) for d in drives] + [pool_item(p, flags) for p in pools]
        world.self_cache = copy.deepcopy(items)
        world.self_last_answer = time.time()
    else:
        items = world.self_cache if world.self_cache is not None else (
            [drive_item(d, flags) for d in drives] + [pool_item(p, flags) for p in pools])
        items = copy.deepcopy(items)
        for i in items:
            i["stale"] = True
    items, fs_cards = with_cards(items, self_filesystems(flags))
    if status != "ok":
        for c in fs_cards:
            c["stale"] = True
    last = datetime.fromtimestamp(world.self_last_answer, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    agent = {"hostname": agent_host(flags), "version": AGENT_VERSION, "os": "linux", "os_name": None,
             "uptime_seconds": int(time.time() - world.started) + 3600,
             "drives": len(drives), "pools": len(pools)} if status == "ok" else None
    return {"id": "self", "kind": "self", "status": status,
            "read_at": iso(0) if status == "ok" else None, "last_answer": last,
            "stale": status != "ok", "agent": agent, "summary": summarize(items, status),
            "items": items, "filesystem_cards": fs_cards,
            "pools_status": "ok" if pools else "none",
            "filesystems_status": "ok" if fs_cards or any(i.get("filesystems") for i in items) else "none",
            "rules_version": None if flags["RULES"] == "missing" else RULES_VERSION}


def integration(flags, world: World) -> dict:
    """GET /ha/integration, as the proxy answers it."""
    which = flags["INTEGRATION"]
    out = {"reachable": which != "unreachable", "state": None, "version": None, "panel": None,
           "panel_path": "/smart-sniffer", "haos_entry": None, "create": False, "hacs": None,
           "integration_url": "/config/integrations/integration/smart_sniffer",
           "dismissed": dict(world.dismissed)}
    if which == "unreachable":
        return out
    if which in ("loaded", "panel_failed"):
        haos = flags["HAOS_ENTRY"] != "0" or world.haos_created
        out.update(state="loaded", version="0.9.2", panel=which == "loaded",
                   panel_path=flags["PANEL_PATH"] or "/smart-sniffer", haos_entry=haos,
                   create=not haos)
    elif which == "no_entries":
        out.update(state="no_entries", version="0.9.2", haos_entry=False,
                   create=not world.haos_created)
        if world.haos_created:
            out.update(state="loaded", panel=True, haos_entry=True)
    elif which == "restart":
        out.update(state="restart", version="0.9.2")
    elif which == "update":
        out.update(state="update", version="0.8.5")
    elif which == "missing":
        out.update(state="missing", hacs=flags["HACS"] != "missing")
    return out


def flags_for(handler) -> dict[str, str]:
    flags = {k: os.environ.get(k, v) for k, v in DEFAULTS.items()}
    sources = ((urlparse(handler.headers.get("Referer", "")).query, False),
               (urlparse(handler.path).query, True))
    for source, own in sources:
        for k, v in parse_qs(source).items():
            if own and k.islower():
                continue
            if k.upper() in DEFAULTS and v:
                flags[k.upper()] = v[0]
    return flags


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("QUIET"):
            return
        sys.stderr.write("[dev] " + (fmt % args) + "\n")

    def _send(self, code, data=None, ctype="application/json", body=None, headers=None):
        if body is None:
            body = json.dumps(data if data is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, error, message=None, **extra):
        body = {"error": error}
        if message:
            body["message"] = message
        body.update(extra)
        return self._send(code, body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except ValueError:
            return None

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

    def do_PUT(self):
        self._dispatch("PUT")

    def _dispatch(self, method):
        route = self._route()
        head = route.split("/")[0]
        if head not in ("api", "mock", "app", "evaluate", "ha", "fleet", "replicas", "dev", "cards", "hosts",
                        "agentic-access", "agentic-access.json"):
            if method != "GET":
                return self._err(405, "method_not_allowed")
            return self._static(route)
        flags = flags_for(self)
        time.sleep(float(os.environ.get("LATENCY", "0.02")))
        if route.strip("/") == "fleet/hosts/self/drives":
            try:
                time.sleep(max(0.0, float(flags["SELF_DELAY"])))
            except ValueError:
                pass
        try:
            with LOCK:
                self._api(method, [unquote(p) for p in route.strip("/").split("/")], flags)
        except BrokenPipeError:
            pass

    def _static(self, route):
        rel = route or "index.html"
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR not in target.parents and target != WEB_DIR:
            return self._err(403, "forbidden")
        if not target.is_file():
            if "." in rel.split("/")[-1]:
                return self._send(404, body=b"not found", ctype="text/plain")
            target = WEB_DIR / "index.html"
        body = target.read_bytes()
        if body.startswith(b"// PLACEHOLDER") and os.environ.get("VENDOR_DIR"):
            real = Path(os.environ["VENDOR_DIR"]) / target.name
            if real.is_file():
                body = real.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, body=body, ctype=ctype)

    # routes
    def _api(self, method, parts, flags):
        head = parts[0]
        if head == "dev" and parts[1:] == ["reset"] and method == "POST":
            world_for(flags, fresh=True)
            return self._send(200, {"reset": True})
        world = world_for(flags)
        if head == "api":
            return self._agent_api(parts[1:], flags)
        if parts == ["app", "info"] and method == "GET":
            return self._app_info(flags)
        if parts == ["app", "notice", "dismiss"] and method == "POST":
            body = self._body() or {}
            which = body.get("notice", "install") if isinstance(body, dict) else "install"
            if which not in ("install", "update"):
                return self._err(400, "bad_request")
            world.dismissed[which] = True
            return self._send(200, {"notice": which, "dismissed": True})
        if parts == ["fleet", "hosts", "self", "drives"] and method == "GET":
            return self._send(200, self_response(flags, world))
        if parts == ["ha", "integration"] and method == "GET":
            return self._send(200, integration(flags, world))
        if parts == ["ha", "add"] and method == "POST":
            body = self._body() or {}
            world.adds.append(body)
            if not isinstance(body, dict) or body.get("target") != "self":
                return self._err(400, "bad_target", 'target must be "self"')
            if flags["ADD"] == "error":
                return self._send(502, {"error": "add_failed", "result": "error", "message": "flow_failed"})
            world.haos_created = True
            return self._send(200, {"result": "created", "entry_id": "entry-haos"})
        if len(parts) == 3 and parts[0] == "cards" and parts[2] == "file" and method == "GET":
            response = self_response(flags, world)
            item = next((i for i in response["items"] if i["card_id"] == parts[1]), None)
            if item is None:
                return self._err(404, "not_found")
            doc = ({"pool": item["pool"], "filesystems": item.get("filesystems") or []}
                   if item["type"] == "pool" else
                   {"drive": item["drive"], "filesystems": item.get("filesystems") or []})
            name = f"pool-{item['pool']['name']}" if item["type"] == "pool" else f"drive-{item['drive']['model']}"
            safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in name).strip("-")
            return self._send(200, body=json.dumps(doc, indent=2).encode(),
                              headers={"Content-Disposition": f'attachment; filename="{safe}.json"'})
        return self._err(404, "not_found")

    def _app_info(self, flags):
        version = flags["HA_VERSION"].strip()
        slug = flags["SLUG"].strip()
        return self._send(200, {
            "app_version": APP_VERSION,
            "rules_version": None if flags["RULES"] == "missing" else RULES_VERSION,
            "agent_port": AGENT_PORT,
            "hostname": agent_host(flags),
            "slug": None if slug.lower() in ("", "none", "null") else slug,
            "mdns_name": "smartha-homeassistant" if flags["MDNS"] != "off" else None,
            "ha_version": None if flags["INTEGRATION"] == "unreachable" or version.lower() in ("", "none") else version,
            "ha_api": flags["INTEGRATION"] != "unreachable", "supervisor": True,
        })

    def _agent_api(self, parts, flags):
        if flags["AGENT"] == "down":
            return self._err(502, "backend_unavailable")
        drives = self_drives(flags)
        if parts == ["health"]:
            return self._send(200, {"status": "ok", "version": AGENT_VERSION, "os": "linux",
                                    "endpoints": ["/api/health", "/api/drives", "/api/drives/{id}"],
                                    "drives": len(drives)})
        if parts == ["drives"]:
            return self._send(200, [{k: d[k] for k in ("id", "device_path", "model", "serial", "protocol")}
                                    for d in drives])
        if len(parts) == 2 and parts[0] == "drives":
            for d in drives:
                if d["id"] == parts[1]:
                    return self._send(200, d)
        return self._err(404, "not_found")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8099)))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"[dev] App page dev harness on http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
