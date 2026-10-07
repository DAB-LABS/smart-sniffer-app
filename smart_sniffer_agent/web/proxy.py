#!/usr/bin/env python3
"""SMART Sniffer App: Web UI proxy server.

Serves static files from /opt/web/ at the root path and proxies:
  /api/*  -> localhost:$AGENT_PORT  (this app's Go agent, app token injected)
  /mock/* -> localhost:$MOCK_PORT   (the replica agent, /mock/ rewritten to /api/)

It also answers the Control Center's own routes:
  GET  /app/info                 versions, ports, identity, network, replicas
  POST /evaluate                 the verdict (and view) for a drive, or for a
                                 ZFS pool ({"pool": {...}})
  POST /fleet/scan               scan a block for agents; GET and DELETE
                                 /fleet/scan/<id> poll and cancel it
  POST /fleet/probe              check one host by hand
  GET, POST /fleet/hosts         the field agents stored in the app options;
  PATCH, DELETE /fleet/hosts/<id>  password, hidden, removal
  GET  /fleet/hosts/<id>/drives  one judged read of self, replicas or a host
  GET  /ha/devices               entries and device pages in Home Assistant
  POST /ha/add                   add self, the replicas or a host to it
  GET, POST /replicas            replica files: the list, an upload
  POST /replicas/add             add replica cards from files
  POST /replicas/save-as         save a real drive as a replica file
  GET  /replicas/<card>/file     download a replica as it reads now
  DELETE /replicas/<card>        take a replica off the page

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
from concurrent.futures import ThreadPoolExecutor
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
MAX_REPLICA_BYTES = 1_048_576  # a replica file, as the replica agent limits it

# Matches the titles the integration's config flow gives its entries:
#   user step:     "SMART Sniffer (<host>:<port>)"
#   zeroconf step: "SMART Sniffer (<hostname>)"
_TITLE_RE = re.compile(r"^SMART Sniffer \((?P<inner>.+)\)$")

INTEGRATION_URL = "/config/integrations/integration/smart_sniffer"

# Every device of the integration with its id, name, serial number,
# identifiers and config entries, printed as JSON by Core's template
# endpoint. The REST API has no device registry list; the websocket API does,
# but this proxy has no websocket client. Sets become lists for to_json.
DEVICES_TEMPLATE = (
    "{%- set ns = namespace(ids=[]) -%}"
    "{%- for e in integration_entities('smart_sniffer') -%}"
    "{%- set d = device_id(e) -%}"
    "{%- if d and d not in ns.ids -%}{%- set ns.ids = ns.ids + [d] -%}{%- endif -%}"
    "{%- endfor -%}"
    "[{%- for d in ns.ids -%}"
    '{"id": {{ d | to_json }}, '
    '"name": {{ device_attr(d, \'name\') | to_json }}, '
    '"serial_number": {{ device_attr(d, \'serial_number\') | to_json }}, '
    '"identifiers": {{ (device_attr(d, \'identifiers\') or []) | list | map(\'list\') | list | to_json }}, '
    '"config_entries": {{ (device_attr(d, \'config_entries\') or []) | list | to_json }}}'
    "{{ ',' if not loop.last else '' }}"
    "{%- endfor -%}]"
)


def log(message: str) -> None:
    print(f"[webui] {message}", flush=True)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ApiError(Exception):
    """An error answer: {"error": code, "message": text} with an HTTP status."""

    def __init__(self, status: int, code: str, message: str | None = None,
                 **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


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


# ── Field agents: addresses, probing and the scan ───────────────────────────

SCAN_WORKERS = 64
SCAN_CONNECT_TIMEOUT = 0.4
SCAN_READ_TIMEOUT = 1.0
PROBE_TIMEOUT = 3.0
SCAN_KEEP_SECONDS = 600  # a finished scan can be read for 10 minutes
MIN_SCAN_PREFIX = 22  # wider blocks are refused with a suggestion
LOOKUP_BUDGET = 1.0  # seconds for one reverse or forward name lookup
RESERVED_HOST_IDS = ("self", "replicas")

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


def valid_address(text: Any) -> str | None:
    """An IPv4 literal or a host name, lower-cased; None when it is neither."""
    if not isinstance(text, str):
        return None
    text = text.strip().lower()
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        pass
    if re.fullmatch(r"[0-9.]+", text):
        return None  # looks like an address but is not one
    return text if _HOSTNAME_RE.match(text) else None


def valid_port(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and 1 <= value <= 65535:
        return value
    return None


def host_id(address: str, port: int) -> str:
    return f"{address.lower()}:{port}"


def _with_budget(func, *args, budget: float = LOOKUP_BUDGET) -> Any:
    """Run a blocking lookup in a thread; None when it takes longer than the
    budget or fails."""
    box: list[Any] = []

    def run() -> None:
        try:
            box.append(func(*args))
        except (OSError, UnicodeError, ValueError):
            pass

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(budget)
    return box[0] if box else None


def resolve_ipv4(name: str, budget: float = LOOKUP_BUDGET) -> str | None:
    """The first IPv4 address of a name (or the literal itself)."""
    try:
        return str(ipaddress.IPv4Address(name))
    except ValueError:
        pass
    infos = _with_budget(socket.getaddrinfo, name, None, socket.AF_INET, socket.SOCK_STREAM,
                         budget=budget)
    for info in infos or []:
        return str(info[4][0])
    return None


_IPV4_CACHE: dict[str, tuple[float, str | None]] = {}


def cached_ipv4(name: str) -> str | None:
    """resolve_ipv4(), remembered for five minutes."""
    hit = _IPV4_CACHE.get(name)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    address = resolve_ipv4(name)
    _IPV4_CACHE[name] = (time.monotonic(), address)
    return address


def reverse_name(address: str) -> str | None:
    """A short host name for an address, within the lookup budget."""
    found = _with_budget(socket.gethostbyaddr, address)
    if not found:
        return None
    name = str(found[0]).split(".")[0]
    return name or None


def _is_agent_health(reply: AgentReply) -> bool:
    body = reply.body
    return (
        reply.status == 200
        and isinstance(body, dict)
        and isinstance(body.get("version"), str)
        and isinstance(body.get("endpoints"), list)
        and "/api/drives" in body["endpoints"]
    )


def _wants_password(reply: AgentReply) -> bool:
    """A 401 with the agent's own body. The real agent leaves /api/health
    public; the mock agent run with --token does not, and is still an agent."""
    return reply.status == 401 and isinstance(reply.body, dict) and reply.body.get("error") == "unauthorized"


def probe(address: str, port: int, connect_timeout: float, read_timeout: float,
          connect_to: str | None = None) -> tuple[str, dict[str, Any] | None]:
    """Is there an agent on address:port? Returns ("ok", scan row),
    ("not_answering", None) or ("not_an_agent", None). Sends GET /api/health
    and GET /api/drives only, never with credentials."""
    target = connect_to or address
    health = agent_call(target, port, "GET", "/api/health",
                        connect_timeout=connect_timeout, read_timeout=read_timeout)
    if health.status is None:
        return "not_answering", None
    body: dict[str, Any] = {}
    if _is_agent_health(health):
        body = health.body
        drives = agent_call(target, port, "GET", "/api/drives",
                            connect_timeout=connect_timeout, read_timeout=read_timeout)
        if drives.status is None:
            return "not_answering", None
        auth = "password" if drives.status == 401 else "open"
        count = len(drives.body) if drives.status == 200 and isinstance(drives.body, list) else None
    elif _wants_password(health):
        auth, count = "password", None
    else:
        return "not_an_agent", None
    if count is None and isinstance(body.get("drives"), int):
        count = body["drives"]
    endpoints = body.get("endpoints") if isinstance(body.get("endpoints"), list) else []
    pools = body.get("pools") if isinstance(body.get("pools"), int) else (
        0 if body and "/api/pools" not in endpoints else None)
    hostname = body.get("hostname") if isinstance(body.get("hostname"), str) else None
    if not hostname:
        try:
            ipaddress.IPv4Address(address)
            hostname = reverse_name(address)
        except ValueError:
            hostname = address.split(".")[0]
    return "ok", {
        "id": host_id(address, port), "address": address, "port": port,
        "hostname": hostname or address,
        "version": body.get("version") if isinstance(body.get("version"), str) else None,
        "drives": count, "pools": pools, "auth": auth, "added": False,
    }


def parse_block(text: Any) -> ipaddress.IPv4Network:
    """An IPv4 CIDR block (a bare address is a /32); ApiError 400 otherwise."""
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "bad_block", "the block must be an IPv4 address or CIDR block")
    try:
        net = ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        raise ApiError(400, "bad_block", "the block must be an IPv4 address or CIDR block") from None
    if net.version != 4:
        raise ApiError(400, "bad_block", "only IPv4 blocks can be scanned")
    return net  # type: ignore[return-value]


class Scan:
    def __init__(self, scan_id: str, block: ipaddress.IPv4Network, port: int) -> None:
        self.id = scan_id
        self.block = block
        self.port = port
        hosts = list(block.hosts()) if block.prefixlen < 31 else list(block)
        self.addresses = [str(a) for a in hosts]
        self.total = len(self.addresses)
        self.probed = 0
        self.found: list[dict[str, Any]] = []
        self.state = "running"
        self.started_at = utc_now()
        self.finished_at: str | None = None
        self.finished_mono: float | None = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "scan_id": self.id, "block": str(self.block), "port": self.port,
                "state": self.state, "probed": self.probed, "total": self.total,
                "found": [dict(row) for row in self.found],
                "started_at": self.started_at, "finished_at": self.finished_at,
            }

    def finish(self, state: str) -> None:
        with self.lock:
            if self.state == "running":
                self.state = state
                self.finished_at = utc_now()
                self.finished_mono = time.monotonic()


class ScanManager:
    """One scan at a time; a new one cancels the running one."""

    def __init__(self) -> None:
        self.scans: dict[str, Scan] = {}
        self.lock = threading.Lock()

    def _purge(self) -> None:
        now = time.monotonic()
        for scan_id, scan in list(self.scans.items()):
            if scan.finished_mono is not None and now - scan.finished_mono > SCAN_KEEP_SECONDS:
                del self.scans[scan_id]

    def get(self, scan_id: str) -> Scan | None:
        with self.lock:
            self._purge()
            return self.scans.get(scan_id)

    def start(self, block: ipaddress.IPv4Network, port: int, skip: str | None,
              stored: list[dict[str, Any]]) -> Scan:
        scan = Scan(os.urandom(4).hex(), block, port)
        with self.lock:
            self._purge()
            for other in self.scans.values():
                if other.state == "running":
                    other.cancel.set()
                    other.finish("cancelled")
            self.scans[scan.id] = scan
        threading.Thread(target=self._run, args=(scan, skip, stored), daemon=True).start()
        return scan

    def cancel(self, scan_id: str) -> Scan | None:
        scan = self.get(scan_id)
        if scan is not None:
            scan.cancel.set()
            scan.finish("cancelled")
        return scan

    def _run(self, scan: Scan, skip: str | None, stored: list[dict[str, Any]]) -> None:
        added = stored_keys(stored)

        def one(address: str) -> None:
            if scan.cancel.is_set():
                return
            row = None
            if address != skip:
                outcome, row = probe(address, scan.port, SCAN_CONNECT_TIMEOUT, SCAN_READ_TIMEOUT)
                if outcome != "ok":
                    row = None
            with scan.lock:
                if scan.state != "running":
                    return
                scan.probed += 1
                if row is not None:
                    row["added"] = (row["address"], row["port"]) in added
                    scan.found.append(row)

        pool = ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="scan")
        try:
            for address in scan.addresses:
                if scan.cancel.is_set():
                    break
                pool.submit(one, address)
        finally:
            pool.shutdown(wait=not scan.cancel.is_set(), cancel_futures=scan.cancel.is_set())
        scan.finish("cancelled" if scan.cancel.is_set() else "done")


def stored_keys(stored: list[dict[str, Any]]) -> set[tuple[str, int]]:
    """(address, port) pairs a scan row is compared with: each stored host's
    address, and the IPv4 address its name resolves to."""
    keys: set[tuple[str, int]] = set()
    for host in stored:
        address, port = host["address"], host["port"]
        keys.add((address, port))
        resolved = resolve_ipv4(address)
        if resolved:
            keys.add((resolved, port))
    return keys


# ── Field agents: stored hosts and the last-good cache ──────────────────────

class FleetCache:
    """/data/fleet-cache.json: per host the last good response, its time,
    when the host was added and since when it is hidden. Written atomically."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.hosts: dict[str, dict[str, Any]] = {}
        self._warned = False
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                hosts = data.get("hosts") if isinstance(data, dict) else None
                if isinstance(hosts, dict):
                    self.hosts = {k: v for k, v in hosts.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                pass

    def get(self, key: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.hosts.get(key) or {})

    def update(self, key: str, **fields: Any) -> None:
        with self.lock:
            entry = self.hosts.setdefault(key, {})
            entry.update(fields)
            self._save()

    def drop(self, key: str) -> None:
        with self.lock:
            if self.hosts.pop(key, None) is not None:
                self._save()

    def _save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "hosts": self.hosts}, handle)
            os.replace(tmp, self.path)
        except OSError as err:
            if not self._warned:
                log(f"Cannot write {self.path}: {err.strerror or err}; the cache stays in memory")
                self._warned = True


def _normalize_hosts(raw: Any) -> list[dict[str, Any]]:
    hosts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        address = valid_address(item.get("address"))
        port = valid_port(item.get("port"))
        if address is None or port is None or host_id(address, port) in seen:
            continue
        seen.add(host_id(address, port))
        password = item.get("password")
        hosts.append({
            "address": address, "port": port,
            "password": password if isinstance(password, str) else "",
            "hidden": item.get("hidden") is True,
        })
    return hosts


def _serialize_host(host: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"address": host["address"], "port": host["port"]}
    if host.get("password"):
        out["password"] = host["password"]
    if host.get("hidden"):
        out["hidden"] = True
    return out


class HostStore:
    """The field agents, kept in the app's options (the source of truth).

    The Supervisor's options call replaces the whole object, so every write
    takes the process lock, reads the current options, replaces hosts only
    and posts them back: one write per user action. Writing options does not
    restart the app."""

    MEMO_SECONDS = 5

    def __init__(self, supervisor: Supervisor) -> None:
        self.sv = supervisor
        self.lock = threading.Lock()
        self.last_known: list[dict[str, Any]] | None = None
        self._memo_at = 0.0

    def _options(self) -> dict[str, Any]:
        if not self.sv.available:
            raise ApiError(503, "supervisor_unavailable", "the Supervisor is not available")
        status, info = self.sv.call("GET", "/addons/self/info")
        data = info.get("data") if isinstance(info, dict) else None
        options = data.get("options") if isinstance(data, dict) else None
        if status != 200 or not isinstance(options, dict):
            raise ApiError(502, "supervisor_failed", "could not read the app options")
        return options

    def hosts(self) -> list[dict[str, Any]]:
        hosts = _normalize_hosts(self._options().get("hosts"))
        self.last_known = hosts
        self._memo_at = time.monotonic()
        return hosts

    def hosts_quick(self) -> list[dict[str, Any]]:
        """The stored hosts, read again at most every MEMO_SECONDS; the last
        known list (or none) when the Supervisor cannot be read."""
        if self.last_known is not None and time.monotonic() - self._memo_at < self.MEMO_SECONDS:
            return self.last_known
        try:
            return self.hosts()
        except ApiError:
            return self.last_known or []

    def find(self, key: str) -> dict[str, Any] | None:
        return next((h for h in self.hosts_quick() if host_id(h["address"], h["port"]) == key), None)

    def write(self, change) -> list[dict[str, Any]]:
        """Apply change() to the stored hosts and save them in one options
        write. change() returning None means nothing to save."""
        with self.lock:
            options = self._options()
            current = _normalize_hosts(options.get("hosts"))
            hosts = change([dict(h) for h in current])
            if hosts is None:
                self.last_known = current
                self._memo_at = time.monotonic()
                return current
            options = {**options, "hosts": [_serialize_host(h) for h in hosts]}
            status, reply = self.sv.call("POST", "/addons/self/options", {"options": options})
            if status != 200:
                message = reply.get("message") if isinstance(reply, dict) else None
                raise ApiError(502, "supervisor_failed", message or "could not save the app options")
            self.last_known = hosts
            self._memo_at = time.monotonic()
            log(f"Field agents saved ({len(hosts)} stored)")
            return hosts


# ── Reading an agent's drives ───────────────────────────────────────────────

AGENT_HEALTH_TIMEOUT = 3.0
AGENT_READ_TIMEOUT = 5.0
DRIVE_READERS = 8


def summarize(items: list[dict[str, Any]], ok: bool) -> dict[str, Any]:
    """The host header (section 2) and the counts behind it."""
    drives = [i for i in items if i.get("type") == "drive"]
    pools = [i for i in items if i.get("type") == "pool"]
    states = [((i.get("verdict") or {}).get("state")) for i in drives]
    pool_states = [((i.get("verdict") or {}).get("state")) for i in pools
                   if not (i.get("view") or {}).get("vanished")]
    attention = states.count("YES") + pool_states.count("YES")
    maybe = states.count("MAYBE")
    unsupported = states.count("UNSUPPORTED")
    judged = any(s in ("YES", "MAYBE", "NO") for s in states + pool_states)
    if not ok:
        header = "unknown"
    elif attention:
        header = "attention"
    elif maybe:
        header = "maybe"
    elif judged:
        header = "ok"
    else:
        header = "unknown"
    return {"header": header, "attention": attention, "maybe": maybe,
            "unsupported": unsupported, "drives": len(drives), "pools": len(pools)}


def judge_drive(rules: Rules, drive: dict[str, Any], rules_ok: bool) -> tuple[Any, Any]:
    if not rules_ok:
        return None, drive_view(drive, None)
    work = dict(drive)
    verdict = rules.evaluate(work)
    return verdict, drive_view(work, verdict)


def judge_pool(rules: Rules, pool: dict[str, Any], lab: dict[str, Any] | None,
               rules_ok: bool) -> tuple[Any, Any]:
    verdict = None
    if rules_ok and rules.pool_health is not None:
        verdict = pool_verdict(pool, rules)
    return verdict, pool_view(pool, verdict, lab)


# ── Request handler ─────────────────────────────────────────────────────────

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
    host_store: HostStore = HostStore(supervisor)
    cache: FleetCache = FleetCache(None)
    scans: ScanManager = ScanManager()
    ha_cache_seconds: float = HA_API_CACHE_SECONDS
    _ha_cache: tuple[float, dict[str, Any]] | None = None

    # The routes answered by the proxy itself: (method, path pattern, handler
    # name). Path parameters are named groups, percent-decoded before use.
    ROUTES: list[tuple[str, str, str]] = [
        ("GET", r"/app/info", "_route_app_info"),
        ("POST", r"/evaluate", "_route_evaluate"),
        ("POST", r"/fleet/scan", "_route_scan_start"),
        ("GET", r"/fleet/scan/(?P<scan_id>[^/]+)", "_route_scan_get"),
        ("DELETE", r"/fleet/scan/(?P<scan_id>[^/]+)", "_route_scan_cancel"),
        ("POST", r"/fleet/probe", "_route_probe"),
        ("GET", r"/fleet/hosts", "_route_hosts_list"),
        ("POST", r"/fleet/hosts", "_route_hosts_add"),
        ("PATCH", r"/fleet/hosts/(?P<key>[^/]+)", "_route_host_patch"),
        ("DELETE", r"/fleet/hosts/(?P<key>[^/]+)", "_route_host_delete"),
        ("GET", r"/fleet/hosts/(?P<key>[^/]+)/drives", "_route_host_drives"),
        ("GET", r"/ha/devices", "_route_ha_devices"),
        ("POST", r"/ha/add", "_route_ha_add"),
        ("GET", r"/replicas", "_route_replicas_list"),
        ("POST", r"/replicas", "_route_replicas_upload"),
        ("POST", r"/replicas/add", "_route_replicas_add"),
        ("POST", r"/replicas/save-as", "_route_replicas_save_as"),
        ("GET", r"/replicas/(?P<card_id>[^/]+)/file", "_route_replica_file"),
        ("DELETE", r"/replicas/(?P<card_id>[^/]+)", "_route_replica_delete"),
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

    # ── /fleet/scan, /fleet/probe ──

    def _route_scan_start(self, _parsed) -> None:
        body = self._read_json()
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "body must be an object")
        block = parse_block(body.get("block"))
        port = valid_port(body.get("port", 9099))
        if port is None:
            raise ApiError(400, "bad_port", "the port must be 1 to 65535")
        network = self.supervisor.network()
        if block.prefixlen < MIN_SCAN_PREFIX:
            suggest_from = network["address"] if network else str(block.network_address)
            suggest = ipaddress.ip_network(f"{suggest_from}/24", strict=False)
            raise ApiError(422, "too_wide", f"blocks wider than /{MIN_SCAN_PREFIX} are not scanned",
                           addresses=2 ** (32 - block.prefixlen) - 2, suggest=str(suggest))
        scan = self.scans.start(block, port, network["address"] if network else None,
                                self.host_store.hosts_quick())
        log(f"Scan {scan.id}: {block} port {port}, {scan.total} addresses")
        self._send_json(202, {"scan_id": scan.id, "total": scan.total})

    def _route_scan_get(self, _parsed, scan_id: str) -> None:
        scan = self.scans.get(scan_id)
        if scan is None:
            raise ApiError(404, "not_found", "no such scan, or it has expired")
        self._send_json(200, scan.snapshot())

    def _route_scan_cancel(self, _parsed, scan_id: str) -> None:
        if self.scans.cancel(scan_id) is None:
            raise ApiError(404, "not_found", "no such scan, or it has expired")
        self._send_json(200, {"state": "cancelled"})

    def _route_probe(self, _parsed) -> None:
        body = self._read_json()
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "body must be an object")
        address = valid_address(body.get("host"))
        port = valid_port(body.get("port", 9099))
        if address is None or port is None:
            raise ApiError(400, "bad_request", "host must be an IPv4 address or a host name, "
                                               "port 1 to 65535")
        connect_to = resolve_ipv4(address, budget=PROBE_TIMEOUT)
        if connect_to is None:
            raise ApiError(400, "cannot_resolve", f"{address} does not resolve")
        outcome, row = probe(address, port, PROBE_TIMEOUT, PROBE_TIMEOUT, connect_to=connect_to)
        if outcome == "not_answering":
            raise ApiError(504, "not_answering", f"nothing answered on {address}, port {port}")
        if row is None:
            raise ApiError(404, "not_an_agent", f"{address}, port {port} is not a SMART Sniffer agent")
        stored = self.host_store.hosts_quick()
        keys = {(h["address"], h["port"]) for h in stored}
        row["added"] = (address, port) in keys or (
            (connect_to, port) in stored_keys(stored))
        self._send_json(200, row)

    # ── /fleet/hosts ──

    def _host_object(self, host: dict[str, Any]) -> dict[str, Any]:
        key = host_id(host["address"], host["port"])
        cached = self.cache.get(key)
        return {
            "id": key, "address": host["address"], "port": host["port"],
            "has_password": bool(host.get("password")), "hidden": bool(host.get("hidden")),
            "hidden_since": cached.get("hidden_since") if host.get("hidden") else None,
            "added_at": cached.get("added_at"),
        }

    def _route_hosts_list(self, _parsed) -> None:
        hosts = self.host_store.hosts()
        self._send_json(200, {"hosts": [self._host_object(h) for h in hosts]})

    def _route_hosts_add(self, _parsed) -> None:
        body = self._read_json()
        items = body.get("hosts") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items:
            raise ApiError(400, "bad_request", 'give {"hosts": [{"address", "port"}, ...]}')
        wanted: list[dict[str, Any]] = []
        for item in items:
            address = valid_address(item.get("address")) if isinstance(item, dict) else None
            port = valid_port(item.get("port", 9099)) if isinstance(item, dict) else None
            if address is None or port is None:
                raise ApiError(400, "bad_request",
                               "each host needs an IPv4 address or host name and a port 1 to 65535")
            wanted.append({"address": address, "port": port, "password": "", "hidden": False})

        added: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        def change(hosts: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            added.clear()
            skipped.clear()
            known = {host_id(h["address"], h["port"]) for h in hosts}
            for host in wanted:
                key = host_id(host["address"], host["port"])
                if key in known:
                    skipped.append({"address": host["address"], "port": host["port"],
                                    "reason": "already_added"})
                    continue
                known.add(key)
                added.append(host)
            return hosts + added if added else None  # nothing new: no write

        self.host_store.write(change)
        now = utc_now()
        for host in added:
            self.cache.update(host_id(host["address"], host["port"]), added_at=now,
                              hidden_since=None)
        self._send_json(201, {"added": [self._host_object(h) for h in added], "skipped": skipped})

    def _route_host_patch(self, _parsed, key: str) -> None:
        body = self._read_json()
        if not isinstance(body, dict) or not ({"password", "hidden"} & set(body)):
            raise ApiError(400, "bad_request", 'give "password" and/or "hidden"')
        password = body.get("password")
        hidden = body.get("hidden")
        if "password" in body and not isinstance(password, str):
            raise ApiError(400, "bad_request", "password must be a string")
        if "hidden" in body and not isinstance(hidden, bool):
            raise ApiError(400, "bad_request", "hidden must be true or false")
        host = next((h for h in self.host_store.hosts()
                     if host_id(h["address"], h["port"]) == key), None)
        if host is None:
            raise ApiError(404, "not_found", "no such field agent")
        if password:
            check = agent_call(host["address"], host["port"], "GET", "/api/drives",
                               token=password, connect_timeout=AGENT_HEALTH_TIMEOUT,
                               read_timeout=AGENT_READ_TIMEOUT)
            if check.status is None:
                raise ApiError(504, "not_answering", "the agent did not answer")
            if check.status == 401:
                raise ApiError(422, "password_refused", "the agent refused that password")
            if check.status != 200:
                raise ApiError(502, "agent_failed", f"the agent answered HTTP {check.status}")

        def change(hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for item in hosts:
                if host_id(item["address"], item["port"]) == key:
                    if "password" in body:
                        item["password"] = password
                    if "hidden" in body:
                        item["hidden"] = hidden
            return hosts

        hosts = self.host_store.write(change)
        if "hidden" in body:
            self.cache.update(key, hidden_since=utc_now() if hidden else None)
        updated = next((h for h in hosts if host_id(h["address"], h["port"]) == key), None)
        if updated is None:
            raise ApiError(404, "not_found", "no such field agent")
        self._send_json(200, {"host": self._host_object(updated)})

    def _route_host_delete(self, _parsed, key: str) -> None:
        if not any(host_id(h["address"], h["port"]) == key for h in self.host_store.hosts()):
            raise ApiError(404, "not_found", "no such field agent")
        self.host_store.write(
            lambda hosts: [h for h in hosts if host_id(h["address"], h["port"]) != key])
        self.cache.drop(key)
        self._send_json(200, {"removed": key})

    # ── /fleet/hosts/<id>/drives ──

    def _route_host_drives(self, _parsed, key: str) -> None:
        if key == "self":
            target = ("self", "127.0.0.1", self.agent_port, self.token)
        elif key == "replicas":
            target = ("replicas", "127.0.0.1", self.mock_port, "")
        else:
            host = self.host_store.find(key)
            if host is None:
                raise ApiError(404, "not_found", "no such field agent")
            target = ("field", host["address"], host["port"], host.get("password") or "")
        self._send_json(200, self._read_agent(key, *target))

    def _agent_hostname(self, kind: str, address: str, health: dict[str, Any]) -> str | None:
        name = health.get("hostname")
        if isinstance(name, str) and name and kind == "field":
            return name
        if kind != "field":
            return self.agent_host
        try:
            ipaddress.IPv4Address(address)
        except ValueError:
            return address.split(".")[0]
        return reverse_name(address) or address

    def _read_agent(self, key: str, kind: str, address: str, port: int, token: str) -> dict[str, Any]:
        """One read of an agent: health, drive list, each drive, pools; every
        drive and pool judged. On failure the last good read, marked stale."""
        def call(path: str, timeout: float = AGENT_READ_TIMEOUT) -> AgentReply:
            return agent_call(address, port, "GET", path, token=token,
                              connect_timeout=AGENT_HEALTH_TIMEOUT, read_timeout=timeout)

        health = call("/api/health", AGENT_HEALTH_TIMEOUT)
        if health.status == 401:
            return self._stale(key, kind, "password_refused" if token else "password_needed")
        if not _is_agent_health(health):
            return self._stale(key, kind, "unreachable")
        info = health.body

        rules_ok = self.rules.load()
        lab_drives: dict[str, dict[str, Any]] = {}
        lab_pools: list[dict[str, Any]] | None = None
        failed_ids: set[str] = set()
        if kind == "replicas":
            state = call("/api/state")
            if state.status != 200 or not isinstance(state.body, dict):
                return self._stale(key, kind, "unreachable")
            drives = [d for d in state.body.get("drives") or [] if isinstance(d, dict)]
            lab_pools = [p for p in state.body.get("pools") or [] if isinstance(p, dict)]
            payloads: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
                ({k: v for k, v in d.items() if k != "lab"}, d.get("lab")) for d in drives]
            for drive, lab in payloads:
                lab_drives[str(drive.get("id"))] = lab or {}
        else:
            listing = call("/api/drives")
            if listing.status == 401:
                return self._stale(key, kind, "password_refused" if token else "password_needed")
            if listing.status != 200 or not isinstance(listing.body, list):
                return self._stale(key, kind, "unreachable")
            summaries = [d for d in listing.body if isinstance(d, dict) and d.get("id")]

            def read_one(summary: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
                reply = call(f"/api/drives/{quote(str(summary['id']), safe='')}")
                if reply.status == 200 and isinstance(reply.body, dict):
                    return reply.body, None
                return summary, None

            with ThreadPoolExecutor(max_workers=DRIVE_READERS) as pool:
                payloads = list(pool.map(read_one, summaries))
            # A drive whose read failed is kept as its summary, unjudged.
            failed_ids = {str(d.get("id")) for d, _ in payloads if "smart_data" not in d}

        items: list[dict[str, Any]] = []
        for drive, lab in payloads:
            drive_id = str(drive.get("id"))
            if kind != "replicas" and drive_id in failed_ids:
                items.append({"type": "drive", "id": drive_id, "drive": drive, "verdict": None,
                              "view": None, "lab": None, "error": "not_answering"})
                continue
            verdict, view = judge_drive(self.rules, drive, rules_ok)
            items.append({"type": "drive", "id": drive_id, "drive": drive, "verdict": verdict,
                          "view": view, "lab": lab if kind == "replicas" else None})

        endpoints = info.get("endpoints") or []
        pools_status = "none"
        pool_list: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        if lab_pools is not None:
            pools_status = "ok"
            pool_list = [({k: v for k, v in p.items() if k != "lab"}, p.get("lab")) for p in lab_pools]
        elif "/api/pools" in endpoints:
            reply = call("/api/pools")
            if reply.status == 200 and isinstance(reply.body, list):
                pools_status = "ok"
                pool_list = [(p, None) for p in reply.body if isinstance(p, dict) and p.get("name")]
            else:
                pools_status = "failed"
        for pool_payload, lab in pool_list:
            verdict, view = judge_pool(self.rules, pool_payload, lab, rules_ok)
            items.append({"type": "pool", "id": f"pool.{pool_payload.get('name')}",
                          "pool": pool_payload, "verdict": verdict, "view": view, "lab": lab})

        now = utc_now()
        response = {
            "id": key, "kind": kind, "status": "ok", "read_at": now, "last_answer": now,
            "agent": {
                "hostname": self._agent_hostname(kind, address, info),
                "version": info.get("version"), "os": info.get("os"),
                "os_name": info.get("os_name"), "uptime_seconds": info.get("uptime_seconds"),
                "drives": sum(1 for i in items if i["type"] == "drive"),
                "pools": sum(1 for i in items if i["type"] == "pool"),
            },
            "summary": summarize(items, True),
            "items": items,
            "pools_status": pools_status,
            "rules_version": self.rules.version if rules_ok else None,
            "stale": False,
        }
        self.cache.update(key, response=response, last_answer=now)
        return response

    def _stale(self, key: str, kind: str, status: str) -> dict[str, Any]:
        """The last good read of a host that did not answer as it should."""
        cached = self.cache.get(key)
        last = cached.get("response") if isinstance(cached.get("response"), dict) else {}
        items = [{**item, "stale": True} for item in last.get("items") or []]
        return {
            "id": key, "kind": kind, "status": status, "read_at": utc_now(),
            "last_answer": cached.get("last_answer"),
            "agent": last.get("agent"),
            "summary": summarize(items, False),
            "items": items,
            "pools_status": last.get("pools_status"),
            "rules_version": last.get("rules_version") or self.rules.version,
            "stale": True,
        }

    # ── /replicas ──

    def _mock_or_down(self, method: str, path: str, body: Any = None,
                      raw_body: bytes | None = None) -> AgentReply:
        reply = self._mock(method, path, body=body, raw_body=raw_body)
        if reply.status is None:
            raise ApiError(503, "replica_agent_down", "the replica agent is not answering")
        return reply

    def _relay(self, reply: AgentReply, ok: int | None = None) -> None:
        """Answer with the replica agent's own status and JSON body."""
        body = reply.body if reply.body is not None else {
            "error": "replica_agent_failed", "message": f"HTTP {reply.status}"}
        if isinstance(body, dict) and "error" in body and "message" not in body:
            body = {**body, "message": None}
        self._send_json(ok if ok is not None and reply.status in (200, 201) else reply.status, body)

    def _route_replicas_list(self, _parsed) -> None:
        self._relay(self._mock_or_down("GET", "/api/replicas"))

    def _route_replicas_upload(self, _parsed) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise ApiError(400, "bad_request", "bad Content-Length") from None
        if length > MAX_REPLICA_BYTES + 64 * 1024:
            # Read and drop what was sent (up to a limit), so the page gets
            # the answer rather than a broken connection.
            remaining = length if length <= 16 * MAX_REPLICA_BYTES else 0
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.close_connection = True
            raise ApiError(413, "too_large", "A replica file is at most 1 MB.")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            doc = json.loads(raw)
        except ValueError:
            raise ApiError(422, "invalid_replica", "not valid JSON") from None
        if not isinstance(doc, dict):
            raise ApiError(422, "invalid_replica", "a replica file is a JSON object")
        body = doc if set(doc) == {"file"} else {"file": doc}
        self._relay(self._mock_or_down("POST", "/api/replicas", body=body))

    def _route_replicas_add(self, _parsed) -> None:
        body = self._read_json()
        files = body.get("files") if isinstance(body, dict) else None
        if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
            raise ApiError(400, "bad_request", 'give {"files": ["<file id>", ...]}')
        cards: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for file_id in files:
            reply = self._mock_or_down("POST", "/api/drives", body={"replica": file_id})
            if reply.status == 201 and isinstance(reply.body, dict):
                cards.append({"file": file_id, "card_id": self._card_id(reply.body)})
            else:
                error = reply.body.get("error") if isinstance(reply.body, dict) else None
                failed.append({"file": file_id, "error": error or f"http_{reply.status}"})
        self._send_json(201, {"cards": cards, "failed": failed})

    @staticmethod
    def _card_id(added: dict[str, Any]) -> str:
        if added.get("kind") == "pool":
            return f"pool.{added.get('name') or added.get('id')}"
        return str(added.get("id"))

    @staticmethod
    def _replica_path(card_id: str) -> str:
        if card_id.startswith("pool."):
            return f"/api/pools/{quote(card_id[len('pool.'):], safe='')}"
        return f"/api/drives/{quote(card_id, safe='')}"

    def _route_replica_file(self, _parsed, card_id: str) -> None:
        reply = self._mock_or_down("GET", self._replica_path(card_id) + "/replica")
        if reply.status != 200:
            raise ApiError(404, "not_found", "no such replica")
        disposition = reply.headers.get("content-disposition") or (
            f'attachment; filename="{re.sub(r"[^A-Za-z0-9._-]+", "-", card_id)}.json"')
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(reply.raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(reply.raw)

    def _route_replica_delete(self, _parsed, card_id: str) -> None:
        reply = self._mock_or_down("DELETE", self._replica_path(card_id))
        if reply.status != 200:
            raise ApiError(404, "not_found", "no such replica")
        self._send_json(200, {"removed": card_id})

    def _route_replicas_save_as(self, _parsed) -> None:
        body = self._read_json()
        source = body.get("host") if isinstance(body, dict) else None
        drive_id = body.get("drive_id") if isinstance(body, dict) else None
        if not isinstance(source, str) or not isinstance(drive_id, str) or not drive_id:
            raise ApiError(400, "bad_request", 'give {"host": "self" or a field agent id, "drive_id"}')
        if source == "self":
            address, port, token = "127.0.0.1", self.agent_port, self.token
        else:
            host = self.host_store.find(source)
            if host is None:
                raise ApiError(404, "not_found", "no such field agent")
            address, port, token = host["address"], host["port"], host.get("password") or ""
        reply = agent_call(address, port, "GET", f"/api/drives/{quote(drive_id, safe='')}",
                           token=token, connect_timeout=AGENT_HEALTH_TIMEOUT,
                           read_timeout=AGENT_READ_TIMEOUT)
        if reply.status is None:
            raise ApiError(504, "not_answering", "the agent did not answer")
        if reply.status == 401:
            raise ApiError(422, "password_refused" if token else "password_needed",
                           "the agent wants a password")
        if reply.status != 200 or not isinstance(reply.body, dict):
            raise ApiError(404, "not_found", "no such drive")
        drive = reply.body
        verdict = None
        if self.rules.load():
            full = self.rules.evaluate(dict(drive))
            if full["state"] == "UNSUPPORTED":
                raise ApiError(409, "unsupported_drive", "This drive gives no SMART data to copy.")
            verdict = {"state": full["state"], "severity": full["severity"],
                       "reasons": full["reasons"], "rules_version": full["rules_version"],
                       "at": utc_now()}
        saved = self._mock_or_down("POST", "/api/replicas",
                                   body={"from_drive": drive, "verdict": verdict})
        if saved.status != 201 or not isinstance(saved.body, dict):
            self._relay(saved)
            return
        entry = saved.body
        added = self._mock_or_down("POST", "/api/drives", body={"replica": entry.get("id")})
        card_id = self._card_id(added.body) if added.status == 201 and isinstance(added.body, dict) else None
        log(f"Saved a replica of drive {drive_id}")
        self._send_json(201, {"file": entry, "card_id": card_id})

    # ── /ha/devices ──

    def _own_hosts(self) -> set[str]:
        hosts = {self.agent_host.lower()}
        try:
            hosts.add(socket.gethostname().split(".")[0].lower())
        except OSError:
            pass
        return hosts

    def _entry_config(self, entry_id: Any) -> dict[str, Any] | None:
        """host and port from an entry's diagnostics, or None."""
        if not isinstance(entry_id, str) or not entry_id:
            return None
        status, diag = self.supervisor.core(
            "GET", f"/diagnostics/config_entry/{quote(entry_id, safe='')}"
        )
        if status != 200 or not isinstance(diag, dict):
            return None
        data = diag.get("data") if isinstance(diag.get("data"), dict) else diag
        config = data.get("config_entry") if isinstance(data, dict) else None
        return config if isinstance(config, dict) else None

    def _match_stored(self, host: str, port: int, stored: list[dict[str, Any]]) -> str | None:
        """The stored field agent at host:port, compared on the stored address
        and on the IPv4 address both names resolve to."""
        host = host.lower()
        candidates = [h for h in stored if h["port"] == port]
        for item in candidates:
            if item["address"] == host:
                return host_id(item["address"], item["port"])
        wanted = cached_ipv4(host)
        if wanted is None:
            return None
        for item in candidates:
            if cached_ipv4(item["address"]) == wanted:
                return host_id(item["address"], item["port"])
        return None

    def _entry_target(self, entry: dict[str, Any], stored: list[dict[str, Any]]) -> str | None:
        """Which target an entry belongs to: "self", "replicas", a stored
        host's id, or None. Decided from the title the config flow wrote, and
        for a zeroconf title (hostname only) from the entry's diagnostics."""
        match = _TITLE_RE.match(str(entry.get("title") or ""))
        if not match:
            return None
        inner = match.group("inner").strip()
        own = self._own_hosts()
        host, sep, port_text = inner.rpartition(":")
        if sep and port_text.isdigit():
            port = int(port_text)
            if host.lower() in own:
                if port == self.agent_port:
                    return "self"
                if port == self.mock_port:
                    return "replicas"
                return None
            return self._match_stored(host, port, stored)
        config = self._entry_config(entry.get("entry_id"))
        port = valid_port(config.get("port")) if config else None
        if inner.lower() in own:
            # Both of this app's agents advertise the container hostname, so
            # the port tells them apart; without it the entry is the agent's.
            return "replicas" if port == self.mock_port else "self"
        if config is None or port is None:
            return None
        found = self._match_stored(str(config.get("host") or ""), port, stored)
        return found or self._match_stored(inner, port, stored)

    def _ha_lookup(self, fresh: bool = False) -> dict[str, Any]:
        """The /ha/devices answer, cached ha_cache_seconds (30 s); dropped
        after any /ha/add."""
        cls = type(self)
        cached = cls._ha_cache
        if not fresh and cached and time.monotonic() - cached[0] < cls.ha_cache_seconds:
            return cached[1]
        sv = self.supervisor
        result: dict[str, Any] = {
            "installed": None, "reachable": False, "lookup": None,
            "integration_url": INTEGRATION_URL, "targets": {},
        }
        if sv.available and sv.core_reachable():
            entries = sv.entries() or []
            result["reachable"] = True
            result["installed"] = sv.integration_installed(entries)
            stored = self.host_store.hosts_quick()
            targets: dict[str, dict[str, Any]] = {}
            for entry in entries:
                key = self._entry_target(entry, stored)
                entry_id = entry.get("entry_id")
                if key and key not in targets and isinstance(entry_id, str):
                    targets[key] = {
                        "entry_id": entry_id,
                        "entry_url": f"{INTEGRATION_URL}#config_entry={quote(entry_id, safe='')}",
                        "state": entry.get("state"),
                        "devices": {},
                    }
            status, devices = sv.call("POST", "/core/api/template",
                                      {"template": DEVICES_TEMPLATE})
            if status == 200 and isinstance(devices, list):
                result["lookup"] = "registry"
                by_entry = {t["entry_id"]: t for t in targets.values()}
                for device in devices:
                    if not isinstance(device, dict) or not isinstance(device.get("id"), str):
                        continue
                    for entry_id in device.get("config_entries") or []:
                        target = by_entry.get(entry_id)
                        if target is None:
                            continue
                        serial = device.get("serial_number")
                        if isinstance(serial, str) and serial:
                            target["devices"][serial] = device["id"]
                        prefix = f"{entry_id}_zpool_"
                        for ident in device.get("identifiers") or []:
                            if (isinstance(ident, list) and len(ident) == 2
                                    and ident[0] == INTEGRATION_DOMAIN
                                    and str(ident[1]).startswith(prefix)):
                                target["devices"][f"pool:{str(ident[1])[len(prefix):]}"] = device["id"]
            else:
                result["lookup"] = "entries_only"
            result["targets"] = targets
        cls._ha_cache = (time.monotonic(), result)
        return result

    def _route_ha_devices(self, _parsed) -> None:
        self._send_json(200, self._ha_lookup())

    # ── /ha/add ──

    def _route_ha_add(self, _parsed) -> None:
        body = self._read_json()
        target = body.get("target") if isinstance(body, dict) else None
        if target == "self":
            host, port, token = self.agent_host, self.agent_port, self.token
        elif target == "replicas":
            host, port, token = self.agent_host, self.mock_port, ""
        elif isinstance(target, str):
            stored = next((h for h in self.host_store.hosts()
                           if host_id(h["address"], h["port"]) == target), None)
            if stored is None:
                raise ApiError(400, "bad_target", 'target must be "self", "replicas" or a field agent id')
            host, port, token = stored["address"], stored["port"], stored.get("password") or ""
        else:
            raise ApiError(400, "bad_target", 'target must be "self", "replicas" or a field agent id')
        try:
            outcome, code = self._ha_add(target, host, port, token)
        finally:
            type(self)._ha_cache = None
            self.supervisor.forget_installed()
        self._send_json(code, outcome)

    def _ha_add(self, target: str, host: str, port: int, token: str) -> tuple[dict[str, Any], int]:
        def failed(message: str) -> tuple[dict[str, Any], int]:
            return {"error": "add_failed", "result": "error", "message": message}, 502

        lookup = self._ha_lookup(fresh=True)
        if not lookup["reachable"]:
            return failed("unreachable")
        existing = lookup["targets"].get(target)
        sv = self.supervisor
        if existing:
            entry_id = existing["entry_id"]
            if target != "replicas":
                return {"result": "already_added", "entry_id": entry_id}, 200
            # All replicas share one entry: reload it so a new replica shows
            # now rather than at the next poll.
            status, _ = sv.core(
                "POST", f"/config/config_entries/entry/{quote(entry_id, safe='')}/reload"
            )
            if status != 200:
                return failed("flow_failed")
            return {"result": "reloaded", "entry_id": entry_id}, 200

        status, flow = sv.core("POST", "/config/config_entries/flow", {
            "handler": INTEGRATION_DOMAIN,
            "show_advanced_options": False,
        })
        if status != 200 or not isinstance(flow, dict):
            return failed("integration_not_loaded" if status == 404 else "flow_failed")
        if flow.get("type") != "form" or not flow.get("flow_id"):
            return self._flow_outcome(flow)

        flow_id = str(flow["flow_id"])
        flow_path = f"/config/config_entries/flow/{quote(flow_id, safe='')}"
        status, step = sv.core("POST", flow_path, {
            "host": host, "port": port, "token": token, "scan_interval": 60,
        })
        if status != 200 or not isinstance(step, dict):
            sv.core("DELETE", flow_path)
            return failed("flow_failed")
        if step.get("type") == "form":
            sv.core("DELETE", flow_path)  # do not leave a half-finished flow behind
        outcome = self._flow_outcome(step)
        if outcome[0].get("result") == "created":
            log(f"Integration entry added for {target}")
        return outcome

    @staticmethod
    def _flow_outcome(step: dict[str, Any]) -> tuple[dict[str, Any], int]:
        kind = step.get("type")
        if kind == "create_entry":
            entry = step.get("result")
            entry_id = entry.get("entry_id") if isinstance(entry, dict) else None
            return {"result": "created", "entry_id": entry_id or step.get("entry_id")}, 200
        message: Any = None
        if kind == "abort":
            reason = step.get("reason")
            if reason == "already_configured":
                return {"result": "already_added", "entry_id": None}, 200
            message = reason or "aborted"
        elif kind == "form":
            errors = step.get("errors") or {}
            message = errors.get("base") if isinstance(errors, dict) else None
            if not message and isinstance(errors, dict) and errors:
                message = next(iter(errors.values()))
            message = message or "unexpected_form"
        else:
            message = f"unexpected_{kind or 'reply'}"
        return {"error": "add_failed", "result": "error", "message": message}, 502

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
    parser.add_argument("--ha-cache-seconds", type=float, default=HA_API_CACHE_SECONDS,
                        help="How long /ha/devices is cached")
    parser.add_argument("--data-dir", type=str, default="/data",
                        help="Where fleet-cache.json is kept (empty: memory only)")
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
    ProxyHandler.host_store = HostStore(ProxyHandler.supervisor)
    ProxyHandler.ha_cache_seconds = args.ha_cache_seconds
    ProxyHandler.cache = FleetCache(
        os.path.join(args.data_dir, "fleet-cache.json") if args.data_dir else None
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
