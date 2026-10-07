#!/usr/bin/env python3
"""SMART Sniffer App: Web UI proxy server.

Serves static files from /opt/web/ at the root path and proxies:
  /api/*  -> localhost:$AGENT_PORT  (this app's Go agent, app token injected)
  /mock/* -> localhost:$MOCK_PORT   (the replica agent, /mock/ rewritten to /api/)

It also answers the Control Center's own routes:
  GET  /app/info     versions, ports, the app's identity, network and replicas
  POST /evaluate     the integration's verdict for a drive (plus its view), or
                     for a ZFS pool ({"pool": {...}})

The verdict comes from the integration's own rule modules (attention.py,
devstat.py, extract.py, pool_health.py), which the Dockerfile takes from the
smart-sniffer release this app is built against. They import nothing from
Home Assistant.

drive_view(), pool_verdict(), pool_view() and Rules are importable without
side effects, so the page's dev harness can use them.

The Home Assistant routes talk to the Supervisor (and, through it, to Home
Assistant Core) with the SUPERVISOR_TOKEN the Supervisor puts in the app's
environment. The token is never logged and never sent to the browser.

Runs on the ingress port (8099) so Home Assistant can embed the page in the
sidebar. Every request from the page goes to the same origin, so there is no
CORS. Python standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import importlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType
from typing import Any
from urllib.parse import quote, unquote, urlparse

# Ensure .js and .css MIME types are correct
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# Directory containing static web assets
STATIC_DIR = "/opt/web"

# Where the Dockerfile puts the integration's rule modules: this directory
# holds smart_sniffer/{attention,devstat,extract,pool_health,const}.py, and
# its parent holds VERSION (the smart-sniffer tag or branch they came from).
DEFAULT_RULES_DIR = "/opt/ssrules/custom_components"

# The hostname Home Assistant uses to reach this app's agents from the
# integration. run.sh passes the container's real hostname with --agent-host
# (it follows the slug, so a local install is local-smart-sniffer-agent); this
# default, the published app's hostname, is only for running the proxy by hand.
DEFAULT_AGENT_HOST = "0449a086-smart-sniffer-agent"

INTEGRATION_DOMAIN = "smart_sniffer"
SUPERVISOR_TIMEOUT = 5  # seconds, every Supervisor and Core call
MAX_BODY = 4 * 1024 * 1024  # largest JSON request body the app routes accept
HA_API_CACHE_SECONDS = 30
REFRESH_MINUTES = 15  # how often the page re-reads stored field agents
REPLICA_AGENT_TIMEOUT = 1.0  # seconds, /app/info's check of the replica agent

# Matches the titles the integration's config flow gives its entries:
#   user step:     "SMART Sniffer (<host>:<port>)"
#   zeroconf step: "SMART Sniffer (<hostname>)"
_TITLE_RE = re.compile(r"^SMART Sniffer \((?P<inner>.+)\)$")


def log(message: str) -> None:
    print(f"[webui] {message}", flush=True)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Integration rules ───────────────────────────────────────────────────────

# The Rules instance that loaded last; drive_view() and pool_verdict() read
# the rule modules' own tables through it.
_ACTIVE_RULES: Rules | None = None


class Rules:
    """The integration's rule modules, loaded once.

    The modules use relative imports (attention imports devstat, pool_health
    imports const), so they are loaded as members of a synthetic package
    custom_components.smart_sniffer whose __path__ is the rules directory.
    The integration's own __init__.py (which imports Home Assistant) is never
    present and never executed.
    """

    def __init__(self, rules_dir: str) -> None:
        self.rules_dir = rules_dir
        self.attention: ModuleType | None = None
        self.devstat: ModuleType | None = None
        self.pool_health: ModuleType | None = None
        self.version: str | None = None
        self._lock = threading.Lock()
        self._tried = False
        self.version = self._read_version()

    def _read_version(self) -> str | None:
        for candidate in (
            os.path.join(os.path.dirname(self.rules_dir.rstrip("/")), "VERSION"),
            os.path.join(self.rules_dir, "VERSION"),
        ):
            try:
                with open(candidate, encoding="utf-8") as handle:
                    text = handle.read().strip()
                if text:
                    return text
            except OSError:
                continue
        return None

    def _import(self, name: str) -> ModuleType:
        return importlib.import_module(f"custom_components.smart_sniffer.{name}")

    def load(self) -> bool:
        """Import the modules on first use. True when drives can be judged."""
        global _ACTIVE_RULES
        with self._lock:
            if self._tried:
                return self.attention is not None
            self._tried = True
            package_dir = os.path.join(self.rules_dir, "smart_sniffer")
            if not os.path.isfile(os.path.join(package_dir, "attention.py")):
                log(f"Integration rules not found in {package_dir}; verdicts are off")
                return False
            try:
                parent = sys.modules.get("custom_components")
                if parent is None:
                    parent = ModuleType("custom_components")
                    parent.__path__ = [self.rules_dir]  # type: ignore[attr-defined]
                    sys.modules["custom_components"] = parent
                if "custom_components.smart_sniffer" not in sys.modules:
                    package = ModuleType("custom_components.smart_sniffer")
                    package.__path__ = [package_dir]  # type: ignore[attr-defined]
                    sys.modules["custom_components.smart_sniffer"] = package
                attention = self._import("attention")
                for name in ("evaluate_attention", "current_readings", "compose_reasons_text"):
                    if not callable(getattr(attention, name, None)):
                        raise ImportError(f"attention.{name} is missing")
                self.attention = attention
                # devstat.py arrived with integration v0.8.0. Without it the
                # verdict is computed with no Device Statistics merge.
                if os.path.isfile(os.path.join(package_dir, "devstat.py")):
                    devstat = self._import("devstat")
                    if callable(getattr(devstat, "merge_devstat", None)) and hasattr(
                        devstat, "DEVSTAT_KEY"
                    ):
                        self.devstat = devstat
            except Exception as err:  # noqa: BLE001 -- any failure means no rules
                log(f"Could not load the integration rules: {type(err).__name__}: {err}")
                self.attention = None
                self.devstat = None
                return False
            # pool_health.py (with const.py) judges ZFS pools. Without it the
            # drives are still judged and pools carry no verdict.
            if os.path.isfile(os.path.join(package_dir, "pool_health.py")):
                try:
                    pool_health = self._import("pool_health")
                    if callable(getattr(pool_health, "pool_problems", None)):
                        self.pool_health = pool_health
                except Exception as err:  # noqa: BLE001
                    log(f"Could not load the pool rules: {type(err).__name__}: {err}")
            _ACTIVE_RULES = self
            _build_reason_table(self.attention)
            log(f"Integration rules loaded ({self.version or 'unknown version'})")
            return True

    def evaluate(self, drive: dict[str, Any]) -> dict[str, Any]:
        """The verdict the integration would give this drive, at default
        thresholds and with no held Device Statistics readings. Puts the
        merged Device Statistics readings on the drive as _devstat, as the
        integration's coordinator does."""
        attention = self.attention
        assert attention is not None
        if self.devstat is not None:
            drive[self.devstat.DEVSTAT_KEY], _ = self.devstat.merge_devstat(drive, None)
        state, severity, reasons, accepted = attention.evaluate_attention(drive)
        readings = attention.current_readings(drive)
        reasons_text = attention.compose_reasons_text(state, reasons, accepted)
        return {
            "state": state,
            "severity": severity,
            "reasons": list(reasons),
            "accepted": list(accepted),
            "readings": readings,
            "reasons_text": reasons_text,
            "rules_version": self.version,
        }


def pool_verdict(pool: dict[str, Any], rules: Rules | None = None) -> dict[str, Any]:
    """The integration's verdict for one ZFS pool: {"state": "YES" | "NO",
    "problems": [...]}. Pools have no Maybe. Uses the given rules, else the
    last Rules that loaded; RuntimeError when no pool rules are loaded."""
    rules = rules or _ACTIVE_RULES
    pool_health = rules.pool_health if rules is not None else None
    if pool_health is None:
        raise RuntimeError("pool rules are not loaded")
    problems = list(pool_health.pool_problems(pool))
    return {"state": "YES" if problems else "NO", "problems": problems}


# ── The drive view ──────────────────────────────────────────────────────────
#
# drive_view() turns a drive payload and its verdict into what the page
# draws: the header word, the stats, one row per watched counter with the
# mark of the reason that names it, the data volumes and the Field report
# size. The page does no rule matching of its own.

HEADER_BY_STATE = {"YES": "attention", "MAYBE": "maybe", "NO": "ok", "UNSUPPORTED": "unknown"}

# Wear attribute names, as attention._ATA_WEAR_NAMES has them (refreshed from
# the module when it loads).
_WEAR_NAMES: set[str] = {
    "Wear_Leveling_Count", "Media_Wearout_Indicator", "SSD_Life_Left",
    "Remaining_Lifetime_Perc", "Percent_Lifetime_Remain", "Perc_Rated_Life_Remain",
    "Percent_Life_Remaining",
}

# ATA rows in display order: key, name, attribute names, attribute id.
_ATA_ROWS: list[tuple[str, str, tuple[str, ...], int | None]] = [
    ("reallocated", "Reallocated sectors", ("Reallocated_Sector_Ct", "Retired_Block_Count"), 5),
    ("pending", "Pending sectors",
     ("Current_Pending_Sector", "Current_Pending_Sector_Ct", "Total_Pending_Sectors"), 197),
    ("offline_unc", "Offline uncorrectable", ("Offline_Uncorrectable", "Total_Offl_Uncorrectabl"), 198),
    ("reported_unc", "Reported uncorrectable", ("Reported_Uncorrect", "Uncorrectable_Error_Cnt"), 187),
    ("cmd_timeout", "Command timeout", ("Command_Timeout",), 188),
    ("realloc_events", "Reallocated events", ("Reallocated_Event_Count",), 196),
    ("spin_retries", "Spin retries", ("Spin_Retry_Count",), 10),
    ("wear", "Wear used", (), None),
]
_NVME_ROWS: list[tuple[str, str]] = [
    ("critical_warning", "Critical warning"),
    ("media_errors", "Media errors"),
    ("available_spare", "Available spare"),
    ("wear", "Wear used"),
]
_SCSI_ROWS: list[tuple[str, str]] = [("grown_defects", "Grown defects")]

# The two rows an Unsupported drive shows as "not known", by protocol.
_MAIN_ROWS = {
    "ata": [("reallocated", "Reallocated sectors"), ("pending", "Pending sectors")],
    "nvme": [("critical_warning", "Critical warning"), ("media_errors", "Media errors")],
    "scsi": [("grown_defects", "Grown defects")],
}

# Device Statistics readings that stand in for a missing attribute (the
# integration's gap-fill): reading -> (page, offset, row key, row name,
# the attribute row it stands in for, the note on that row).
_DEVSTAT_ROWS: dict[str, tuple[int, int, str, str, str, str]] = {
    "unc": (4, 0x008, "reported_unc_devstat", "Uncorrectable, page 4",
            "reported_unc", "no attribute 187"),
    "realloc": (3, 0x020, "reallocated_devstat", "Reallocated, page 3",
                "reallocated", "no attribute 5"),
    "wear_used": (7, 0x008, "wear_devstat", "Wear used, page 7",
                  "wear", "no wear attribute"),
}
_DEVSTAT_SUFFIX = "from device statistics"

# Reason label (the text before the colon) -> (row key, mark). Built from the
# rule module's own dictionaries when it loads; this copy is the fallback.
_REASON_LABELS: dict[str, tuple[str, str]] = {
    "Reallocated Sector Count": ("reallocated", "crit"),
    "Current Pending Sector Count": ("pending", "crit"),
    "Offline Uncorrectable Errors": ("offline_unc", "crit"),
    "Total Offline Uncorrectable": ("offline_unc", "crit"),
    "Reported Uncorrectable Errors": ("reported_unc", "crit"),
    "Uncorrectable Error Count": ("reported_unc", "crit"),
    "Reallocated Event Count": ("realloc_events", "warn"),
    "Spin Retry Count": ("spin_retries", "warn"),
    "Command Timeout": ("cmd_timeout", "warn"),
    "NVMe media errors": ("media_errors", "crit"),
}
# Thresholds named in the expectations, refreshed from the module.
_THRESHOLDS = {"spare_warn_below": 20, "wear_warn_at": 90}


def _build_reason_table(attention: ModuleType | None) -> None:
    """Refresh the reason table from attention.py's own dictionaries, so a
    counter the integration starts watching gets its row and mark."""
    if attention is None:
        return
    names_to_key = {name: key for key, _, names, _ in _ATA_ROWS for name in names}
    critical = getattr(attention, "_CRITICAL_ATA", None)
    warning = getattr(attention, "_WARNING_ATA", None)
    for table, mark in ((critical, "crit"), (warning, "warn")):
        if not isinstance(table, dict):
            continue
        for name, label in table.items():
            key = names_to_key.get(name) or re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")
            _REASON_LABELS[str(label)] = (key, mark)
    media = getattr(attention, "LABEL_NVME_MEDIA_ERRORS", None)
    if isinstance(media, str):
        _REASON_LABELS.setdefault("NVMe media errors", ("media_errors", "crit"))
    wear = getattr(attention, "_ATA_WEAR_NAMES", None)
    if isinstance(wear, (set, frozenset)) and wear:
        _WEAR_NAMES.clear()
        _WEAR_NAMES.update(str(n) for n in wear)
    for key, attr in (("spare_warn_below", "_NVME_SPARE_WARN_BELOW"),
                      ("wear_warn_at", "_ATA_WEAR_WARN_THRESHOLD")):
        value = getattr(attention, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            _THRESHOLDS[key] = value


def _short_expect(paren: str | None) -> str | None:
    """A reason's parenthesis, made short: "expected 0", "warns above 100"."""
    if not paren:
        return None
    parts = [p.strip() for p in paren.split(";")]
    out: list[str] = []
    for part in parts:
        if part == _DEVSTAT_SUFFIX:
            continue
        match = re.fullmatch(r"threshold (\d+)", part)
        out.append(f"warns above {match.group(1)}" if match else part)
    text = ", ".join(p for p in out if p)
    if _DEVSTAT_SUFFIX in parts:
        text = f"{text}, {_DEVSTAT_SUFFIX}" if text else _DEVSTAT_SUFFIX
    return text or None


def classify_reason(reason: str) -> dict[str, Any] | None:
    """Which row a verdict reason marks: {"key", "mark", "expect", "value"}.
    "value" is the reading the reason states, for a row the drive data does
    not have. None for a reason that matches nothing."""
    text = str(reason)
    devstat = _DEVSTAT_SUFFIX in text
    suffix = "_devstat" if devstat else ""
    tail = f", {_DEVSTAT_SUFFIX}" if devstat else ""
    if text.startswith("SMART overall status"):
        return {"key": "smart_status", "mark": "crit", "expect": None, "value": "FAILED"}
    match = re.match(r"NVMe critical warning flag set \((0x[0-9a-fA-F]+)\)", text)
    if match:
        return {"key": "critical_warning", "mark": "crit", "expect": "expected 0x00",
                "value": match.group(1)}
    match = re.match(r"NVMe available spare \((\d+)%\) at or below drive threshold \((\d+)%\)", text)
    if match:
        return {"key": "available_spare", "mark": "crit",
                "expect": f"at or below the drive's own {match.group(2)}%",
                "value": f"{match.group(1)}%"}
    match = re.match(r"NVMe available spare low: (\d+)% remaining(?: \(accepted down to (\d+)%\))?", text)
    if match:
        expect = (f"accepted down to {match.group(2)}%" if match.group(2)
                  else f"warns below {_THRESHOLDS['spare_warn_below']}%")
        return {"key": "available_spare", "mark": "warn", "expect": expect,
                "value": f"{match.group(1)}%"}
    match = re.match(r"(?:NVMe drive|SSD) wear at (\d+)% of rated life(?:.*?\(accepted (\d+)%\))?", text)
    if match:
        expect = (f"accepted {match.group(2)}%" if match.group(2)
                  else f"warns at {_THRESHOLDS['wear_warn_at']}%")
        return {"key": "wear" + suffix, "mark": "warn", "expect": expect + tail,
                "value": f"{match.group(1)}%"}
    match = re.match(r"(?P<label>[^:()]+): (?P<value>[^(]+?)(?: \((?P<paren>[^)]*)\))?$", text)
    if match:
        found = _REASON_LABELS.get(match.group("label").strip())
        if found:
            key, mark = found
            value = match.group("value").strip()
            if re.fullmatch(r"\d+", value):
                value = f"{int(value):,}"
            return {"key": key + suffix, "mark": mark,
                    "expect": _short_expect(match.group("paren")), "value": value}
    return None


def _smart_data(drive: dict[str, Any]) -> dict[str, Any]:
    raw = drive.get("smart_data") if isinstance(drive, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return raw if isinstance(raw, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _fmt(value: int) -> str:
    return f"{value:,}"


def _decode_raw(name: str, raw: Any) -> int | None:
    """An ATA raw value as a count: Command_Timeout's low 16 bits, a packed
    value's leading figure from raw.string (as the integration reads them)."""
    if not isinstance(raw, dict):
        return _int(raw)
    value = _int(raw.get("value"))
    if value is None:
        return None
    if name == "Command_Timeout" and value > 0xFFFF:
        return value & 0xFFFF
    if value > 0xFFFF:
        match = re.match(r"\s*(\d+)", str(raw.get("string") or ""))
        if match and int(match.group(1)) != value:
            return int(match.group(1))
    return value


def _is_dead_wear(attr: dict[str, Any]) -> bool:
    flags = attr.get("flags")
    flags_value = flags.get("value") if isinstance(flags, dict) else None
    fields = (attr.get("value"), attr.get("worst"), attr.get("thresh"), flags_value)
    return all(isinstance(f, int) and not isinstance(f, bool) and f == 0 for f in fields)


def _usable_wear(table: list[dict[str, Any]]) -> dict[str, Any] | None:
    for attr in table:
        if attr.get("name") in _WEAR_NAMES and not _is_dead_wear(attr):
            return attr
    return None


def _nvme_value(log_: dict[str, Any], key: str) -> tuple[bool, int | None]:
    """(present, value) for an NVMe counter; a 128-bit value at or above
    2^64 is present but not readable, as the integration reads it."""
    if f"{key}_le" in log_:
        return True, None
    raw = log_.get(f"{key}_s", log_.get(key))
    if raw is None:
        return key in log_, None
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return True, None
    if value < 0 or value >= 1 << 64:
        return True, None
    return True, value


def _devstat_entry(pages: Any, page: int, offset: int) -> int | None:
    if not isinstance(pages, list):
        return None
    for candidate in pages:
        if not isinstance(candidate, dict) or candidate.get("number") != page:
            continue
        for entry in candidate.get("table") or []:
            if not isinstance(entry, dict) or entry.get("offset") != offset:
                continue
            flags = entry.get("flags")
            if not (isinstance(flags, dict) and flags.get("valid") is True):
                return None
            return _int(entry.get("value"))
    return None


def _devstat_readings(drive: dict[str, Any]) -> dict[str, int]:
    """The three Device Statistics readings: the merged ones the rules put on
    the drive (_devstat), else this read's pages."""
    merged = drive.get("_devstat")
    out: dict[str, int] = {}
    if isinstance(merged, dict) and merged:
        for name in _DEVSTAT_ROWS:
            value = _int(merged.get(name))
            if value is not None and value >= 0:
                out[name] = value
        return out
    block = drive.get("device_statistics")
    if not isinstance(block, dict) or block.get("status") != "present":
        return out
    for name, (page, offset, *_rest) in _DEVSTAT_ROWS.items():
        value = _devstat_entry(block.get("pages"), page, offset)
        if value is not None and value >= 0:
            out[name] = value
    return out


def _protocol(drive: dict[str, Any], smart: dict[str, Any]) -> str | None:
    if smart.get("nvme_smart_health_information_log"):
        return "nvme"
    if (smart.get("ata_smart_attributes") or {}).get("table"):
        return "ata"
    proto = str(drive.get("protocol") or (smart.get("device") or {}).get("protocol") or "").lower()
    if "nvme" in proto:
        return "nvme"
    if "scsi" in proto or "sas" in proto or "scsi_grown_defect_list" in smart:
        return "scsi"
    if "ata" in proto or "sat" in proto:
        return "ata"
    return None


def _row(key: str, name: str, value: str, mark: str | None = None,
         expect: str | None = None, note: str | None = None) -> dict[str, Any]:
    return {"key": key, "name": name, "value": value, "mark": mark, "expect": expect, "note": note}


def _ghost(key: str, name: str, note: str | None = None) -> dict[str, Any]:
    return _row(key, name, "not known", "unk", None, note)


def _data_rows(drive: dict[str, Any], smart: dict[str, Any], protocol: str | None) -> list[dict[str, Any]]:
    """Rows for the counters the drive reports, unmarked, in display order."""
    rows: list[dict[str, Any]] = []
    if protocol == "nvme":
        log_ = smart.get("nvme_smart_health_information_log") or {}
        for key, name in _NVME_ROWS:
            if key == "critical_warning":
                value = _int(log_.get("critical_warning"))
                if value is not None:
                    rows.append(_row(key, name, f"0x{value:02x}"))
            elif key == "media_errors":
                present, value = _nvme_value(log_, "media_errors")
                if present:
                    rows.append(_row(key, name, _fmt(value)) if value is not None
                                else _ghost(key, name, "not readable"))
            elif key == "available_spare":
                value = _int(log_.get("available_spare"))
                if value is not None:
                    rows.append(_row(key, name, f"{value}%"))
            elif key == "wear":
                value = _int(log_.get("percentage_used"))
                if value is not None:
                    rows.append(_row(key, name, f"{value}%"))
        return rows
    if protocol == "scsi":
        value = _int(smart.get("scsi_grown_defect_list"))
        if value is not None:
            rows.append(_row("grown_defects", "Grown defects", _fmt(value)))
        return rows
    if protocol != "ata":
        return rows

    table = [a for a in (smart.get("ata_smart_attributes") or {}).get("table") or []
             if isinstance(a, dict)]
    ids = {a.get("id") for a in table}
    gaps = {}
    readings = _devstat_readings(drive)
    for reading, (_page, _offset, dkey, dname, base, note) in _DEVSTAT_ROWS.items():
        value = readings.get(reading)
        if value is None:
            continue
        if reading == "wear_used":
            if _usable_wear(table) is not None:
                continue
        elif _DEVSTAT_BLOCKING[reading] in ids:
            continue
        gaps[base] = (dkey, dname, note, value, reading)

    for key, name, names, _attr_id in _ATA_ROWS:
        if key == "wear":
            attr = _usable_wear(table)
            value = _int(attr.get("value")) if attr else None
            if value is not None:
                rows.append(_row(key, name, f"{max(0, 100 - value)}%"))
            elif key in gaps:
                rows.append(_ghost(key, name, gaps[key][2]))
        else:
            attr = next((a for a in table if a.get("name") in names), None)
            value = _decode_raw(str(attr.get("name")), attr.get("raw")) if attr else None
            if value is not None:
                rows.append(_row(key, name, _fmt(value)))
            elif key in gaps:
                rows.append(_ghost(key, name, gaps[key][2]))
        if key in gaps:
            dkey, dname, _note, value, reading = gaps[key]
            text = f"{value}%" if reading == "wear_used" else _fmt(value)
            rows.append(_row(dkey, dname, text))
    return rows


# The attribute id whose presence blocks a gap-fill reading.
_DEVSTAT_BLOCKING = {"unc": 187, "realloc": 5, "wear_used": None}


def _stats(smart: dict[str, Any]) -> dict[str, int | None]:
    log_ = smart.get("nvme_smart_health_information_log") or {}
    temp = _int((smart.get("temperature") or {}).get("current"))
    hours = _int((smart.get("power_on_time") or {}).get("hours"))
    cycles = _int(smart.get("power_cycle_count"))
    if temp is None:
        temp = _int(log_.get("temperature"))
    if hours is None:
        hours = _int(log_.get("power_on_hours"))
    if cycles is None:
        cycles = _int(log_.get("power_cycles"))
    return {"temp_c": temp, "power_on_hours": hours, "power_cycles": cycles}


_VOLUME_SOURCES = {"ata_attribute": "attribute", "nvme": "nvme", "ata_device_statistics": "devstat"}


def _volumes(drive: dict[str, Any]) -> dict[str, Any]:
    derived = drive.get("derived")
    out: dict[str, Any] = {}
    for word, key in (("written", "host_writes"), ("read", "host_reads")):
        if not isinstance(derived, dict):
            out[word] = {"omitted": "not_reported"}
            continue
        volume = derived.get(key)
        if isinstance(volume, dict) and _int(volume.get("bytes")) is not None:
            entry: dict[str, Any] = {
                "bytes": _int(volume.get("bytes")),
                "source": _VOLUME_SOURCES.get(str(volume.get("source")), volume.get("source")),
            }
            if volume.get("attribute_id") is not None:
                entry["attribute_id"] = volume.get("attribute_id")
            out[word] = entry
        else:
            out[word] = {"omitted": derived.get(f"{key}_omitted") or "no_source"}
    return out


def _report(smart: dict[str, Any], protocol: str | None) -> dict[str, Any]:
    if protocol == "ata":
        table = (smart.get("ata_smart_attributes") or {}).get("table") or []
        return {"kind": "ata", "count": len(table)}
    if protocol == "nvme":
        return {"kind": "nvme", "count": len(smart.get("nvme_smart_health_information_log") or {})}
    if protocol == "scsi" and smart:
        keys = [k for k in smart if k.startswith("scsi_")]
        return {"kind": "scsi", "count": len(keys)}
    return {"kind": None, "count": 0}


def drive_view(drive: dict[str, Any], verdict: dict[str, Any] | None) -> dict[str, Any]:
    """What the page draws for one drive (spec section 4.7). Pure: reads only
    its arguments and the reason table. A verdict of None (no rules) gives
    the header "unknown" and no marks."""
    drive = drive if isinstance(drive, dict) else {}
    smart = _smart_data(drive)
    protocol = _protocol(drive, smart)
    state = (verdict or {}).get("state")
    header = HEADER_BY_STATE.get(str(state), "unknown")
    status = smart.get("smart_status")
    health_failed = isinstance(status, dict) and status.get("passed") is False

    if state == "UNSUPPORTED":
        rows = [_ghost(key, name) for key, name in _MAIN_ROWS.get(protocol or "ata", _MAIN_ROWS["ata"])]
    else:
        rows = _data_rows(drive, smart, protocol)
    by_key = {row["key"]: row for row in rows}
    severity = (verdict or {}).get("severity")
    for reason in (verdict or {}).get("reasons") or []:
        found = classify_reason(reason)
        if found is None:
            rows.append(_row("other", str(reason), "",
                             "crit" if severity == "critical" else "warn"))
            continue
        if found["key"] == "smart_status":
            continue  # the header's Health FAILED says it
        row = by_key.get(found["key"])
        if row is None:
            name = next((n for k, n, *_ in _ATA_ROWS if k == found["key"]), None) or next(
                (n for k, n in _NVME_ROWS + _SCSI_ROWS if k == found["key"]), None)
            if name is None:
                dev = next((d for d in _DEVSTAT_ROWS.values() if d[2] == found["key"]), None)
                name = dev[3] if dev else found["key"]
            row = _row(found["key"], name, found["value"])
            rows.append(row)
            by_key[row["key"]] = row
        if row["mark"] != "crit":  # a critical mark is never weakened
            row["mark"] = found["mark"]
        row["expect"] = found["expect"]

    block = drive.get("device_statistics")
    devstat = block.get("status") if isinstance(block, dict) else None
    view = {
        "header": header,
        "health_failed": health_failed,
        "protocol": protocol,
        "stats": _stats(smart),
        "rows": rows,
        "volumes": _volumes(drive),
        "devstat": devstat,
        "devstat_reason": block.get("reason") if isinstance(block, dict) else None,
        "report": _report(smart, protocol),
        "can_save": state != "UNSUPPORTED" and bool(smart),
    }
    return view


_NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven",
                 "eight", "nine", "ten", "eleven", "twelve"]


def _layout(vdev: Any, devices: list[dict[str, Any]]) -> str | None:
    if not isinstance(vdev, dict):
        return None
    kind = str(vdev.get("type") or "").lower()
    count = len(devices)
    word = _NUMBER_WORDS[count] if count < len(_NUMBER_WORDS) else str(count)
    if kind in ("disk", "file") or (not kind and count == 1):
        return "single disk"
    if kind in ("mirror", "raidz", "raidz1", "raidz2", "raidz3", "draid"):
        return f"{word}-disk {kind}"
    return f"{kind or 'stripe'} of {word} disks"


def pool_view(pool: dict[str, Any], verdict: dict[str, Any] | None,
              lab: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the page draws for one pool. The layout and every disk come only
    from a replica's lab block; a real agent lists only problem vdevs."""
    pool = pool if isinstance(pool, dict) else {}
    lab = lab if isinstance(lab, dict) else {}
    vanished = bool(lab.get("vanished"))
    devices = [d for d in lab.get("devices") or [] if isinstance(d, dict)]
    disks = None
    if devices:
        disks = []
        for dev in devices:
            errors = [_int(dev.get(k)) or 0 for k in ("read", "write", "cksum")]
            bad = dev.get("state") != "ONLINE" or any(errors)
            disks.append({"name": dev.get("name"), "state": dev.get("state"),
                          "read": errors[0], "write": errors[1], "cksum": errors[2],
                          "mark": "crit" if bad else None})
    state = (verdict or {}).get("state")
    header = "unknown" if vanished or state is None else ("attention" if state == "YES" else "ok")
    return {
        "header": header,
        "layout": _layout(lab.get("vdev"), devices) if devices else None,
        "state": pool.get("state"),
        "disks": disks,
        "vanished": vanished,
    }


# ── Supervisor and Home Assistant Core ──────────────────────────────────────

class Supervisor:
    """Small client for the Supervisor API and the Core API behind it."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._ha_api_cache: tuple[float, bool] | None = None
        self._slug: str | None = None
        self._slug_failed_at: float | None = None
        self._ha_version: str | None = None
        self._ha_version_failed_at: float | None = None
        self._network: dict[str, Any] | None = None
        self._network_failed_at: float | None = None
        self._installed_cache: tuple[float, bool | None] | None = None

    @property
    def available(self) -> bool:
        return bool(self.token)

    def call(
        self, method: str, path: str, body: Any = None, raw_text: bool = False
    ) -> tuple[int | None, Any]:
        """Return (HTTP status, parsed JSON or None). Status is None when the
        Supervisor could not be reached at all or there is no token."""
        if not self.token:
            return None, None
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method
        )
        request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=SUPERVISOR_TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status = err.code
            try:
                raw = err.read()
            except Exception:  # noqa: BLE001
                raw = b""
            log(f"Supervisor {method} {path.split('?')[0]}: HTTP {status}")
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            reason = getattr(err, "reason", err)
            log(f"Supervisor {method} {path.split('?')[0]}: {type(err).__name__}: {reason}")
            return None, None
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode("utf-8", "replace") if raw_text else None
        return status, parsed

    def core(self, method: str, path: str, body: Any = None) -> tuple[int | None, Any]:
        return self.call(method, "/core/api" + path, body)

    def core_reachable(self) -> bool:
        status, _ = self.core("GET", "/")
        reachable = status == 200
        self._ha_api_cache = (time.monotonic(), reachable)
        return reachable

    def core_reachable_cached(self) -> bool:
        cached = self._ha_api_cache
        if cached and time.monotonic() - cached[0] < HA_API_CACHE_SECONDS:
            return cached[1]
        return self.core_reachable()

    def app_slug(self) -> str | None:
        """This app's slug (the repository's id plus _smart_sniffer_agent when
        installed from a repository, local_smart_sniffer_agent as a local
        app). It cannot change while the app runs, so the first answer is
        kept. None when the Supervisor cannot tell; asked again after
        HA_API_CACHE_SECONDS."""
        if self._slug:
            return self._slug
        if not self.available:
            return None
        failed = self._slug_failed_at
        if failed is not None and time.monotonic() - failed < HA_API_CACHE_SECONDS:
            return None
        status, info = self.call("GET", "/addons/self/info")
        data = info.get("data") if isinstance(info, dict) else None
        slug = data.get("slug") if isinstance(data, dict) else None
        if status == 200 and isinstance(slug, str) and slug:
            self._slug = slug
            return slug
        self._slug_failed_at = time.monotonic()
        return None

    def ha_version(self) -> str | None:
        """Home Assistant's version ("2026.7.2"), from Core's /api/config. The
        page picks the paths of its Home Assistant links by it. Kept once known,
        like the slug; None when Core cannot be reached, asked again after
        HA_API_CACHE_SECONDS."""
        if self._ha_version:
            return self._ha_version
        if not self.available:
            return None
        failed = self._ha_version_failed_at
        if failed is not None and time.monotonic() - failed < HA_API_CACHE_SECONDS:
            return None
        status, config = self.core("GET", "/config")
        version = config.get("version") if isinstance(config, dict) else None
        if status == 200 and isinstance(version, str) and version:
            self._ha_version = version
            return version
        self._ha_version_failed_at = time.monotonic()
        return None

    def network(self) -> dict[str, Any] | None:
        """The host's own block from the Supervisor's network info: the first
        interface that is primary and connected with an IPv4 address, as
        {"block", "address", "prefix", "source"}. Kept for the process life
        once known; None when the Supervisor cannot tell."""
        if self._network:
            return self._network
        if not self.available:
            return None
        failed = self._network_failed_at
        if failed is not None and time.monotonic() - failed < HA_API_CACHE_SECONDS:
            return None
        status, info = self.call("GET", "/network/info")
        data = info.get("data") if isinstance(info, dict) else None
        interfaces = data.get("interfaces") if isinstance(data, dict) else None
        if status == 200 and isinstance(interfaces, list):
            for iface in interfaces:
                if not (isinstance(iface, dict) and iface.get("primary") and iface.get("connected")):
                    continue
                ipv4 = iface.get("ipv4") if isinstance(iface.get("ipv4"), dict) else {}
                for text in ipv4.get("address") or []:
                    try:
                        address = ipaddress.ip_interface(str(text))
                    except ValueError:
                        continue
                    if address.version != 4:
                        continue
                    self._network = {
                        "block": str(address.network),
                        "address": str(address.ip),
                        "prefix": address.network.prefixlen,
                        "source": "supervisor",
                    }
                    return self._network
        self._network_failed_at = time.monotonic()
        return None

    def entries(self) -> list[dict[str, Any]] | None:
        """The integration's config entries; None when Core cannot answer."""
        status, entries = self.core(
            "GET", f"/config/config_entries/entry?domain={INTEGRATION_DOMAIN}"
        )
        if status != 200 or not isinstance(entries, list):
            return None
        return [
            e for e in entries
            if isinstance(e, dict) and e.get("domain", INTEGRATION_DOMAIN) == INTEGRATION_DOMAIN
        ]

    def integration_installed(self, entries: list[dict[str, Any]] | None = None) -> bool | None:
        """Whether the integration is there: any entry, set up as a component,
        or offering a config flow (installed through HACS, no entry yet).
        None when Core cannot be reached. Cached HA_API_CACHE_SECONDS."""
        cached = self._installed_cache
        if entries is None and cached and time.monotonic() - cached[0] < HA_API_CACHE_SECONDS:
            return cached[1]
        if not self.available or not self.core_reachable():
            self._installed_cache = (time.monotonic(), None)
            return None
        if entries is None:
            entries = self.entries() or []
        installed = bool(entries)
        if not installed:
            status, config = self.core("GET", "/config")
            if status == 200 and isinstance(config, dict):
                installed = INTEGRATION_DOMAIN in (config.get("components") or [])
        if not installed:
            status, handlers = self.core("GET", "/config/config_entries/flow_handlers")
            if status == 200 and isinstance(handlers, list):
                installed = INTEGRATION_DOMAIN in handlers
        self._installed_cache = (time.monotonic(), installed)
        return installed

    def forget_installed(self) -> None:
        self._installed_cache = None


# ── Talking to agents ───────────────────────────────────────────────────────

class AgentReply:
    """One answer from an agent: status None when nothing answered."""

    def __init__(self, status: int | None, body: Any = None, raw: bytes = b"",
                 headers: dict[str, str] | None = None, error: str | None = None) -> None:
        self.status = status
        self.body = body
        self.raw = raw
        self.headers = headers or {}
        self.error = error


def agent_call(host: str, port: int, method: str, path: str, *, token: str = "",
               body: Any = None, raw_body: bytes | None = None,
               connect_timeout: float = 3.0, read_timeout: float = 5.0) -> AgentReply:
    """One HTTP request to an agent (or the replica agent). Never raises.
    The token, when given, goes in the Authorization header only."""
    conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(read_timeout)
        headers = {"Accept": "application/json"}
        data = raw_body
        if data is None and body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        reply_headers = {k.lower(): v for k, v in resp.getheaders()}
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return AgentReply(resp.status, parsed, raw, reply_headers)
    except (OSError, http.client.HTTPException, ValueError) as err:
        return AgentReply(None, error=type(err).__name__)
    finally:
        conn.close()


# ── Request handler ─────────────────────────────────────────────────────────

class ApiError(Exception):
    """An error answer: {"error": code, "message": text} with an HTTP status."""

    def __init__(self, status: int, code: str, message: str | None = None,
                 **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


# Path prefixes that belong to the proxy itself: anything under them that no
# route matches is a JSON 404, never the page.
_APP_PREFIXES = ("/app/", "/evaluate", "/fleet/", "/ha/", "/lab/", "/replicas")


class ProxyHandler(BaseHTTPRequestHandler):
    """Serves static files, proxies API requests, answers the app routes."""

    agent_port: int = 9099
    mock_port: int = 9100
    token: str = ""
    app_version: str | None = None
    agent_host: str = DEFAULT_AGENT_HOST
    mdns_name: str | None = None
    rules: Rules = Rules(DEFAULT_RULES_DIR)
    supervisor: Supervisor = Supervisor("http://supervisor", "")

    # The routes answered by the proxy itself: (method, path pattern, handler
    # name). Path parameters are named groups, percent-decoded before use.
    ROUTES: list[tuple[str, str, str]] = [
        ("GET", r"/app/info", "_route_app_info"),
        ("POST", r"/evaluate", "_route_evaluate"),
        ("GET", r"/ha/status", "_route_ha_status"),
        ("POST", r"/ha/connect", "_route_ha_connect"),
        ("POST", r"/ha/disconnect", "_route_ha_disconnect"),
        ("GET", r"/ha/verdict", "_route_ha_verdict"),
    ]

    def log_message(self, format, *args):
        """Send access logs to stdout for the app's log."""
        print(f"[webui] {self.address_string()} - {format % args}", flush=True)

    def do_GET(self):
        self._handle_request("GET")

    def do_POST(self):
        self._handle_request("POST")

    def do_PUT(self):
        self._handle_request("PUT")

    def do_DELETE(self):
        self._handle_request("DELETE")

    def do_PATCH(self):
        self._handle_request("PATCH")

    @classmethod
    def _compiled_routes(cls) -> list[tuple[str, re.Pattern[str], str]]:
        compiled = cls.__dict__.get("_ROUTES_COMPILED")
        if compiled is None:
            compiled = [(m, re.compile(p + r"/?"), h) for m, p, h in cls.ROUTES]
            cls._ROUTES_COMPILED = compiled
        return compiled

    def _match_route(self, method: str, path: str) -> tuple[str | None, dict[str, str], list[str]]:
        allowed: list[str] = []
        for route_method, pattern, handler in self._compiled_routes():
            match = pattern.fullmatch(path)
            if not match:
                continue
            if route_method == method:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                return handler, params, allowed
            allowed.append(route_method)
        return None, {}, allowed

    def _handle_request(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path

        handler_name, params, allowed = self._match_route(method, path)
        if handler_name is not None:
            try:
                getattr(self, handler_name)(parsed, **params)
            except ApiError as err:
                self._send_error(err)
            except Exception as err:  # noqa: BLE001 -- answer, never crash the server
                log(f"{path} failed: {type(err).__name__}: {err}")
                self._send_json(500, {"error": "internal_error", "message": None})
            return
        if allowed:
            self._send_json(405, {"error": "method_not_allowed",
                                  "message": f"use {' or '.join(sorted(set(allowed)))}"})
            return
        if path.startswith(_APP_PREFIXES):
            self._send_json(404, {"error": "not_found", "message": None})
            return

        # Proxy /api/* to the real Go agent
        if path.startswith("/api/"):
            self._proxy_request(method, "127.0.0.1", self.agent_port, self.path)
            return

        # Proxy /mock/* to the replica agent. Rewrite /mock/ -> /api/ because
        # the replica agent serves the same /api/* routes.
        if path.startswith("/mock/"):
            rewritten = self.path.replace("/mock/", "/api/", 1)
            self._proxy_request(method, "127.0.0.1", self.mock_port, rewritten, inject_token=False)
            return

        # Serve static files (GET only for static)
        if method != "GET":
            self.send_error(405, "Method Not Allowed")
            return

        self._serve_static(path)

    # ── helpers ──

    def _send_json(self, code: int, data: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, err: ApiError) -> None:
        self._send_json(err.status, {"error": err.code, "message": err.message, **err.extra})

    def _read_json(self, limit: int = MAX_BODY) -> Any:
        """The request body as JSON; ApiError 400 when it is not."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise ApiError(400, "bad_request", "bad Content-Length") from None
        if length > limit:
            self.close_connection = True
            raise ApiError(413, "too_large", "the request body is too large")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            raise ApiError(400, "bad_request", "empty body")
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(400, "bad_request", "body is not JSON") from None

    def _target_port(self, target: str) -> int | None:
        if target in ("agent", "self"):
            return self.agent_port
        if target in ("testlab", "replicas"):
            return self.mock_port
        return None

    def _mock(self, method: str, path: str, body: Any = None, timeout: float = 5.0,
              raw_body: bytes | None = None) -> AgentReply:
        return agent_call("127.0.0.1", self.mock_port, method, path, body=body,
                          raw_body=raw_body, connect_timeout=min(timeout, 2.0),
                          read_timeout=timeout)

    # ── /app/info ──

    def _replicas_info(self) -> dict[str, Any]:
        reply = self._mock("GET", "/api/replicas", timeout=REPLICA_AGENT_TIMEOUT)
        listing = reply.body if reply.status == 200 and isinstance(reply.body, dict) else None
        if listing is None:
            return {"agent": "down", "folder": None, "path": None, "shipped": None, "user": None}
        folder = listing.get("folder") if isinstance(listing.get("folder"), dict) else {}
        files = [f for f in listing.get("files") or [] if isinstance(f, dict) and f.get("valid")]
        return {
            "agent": "ok",
            "folder": folder.get("status"),
            "path": folder.get("path"),
            "shipped": sum(1 for f in files if f.get("source") == "shipped"),
            "user": sum(1 for f in files if f.get("source") == "user"),
        }

    def _route_app_info(self, _parsed) -> None:
        sv = self.supervisor
        ha_api = sv.available and sv.core_reachable_cached()
        slug = sv.app_slug()
        self._send_json(200, {
            "app_version": self.app_version,
            "rules_version": self.rules.version,
            "agent_port": self.agent_port,
            "mock_port": self.mock_port,
            "hostname": self.agent_host,
            "slug": slug,
            "mdns_name": self.mdns_name,
            "ha_version": sv.ha_version(),
            "ha_api": ha_api,
            "supervisor": slug is not None,
            "integration_installed": sv.integration_installed() if ha_api else None,
            "network": sv.network(),
            "replicas": self._replicas_info(),
            "refresh_minutes": REFRESH_MINUTES,
        })

    # ── /evaluate ──

    def _route_evaluate(self, _parsed) -> None:
        body = self._read_json()
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "body must be a drive or {\"pool\": {...}}")
        if not self.rules.load():
            raise ApiError(503, "rules_unavailable", "the integration rules are not loaded")
        if "pool" in body and "smart_data" not in body:
            pool = body["pool"]
            if not isinstance(pool, dict):
                raise ApiError(400, "bad_request", "pool must be an object")
            if self.rules.pool_health is None:
                raise ApiError(503, "rules_unavailable", "the pool rules are not loaded")
            self._send_json(200, pool_verdict(pool, self.rules))
            return
        verdict = self.rules.evaluate(body)
        self._send_json(200, {**verdict, "view": drive_view(body, verdict)})

    # ── /ha/status ──

    def _own_hosts(self) -> set[str]:
        hosts = {self.agent_host.lower()}
        try:
            hosts.add(socket.gethostname().split(".")[0].lower())
        except OSError:
            pass
        return hosts

    def _classify_entry(self, entry: dict[str, Any]) -> str | None:
        """Which of this app's agents an entry points at: "agent", "testlab"
        or None. Decided from the title the config flow wrote."""
        match = _TITLE_RE.match(str(entry.get("title") or ""))
        if not match:
            return None
        inner = match.group("inner").strip()
        own = self._own_hosts()
        host, sep, port = inner.rpartition(":")
        if sep and port.isdigit():
            if host.lower() not in own:
                return None
            if int(port) == self.agent_port:
                return "agent"
            if int(port) == self.mock_port:
                return "testlab"
            return None
        if inner.lower() not in own:
            return None
        # A zeroconf entry names the host but not the port. Both of this app's
        # agents advertise the same container hostname, so ask the entry's
        # diagnostics for its port; when that is not possible it is the agent.
        port_seen = self._entry_port(entry.get("entry_id"))
        if port_seen == self.mock_port:
            return "testlab"
        return "agent"

    def _entry_port(self, entry_id: Any) -> int | None:
        if not isinstance(entry_id, str) or not entry_id:
            return None
        status, diag = self.supervisor.core(
            "GET", f"/diagnostics/config_entry/{quote(entry_id, safe='')}"
        )
        if status != 200 or not isinstance(diag, dict):
            return None
        data = diag.get("data") if isinstance(diag.get("data"), dict) else diag
        config = data.get("config_entry") if isinstance(data, dict) else None
        port = config.get("port") if isinstance(config, dict) else None
        try:
            return int(port)
        except (TypeError, ValueError):
            return None

    def _ha_status(self) -> dict[str, Any]:
        sv = self.supervisor
        targets = {
            name: {
                "host": self.agent_host,
                "port": self._target_port(name),
                "entry_id": None,
                "state": None,
            }
            for name in ("agent", "testlab")
        }
        result = {"reachable": False, "integration_loaded": False, "targets": targets}
        if not sv.available or not sv.core_reachable():
            return result
        result["reachable"] = True

        status, entries = sv.core(
            "GET", f"/config/config_entries/entry?domain={INTEGRATION_DOMAIN}"
        )
        if status != 200 or not isinstance(entries, list):
            entries = []
        entries = [
            e for e in entries
            if isinstance(e, dict) and e.get("domain", INTEGRATION_DOMAIN) == INTEGRATION_DOMAIN
        ]
        for entry in entries:
            name = self._classify_entry(entry)
            if name and targets[name]["entry_id"] is None:
                targets[name]["entry_id"] = entry.get("entry_id")
                targets[name]["state"] = entry.get("state")

        # The integration counts as there when it has any entry, is set up, or
        # offers a config flow (installed through HACS, no entry yet).
        loaded = bool(entries)
        if not loaded:
            status, config = sv.core("GET", "/config")
            if status == 200 and isinstance(config, dict):
                loaded = INTEGRATION_DOMAIN in (config.get("components") or [])
        if not loaded:
            status, handlers = sv.core("GET", "/config/config_entries/flow_handlers")
            if status == 200 and isinstance(handlers, list):
                loaded = INTEGRATION_DOMAIN in handlers
        result["integration_loaded"] = loaded
        return result

    def _route_ha_status(self, _parsed) -> None:
        self._send_json(200, self._ha_status())

    # ── /ha/connect ──

    def _route_ha_connect(self, _parsed) -> None:
        try:
            body = self._read_json()
        except ApiError:
            body = None
        target = body.get("target") if isinstance(body, dict) else None
        port = self._target_port(target) if target in ("agent", "testlab") else None
        if port is None:
            self._send_json(400, {"error": 'target must be "agent" or "testlab"'})
            return

        status_now = self._ha_status()
        if not status_now["reachable"]:
            self._send_json(502, {"result": "error", "message": "unreachable"})
            return
        existing = status_now["targets"][target]["entry_id"]
        if existing:
            self._send_json(200, {"result": "already_connected", "entry_id": existing})
            return

        sv = self.supervisor
        status, flow = sv.core("POST", "/config/config_entries/flow", {
            "handler": INTEGRATION_DOMAIN,
            "show_advanced_options": False,
        })
        if status != 200 or not isinstance(flow, dict):
            message = "integration_not_loaded" if status == 404 else "flow_failed"
            self._send_json(502, {"result": "error", "message": message})
            return
        if flow.get("type") != "form" or not flow.get("flow_id"):
            self._send_json(200, self._flow_outcome(flow))
            return

        flow_id = str(flow["flow_id"])
        flow_path = f"/config/config_entries/flow/{quote(flow_id, safe='')}"
        status, step = sv.core("POST", flow_path, {
            "host": self.agent_host,
            "port": port,
            "token": self.token if target == "agent" else "",
            "scan_interval": 60,
        })
        if status != 200 or not isinstance(step, dict):
            sv.core("DELETE", flow_path)
            self._send_json(502, {"result": "error", "message": "flow_failed"})
            return
        outcome = self._flow_outcome(step)
        if step.get("type") == "form":
            sv.core("DELETE", flow_path)  # do not leave a half-finished flow behind
        if outcome["result"] == "created":
            log(f"Integration entry added for the {target} on port {port}")
        self._send_json(200, outcome)

    @staticmethod
    def _flow_outcome(step: dict[str, Any]) -> dict[str, Any]:
        kind = step.get("type")
        if kind == "create_entry":
            entry = step.get("result")
            entry_id = entry.get("entry_id") if isinstance(entry, dict) else None
            return {"result": "created", "entry_id": entry_id or step.get("entry_id")}
        if kind == "abort":
            reason = step.get("reason")
            if reason == "already_configured":
                return {"result": "already_connected"}
            return {"result": "error", "message": reason or "aborted"}
        if kind == "form":
            errors = step.get("errors") or {}
            message = errors.get("base") if isinstance(errors, dict) else None
            if not message and isinstance(errors, dict) and errors:
                message = next(iter(errors.values()))
            return {"result": "error", "message": message or "unexpected_form"}
        return {"result": "error", "message": f"unexpected_{kind or 'reply'}"}

    # ── /ha/disconnect ──

    def _route_ha_disconnect(self, _parsed) -> None:
        try:
            body = self._read_json()
        except ApiError:
            body = None
        target = body.get("target") if isinstance(body, dict) else None
        if target != "testlab":
            self._send_json(400, {"error": "only the Test Lab can be disconnected here"})
            return
        status_now = self._ha_status()
        if not status_now["reachable"]:
            self._send_json(502, {"result": "error", "message": "unreachable"})
            return
        entry_id = status_now["targets"]["testlab"]["entry_id"]
        if not entry_id:
            self._send_json(200, {"result": "not_connected"})
            return
        status, _ = self.supervisor.core(
            "DELETE", f"/config/config_entries/entry/{quote(str(entry_id), safe='')}"
        )
        if status != 200:
            self._send_json(502, {"result": "error", "message": "delete_failed"})
            return
        log("Integration entry for the Test Lab removed")
        self._send_json(200, {"result": "removed", "entry_id": entry_id})

    # ── /ha/verdict ──

    def _route_ha_verdict(self, parsed) -> None:
        from urllib.parse import parse_qs

        serial = (parse_qs(parsed.query).get("serial") or [""])[0]
        slug = re.sub(r"[^a-z0-9]+", "_", str(serial).lower()).strip("_")
        if not slug:
            self._send_json(400, {"error": "serial is required"})
            return
        if not self.supervisor.available:
            self._send_json(502, {"error": "home assistant not reachable"})
            return
        status, states = self.supervisor.core("GET", "/states")
        if status != 200 or not isinstance(states, list):
            self._send_json(502, {"error": "home assistant not reachable"})
            return

        matches = [
            s for s in states
            if isinstance(s, dict)
            and str(s.get("entity_id", "")).endswith("_attention_needed")
            and slug in str(s.get("entity_id", ""))
        ]
        matches.sort(key=lambda s: s.get("state") in ("unavailable", "unknown"))
        if not matches:
            self._send_json(200, {
                "found": False, "entity_id": None, "state": None,
                "severity": None, "reasons": None,
            })
            return
        best = matches[0]
        attrs = best.get("attributes") if isinstance(best.get("attributes"), dict) else {}
        self._send_json(200, {
            "found": True,
            "entity_id": best.get("entity_id"),
            "state": best.get("state"),
            "severity": attrs.get("severity"),
            "reasons": attrs.get("reasons"),
        })

    # ── proxy and static files ──

    def _proxy_request(self, method: str, host: str, port: int, path: str,
                       inject_token: bool = True):
        """Forward a request to a backend service and relay the response."""
        try:
            # Read request body if present
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            # Build headers to forward (skip hop-by-hop)
            skip_headers = {"host", "connection", "transfer-encoding"}
            headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in skip_headers
            }
            headers["Host"] = f"{host}:{port}"

            # Inject bearer token for ingress requests (HA's ingress proxy
            # doesn't know our token, so the web UI can't authenticate).
            # This is safe: only localhost can reach this proxy.
            if inject_token and self.token and "Authorization" not in headers:
                headers["Authorization"] = f"Bearer {self.token}"

            # Make the proxied request
            conn = http.client.HTTPConnection(host, port, timeout=30)
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()

            # Relay response
            self.send_response(resp.status)
            skip_resp = {"transfer-encoding", "connection"}
            for header, value in resp.getheaders():
                if header.lower() not in skip_resp:
                    self.send_header(header, value)
            self.end_headers()

            # Stream the response body
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)

            conn.close()

        except ConnectionRefusedError:
            self.send_error(502, f"Backend unavailable on port {port}")
        except Exception as e:  # noqa: BLE001
            self.send_error(502, f"Proxy error: {type(e).__name__}")

    def _serve_static(self, path: str):
        """Serve a static file from STATIC_DIR."""
        # Default to index.html
        if path == "/" or path == "":
            path = "/index.html"

        # Security: prevent directory traversal
        safe_path = os.path.normpath(path).lstrip("/")
        file_path = os.path.join(STATIC_DIR, safe_path)

        if not file_path.startswith(STATIC_DIR):
            self.send_error(403, "Forbidden")
            return

        if not os.path.isfile(file_path):
            # Fall back to index.html for SPA routing
            file_path = os.path.join(STATIC_DIR, "index.html")
            if not os.path.isfile(file_path):
                self.send_error(404, "Not Found")
                return

        # Determine content type
        content_type, _ = mimetypes.guess_type(file_path)
        if content_type is None:
            content_type = "application/octet-stream"

        try:
            with open(file_path, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            # Cache static assets (except index.html)
            if safe_path != "index.html":
                self.send_header("Cache-Control", "public, max-age=86400")
            else:
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)

        except IOError:
            self.send_error(500, "Internal Server Error")


def main():
    parser = argparse.ArgumentParser(description="SMART Sniffer Web UI Proxy")
    parser.add_argument("--port", type=int, default=8099, help="Port to listen on")
    parser.add_argument("--agent-port", type=int, default=9099, help="This app's agent port")
    parser.add_argument("--mock-port", type=int, default=9100, help="Replica agent port")
    parser.add_argument("--token", type=str, default="", help="Bearer token for agent auth")
    parser.add_argument("--app-version", type=str, default=os.environ.get("APP_VERSION") or None,
                        help="App version shown on the page (default: $APP_VERSION)")
    parser.add_argument("--rules-dir", type=str, default=DEFAULT_RULES_DIR,
                        help="Directory holding smart_sniffer/attention.py; VERSION sits beside it")
    parser.add_argument("--supervisor-url", type=str, default="http://supervisor",
                        help="Supervisor API base URL")
    parser.add_argument("--agent-host", type=str, default=DEFAULT_AGENT_HOST,
                        help="Hostname Home Assistant uses to reach this app's agents "
                             "(run.sh passes the container hostname)")
    parser.add_argument("--mdns-name", type=str, default="",
                        help="The agent's mDNS name, shown on the page (empty: none)")
    args = parser.parse_args()

    ProxyHandler.agent_port = args.agent_port
    ProxyHandler.mock_port = args.mock_port
    ProxyHandler.token = args.token
    ProxyHandler.app_version = args.app_version
    ProxyHandler.agent_host = args.agent_host.strip() or DEFAULT_AGENT_HOST
    ProxyHandler.mdns_name = args.mdns_name.strip() or None
    ProxyHandler.rules = Rules(args.rules_dir)
    ProxyHandler.supervisor = Supervisor(
        args.supervisor_url, os.environ.get("SUPERVISOR_TOKEN", "")
    )

    server = ThreadingHTTPServer(("0.0.0.0", args.port), ProxyHandler)
    server.daemon_threads = True
    log(f"Serving on port {args.port}")
    log(f"Proxying /api/* -> 127.0.0.1:{args.agent_port}")
    log(f"Proxying /mock/* -> 127.0.0.1:{args.mock_port}")
    log(f"Integration rules: {ProxyHandler.rules.version or 'not found'}")
    log(f"Agent host for the integration: {ProxyHandler.agent_host}")
    if not ProxyHandler.supervisor.available:
        log("No Supervisor token: field agents cannot be stored and Home Assistant routes are off")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
