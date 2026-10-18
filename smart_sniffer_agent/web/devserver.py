#!/usr/bin/env python3
"""Dev harness for the Control Center page. Not used by the app.

Serves this directory and fakes every v3 route the page calls, so the page
can be opened and tested without the app, the Go agent, the replica agent,
field agents or Home Assistant. Python 3.9+, standard library only.

    python3 devserver.py                         # http://127.0.0.1:8099/
    HOSTS=none REPLICAS=0 python3 devserver.py --port 8123

Switches come from the environment or from the page's query string (for
example http://127.0.0.1:8099/?HA=not_installed&HOSTS=none). The page's own
requests carry no query string, so the server reads the switches from the
Referer.

    AGENT           up | down                 this app's agent; down: the HAOS cards keep
                                              their last readings, not answering
    DRIVES          board | 0 | 1 | 2         HAOS drives. board: a healthy NVMe with / at
                                              41% and /boot at 12%, and a SATA drive at
                                              Attention; 0: none; 1: the NVMe; 2: the NVMe
                                              and a WDC with Device Statistics
    VDISK           0 | 1                     1: add a QEMU virtual disk
    RULES           ok | missing              missing: no verdicts, the rules line says so
    HA              installed | not_installed | unreachable
                                              exit icons; none and no Integration link; none
    HA_LOOKUP       registry | entries_only   device links or entry links
    HA_SELF         1 | 0                     this app's agent in HA (blue or grey icons)
    ADD             ok | error                /ha/add succeeds or answers flow_failed
    HA_NAME         ok | fail                 a rename reaches Home Assistant's device, or
                                              Home Assistant does not take it
    HA_REMOVE       ok | fail                 the trash's Home Assistant half (disable,
                                              delete, entry, replica) is taken, or refused
    NETWORK         supervisor | none | wide  prefill 10.0.10.0/24; null (page falls
                                              back to its own address); 10.0.0.0/16
    SCAN            normal | slow | none      rows arrive over 3 s; over 12 s; nothing
                                              answers
    HOSTS           board | one | none        board: nas-01, studio-mac and backup-pi;
                                              one: only proxmox-01; none: first run
    POOL            online | degraded         the tank pools of nas-01 and proxmox-01
    REPLICAS        board | carried | 0       three replicas (SMART failed, NVMe wear, a
                                              degraded pool), the first in HA; carried:
                                              the same, the NVMe one with no current
                                              scenario (as the 0.2.x mock drives left
                                              it), and
                                              the Device Statistics replica; none
    REPLICA_FOLDER  ok | missing | read_only  upload and save enabled or not
    MDNS            on | off                  the agent's mDNS name in /app/info, or none
    LATENCY         seconds                   added to every route (default 0.02)
    SLUG, HOSTNAME, HA_VERSION                the app's slug, its agent host name, Home
                                              Assistant's version (2025.x and earlier
                                              give the /hassio/... link paths)

Writes (stored hosts, card order, hidden and removed cards, passwords,
replicas, HA adds) live in memory until the server stops. Each set of the
switches that shape them (HOSTS, POOL, REPLICAS, HA, HA_SELF, DRIVES, VDISK)
has its own copy; a POST to /dev/reset with the switches in its query string
starts that copy afresh, as the page checks do before each page they open.

The board's field agents: nas-01 (six drives: one at Attention with five
problem rows, one Maybe, one not judged; disk space at 82%, 91% and 97%; a
raidz1 pool tank with three datasets; a filesystem on /dev/md1 that belongs
to no drive; in Home Assistant except its two newest drives, and two of
those carry thresholds set there: sdc at Attention with 8 reallocated
sectors over its 5, sdd with 2 accepted under its 4; sdb's device there
has a drive id the agent no longer reports, so its trash offers a delete,
the rest a disable), studio-mac (not
answering, last answer 3 h ago), backup-pi (wants a password, it is
fieldpass).

Thresholds: a drive in Home Assistant uses the integration's, any other the
app's own; GET and PUT /cards/<id>/thresholds and POST .../accept and
.../reset answer as the proxy does, and /ha/add moves the app's into Home
Assistant.

A scan of 10.0.10.0/24 on port 9099 finds office-nuc (10.0.10.61, open),
backup-pi (10.0.10.52, 401) and proxmox-01 (10.0.10.21), at 0.8 s, 1.6 s
and 2.2 s; Check answers for every machine of the catalog (nas.lan and
10.0.20.14 among them), 10.0.10.98 answers but is not an agent, and
nothing else answers.

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
import ipaddress
import json
import mimetypes
import os
import random
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
MOCK_PORT = 9100
APP_VERSION = "0.3.1"
AGENT_VERSION = "0.8.1"
RULES_VERSION_FALLBACK = "v0.8.4"
REPLICA_PATH = "/share/smart-sniffer/replicas"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

DEFAULTS = {
    "AGENT": "up",
    "DRIVES": "board",
    "VDISK": "0",
    "RULES": "ok",
    "HA": "installed",
    "HA_LOOKUP": "registry",
    "HA_SELF": "1",
    "ADD": "ok",
    "HA_NAME": "ok",
    "HA_REMOVE": "ok",
    "NETWORK": "supervisor",
    "SCAN": "normal",
    "HOSTS": "board",
    "POOL": "online",
    "REPLICAS": "board",
    "REPLICA_FOLDER": "ok",
    "SLUG": "local_smart_sniffer_agent",
    "HOSTNAME": "",
    "HA_VERSION": "2026.10.1",
    "MDNS": "on",
}
# The switches that shape the in-memory world; a change rebuilds it.
WORLD_KEYS = ("HOSTS", "POOL", "REPLICAS", "HA", "HA_SELF", "DRIVES", "VDISK")

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


def threshold_answer(card_id: str, source: str, drive: dict, overrides: dict) -> dict:
    """GET /cards/<id>/thresholds, as the proxy answers it."""
    verdict = RULES.evaluate(copy.deepcopy(drive), overrides)
    return {"card_id": card_id, "source": source, "labels": verdict["limits"]}


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
              passed=True, devstat_unc_count=None, filler=0, dtype="sat", last=12):
    """An ATA drive as the agent serves it. counters maps attribute id to raw."""
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
                entry(16, "Resets Between Cmd Acceptance and Completion", 4, 0)]}]}
    else:
        ds = {"status": "absent"}
    return {"id": drive_id, "model": model, "serial": serial, "protocol": "ATA",
            "device_path": path, "readable": True, "in_standby": False,
            "last_updated": iso(last), "smart_data": sd, "device_statistics": ds, "derived": derived}


def nvme_drive(drive_id, model, serial, path, *, temp, poh, cycles, written_tb, read_tb=None,
               spare=100, used=2, media=0, cw=0, last=12):
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
                      temp=41, poh=2210, cycles=11, written_tb=4.67, read_tb=9.34)
    out = []
    if which == "board":
        bc = ata_drive("sda", "Seagate BarraCuda 2TB ST2000DM008", "ZFL0K7Q2", "/dev/sda",
                       temp=38, poh=31502, cycles=1204, counters={5: 24, 197: 3, 198: 0, 187: 0, 188: 0},
                       written_tb=61.20, filler=13, last=4)
        bc["device_statistics"] = {"status": "present", "complete": True, "pages": []}
        out = [nvme, bc]
    elif which in ("1", "2"):
        out = [nvme]
        if which == "2":
            out.append(ata_drive("sdb", "WDC  WUH721414ALE604", "9JG0X1AB", "/dev/sdb", temp=34,
                                 poh=27694, cycles=10, counters={5: 0, 197: 0, 198: 0, 196: 0, 10: 0},
                                 written_tb=107.83, written_from="devstat", read_tb=791.80,
                                 devstat_unc_count=0))
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


# ── Field agents ─────────────────────────────────────────────────────────────

def field_catalog(flags) -> dict:
    """Every agent the harness knows, by address. Built fresh for each world."""
    cat: dict[str, dict] = {}

    tank = pool("tank", "mirror", ["ZGY8A1C2", "ZGY8B2D1"])
    if flags["POOL"] == "degraded":
        tank["state"] = "DEGRADED"
        tank["devices"][1].update(state="FAULTED", read=12)
    refresh_pool(tank)
    cat["10.0.10.21"] = {
        "hostname": "proxmox-01", "version": "0.8.1", "os_name": "Proxmox VE 8.2",
        "uptime": (43 if flags["POOL"] == "degraded" else 41) * DAY, "auth": "password",
        "password": "fieldpass",
        "drives": [
            ata_drive("sda", "Seagate IronWolf 4TB", "ZGY8A1C2", "/dev/sda", temp=39, poh=18220, cycles=96,
                      counters={5: 0, 197: 0, 196: 1}, written_tb=22.10, read_tb=40.81),
            ata_drive("sdb", "Seagate IronWolf 4TB", "ZGY8B2D1", "/dev/sdb", temp=38, poh=18220, cycles=96,
                      counters={5: 0, 197: 0, 196: 0}, written_tb=21.96, read_tb=39.02),
            nvme_drive("nvme0", "WD_BLACK SN850X 1TB", "23124F801234", "/dev/nvme0n1", temp=44,
                       poh=9120, cycles=210, written_tb=7.92, read_tb=11.40),
        ],
        "pools": [tank],
    }
    cat["10.0.10.61"] = {
        "hostname": "office-nuc", "version": "0.8.1", "os_name": "Ubuntu 24.04", "uptime": 6 * DAY,
        "auth": "open",
        "drives": [
            nvme_drive("nvme0", "Crucial P3 Plus 2TB", "2309E6A1B2C3", "/dev/nvme0n1", temp=47, poh=5120,
                       cycles=301, written_tb=12.05, read_tb=20.10),
            ata_drive("sda", "Kingston A400 240GB", "50026B7782A1B2C3", "/dev/sda", temp=31, poh=14100,
                      cycles=880, counters={5: 0, 187: 0, 196: 0}, written_tb=9.87),
        ],
        "pools": None,
    }
    cat["10.0.10.52"] = {
        "hostname": "backup-pi", "version": "0.8.1", "os_name": "Raspberry Pi OS 12", "uptime": 88 * DAY,
        "auth": "password", "password": "fieldpass",
        "drives": [nvme_drive("nvme0", "SanDisk Extreme Pro 1TB", "32315D480012", "/dev/nvme0n1",
                              temp=35, poh=2100, cycles=40, written_tb=2.40, read_tb=3.10)],
        "pools": None,
    }
    cat["10.0.10.44"] = {
        "hostname": "studio-mac", "version": "0.8.0", "os_name": "macOS 15.1", "uptime": 30 * DAY,
        "auth": "open", "down": True, "last_answer": 3 * 3600 + 60,
        "drives": [nvme_drive("disk0", "APPLE SSD AP1024Z", "0ba01c2d4e5f6a7b", "/dev/disk0", temp=33,
                              poh=8800, cycles=1500, written_tb=21.06, read_tb=30.2, last=3 * 3600 + 60)],
        "pools": None,
    }
    nas_tank = pool("tank", "raidz1", ["sdc", "sdd", "sdf"], scrub_days=6)
    if flags["POOL"] == "degraded":
        nas_tank["state"] = "DEGRADED"
        nas_tank["devices"][2].update(state="FAULTED", read=12, cksum=3)
    refresh_pool(nas_tank)
    cat["10.0.10.20"] = {
        "hostname": "nas-01", "version": "0.8.1", "os_name": "Debian 12", "uptime": 19 * DAY,
        "auth": "open",
        "drives": [
            ata_drive("sda", "WDC WD80EFZZ 8TB", "VK0ABCDE", "/dev/sda", temp=44, poh=41230, cycles=88,
                      counters={5: 24, 197: 3, 198: 2, 187: 1, 188: 120}, written_tb=96.30, read_tb=310.4,
                      filler=8),
            ata_drive("sdb", "WDC WD80EFZZ 8TB", "VK0FGHIJ", "/dev/sdb", temp=41, poh=41230, cycles=88,
                      counters={5: 0, 197: 0, 198: 0, 196: 1}, written_tb=95.12, filler=8),
            ata_drive("sdc", "Seagate IronWolf 8TB", "ZA1B2C3D", "/dev/sdc", temp=38, poh=22140, cycles=41,
                      counters={5: 8, 197: 0, 198: 0, 187: 0}, written_tb=48.70),
            ata_drive("sdd", "Seagate IronWolf 8TB", "ZA1B2C3E", "/dev/sdd", temp=37, poh=22140, cycles=41,
                      counters={5: 2, 197: 0, 198: 0, 187: 0}, written_tb=47.95),
            unsupported_drive("sde", "JMicron USB to SATA bridge", "DD56419883961", "/dev/sde"),
            nvme_drive("nvme0", "Samsung SSD 990 EVO 1TB", "S7M3NS0X402118P", "/dev/nvme0n1", temp=45,
                       poh=3100, cycles=70, written_tb=14.62, read_tb=20.4),
        ],
        "pools": [nas_tank],
        "filesystems": [
            fs("fs-1d2e3f40", "/mnt/disk1", "/dev/sda1", 82, 8.0, "xfs"),
            fs("fs-2a3b4c50", "/mnt/disk2", "/dev/sdb1", 91, 8.0, "xfs"),
            fs("fs-3c4d5e60", "/mnt/disk3", "/dev/sdb2", 97, 0.5, "xfs"),
            fs("fs-4e5f6a70", "/mnt/usb", "/dev/sde1", 0, 2.0, "exfat", status="unavailable"),
            fs("fs-5f6a7b80", "/mnt/cache", "/dev/nvme0n1p1", 23, 1.0, "btrfs"),
            fs("fs-6a7b8c90", "/tank", "tank", 34, 15.8, "zfs"),
            fs("fs-7b8c9da0", "/tank/media", "tank/media", 58, 15.8, "zfs"),
            fs("fs-8c9daeb0", "/tank/backup", "tank/backup", 12, 15.8, "zfs"),
            fs("fs-9daebfc0", "/mnt/user", "/dev/md1", 64, 24.0, "xfs"),
        ],
    }
    # Check (Add by hand) answers for these two.
    cat["nas.lan"] = {
        "hostname": "nas", "version": "0.8.1", "os_name": None, "uptime": 9 * DAY, "auth": "open",
        "drives": [ata_drive("sda", "Seagate IronWolf Pro 12TB", "ZL2ABC01", "/dev/sda", temp=36, poh=9000,
                             cycles=20, counters={5: 0, 197: 0, 198: 0}, written_tb=55.2),
                   ata_drive("sdb", "Seagate IronWolf Pro 12TB", "ZL2ABC02", "/dev/sdb", temp=37, poh=9000,
                             cycles=20, counters={5: 0, 197: 0, 198: 0}, written_tb=54.9)],
        "pools": [refresh_pool(pool("data", "mirror", ["ZL2ABC01", "ZL2ABC02"]))],
    }
    cat["10.0.20.14"] = {
        "hostname": "media-box", "version": "0.8.1", "os_name": None, "uptime": 2 * DAY, "auth": "open",
        "drives": [nvme_drive("nvme0", "Samsung SSD 970 EVO Plus 1TB", "S4EWNX0R123456", "/dev/nvme0n1",
                              temp=40, poh=11000, cycles=500, written_tb=31.4)],
        "pools": None,
    }
    return cat


BOARD_HOSTS = [  # address, password stored, hidden days
    ("10.0.10.20", None, None),   # nas-01: six drives and a pool, partly in Home Assistant
    ("10.0.10.44", None, None),   # studio-mac: not answering, last answer 3 h ago
    ("10.0.10.52", None, None),   # backup-pi: wants a password (fieldpass)
]
ONE_HOST = [("10.0.10.21", "fieldpass", None)]

# Scan answers on 10.0.10.0/24 port 9099: address, seconds after the start.
SCAN_ROWS = [("10.0.10.61", 0.8), ("10.0.10.52", 1.6), ("10.0.10.21", 2.2)]
SCAN_SECONDS = 3.0


# ── Replicas ─────────────────────────────────────────────────────────────────

SHIPPED = [
    {"id": "shipped.sata-hdd-healthy", "name": "SATA HDD, healthy", "kind": "ata",
     "description": "A healthy Seagate Barracuda 2TB.", "order": 1},
    {"id": "shipped.sata-hdd-reallocated", "name": "SATA HDD, reallocated sectors growing", "kind": "ata",
     "description": "A Seagate Barracuda 2TB with reallocated sectors growing.", "order": 2},
    {"id": "shipped.sata-hdd-devstat-uncorrectables",
     "name": "SATA HDD with Device Statistics, uncorrectables on page 4", "kind": "ata_devstat",
     "description": "A WDC Ultrastar 14TB that reports uncorrectables only in Device Statistics.", "order": 3},
    {"id": "shipped.nvme-wear", "name": "NVMe, wear", "kind": "nvme",
     "description": "A Samsung 980 PRO 1TB running low on spare blocks.", "order": 4},
    {"id": "shipped.zfs-pool-degraded", "name": "ZFS pool, degraded", "kind": "zfs_pool",
     "description": "A two-disk mirror named tank with one disk faulted.", "order": 5},
    {"id": "shipped.smart-failed", "name": "SMART failed", "kind": "ata",
     "description": "A Seagate Barracuda 2TB whose SMART health has failed.", "order": 6},
]

SCENARIOS = {
    "ata": [("healthy", "Healthy SATA HDD"), ("reallocated_growing", "Reallocated sectors growing"),
            ("pending_sectors", "Pending sectors"), ("command_timeouts", "Command timeouts"),
            ("smart_failed", "SMART failed")],
    "ata_devstat": [("healthy", "Healthy, Device Statistics present"),
                    ("devstat_uncorrectables", "Uncorrectables in Device Statistics")],
    "nvme": [("healthy", "Healthy NVMe"), ("nvme_wear", "NVMe wear"), ("media_errors", "Media errors"),
             ("critical_warning", "Critical warning")],
    "zfs_pool": [("healthy", "Healthy pool"), ("degraded", "ZFS pool degraded"),
                 ("faulted", "Pool faulted"), ("vanished", "Pool vanished")],
    "scsi": [], "unsupported": [],
}

FILE_SEED = {  # shipped file id: (scenario, device builder)
    "shipped.sata-hdd-healthy": "healthy",
    "shipped.sata-hdd-reallocated": "reallocated_growing",
    "shipped.sata-hdd-devstat-uncorrectables": "devstat_uncorrectables",
    "shipped.nvme-wear": "nvme_wear",
    "shipped.zfs-pool-degraded": "degraded",
    "shipped.smart-failed": "smart_failed",
}


def shipped_body(file_id: str) -> dict:
    """The drive or pool a shipped file starts as, before its scenario."""
    if file_id == "shipped.sata-hdd-healthy":
        return ata_drive("sdz", "Seagate Barracuda 2TB", "MOCK-SATA-0002", "/dev/sdz", temp=34, poh=1020,
                         cycles=40, counters={5: 0, 197: 0, 188: 0}, written_tb=3.10, filler=9, last=2)
    if file_id == "shipped.sata-hdd-reallocated":
        return ata_drive("sdw", "Seagate Barracuda 2TB", "MOCK-SATA-0003", "/dev/sdw", temp=35, poh=8760,
                         cycles=142, written_tb=12.30, filler=9, last=2,
                         counters={5: 0, 197: 0, 198: 0, 187: 0, 188: 0, 196: 0})
    if file_id == "shipped.smart-failed":
        return ata_drive("sdx", "Seagate Barracuda 2TB", "MOCK-SATA-0001", "/dev/sdx", temp=36, poh=18240,
                         cycles=212, written_tb=61.20, filler=13, last=2,
                         counters={5: 0, 197: 0, 198: 0, 187: 0, 188: 0})
    if file_id == "shipped.sata-hdd-devstat-uncorrectables":
        return ata_drive("sdy", "WDC Ultrastar 14TB WUH721414ALE6L4", "MOCK-DS-9JG0X1", "/dev/sdy", temp=34,
                         poh=27694, cycles=10, counters={5: 0, 197: 0, 198: 0}, written_tb=88.40,
                         written_from="devstat", devstat_unc_count=0, last=2, filler=6)
    if file_id == "shipped.nvme-wear":
        return nvme_drive("nvme9", "Samsung SSD 980 PRO 1TB", "MOCK-NVME-0001", "/dev/nvme9n1", temp=41,
                          poh=2210, cycles=11, written_tb=4.67, last=2)
    if file_id == "shipped.zfs-pool-degraded":
        return pool("tank", "mirror", ["MOCK0001", "MOCK0002"], scrub_days=2)
    raise KeyError(file_id)


def apply_scenario(rec: dict, scenario: str) -> None:
    body = rec["body"]
    kind = rec["kind"]
    if kind == "zfs_pool":
        rec["vanished"] = scenario == "vanished"
        if scenario != "vanished":
            body["state"] = "ONLINE"
            for d in body["devices"]:
                d.update(state="ONLINE", read=0, write=0, cksum=0)
        if scenario == "degraded":
            body["state"] = "DEGRADED"
            body["devices"][1].update(state="FAULTED", read=12)
        if scenario == "faulted":
            body["state"] = "FAULTED"
            for d in body["devices"]:
                d["state"] = "UNAVAIL"
        refresh_pool(body)
    elif kind == "nvme":
        log = body["smart_data"]["nvme_smart_health_information_log"]
        log.update(critical_warning=0, media_errors=0, available_spare=100, percentage_used=2)
        if scenario == "nvme_wear":
            log.update(available_spare=15, percentage_used=3)
        elif scenario == "media_errors":
            log["media_errors"] = 2
        elif scenario == "critical_warning":
            log["critical_warning"] = 4
    elif kind == "ata_devstat":
        set_devstat(body, 18 if scenario == "devstat_uncorrectables" else 0)
    elif kind == "ata":
        reset = {n: 0 for n in ATA_COUNTERS.values()}
        patch_smart(body, dict(reset, smart_passed=True))
        patch_smart(body, {
            "reallocated_growing": {"Reallocated_Sector_Ct": 8, "Reallocated_Event_Count": 2},
            "pending_sectors": {"Current_Pending_Sector": 3},
            "command_timeouts": {"Command_Timeout": 120},
            "smart_failed": {"Reallocated_Sector_Ct": 24, "Current_Pending_Sector": 3,
                             "Reported_Uncorrect": 1, "smart_passed": False},
        }.get(scenario, {}))
    rec["scenario"] = scenario
    if "last_updated" in body:
        body["last_updated"] = iso(0)


def patch_smart(drive: dict, updates: dict) -> None:
    updates = dict(updates)
    sd = drive["smart_data"]
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
    drive["last_updated"] = iso(0)


def set_devstat(drive: dict, value: int) -> None:
    for page in (drive.get("device_statistics") or {}).get("pages") or []:
        for e in page["table"]:
            if "Reported Uncorrectable" in e["name"]:
                e["value"] = int(value)
    drive["last_updated"] = iso(0)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "replica"


def file_kind(drive: dict) -> str | None:
    sd = drive.get("smart_data") or {}
    if sd.get("nvme_smart_health_information_log"):
        return "nvme"
    table = (sd.get("ata_smart_attributes") or {}).get("table")
    if table and (drive.get("device_statistics") or {}).get("status") == "present":
        return "ata_devstat"
    if table:
        return "ata"
    if sd.get("scsi_grown_defect_list") is not None:
        return "scsi"
    return None


def saved_doc(drive: dict) -> dict:
    """A real drive as a replica file, its serial replaced, as the replica
    agent saves one."""
    serial = "REPLICA-%08X" % random.randrange(16 ** 8)
    text = json.dumps({"smart": drive["smart_data"], "devstat": drive["device_statistics"],
                       "derived": drive["derived"]}).replace(drive["serial"], serial)
    parts_doc = json.loads(text)
    day = datetime.now().strftime("%Y-%m-%d")
    return {"format": "smart-sniffer-replica", "version": 1, "name": f"{drive['model']}, saved {day}",
            "kind": file_kind(drive),
            "description": f"Saved from a real drive on {day}. Serial and WWN replaced.",
            "origin": {"type": "saved", "saved_at": iso(0)},
            "meta": {"model": drive["model"], "serial": serial, "protocol": drive["protocol"],
                     "device_path": drive["device_path"]},
            "smart": parts_doc["smart"], "devstat": parts_doc["devstat"], "derived": parts_doc["derived"],
            "scenario": {"current": None}}


# ── The world: everything the page can change ────────────────────────────────

class World:
    def __init__(self, flags: dict):
        self.key = tuple(flags[k] for k in WORLD_KEYS)
        self.started = time.time()
        self.catalog = field_catalog(flags)
        self.hosts: list[dict] = []
        self.options_writes = 0
        hosts = {"board": BOARD_HOSTS, "one": ONE_HOST, "none": []}.get(flags["HOSTS"], [])
        for address, password, hidden_days in hosts:
            self.hosts.append({"id": f"{address}:9099", "address": address, "port": 9099,
                               "password": password, "hidden": hidden_days is not None,
                               "hidden_since": iso(hidden_days * DAY) if hidden_days else None,
                               "added_at": iso(30 * DAY), "removed": []})
        self.card_order: list[str] = []
        self.hidden_cards: list[str] = []
        # Names given in the page, and each Home Assistant device's own name
        # (name_by_user) by device id.
        self.card_names: dict[str, str] = {}
        self.host_names: dict[str, str] = {}
        self.ha_names: dict[str, str] = {}
        self.self_cache: list | None = None
        self.self_last_answer = time.time() - 240
        self.scans: dict[str, dict] = {}
        self.user_files: dict[str, dict] = {}  # file id -> {"entry", "doc"}
        self.replicas: list[dict] = []
        self.counter = 0
        if flags["REPLICAS"] in ("board", "carried"):
            for fid in ("shipped.smart-failed", "shipped.nvme-wear", "shipped.zfs-pool-degraded"):
                self.add_replica(fid)
            if flags["REPLICAS"] == "carried":
                self.replicas[1]["scenario"] = None
                self.add_replica("shipped.sata-hdd-devstat-uncorrectables")
        # Thresholds by card id: the integration's (for cards in Home
        # Assistant) and the app's own (for the rest).
        self.int_thresholds: dict[str, dict] = {}
        self.app_thresholds: dict[str, dict] = {}
        # Home Assistant: target -> {"entry_id", "devices": {serial or pool:<name>: device id}}
        self.ha: dict[str, dict] = {}
        # Cards whose Home Assistant device carries a drive id the agent no
        # longer reports (an older scheme), so it can be deleted there.
        self.ha_gone: set[str] = set()
        if flags["HA"] == "installed":
            if flags["HA_SELF"] == "1":
                self.ha["self"] = self._entry("01J9AGENT", self_drives(flags), [])
            if flags["REPLICAS"] in ("board", "carried"):
                self.ha["replicas"] = {"entry_id": "01J9REPLICAS",
                                       "devices": {"MOCK-SATA-0001": "d58c3a1f7e0b4c92a6f4b8e2d1c90a73"}}
            if any(h["address"] == "10.0.10.20" for h in self.hosts):
                # nas-01 is in Home Assistant, but its two newest drives are not yet.
                c = self.catalog["10.0.10.20"]
                self.ha["10.0.10.20:9099"] = self._entry("01J9ZK4T6QH2W8XG5N3B7RVD0C", c["drives"][:4],
                                                         c["pools"] or [])
                # Two of them carry thresholds set in Home Assistant: sdc at 8
                # reallocated sectors over its 5, sdd at 2 within its 4.
                self.int_thresholds["10.0.10.20:9099|ZA1B2C3D"] = {"Reallocated Sector Count": 5}
                # sdb's device was registered under an id the agent no longer reports.
                self.ha_gone.add("10.0.10.20:9099|VK0FGHIJ")
                self.int_thresholds["10.0.10.20:9099|ZA1B2C3E"] = {"Reallocated Sector Count": 4}

    @staticmethod
    def _entry(entry_id, drives, pools):
        devices = {}
        for d in drives:
            devices[d["serial"]] = (re.sub(r"[^0-9a-f]", "", d["serial"].lower()) + "0" * 32)[:32]
        for p in pools:
            devices[f"pool:{p['name']}"] = ("f0b6e2d9" + slugify(p["name"]).replace("-", "") + "0" * 32)[:32]
        return {"entry_id": entry_id, "devices": devices}

    # replicas
    def add_replica(self, file_id: str) -> str:
        if file_id in self.user_files:
            doc = copy.deepcopy(self.user_files[file_id]["doc"])
            kind = doc["kind"]
            if kind == "zfs_pool":
                body = refresh_pool(copy.deepcopy(doc["pool"]))
            else:
                meta = doc["meta"]
                body = {"id": "", "model": meta["model"], "serial": meta["serial"],
                        "protocol": meta["protocol"], "device_path": meta["device_path"],
                        "readable": True, "in_standby": False, "last_updated": iso(0),
                        "smart_data": doc["smart"], "device_statistics": doc.get("devstat") or {"status": "absent"},
                        "derived": doc.get("derived") or {"host_writes_omitted": "no_source",
                                                          "host_reads_omitted": "no_source"}}
            scenario = (doc.get("scenario") or {}).get("current")
            entry = self.user_files[file_id]["entry"]
            name, desc = entry["name"], entry["description"]
        else:
            meta = next(f for f in SHIPPED if f["id"] == file_id)
            kind, name, desc = meta["kind"], meta["name"], meta["description"]
            body = shipped_body(file_id)
            scenario = FILE_SEED[file_id]
        self.counter += 1
        rec = {"file": file_id, "kind": kind, "name": name, "description": desc,
               "body": body, "scenario": None, "vanished": False}
        if kind == "zfs_pool":
            base, n = body["name"], 1
            while any(r["kind"] == "zfs_pool" and r["body"]["name"] == body["name"] for r in self.replicas):
                n += 1
                body["name"] = f"{base}{n}"
            rec["card_id"] = f"pool.{body['name']}"
        else:
            serial = body["serial"]
            while any(r["kind"] != "zfs_pool" and r["body"]["serial"] == body["serial"] for r in self.replicas):
                body["serial"] = f"{serial}-{random.randrange(16 ** 4):04X}"
                body["smart_data"]["serial_number"] = body["serial"]
            body["id"] = slugify(body["serial"])
            rec["card_id"] = body["id"]
        if scenario and kind != "unsupported":
            apply_scenario(rec, scenario)
        rec["scenario"] = scenario
        self.replicas.append(rec)
        return rec["card_id"]

    def replica(self, card_id: str) -> dict | None:
        return next((r for r in self.replicas if r["card_id"] == card_id), None)

    def export(self, rec: dict) -> dict:
        doc = {"format": "smart-sniffer-replica", "version": 1, "name": rec["name"], "kind": rec["kind"],
               "description": rec["description"],
               "origin": {"type": "shipped"} if rec["file"].startswith("shipped.") else {"type": "uploaded"}}
        body = rec["body"]
        if rec["kind"] == "zfs_pool":
            doc["pool"] = {k: copy.deepcopy(v) for k, v in body.items() if k != "problem_vdevs"}
        else:
            doc["meta"] = {k: body[k] for k in ("model", "serial", "protocol", "device_path")}
            doc["smart"] = copy.deepcopy(body["smart_data"])
            doc["devstat"] = copy.deepcopy(body["device_statistics"])
            doc["derived"] = copy.deepcopy(body["derived"])
        doc["scenario"] = {"current": rec["scenario"]}
        return doc

    def file_entry(self, doc: dict, source: str) -> dict:
        base = slugify(doc["name"])
        stem, n = base, 1
        while f"user.{stem}" in self.user_files:
            n += 1
            stem = f"{base}-{n}"
        entry = {"id": f"user.{stem}", "name": doc["name"], "kind": doc["kind"],
                 "description": doc.get("description", ""), "source": source, "order": None, "valid": True}
        self.user_files[entry["id"]] = {"entry": entry, "doc": doc}
        return entry

    def host(self, host_id: str) -> dict | None:
        return next((h for h in self.hosts if h["id"] == host_id), None)

    def in_ha(self, flags, card_id: str) -> bool:
        if flags["HA"] != "installed":
            return False
        host_id, _, key = card_id.partition("|")
        entry = self.ha.get(host_id)
        return bool(entry) and (key in entry["devices"] or flags["HA_LOOKUP"] == "entries_only")

    def thresholds(self, flags, card_id: str) -> tuple[str, dict]:
        """(source, overrides) in force for a drive card."""
        if self.in_ha(flags, card_id):
            return "integration", dict(self.int_thresholds.get(card_id) or {})
        return "app", dict(self.app_thresholds.get(card_id) or {})

    def set_thresholds(self, flags, card_id: str, overrides: dict) -> str:
        source = "integration" if self.in_ha(flags, card_id) else "app"
        store = self.int_thresholds if source == "integration" else self.app_thresholds
        if overrides:
            store[card_id] = dict(overrides)
        else:
            store.pop(card_id, None)
        if source == "app":
            self.options_writes += 1
        return source


# One world per set of shaping switches, so two pages open on different
# switches (as the page checks do) do not rebuild each other's world.
WORLDS: dict[tuple, World] = {}


def world_for(flags, fresh: bool = False) -> World:
    key = tuple(flags[k] for k in WORLD_KEYS)
    if fresh or key not in WORLDS:
        WORLDS[key] = World(flags)
    return WORLDS[key]


# ── Responses ────────────────────────────────────────────────────────────────

def drive_item(drive: dict, flags, lab=None, stale=False, world=None, host_id=None) -> dict:
    source, overrides = "app", {}
    if world is not None and host_id:
        key = str(drive.get("serial") or "").strip() or f"id:{drive['id']}"
        source, overrides = world.thresholds(flags, f"{host_id}|{key}")
    verdict = None if flags["RULES"] == "missing" else evaluate(drive, overrides)
    item = {"type": "drive", "id": drive["id"], "drive": drive, "verdict": verdict,
            "view": drive_view(drive, verdict), "lab": lab, "thresholds_source": source}
    if stale:
        item["stale"] = True
    return item


def pool_item(p: dict, flags, lab=None, card_id=None, stale=False) -> dict:
    """A pool as the proxy answers it. A replica's lab block carries its
    layout and every disk; the board's field agents get the same here, which
    a real agent does not report yet (spec Q6), so the board's pictures hold."""
    verdict = None if flags["RULES"] == "missing" else pool_verdict(p)
    shape = lab or {"vdev": p.get("vdev"), "devices": p.get("devices")}
    if HAVE_POOL_VIEW:
        view = PROXY.pool_view(p, verdict, shape)
    else:
        devices = shape.get("devices")
        vanished = bool(shape.get("vanished"))
        state = (verdict or {}).get("state")
        view = {"header": "unknown" if vanished or state is None else ("attention" if state == "YES" else "ok"),
                "layout": pool_layout(p), "state": p.get("state"),
                "disks": copy.deepcopy(devices) if devices else None, "vanished": vanished}
    item = {"type": "pool", "id": card_id or f"pool.{p['name']}", "pool": p, "verdict": verdict,
            "view": view, "lab": lab}
    if stale:
        item["stale"] = True
    return item


def summarize(items: list, status: str) -> dict:
    if PROXY is not None and callable(getattr(PROXY, "summarize", None)):
        return PROXY.summarize(items, status == "ok")
    s = {"header": "unknown", "attention": 0, "maybe": 0, "unsupported": 0,
         "drives": sum(1 for i in items if i["type"] == "drive"),
         "pools": sum(1 for i in items if i["type"] == "pool")}
    judged = False
    for i in items:
        state = (i.get("verdict") or {}).get("state")
        if state == "YES":
            s["attention"] += 1
        elif state == "MAYBE":
            s["maybe"] += 1
        elif state == "UNSUPPORTED":
            s["unsupported"] += 1
        if state in ("YES", "MAYBE", "NO"):
            judged = True
    if status == "unreachable":
        s["header"] = "unknown"
    elif s["attention"]:
        s["header"] = "attention"
    elif s["maybe"]:
        s["header"] = "maybe"
    elif judged:
        s["header"] = "ok"
    return s


def card_key(item: dict) -> str:
    if item["type"] == "pool":
        return f"pool:{item['pool']['name']}"
    serial = str(item["drive"].get("serial") or "").strip()
    return serial or f"id:{item['id']}"


def with_cards(host_id: str, items: list, filesystems: list, removed=()) -> tuple[list, list]:
    """Card ids, each filesystem on its drive or pool, the rest as FILESYSTEM
    cards, and the removed cards left out: as the proxy answers."""
    for item in items:
        item["card_id"] = f"{host_id}|{card_key(item)}"
    filesystems = copy.deepcopy(filesystems)
    if PROXY is not None and callable(getattr(PROXY, "attach_filesystems", None)):
        fs_cards = PROXY.attach_filesystems(host_id, items, filesystems)
    else:
        for item in items:
            item["filesystems"] = []
        fs_cards = [{"type": "filesystem", "id": f["id"], "card_id": f"{host_id}|fs:{f['id']}",
                     "filesystem": f} for f in filesystems]
    gone = {f"{host_id}|{r}" for r in removed}
    return ([i for i in items if i["card_id"] not in gone],
            [c for c in fs_cards if c["card_id"] not in gone])


def drives_response(host_id, kind, status, agent, items, read_at, last_answer, flags,
                    filesystems=(), removed=()):
    items, fs_cards = with_cards(host_id, items, list(filesystems), removed)
    if status != "ok":
        for c in fs_cards:
            c["stale"] = True
    return {"id": host_id, "kind": kind, "status": status, "read_at": read_at, "last_answer": last_answer,
            "stale": status != "ok", "agent": agent, "summary": summarize(items, status),
            "items": items, "filesystem_cards": fs_cards, "pools_status": "ok",
            "filesystems_status": "ok" if filesystems else "none",
            "rules_version": None if flags["RULES"] == "missing" else RULES_VERSION}


def agent_host(flags) -> str:
    host = flags["HOSTNAME"].strip()
    slug = flags["SLUG"].strip()
    if slug.lower() in ("", "none", "null"):
        slug = "smart_sniffer_agent"
    return host or slug.replace("_", "-")


def self_response(flags, world: World) -> dict:
    drives = self_drives(flags)
    agent = {"hostname": agent_host(flags), "version": AGENT_VERSION, "os": "linux", "os_name": None,
             "uptime_seconds": int(time.time() - world.started) + 3600, "drives": len(drives), "pools": 0}
    if flags["AGENT"] == "down":
        cached = world.self_cache if world.self_cache is not None else [
            drive_item(d, flags, world=world, host_id="self") for d in drives]
        for i in cached:
            i["stale"] = True
        last = datetime.fromtimestamp(world.self_last_answer, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return drives_response("self", "self", "unreachable", None, cached, None, last, flags,
                               self_filesystems(flags))
    items = [drive_item(d, flags, world=world, host_id="self") for d in drives]
    world.self_cache = copy.deepcopy(items)
    world.self_last_answer = time.time()
    return drives_response("self", "self", "ok", agent, items, iso(0), iso(0), flags, self_filesystems(flags))


def field_response(flags, world: World, host: dict) -> dict:
    c = world.catalog.get(host["address"])
    if c is None:
        return drives_response(host["id"], "field", "unreachable", None, [], None, None, flags)
    agent = {"hostname": c["hostname"], "version": c["version"], "os": "linux", "os_name": c["os_name"],
             "uptime_seconds": c["uptime"], "drives": len(c["drives"]),
             "pools": len(c["pools"] or [])}
    items = [drive_item(d, flags, world=world, host_id=host["id"]) for d in c["drives"]] + [
        pool_item(p, flags) for p in c["pools"] or []]
    found = c.get("filesystems") or []
    removed = host.get("removed") or []
    if c.get("down"):
        for i in items:
            i["stale"] = True
        return drives_response(host["id"], "field", "unreachable", None, items, None,
                               iso(c["last_answer"]), flags, found, removed) | {
            "agent": dict(agent, uptime_seconds=None)}
    if c["auth"] == "password":
        if not host.get("password"):
            return drives_response(host["id"], "field", "password_needed", agent, [], None, iso(0), flags)
        if host["password"] != c["password"]:
            return drives_response(host["id"], "field", "password_refused", agent, [], None, iso(0), flags)
    return drives_response(host["id"], "field", "ok", agent, items, iso(0), iso(0), flags, found, removed)


def replicas_response(flags, world: World) -> dict:
    items = []
    for rec in world.replicas:
        lab = {"replica": rec["file"], "name": rec["name"], "kind": rec["kind"],
               "description": rec["description"],
               "scenario": {"current": rec["scenario"],
                            "options": [{"id": i, "label": label} for i, label in SCENARIOS[rec["kind"]]]}}
        if rec["kind"] == "zfs_pool":
            body = rec["body"]
            lab.update(vanished=rec["vanished"], vdev=copy.deepcopy(body.get("vdev")),
                       devices=copy.deepcopy(body.get("devices")))
            items.append(pool_item(copy.deepcopy(body), flags, lab, rec["card_id"]))
        else:
            body = copy.deepcopy(rec["body"])
            # The replica agent stamps every read, as the mock does.
            body["last_updated"] = iso(2)
            items.append(drive_item(body, flags, lab, world=world, host_id="replicas"))
    agent = {"hostname": agent_host(flags), "version": AGENT_VERSION + "-mock", "os": "linux",
             "os_name": None, "uptime_seconds": int(time.time() - world.started), "drives": 0, "pools": 0}
    return drives_response("replicas", "replicas", "ok", agent, items, iso(0), iso(0), flags)


def public_host(h: dict) -> dict:
    return {"id": h["id"], "address": h["address"], "port": h["port"], "has_password": bool(h.get("password")),
            "hidden": h["hidden"], "hidden_since": h["hidden_since"], "added_at": h["added_at"]}


def network_info(flags) -> dict | None:
    if flags["NETWORK"] == "none":
        return None
    block = "10.0.0.0/16" if flags["NETWORK"] == "wide" else "10.0.10.0/24"
    return {"block": block, "address": "10.0.10.5", "prefix": int(block.split("/")[1]),
            "source": "supervisor"}


def scan_row(world: World, address: str, port: int) -> dict:
    c = world.catalog[address]
    rid = f"{address}:{port}"
    return {"id": rid, "address": address, "port": port, "hostname": c["hostname"], "version": c["version"],
            "drives": len(c["drives"]), "pools": len(c["pools"] or []), "auth": c["auth"],
            "added": world.host(rid) is not None}


def scan_state(world: World, scan: dict) -> dict:
    elapsed = time.time() - scan["started"]
    duration = scan["duration"]
    out = {k: scan[k] for k in ("scan_id", "block", "port", "total", "started_at")}
    if scan["cancelled"]:
        elapsed = scan["cancelled"] - scan["started"]
    done = elapsed >= duration
    out["state"] = "cancelled" if scan["cancelled"] else "done" if done else "running"
    out["probed"] = scan["total"] if done else int(scan["total"] * elapsed / duration)
    out["found"] = [scan_row(world, a, scan["port"]) for a, at in scan["rows"] if at <= elapsed]
    out["finished_at"] = iso(max(0.0, elapsed - duration)) if done else None
    return out


# ── Handler ──────────────────────────────────────────────────────────────────

def sep_ok(card_id: str) -> bool:
    return "|" in card_id and bool(card_id.partition("|")[2])


def flags_for(handler) -> dict[str, str]:
    flags = {k: os.environ.get(k, v) for k, v in DEFAULTS.items()}
    sources = ((urlparse(handler.headers.get("Referer", "")).query, False),
               (urlparse(handler.path).query, True))
    for source, own in sources:
        for k, v in parse_qs(source).items():
            # A route's own lowercase parameter (DELETE /cards/<id>?ha=) is no switch.
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
        if n > 1024 * 1024:
            return {"__too_large__": True}
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
        if head not in ("api", "mock", "app", "evaluate", "ha", "fleet", "replicas", "dev", "cards", "hosts"):
            if method != "GET":
                return self._err(405, "method_not_allowed")
            return self._static(route)
        flags = flags_for(self)
        time.sleep(float(os.environ.get("LATENCY", "0.02")))
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
        if head == "dev" and parts[1:] == ["ha-name"] and method == "POST":
            # A rename made in Home Assistant: {"device": id, "name": text or null}.
            body = self._body() or {}
            if body.get("name"):
                world.ha_names[str(body.get("device"))] = str(body["name"])
            else:
                world.ha_names.pop(str(body.get("device")), None)
            return self._send(200, {"ok": True})
        if head == "app" and parts[1:] == ["info"]:
            return self._app_info(flags, world)
        if head == "api":
            return self._agent_api(parts[1:], flags)
        if head == "evaluate" and method == "POST":
            if flags["RULES"] == "missing":
                return self._err(503, "rules_unavailable")
            body = self._body() or {}
            if "pool" in body:
                return self._send(200, pool_verdict(body["pool"]))
            v = evaluate(body)
            return self._send(200, dict(v, view=drive_view(body, v)))
        if head == "fleet":
            return self._fleet(method, parts[1:], flags, world)
        if head == "ha":
            return self._ha(method, parts[1:], flags, world)
        if head == "replicas":
            return self._replicas(method, parts[1:], flags, world)
        if head == "mock":
            return self._mock(method, parts[1:], flags, world)
        if head == "cards":
            return self._cards(method, parts[1:], flags, world)
        if head == "hosts" and len(parts) == 3 and parts[2] == "name" and method == "PUT":
            name = (self._body() or {}).get("name")
            if not isinstance(name, str):
                return self._err(400, "bad_request")
            if world.host(parts[1]) is None:
                return self._err(404, "not_found")
            name = " ".join(name.split())[:100]
            if name:
                world.host_names[parts[1]] = name
            else:
                world.host_names.pop(parts[1], None)
            world.options_writes += 1
            return self._send(200, {"id": parts[1], "name": name})
        return self._err(404, "not_found")

    def _app_info(self, flags, world):
        installed = {"installed": True, "not_installed": False}.get(flags["HA"])
        version = flags["HA_VERSION"].strip()
        slug = flags["SLUG"].strip()
        return self._send(200, {
            "app_version": APP_VERSION,
            "rules_version": None if flags["RULES"] == "missing" else RULES_VERSION,
            "agent_port": AGENT_PORT, "mock_port": MOCK_PORT,
            "hostname": agent_host(flags),
            "slug": None if slug.lower() in ("", "none", "null") else slug,
            "mdns_name": "smartha-homeassistant" if flags["MDNS"] != "off" else None,
            "ha_version": None if flags["HA"] == "unreachable" or version.lower() in ("", "none") else version,
            "ha_api": flags["HA"] != "unreachable", "supervisor": True,
            "integration_installed": installed,
            "network": network_info(flags),
            "replicas": {"agent": "ok", "folder": flags["REPLICA_FOLDER"], "path": REPLICA_PATH,
                         "shipped": len(SHIPPED), "user": len(world.user_files)},
            "refresh_minutes": 15,
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

    def _fleet(self, method, parts, flags, world):
        if parts == ["scan"] and method == "POST":
            body = self._body() or {}
            text = str(body.get("block", "")).strip()
            try:
                port = int(body.get("port", 9099))
                if not 1 <= port <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                return self._err(400, "bad_port")
            try:
                net = ipaddress.IPv4Network(text if "/" in text else text + "/32", strict=False)
            except ValueError:
                return self._err(400, "bad_block")
            if net.prefixlen < 22:
                addr = (network_info(flags) or {}).get("address") or str(net.network_address)
                suggest = str(ipaddress.IPv4Network(addr + "/24", strict=False))
                return self._err(422, "too_wide", addresses=2 ** (32 - net.prefixlen) - 2, suggest=suggest)
            total = net.num_addresses - (2 if net.prefixlen <= 30 else 0)
            for s in world.scans.values():
                if not s["cancelled"] and time.time() - s["started"] < s["duration"]:
                    s["cancelled"] = time.time()
            scale = {"slow": 4.0}.get(flags["SCAN"], 1.0)
            rows = []
            if flags["SCAN"] != "none" and str(net) == "10.0.10.0/24" and port == 9099:
                rows = [(a, t * scale) for a, t in SCAN_ROWS]
            scan_id = "%06x" % random.randrange(16 ** 6)
            world.scans[scan_id] = {"scan_id": scan_id, "block": str(net), "port": port, "total": total,
                                    "started": time.time(), "started_at": iso(0),
                                    "duration": SCAN_SECONDS * scale, "rows": rows, "cancelled": 0}
            return self._send(202, {"scan_id": scan_id, "total": total})
        if len(parts) == 2 and parts[0] == "scan":
            scan = world.scans.get(parts[1])
            if scan is None:
                return self._err(404, "not_found")
            if method == "GET":
                return self._send(200, scan_state(world, scan))
            if method == "DELETE":
                if not scan["cancelled"]:
                    scan["cancelled"] = time.time()
                return self._send(200, {"state": "cancelled"})
        if parts == ["probe"] and method == "POST":
            body = self._body() or {}
            host = str(body.get("host", "")).strip().lower()
            try:
                port = int(body.get("port", 9099))
            except (TypeError, ValueError):
                return self._err(400, "bad_request")
            if not host:
                return self._err(400, "bad_request")
            # Every machine of the catalog answers on 9099; 10.0.10.98 answers
            # but is no agent; anything else is silent.
            if host in world.catalog and port == 9099:
                time.sleep(0.3)
                return self._send(200, dict(scan_row(world, host, port), resolved=host))
            if host == "10.0.10.98":
                time.sleep(0.2)
                return self._err(404, "not_an_agent")
            time.sleep(0.6)
            return self._err(504, "not_answering")
        if parts == ["hosts"]:
            if method == "GET":
                return self._send(200, {"hosts": [public_host(h) for h in world.hosts]})
            if method == "POST":
                body = self._body() or {}
                added, skipped = [], []
                for h in body.get("hosts") or []:
                    address = str(h.get("address", "")).strip().lower()
                    port = int(h.get("port") or 9099)
                    hid = f"{address}:{port}"
                    if world.host(hid):
                        skipped.append({"address": address, "port": port, "reason": "already_added"})
                        continue
                    rec = {"id": hid, "address": address, "port": port, "password": None, "hidden": False,
                           "hidden_since": None, "added_at": iso(0)}
                    world.hosts.append(rec)
                    added.append(public_host(rec))
                world.options_writes += 1
                return self._send(201, {"added": added, "skipped": skipped})
        if len(parts) >= 2 and parts[0] == "hosts":
            hid = parts[1]
            if len(parts) == 3 and parts[2] == "drives" and method == "GET":
                if hid == "self":
                    return self._send(200, self_response(flags, world))
                if hid == "replicas":
                    return self._send(200, replicas_response(flags, world))
                host = world.host(hid)
                if host is None:
                    return self._err(404, "not_found")
                return self._send(200, field_response(flags, world, host))
            host = world.host(hid)
            if host is None:
                return self._err(404, "not_found")
            if len(parts) == 2 and method == "PATCH":
                body = self._body() or {}
                if "password" in body:
                    c = world.catalog.get(host["address"])
                    pw = str(body["password"])
                    if pw:
                        if c is None or c.get("down"):
                            return self._err(504, "not_answering")
                        if c["auth"] == "password" and pw != c.get("password"):
                            return self._err(422, "password_refused")
                    host["password"] = pw or None
                if "hidden" in body:
                    host["hidden"] = bool(body["hidden"])
                    host["hidden_since"] = iso(0) if host["hidden"] else None
                world.options_writes += 1
                return self._send(200, {"host": public_host(host)})
            if len(parts) == 2 and method == "DELETE":
                world.hosts.remove(host)
                world.options_writes += 1
                return self._send(200, {"removed": hid})
        return self._err(404, "not_found")

    def _ha(self, method, parts, flags, world):
        reachable = flags["HA"] != "unreachable"
        installed = flags["HA"] == "installed"
        base = "/config/integrations/integration/smart_sniffer"
        if parts == ["devices"] and method == "GET":
            if not reachable:
                return self._send(200, {"installed": None, "reachable": False, "lookup": None,
                                        "integration_url": base, "targets": {}})
            targets = {}
            if installed:
                entries_only = flags["HA_LOOKUP"] == "entries_only"
                for t, e in world.ha.items():
                    devices = {} if entries_only else dict(e["devices"])
                    targets[t] = {"entry_id": e["entry_id"],
                                  "entry_url": f"{base}#config_entry={e['entry_id']}",
                                  "devices": devices,
                                  "names": {k: {"name": None, "name_by_user": world.ha_names.get(d)}
                                            for k, d in devices.items()}}
            return self._send(200, {"installed": installed, "reachable": True,
                                    "lookup": "entries_only" if flags["HA_LOOKUP"] == "entries_only" else "registry",
                                    "integration_url": base, "targets": targets,
                                    "ha_removal": self._ha_removals(flags, world) if installed else {}})
        if parts == ["add"] and method == "POST":
            target = str((self._body() or {}).get("target", ""))
            if target not in ("self", "replicas") and world.host(target) is None:
                return self._err(400, "bad_target")
            if not reachable:
                return self._send(502, {"result": "error", "message": "unreachable"})
            if not installed:
                return self._send(502, {"result": "error", "message": "integration_not_loaded"})
            if flags["ADD"] == "error":
                return self._send(502, {"result": "error", "message": "flow_failed"})
            if target == "self":
                drives, pools = self_drives(flags), []
            elif target == "replicas":
                drives = [r["body"] for r in world.replicas if r["kind"] != "zfs_pool"]
                pools = [r["body"] for r in world.replicas if r["kind"] == "zfs_pool"]
            else:
                host = world.host(target)
                c = world.catalog.get(host["address"]) or {}
                if c.get("auth") == "password" and host.get("password") != c.get("password"):
                    return self._send(502, {"result": "error", "message": "invalid_auth"})
                if c.get("down") or not c:
                    return self._send(502, {"result": "error", "message": "cannot_connect"})
                drives, pools = c["drives"], c["pools"] or []
            if target in world.ha:
                if target != "replicas":
                    return self._send(200, {"result": "already_added", "entry_id": world.ha[target]["entry_id"]})
                world.ha[target]["devices"].update(World._entry("", drives, pools)["devices"])
                return self._send(200, {"result": "reloaded", "entry_id": world.ha[target]["entry_id"]})
            entry_id = "01J9" + "%022X" % random.randrange(16 ** 22)
            world.ha[target] = World._entry(entry_id, drives, pools)
            copied = [cid for cid in list(world.app_thresholds) if cid.partition("|")[0] == target
                      and world.in_ha(flags, cid)]
            for cid in copied:
                world.int_thresholds[cid] = world.app_thresholds.pop(cid)
            body = {"result": "created", "entry_id": entry_id}
            if copied:
                body["thresholds_copied"] = copied
            return self._send(200, body)
        return self._err(404, "not_found")

    def _replicas(self, method, parts, flags, world):
        folder = flags["REPLICA_FOLDER"]
        if parts == [] and method == "GET":
            files = [dict(f, source="shipped", valid=True) for f in SHIPPED]
            files += [u["entry"] for u in sorted(world.user_files.values(), key=lambda u: u["entry"]["name"])]
            files.append({"id": "user.notes", "valid": False, "error": "not a replica file"})
            return self._send(200, {"agent": "ok", "folder": {"path": REPLICA_PATH, "status": folder},
                                    "files": files})
        if parts == [] and method == "POST":
            doc = self._body()
            if isinstance(doc, dict) and doc.get("__too_large__"):
                return self._err(413, "too_large")
            if folder != "ok":
                return self._err(503, "replica_folder_unavailable")
            if not isinstance(doc, dict) or doc.get("format") != "smart-sniffer-replica":
                return self._err(422, "invalid_replica", "This is not a replica file.")
            if doc.get("kind") not in SCENARIOS or not doc.get("name"):
                return self._err(422, "invalid_replica", "The replica file has no name or an unknown kind.")
            if doc["kind"] == "zfs_pool" and not isinstance(doc.get("pool"), dict):
                return self._err(422, "invalid_replica", "A pool replica needs a pool block.")
            if doc["kind"] != "zfs_pool" and not (isinstance(doc.get("meta"), dict) and isinstance(doc.get("smart"), dict)):
                return self._err(422, "invalid_replica", "A drive replica needs meta and smart blocks.")
            return self._send(201, world.file_entry(doc, "user"))
        if parts == ["add"] and method == "POST":
            body = self._body() or {}
            cards, failed = [], []
            for fid in body.get("files") or []:
                if fid in world.user_files or any(f["id"] == fid for f in SHIPPED):
                    cards.append({"file": fid, "card_id": world.add_replica(fid)})
                else:
                    failed.append({"file": fid, "error": "not_found"})
            return self._send(201, {"cards": cards, "failed": failed})
        if parts == ["save-as"] and method == "POST":
            body = self._body() or {}
            hid, drive_id = body.get("host"), body.get("drive_id")
            if hid == "self":
                drives = self_drives(flags) if flags["AGENT"] == "up" else None
                if drives is None:
                    return self._err(504, "not_answering")
            else:
                host = world.host(str(hid))
                c = world.catalog.get(host["address"]) if host else None
                if c is None:
                    return self._err(404, "not_found")
                if c.get("down"):
                    return self._err(504, "not_answering")
                drives = c["drives"]
            drive = next((d for d in drives if d["id"] == drive_id), None)
            if drive is None:
                return self._err(404, "not_found")
            if file_kind(drive) is None:
                return self._err(409, "unsupported_drive")
            if folder != "ok":
                return self._err(503, "replica_folder_unavailable")
            entry = world.file_entry(saved_doc(drive), "user")
            return self._send(201, {"file": entry, "card_id": world.add_replica(entry["id"])})
        if len(parts) == 2 and parts[1] == "file" and method == "GET":
            rec = world.replica(parts[0])
            if rec is None:
                return self._err(404, "not_found")
            doc = world.export(rec)
            name = slugify(rec["body"]["name"] if rec["kind"] == "zfs_pool" else rec["name"])
            return self._send(200, body=json.dumps(doc, indent=2).encode(), headers={
                "Content-Disposition": f'attachment; filename="{name}.json"'})
        if len(parts) == 1 and method == "DELETE":
            rec = world.replica(parts[0])
            if rec is None:
                return self._err(404, "not_found")
            world.replicas.remove(rec)
            return self._send(200, {"removed": parts[0]})
        return self._err(404, "not_found")

    def _ha_removals(self, flags, world) -> dict:
        """Card id -> what the trash can do in Home Assistant, as the proxy
        works it out: "entry" on a machine's last card, "delete" for a drive
        whose device the agent no longer reports (world.ha_gone), "disable"
        for the rest of its cards in Home Assistant, "replica" for every
        replica with a device (the replicas' entry stays). HAOS cards have
        none."""
        out = {}
        entries_only = flags["HA_LOOKUP"] == "entries_only"
        responses = self._responses(flags, world)
        for t, e in world.ha.items():
            if t == "self" or t not in responses:
                continue
            response = responses[t]
            cards = self._card_list(response)
            counted = [c for c in cards if t != "replicas" or c["type"] in ("drive", "pool")]
            for c in cards:
                if c["type"] not in ("drive", "pool"):
                    continue
                cid = c["card_id"]
                device = cid.partition("|")[2] in e["devices"]
                if not device and not entries_only:
                    continue
                if t == "replicas":
                    if device:
                        out[cid] = "replica"
                elif len(counted) == 1 and c is counted[0]:
                    out[cid] = "entry"
                elif device:
                    gone = cid in world.ha_gone and response.get("status") == "ok"
                    out[cid] = "delete" if gone else "disable"
        return out

    def _ha_remove(self, flags, world, ha: str, host_key: str, key: str) -> dict:
        """The Home Assistant half of a removal, after the card left the page."""
        entry = world.ha.get(host_key) if flags["HA"] == "installed" else None
        if entry is None:
            return {"home_assistant": "failed", "ha_error": "the card is not in Home Assistant"}
        if flags["HA_REMOVE"] == "fail":
            return {"home_assistant": "failed",
                    "ha_error": "Unable to remove device: the agent still reports this drive"}
        if ha == "entry":
            del world.ha[host_key]
        elif ha in ("delete", "replica"):
            entry["devices"].pop(key, None)
        return {"home_assistant": "disabled" if ha == "disable" else "deleted", "ha_error": None}

    def _responses(self, flags, world) -> dict:
        out = {"self": self_response(flags, world), "replicas": replicas_response(flags, world)}
        for h in world.hosts:
            out[h["id"]] = field_response(flags, world, h)
        return out

    @staticmethod
    def _card_list(response: dict) -> list:
        return (response.get("items") or []) + (response.get("filesystem_cards") or [])

    def _cards(self, method, parts, flags, world):
        """Card order, hidden cards, removal and download, as the proxy has them."""
        if parts == [] and method == "GET":
            return self._send(200, {"order": world.card_order, "hidden": world.hidden_cards,
                                    "names": world.card_names, "host_names": world.host_names})
        if parts == ["order"] and method == "PUT":
            order = (self._body() or {}).get("order")
            if not isinstance(order, list):
                return self._err(400, "bad_request")
            known = {h["id"] for h in world.hosts}
            for response in self._responses(flags, world).values():
                known |= {c["card_id"] for c in self._card_list(response)}
            wanted = []
            for i in order:
                if isinstance(i, str) and i in known and i not in wanted:
                    wanted.append(i)
            hosts = {"self", "replicas"} | {h["id"] for h in world.hosts}
            world.card_order = wanted + [i for i in world.card_order
                                         if i not in wanted and i.partition("|")[0] in hosts]
            world.options_writes += 1
            return self._send(200, {"order": world.card_order})
        if not parts:
            return self._err(404, "not_found")
        cid = parts[0]
        host_key, sep, key = cid.partition("|")
        host = world.host(host_key)
        if host is None and (host_key not in ("self", "replicas") or not sep):
            return self._err(404, "not_found")
        if len(parts) == 1 and method == "PATCH":
            hidden = (self._body() or {}).get("hidden")
            if not isinstance(hidden, bool):
                return self._err(400, "bad_request")
            if hidden and cid not in world.hidden_cards:
                world.hidden_cards.append(cid)
            if not hidden and cid in world.hidden_cards:
                world.hidden_cards.remove(cid)
            world.options_writes += 1
            return self._send(200, {"id": cid, "hidden": hidden, "hidden_cards": world.hidden_cards})
        response = self._responses(flags, world).get(host_key) or {}
        item = next((c for c in self._card_list(response) if c["card_id"] == cid), None)
        if len(parts) == 2 and parts[1] == "name" and method == "PUT":
            name = (self._body() or {}).get("name")
            if not isinstance(name, str) or not sep:
                return self._err(400, "bad_request")
            name = " ".join(name.split())[:100]
            if name:
                world.card_names[cid] = name
            else:
                world.card_names.pop(cid, None)
            world.options_writes += 1
            ha = None
            entry = world.ha.get(host_key) if flags["HA"] == "installed" else None
            device = entry and entry["devices"].get(key)
            if device:
                ha = "failed" if flags["HA_NAME"] == "fail" else "saved"
                if ha == "saved":
                    if name:
                        world.ha_names[device] = name
                    else:
                        world.ha_names.pop(device, None)
            return self._send(200, {"id": cid, "name": name, "home_assistant": ha})
        if len(parts) >= 2 and parts[1] == "thresholds":
            return self._thresholds(method, parts[2:], flags, world, cid, item)
        if len(parts) == 1 and method == "DELETE":
            if host_key == "self":
                return self._err(409, "cannot_remove", "A drive of this machine can be hidden, not removed.")
            ha = (parse_qs(urlparse(self.path).query).get("ha") or [None])[0]
            if ha is not None and ha not in ("delete", "disable", "entry", "replica"):
                return self._err(400, "bad_request")
            if (ha == "replica" and host_key != "replicas") or (ha == "entry" and host_key == "replicas"):
                return self._err(400, "bad_request")
            host_removed = False
            if host_key == "replicas":
                rec = world.replica(item["id"]) if item else None
                if rec is None:
                    return self._err(404, "not_found")
                world.replicas.remove(rec)
                gone = {cid}
            else:
                if sep and key not in host["removed"]:
                    host["removed"].append(key)
                left = {c["card_id"] for c in self._card_list(response)} - {cid}
                host_removed = not sep or not left
                if host_removed:
                    world.hosts.remove(host)
                gone = {i for i in world.card_order + world.hidden_cards
                        if i == cid or (host_removed and i.partition("|")[0] == host_key)}
            world.card_order = [i for i in world.card_order if i not in gone]
            world.hidden_cards = [i for i in world.hidden_cards if i not in gone]
            world.options_writes += 1
            answer = {"removed": cid, "host_removed": host_removed}
            if ha is not None:
                answer.update(self._ha_remove(flags, world, ha, host_key, key))
            return self._send(200, answer)
        if len(parts) == 2 and parts[1] == "file" and method == "GET":
            if item is None or item["type"] == "filesystem":
                return self._err(404, "not_found")
            if host_key == "replicas":
                rec = world.replica(item["id"])
                doc = world.export(rec)
                name = slugify(rec["body"]["name"] if rec["kind"] == "zfs_pool" else rec["name"])
            elif item["type"] == "pool":
                doc = {"pool": item["pool"], "filesystems": item.get("filesystems") or []}
                name = f"pool-{item['pool']['name']}"
            else:
                if file_kind(item["drive"]) is None:
                    return self._err(409, "unsupported_drive", "This drive gives no SMART data to copy.")
                if flags["REPLICA_FOLDER"] != "ok":
                    return self._err(503, "replica_folder_unavailable")
                doc = saved_doc(item["drive"])
                name = world.file_entry(doc, "user")["id"][len("user."):]
            return self._send(200, body=json.dumps(doc, indent=2).encode(), headers={
                "Content-Disposition": f'attachment; filename="{name}.json"'})
        return self._err(404, "not_found")

    def _thresholds(self, method, rest, flags, world, cid, item):
        """A drive's thresholds: read, set, accept the flagged readings, reset."""
        body = self._body() if method != "GET" else None  # read it whatever the answer: keep-alive
        if item is None or item.get("type") != "drive" or not sep_ok(cid):
            return self._err(404, "not_found", "no such drive")
        if RULES is None or flags["RULES"] == "missing":
            return self._err(503, "rules_unavailable")
        drive = item["drive"]
        source, overrides = world.thresholds(flags, cid)
        limits = threshold_answer(cid, source, drive, overrides)["labels"]
        by_label = {lim["label"]: lim for lim in limits}
        if not rest and method == "GET":
            return self._send(200, threshold_answer(cid, source, drive, overrides))
        if not rest and method == "PUT":
            values = (body or {}).get("values")
            if not isinstance(values, dict):
                return self._err(400, "bad_request")
            for label, value in values.items():
                if label not in by_label or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    return self._err(400, "bad_request", f"{label}: a whole number from 0 up")
            wanted = {**overrides, **values}
        elif rest == ["accept"] and method == "POST":
            flagged = [lim for lim in limits if lim["flagged"]]
            if not flagged:
                return self._err(409, "nothing_flagged")
            wanted = dict(overrides)
            for lim in flagged:
                wanted[lim["label"]] = PROXY.accept_value(lim["reading"], lim["compare"])
        elif rest == ["reset"] and method == "POST":
            wanted = {}
        else:
            return self._err(404, "not_found")
        wanted = {k: int(v) for k, v in wanted.items() if k in by_label and int(v) != by_label[k]["default"]}
        source = world.set_thresholds(flags, cid, wanted)
        world.self_cache = None
        return self._send(200, threshold_answer(cid, source, drive, wanted))

    def _mock(self, method, parts, flags, world):
        """The replica agent's own routes the page uses: scenarios and edits."""
        if len(parts) >= 2 and parts[0] in ("drives", "pools"):
            card_id = parts[1] if parts[0] == "drives" else f"pool.{parts[1]}"
            rec = world.replica(card_id)
            if rec is None:
                return self._err(404, "not_found")
            body = self._body() or {}
            if len(parts) == 3 and parts[2] == "scenario" and method == "POST":
                ids = [i for i, _ in SCENARIOS[rec["kind"]]]
                if body.get("id") not in ids:
                    return self._err(422, "unknown_scenario")
                apply_scenario(rec, body["id"])
                return self._send(200, {"ok": True})
            if parts[0] == "drives" and len(parts) == 3 and method == "PATCH":
                if parts[2] == "smart":
                    patch_smart(rec["body"], body)
                    return self._send(200, {"ok": True})
                if parts[2] == "devstat" and "reported_uncorrectable" in body:
                    set_devstat(rec["body"], body["reported_uncorrectable"])
                    return self._send(200, {"ok": True})
            if parts[0] == "pools" and len(parts) == 2 and method == "PATCH":
                p = rec["body"]
                if "state" in body:
                    p["state"] = str(body["state"])
                for dev in body.get("devices") or []:
                    for d in p["devices"]:
                        if d["name"] == dev.get("name"):
                            d.update({k: dev[k] for k in ("state", "read", "write", "cksum") if k in dev})
                refresh_pool(p)
                return self._send(200, {"ok": True})
        return self._err(404, "not_found")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8099)))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"[dev] Control Center dev harness on http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
