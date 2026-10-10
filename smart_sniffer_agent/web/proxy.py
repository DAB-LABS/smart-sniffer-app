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
  GET  /cards                    the card order and the hidden cards
  PUT  /cards/order              save the card order
  PATCH /cards/<id>              hide or show one card
  PUT  /cards/<id>/name          name a card (empty for the original); a
                                 drive or pool in Home Assistant takes it
                                 as its device name (websocket API)
  PUT  /hosts/<id>/name          name a field agent's machine
  DELETE /cards/<id>             remove one card (not a drive of this machine)
  GET  /cards/<id>/file          one card as a JSON file
  GET, PUT /cards/<id>/thresholds  a drive's thresholds, from Home Assistant
                                 when the drive is in it, else the app's own;
  POST /cards/<id>/thresholds/accept  the flagged readings made normal;
  POST /cards/<id>/thresholds/reset   every override cleared

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
import base64
import hashlib
import http.client
import importlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType
from typing import Any, Callable
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
THRESHOLD_MIN_SECONDS = 5  # the least time Home Assistant's thresholds are kept
REFRESH_MINUTES = 15  # how often the page re-reads stored field agents
REPLICA_AGENT_TIMEOUT = 1.0  # seconds, /app/info's check of the replica agent
MAX_REPLICA_BYTES = 1_048_576  # a replica file, as the replica agent limits it

# Matches the titles the integration's config flow gives its entries:
#   user step:     "SMART Sniffer (<host>:<port>)"
#   zeroconf step: "SMART Sniffer (<hostname>)"
_TITLE_RE = re.compile(r"^SMART Sniffer \((?P<inner>.+)\)$")

INTEGRATION_URL = "/config/integrations/integration/smart_sniffer"

# Every device of the integration with its id, name, serial number,
# identifiers and config entry, printed as JSON by Core's template
# endpoint, with the name the user gave it in Home Assistant (name_by_user).
# The REST API has no device registry list; reading it here keeps the
# websocket (used only for renames) out of every refresh. Sets become lists
# for to_json.
# The entry comes from config_entry_id() of the device's first entity:
# device_attr(d, 'config_entries') raises on Home Assistant 2026.10, where
# DeviceEntry.config_entries is deprecated.
DEVICES_TEMPLATE = (
    "{%- set ns = namespace(ids=[], entries=[]) -%}"
    "{%- for e in integration_entities('smart_sniffer') -%}"
    "{%- set d = device_id(e) -%}"
    "{%- if d and d not in ns.ids -%}{%- set ns.ids = ns.ids + [d] -%}"
    "{%- set ns.entries = ns.entries + [config_entry_id(e)] -%}{%- endif -%}"
    "{%- endfor -%}"
    "[{%- for d in ns.ids -%}"
    '{"id": {{ d | to_json }}, '
    '"name": {{ device_attr(d, \'name\') | to_json }}, '
    '"name_by_user": {{ device_attr(d, \'name_by_user\') | to_json }}, '
    '"serial_number": {{ device_attr(d, \'serial_number\') | to_json }}, '
    '"identifiers": {{ (device_attr(d, \'identifiers\') or []) | list | map(\'list\') | list | to_json }}, '
    '"config_entries": {{ [ns.entries[loop.index0]] | select | list | to_json }}}'
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

    def evaluate(self, drive: dict[str, Any],
                 thresholds: dict[str, int] | None = None) -> dict[str, Any]:
        """The verdict the integration would give this drive, with these
        threshold overrides ({label: int}, none for the defaults) and no held
        Device Statistics readings. Puts the merged Device Statistics
        readings on the drive as _devstat, as the integration's coordinator
        does. "limits" is every threshold label of the drive as
        threshold_limits() gives it."""
        attention = self.attention
        assert attention is not None
        if self.devstat is not None:
            drive[self.devstat.DEVSTAT_KEY], _ = self.devstat.merge_devstat(drive, None)
        overrides = dict(thresholds or {})
        state, severity, reasons, accepted = attention.evaluate_attention(drive, overrides or None)
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
            "limits": threshold_limits(attention, drive, overrides),
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


# ── Thresholds ──────────────────────────────────────────────────────────────
#
# The integration's per-drive thresholds: per label, "accept up to N" (for the
# NVMe spare, "accept down to N"). Only values that differ from the default
# are stored. threshold_limits() lists a drive's labels with the reading, the
# default, the value in force and what it means for the card.

# Label -> the drive view row it belongs to, for the labels no reason table
# names (the rest come from _REASON_LABELS).
_LABEL_ROWS = {
    "SSD Wear Percent Used": "wear",
    "NVMe Media Errors": "media_errors",
    "NVMe Spare Warn Below": "available_spare",
}
_COMPARE_WORDS = {"gt": "alert above", "ge": "alert at", "lt": "alert below"}


def label_row_key(label: str) -> str | None:
    """The drive view row key a threshold label sits on."""
    if label in _LABEL_ROWS:
        return _LABEL_ROWS[label]
    found = _REASON_LABELS.get(label)
    return found[0] if found else None


def _row_name(key: str | None, label: str) -> str:
    for row_key, name, *_rest in _ATA_ROWS:
        if row_key == key:
            return name
    for row_key, name in _NVME_ROWS + _SCSI_ROWS:
        if row_key == key:
            return name
    return label


def _trips(value: int, limit: int, compare: str) -> bool:
    if compare == "ge":
        return value >= limit
    if compare == "lt":
        return value < limit
    return value > limit


def threshold_text(limit: int, default: int, compare: str, unit: str) -> str:
    """ "alert above 5, default 0", the note of a row with an override."""
    return f"{_COMPARE_WORDS.get(compare, 'alert above')} {limit}{unit}, default {default}{unit}"


def accept_value(reading: int, compare: str) -> int:
    """The threshold that accepts this reading as normal."""
    return reading + 1 if compare == "ge" else reading


def clean_overrides(raw: Any) -> dict[str, int]:
    """{label: int} from stored data; anything else is dropped."""
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for label, value in raw.items():
            if isinstance(label, str) and label and not isinstance(value, bool):
                try:
                    out[label] = int(value)
                except (TypeError, ValueError):
                    continue
    return out


def threshold_limits(attention: ModuleType, drive: dict[str, Any],
                     overrides: dict[str, int]) -> list[dict[str, Any]]:
    """Every threshold label the drive's protocol has, in the integration's
    form order: {label, slug, reading, default, value, overridden, flagged,
    accepted, compare, unit, row, name}. reading is None for a label the
    drive does not report. flagged: the reading trips the value in force.
    accepted: an override keeps a reading that the default would flag from
    being flagged. The drive must carry its _devstat (Rules.evaluate)."""
    labels = list(attention.labels_for_drive(drive))
    readings = attention.current_readings(drive)
    order = getattr(attention, "form_order", None)
    if callable(order):
        labels = list(order(labels, readings))
    values = attention.prefill_thresholds(drive, overrides)
    compares = getattr(attention, "_THRESHOLD_COMPARE", {})
    gauges = getattr(attention, "GAUGE_LABELS", frozenset())
    nvme = _smart_data(drive).get("nvme_smart_health_information_log") or {}
    out: list[dict[str, Any]] = []
    for label in labels:
        default = int(attention.default_threshold(label))
        value = int(values.get(label, default))
        compare = compares.get(label, "gt")
        reading = readings.get(label)
        flagged = reading is not None and _trips(reading, value, compare)
        if (flagged and label == "NVMe Spare Warn Below"
                and _int(nvme.get("available_spare_threshold")) is not None
                and reading <= _int(nvme.get("available_spare_threshold"))):
            flagged = False  # the drive's own limit: critical, never a threshold
        overridden = label in overrides and value != default
        accepted = (overridden and reading is not None and not flagged
                    and _trips(reading, default, compare))
        key = label_row_key(label)
        out.append({
            "label": label, "slug": attention.threshold_slug(label), "reading": reading,
            "default": default, "value": value, "overridden": overridden,
            "flagged": flagged, "accepted": accepted, "compare": compare,
            "unit": "%" if label in gauges else "", "row": key, "name": _row_name(key, label),
        })
    return out


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
    # The integration reads these from the ATA table first; a payload with no
    # top-level fields (a store written by an older mock) still has them there.
    table = (smart.get("ata_smart_attributes") or {}).get("table") or []
    by_id = {a.get("id"): a for a in table if isinstance(a, dict)}

    def _raw(*ids: int) -> int | None:
        for attr_id in ids:
            raw = (by_id.get(attr_id) or {}).get("raw") or {}
            match = re.match(r"\s*(\d+)", str(raw.get("string", "")))
            if match:
                return int(match.group(1))
        return None

    if temp is None:
        temp = _raw(194, 190)
    if hours is None:
        hours = _raw(9)
    if cycles is None:
        cycles = _raw(12)
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

    # Thresholds: a row with an override says it in its note, and a reading
    # the override keeps from being flagged is marked accepted.
    for limit in (verdict or {}).get("limits") or []:
        if not (limit.get("overridden") or limit.get("accepted")) or not limit.get("row"):
            continue
        for key in (limit["row"], limit["row"] + "_devstat"):
            row = by_key.get(key)
            if row is None or row["mark"] == "unk":
                continue
            if limit.get("overridden"):
                row["limit"] = {"value": limit["value"], "default": limit["default"],
                                "text": threshold_text(limit["value"], limit["default"],
                                                       limit["compare"], limit["unit"])}
            if limit.get("accepted") and row["mark"] not in ("crit", "warn"):
                row["accepted"] = True

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


# ── A small websocket client (RFC 6455) ─────────────────────────────────────
#
# Home Assistant changes its device registry only over its websocket API, so
# a rename made here reaches it through this client: the HTTP upgrade, masked
# text frames out, text frames in (continuations joined), ping answered with
# pong, and close. No extensions, no TLS: the Supervisor's proxy to Core is
# plain ws:// on the internal network.

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_MESSAGE = 4 * 1024 * 1024


class WebSocketError(Exception):
    """The websocket could not be opened, or broke, or closed."""


class WebSocket:
    """One client connection: connect(), send_text(), recv_text(), close()."""

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 timeout: float = SUPERVISOR_TIMEOUT) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise WebSocketError(f"not a ws:// address: {url}")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    def connect(self) -> "WebSocket":
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as err:
            raise WebSocketError(f"could not connect: {err}") from err
        key = base64.b64encode(os.urandom(16)).decode()
        host = self.host if self.port == 80 else f"{self.host}:{self.port}"
        lines = [f"GET {self.path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in self.headers.items()]
        self._send_raw(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._recv_some()
            head += chunk
            if len(head) > 65536:
                raise WebSocketError("the upgrade answer is too long")
        head, _sep, self._buf = head.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[1] != "101":
            raise WebSocketError(f"the upgrade was refused: {status_line}")
        answer = {}
        for line in header_lines:
            name, _colon, value = line.partition(":")
            answer[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if answer.get("sec-websocket-accept") != expected:
            raise WebSocketError("the upgrade answer has the wrong Sec-WebSocket-Accept")
        return self

    def _send_raw(self, data: bytes) -> None:
        if self.sock is None:
            raise WebSocketError("not connected")
        try:
            self.sock.sendall(data)
        except OSError as err:
            raise WebSocketError(f"send failed: {err}") from err

    def _recv_some(self) -> bytes:
        if self.sock is None:
            raise WebSocketError("not connected")
        try:
            chunk = self.sock.recv(65536)
        except OSError as err:
            raise WebSocketError(f"receive failed: {err}") from err
        if not chunk:
            raise WebSocketError("the connection closed")
        return chunk

    def _read(self, size: int) -> bytes:
        while len(self._buf) < size:
            self._buf += self._recv_some()
        data, self._buf = self._buf[:size], self._buf[size:]
        return data

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        """One final frame, masked as a client's must be."""
        size = len(payload)
        head = bytes([0x80 | opcode])
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._send_raw(head + mask + masked)

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode("utf-8"))

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read(2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", self._read(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self._read(8))[0]
        if size > WS_MAX_MESSAGE:
            raise WebSocketError("a frame is too large")
        mask = self._read(4) if second & 0x80 else b""
        payload = self._read(size)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bool(first & 0x80), first & 0x0F, payload

    def recv_text(self) -> str:
        """The next text message; pings are answered on the way."""
        parts: list[bytes] = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                try:
                    self.send_frame(0x8, payload[:2])
                except WebSocketError:
                    pass
                self._shut()
                raise WebSocketError("the server closed the connection")
            if opcode in (0x1, 0x2) and parts:
                raise WebSocketError("a new message began inside another")
            if opcode == 0x0 and not parts:
                raise WebSocketError("a continuation frame with no message")
            parts.append(payload)
            if sum(len(p) for p in parts) > WS_MAX_MESSAGE:
                raise WebSocketError("a message is too large")
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def send_json(self, data: Any) -> None:
        self.send_text(json.dumps(data))

    def recv_json(self) -> Any:
        try:
            return json.loads(self.recv_text())
        except ValueError as err:
            raise WebSocketError("a message was not JSON") from err

    def close(self) -> None:
        """Send close (1000), wait briefly for the answer, shut the socket."""
        if self.sock is None:
            return
        try:
            self.send_frame(0x8, struct.pack("!H", 1000))
            self.sock.settimeout(1.0)
            while True:
                _fin, opcode, _payload = self._read_frame()
                if opcode == 0x8:
                    break
        except (WebSocketError, OSError, ValueError):
            pass
        self._shut()

    def _shut(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


def ha_ws_command(url: str, token: str, command: dict[str, Any],
                  timeout: float = SUPERVISOR_TIMEOUT) -> dict[str, Any]:
    """Run one Home Assistant websocket command: auth with the token, send
    the command as id 1, return its result message. Raises WebSocketError
    when the connection, the auth or the command fails."""
    ws = WebSocket(url, {"Authorization": f"Bearer {token}"}, timeout).connect()
    try:
        hello = ws.recv_json()
        if not isinstance(hello, dict) or hello.get("type") != "auth_required":
            raise WebSocketError("Home Assistant did not ask for auth")
        ws.send_json({"type": "auth", "access_token": token})
        auth = ws.recv_json()
        if not isinstance(auth, dict) or auth.get("type") != "auth_ok":
            raise WebSocketError("Home Assistant refused the auth")
        ws.send_json({**command, "id": 1})
        while True:
            reply = ws.recv_json()
            if isinstance(reply, dict) and reply.get("id") == 1 and reply.get("type") == "result":
                break
        if not reply.get("success"):
            error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
            raise WebSocketError(f"Home Assistant refused {command.get('type')}: "
                                 f"{error.get('message') or error.get('code') or 'no reason'}")
        return reply
    finally:
        ws.close()


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
        self._host_addresses: set[str] = set()
        self._docker_network: ipaddress.IPv4Network | None = None
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

    def ws_url(self) -> str:
        """Core's websocket through the Supervisor's proxy."""
        parsed = urlparse(self.base_url)
        return f"ws://{parsed.netloc}{parsed.path.rstrip('/')}/core/websocket"

    def set_device_name(self, device_id: str, name: str | None) -> bool:
        """Set a device's name_by_user in Home Assistant (None clears it),
        over the websocket API: the only way Core changes its device
        registry. False, logged, when it did not take."""
        if not self.token:
            return False
        try:
            ha_ws_command(self.ws_url(), self.token, {
                "type": "config/device_registry/update",
                "device_id": device_id, "name_by_user": name})
        except WebSocketError as err:
            log(f"Home Assistant did not take the name of device {device_id}: {err}")
            return False
        return True

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
            self._read_host_addresses(data)
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

    def _read_host_addresses(self, data: dict[str, Any]) -> None:
        """Every IPv4 address of the host from the network info, on any
        interface, primary or not, connected or not; the host's own address
        on the Supervisor's hassio network (its gateway); and that network,
        whose addresses are all containers on this machine."""
        found: set[str] = set()
        for iface in data.get("interfaces") or []:
            ipv4 = iface.get("ipv4") if isinstance(iface, dict) else None
            for text in (ipv4.get("address") or []) if isinstance(ipv4, dict) else []:
                try:
                    address = ipaddress.ip_interface(str(text))
                except ValueError:
                    continue
                if address.version == 4:
                    found.add(str(address.ip))
        docker = data.get("docker") if isinstance(data.get("docker"), dict) else {}
        try:
            found.add(str(ipaddress.IPv4Address(str(docker.get("gateway")))))
        except ValueError:
            pass
        try:
            net = ipaddress.ip_network(str(docker.get("address")), strict=False)
        except ValueError:
            net = None
        # Only a private block of sensible size, never a whole LAN by mistake.
        if net is not None and net.version == 4 and net.is_private and net.prefixlen >= 16:
            self._docker_network = net  # type: ignore[assignment]
        self._host_addresses = found

    def own_addresses(self) -> tuple[set[str], ipaddress.IPv4Network | None]:
        """The host's IPv4 addresses and its hassio network, as the
        Supervisor's network info lists them; empty when it cannot tell."""
        self.network()
        return set(self._host_addresses), self._docker_network

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
    try:  # no PTR record: some resolvers answer the address itself
        ipaddress.IPv4Address(str(found[0]))
        return None
    except ValueError:
        pass
    name = str(found[0]).split(".")[0]
    return name or None


def _route_targets(text: str) -> list[str]:
    """One address per route in /proc/net/route: the gateway, or for a route
    with none, the first address of its network. The kernel writes each
    field as the raw 32-bit value in host byte order."""
    targets: list[str] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            dest, gateway, mask = (struct.pack("=I", int(fields[i], 16)) for i in (1, 2, 7))
        except (ValueError, struct.error):
            continue
        if gateway != bytes(4):
            targets.append(socket.inet_ntoa(gateway))
        elif mask != bytes(4):
            net = ipaddress.IPv4Network(f"{socket.inet_ntoa(dest)}/{socket.inet_ntoa(mask)}",
                                        strict=False)
            if net.num_addresses > 1:
                targets.append(str(net.network_address + 1))
    return targets


def local_ipv4_addresses(route_file: str = "/proc/net/route") -> set[str]:
    """This container's own IPv4 addresses, with the standard library only:
    what its host name resolves to, and for each route the source address
    the kernel picks towards it (connecting a UDP socket sends nothing).
    The scan is IPv4 only, so IPv6 addresses are not collected."""
    found: set[str] = set()
    infos = _with_budget(socket.getaddrinfo, socket.gethostname(), None, socket.AF_INET)
    for info in infos or []:
        found.add(str(info[4][0]))
    try:
        with open(route_file, encoding="ascii", errors="replace") as handle:
            targets = _route_targets(handle.read())
    except OSError:
        targets = []
    for target in targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 9))
                found.add(str(sock.getsockname()[0]))
        except OSError:
            continue
    found.discard("0.0.0.0")
    return found


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
          connect_to: str | None = None,
          own_names: set[str] | frozenset[str] = frozenset()) -> tuple[str, dict[str, Any] | None]:
    """Is there an agent on address:port? Returns ("ok", scan row),
    ("not_answering", None), ("not_an_agent", None), or ("own", None) when
    its health names one of own_names (this app's agents). Sends GET
    /api/health and GET /api/drives only, never with credentials."""
    target = connect_to or address
    health = agent_call(target, port, "GET", "/api/health",
                        connect_timeout=connect_timeout, read_timeout=read_timeout)
    if health.status is None:
        return "not_answering", None
    body: dict[str, Any] = {}
    if _is_agent_health(health):
        body = health.body
        named = body.get("hostname")
        if own_names and isinstance(named, str) and named.split(".")[0].lower() in own_names:
            return "own", None
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

    def start(self, block: ipaddress.IPv4Network, port: int, skip: Callable[[str], bool],
              stored: list[dict[str, Any]],
              own_names: set[str] | frozenset[str] = frozenset()) -> Scan:
        """skip(address) is true for an address of this machine: nothing is
        sent to it. own_names: this app's host names; an agent whose health
        names one is left out of the results."""
        scan = Scan(os.urandom(4).hex(), block, port)
        with self.lock:
            self._purge()
            for other in self.scans.values():
                if other.state == "running":
                    other.cancel.set()
                    other.finish("cancelled")
            self.scans[scan.id] = scan
        threading.Thread(target=self._run, args=(scan, skip, stored, own_names), daemon=True).start()
        return scan

    def cancel(self, scan_id: str) -> Scan | None:
        scan = self.get(scan_id)
        if scan is not None:
            scan.cancel.set()
            scan.finish("cancelled")
        return scan

    def _run(self, scan: Scan, skip: Callable[[str], bool], stored: list[dict[str, Any]],
             own_names: set[str] | frozenset[str]) -> None:
        added = stored_keys(stored)

        def one(address: str) -> None:
            if scan.cancel.is_set():
                return
            row = None
            if not skip(address):
                outcome, row = probe(address, scan.port, SCAN_CONNECT_TIMEOUT, SCAN_READ_TIMEOUT,
                                     own_names=own_names)
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
            "removed": _str_list(item.get("removed")),
        })
    return hosts


def _str_list(raw: Any) -> list[str]:
    """A list of distinct non-empty strings, in order; anything else is dropped."""
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
    return out


def _normalize_thresholds(raw: Any) -> dict[str, dict[str, int]]:
    """The app's own thresholds from the options: a list of {card, label,
    value} (the Supervisor's schema has no free-form maps) as
    {card id: {label: int}}. Anything malformed is dropped."""
    out: dict[str, dict[str, int]] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        card, label, value = item.get("card"), item.get("label"), item.get("value")
        if not (isinstance(card, str) and card and isinstance(label, str) and label):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            continue
        out.setdefault(card, {})[label] = int(value)
    return out


def _serialize_thresholds(thresholds: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    return [{"card": card, "label": label, "value": int(value)}
            for card in sorted(thresholds) for label, value in sorted(thresholds[card].items())]


MAX_NAME = 100  # the longest card or machine name kept


def clean_name(raw: Any) -> str:
    """A name as stored: one line, trimmed, at most MAX_NAME characters."""
    text = " ".join(str(raw).split()) if isinstance(raw, str) else ""
    return text[:MAX_NAME].strip()


def _normalize_names(raw: Any, key: str) -> dict[str, str]:
    """Names given in the Control Center from the options: a list of {key,
    name} (the Supervisor's schema has no free-form maps) as {id: name}.
    Anything malformed or empty is dropped."""
    out: dict[str, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        ident, name = item.get(key), clean_name(item.get("name"))
        if isinstance(ident, str) and ident and name:
            out[ident] = name
    return out


def _serialize_names(names: dict[str, str], key: str) -> list[dict[str, str]]:
    return [{key: ident, "name": names[ident]} for ident in sorted(names)]


def _serialize_host(host: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"address": host["address"], "port": host["port"]}
    if host.get("password"):
        out["password"] = host["password"]
    if host.get("hidden"):
        out["hidden"] = True
    if host.get("removed"):
        out["removed"] = list(host["removed"])
    return out


# ── Cards ───────────────────────────────────────────────────────────────────
#
# Every card on the page has an id that stays the same across reloads: the
# host id ("self", "replicas" or "<address>:<port>"), a "|", then the key the
# drive view already uses for Home Assistant: the drive's serial, else
# "id:<drive id>"; "pool:<name>" for a pool; "fs:<filesystem id>" for a
# filesystem that belongs to no drive or pool. A machine waiting for its
# password (or never read) shows one card whose id is the host id alone.

CARD_SEP = "|"


def drive_card_key(drive: dict[str, Any], drive_id: str) -> str:
    serial = drive.get("serial") if isinstance(drive, dict) else None
    if isinstance(serial, str) and serial.strip():
        return serial.strip()
    return f"id:{drive_id}"


def card_id(host_key: str, key: str) -> str:
    return f"{host_key}{CARD_SEP}{key}"


def split_card_id(text: str) -> tuple[str, str | None]:
    """(host id, card key); the key is None for a machine's own card."""
    host_key, sep, key = str(text).partition(CARD_SEP)
    return host_key, (key if sep else None)


def _part_of(device: str, drive_path: str) -> bool:
    """True when a filesystem's block device is on this drive: the drive's
    path itself, or it followed by a partition number (/dev/sda1,
    /dev/nvme0n1p2, /dev/mmcblk0p1)."""
    if not drive_path or not device.startswith(drive_path):
        return False
    rest = device[len(drive_path):]
    return re.fullmatch(r"(?:n\d+)?(?:p?\d+)?", rest) is not None


def attach_filesystems(host_key: str, items: list[dict[str, Any]],
                       filesystems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put each filesystem on the card it lives on, as item["filesystems"]:
    a block device's on the drive whose path starts it, a ZFS dataset on its
    pool's card. Returns the rest as FILESYSTEM cards."""
    for item in items:
        item["filesystems"] = []
    drives = [i for i in items if i.get("type") == "drive"]
    pools = {str((i.get("pool") or {}).get("name")): i for i in items if i.get("type") == "pool"}
    leftover: list[dict[str, Any]] = []
    for fs in filesystems:
        device = str(fs.get("device") or "")
        owner = None
        if str(fs.get("fstype") or "").lower() == "zfs":
            owner = pools.get(device.split("/", 1)[0])
        else:
            matches = [d for d in drives
                       if _part_of(device, str((d.get("drive") or {}).get("device_path") or ""))]
            if matches:
                owner = max(matches, key=lambda d: len(str(d["drive"].get("device_path"))))
        if owner is not None:
            owner["filesystems"].append(fs)
            continue
        fs_id = str(fs.get("id") or fs.get("mountpoint") or len(leftover))
        leftover.append({"type": "filesystem", "id": fs_id,
                         "card_id": card_id(host_key, f"fs:{fs_id}"), "filesystem": fs})
    return leftover


def _drop_ids(state: dict[str, Any], gone: set[str]) -> dict[str, Any] | None:
    """The state without these card ids in the order, the hidden list and
    the app's thresholds; None (nothing to write) when none has them."""
    thresholds = state.get("thresholds") or {}
    if not any(i in gone for i in state["card_order"] + state["hidden_cards"] + list(thresholds)
               + list(state.get("card_names") or {})):
        return None
    state["card_order"] = [i for i in state["card_order"] if i not in gone]
    state["hidden_cards"] = [i for i in state["hidden_cards"] if i not in gone]
    state["thresholds"] = {k: v for k, v in thresholds.items() if k not in gone}
    state["card_names"] = {k: v for k, v in (state.get("card_names") or {}).items() if k not in gone}
    return state


def _drop_cards(state: dict[str, Any], host_key: str, cid: str,
                outcome: dict[str, Any]) -> dict[str, Any]:
    """After a field card is removed: its id leaves the order and the hidden
    list; when its machine went too, every id of that machine does."""
    def gone(i: str) -> bool:
        return i == cid or (bool(outcome.get("host")) and split_card_id(i)[0] == host_key)

    state["card_order"] = [i for i in state["card_order"] if not gone(i)]
    state["hidden_cards"] = [i for i in state["hidden_cards"] if not gone(i)]
    state["thresholds"] = {k: v for k, v in (state.get("thresholds") or {}).items() if not gone(k)}
    state["card_names"] = {k: v for k, v in (state.get("card_names") or {}).items() if not gone(k)}
    if outcome.get("host"):
        state["host_names"] = {k: v for k, v in (state.get("host_names") or {}).items()
                               if k != host_key}
    return state


def card_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Every card of a drives read: its items, then its FILESYSTEM cards."""
    return [c for c in (response.get("items") or []) + (response.get("filesystem_cards") or [])
            if isinstance(c, dict) and c.get("card_id")]


def without_removed(response: dict[str, Any], removed: list[str]) -> dict[str, Any]:
    """A drives read without the cards removed with the trash; the summary
    counted again over what is left."""
    if not removed:
        return response
    gone = {card_id(str(response.get("id")), r) for r in removed}
    items = [i for i in response.get("items") or [] if i.get("card_id") not in gone]
    fs_cards = [c for c in response.get("filesystem_cards") or [] if c.get("card_id") not in gone]
    return {**response, "items": items, "filesystem_cards": fs_cards,
            "summary": summarize(items, response.get("status") == "ok")}


def card_keys(host_key: str, response: Any) -> set[str]:
    """The key of every card a read of a machine has: its drives, pools and
    FILESYSTEM cards, removed with the trash or not. A read cached before
    cards had ids gets them as the stale read would give them."""
    if not isinstance(response, dict):
        return set()
    keys: set[str] = set()
    for item in response.get("items") or []:
        if not isinstance(item, dict):
            continue
        cid = item.get("card_id")
        if not cid:
            cid = card_id(host_key, f"pool:{(item.get('pool') or {}).get('name')}"
                          if item.get("type") == "pool"
                          else drive_card_key(item.get("drive") or {}, str(item.get("id"))))
        keys.add(str(split_card_id(cid)[1]))
    for card in response.get("filesystem_cards") or []:
        if isinstance(card, dict) and card.get("card_id"):
            keys.add(str(split_card_id(card["card_id"])[1]))
    return keys


def every_card_removed(host: dict[str, Any], response: Any) -> bool:
    """True when the machine's last read had cards and every one of them is
    removed with the trash, so the page shows nothing of it. Hidden cards
    count as shown: hiding is for a while. A machine never read shows its
    own card, so it is not one of these."""
    keys = card_keys(host_id(host["address"], host["port"]), response)
    return bool(keys) and keys <= set(host.get("removed") or [])


# Options of older versions, removed at start when the Supervisor does not
# report the schema to compare with.
REMOVED_OPTIONS = ("mock_mode",)


def _schema_names(schema: Any) -> set[str]:
    """Top-level option names from the Supervisor's schema: a list of
    {"name": ...} (its UI form) or a mapping. Empty when unknown."""
    if isinstance(schema, dict):
        return {str(k) for k in schema}
    if isinstance(schema, list):
        return {str(item["name"]) for item in schema if isinstance(item, dict) and item.get("name")}
    return set()


def drop_stale_options(store: HostStore) -> None:
    """Run once at start: remove options left from an older version."""
    try:
        stale = store.drop_stale_options()
    except ApiError as err:
        log(f"Could not check the app options for old settings: {err.message or err.code}")
        return
    if stale:
        log(f"Removed the old option{'s' if len(stale) > 1 else ''} {', '.join(stale)} from the app options")


class HostStore:
    """The field agents, kept in the app's options (the source of truth).

    The Supervisor's options call replaces the whole object, so every write
    takes the process lock, reads the current options, replaces hosts only
    and posts them back: one write per user action. Writing options does not
    restart the app."""

    MEMO_SECONDS = 5

    def __init__(self, supervisor: Supervisor, cache: FleetCache | None = None) -> None:
        self.sv = supervisor
        self.cache = cache
        self.lock = threading.Lock()
        self.last_known: list[dict[str, Any]] | None = None
        self.last_thresholds: dict[str, dict[str, int]] | None = None
        self._memo_at = 0.0

    def _info(self) -> dict[str, Any]:
        """The Supervisor's info on this app, with its options."""
        if not self.sv.available:
            raise ApiError(503, "supervisor_unavailable", "the Supervisor is not available")
        status, info = self.sv.call("GET", "/addons/self/info")
        data = info.get("data") if isinstance(info, dict) else None
        options = data.get("options") if isinstance(data, dict) else None
        if status != 200 or not isinstance(options, dict):
            raise ApiError(502, "supervisor_failed", "could not read the app options")
        return data  # type: ignore[return-value]

    def _options(self) -> dict[str, Any]:
        return self._info()["options"]

    def _save(self, options: dict[str, Any]) -> None:
        """Replace the whole options object; the caller holds the lock."""
        status, reply = self.sv.call("POST", "/addons/self/options", {"options": options})
        if status != 200:
            message = reply.get("message") if isinstance(reply, dict) else None
            raise ApiError(502, "supervisor_failed", message or "could not save the app options")

    def drop_stale_options(self) -> list[str]:
        """Remove the options the app's schema no longer has, such as
        mock_mode from 0.2.x, in one write under the same lock as the field
        agent writes. The Supervisor keeps such an option after an update
        until the options are next written, and warns about it on every
        check. Without a schema from the Supervisor only REMOVED_OPTIONS go.
        Returns the names removed."""
        with self.lock:
            data = self._info()
            options = data["options"]
            names = _schema_names(data.get("schema"))
            stale = [key for key in options
                     if (key not in names if names else key in REMOVED_OPTIONS)]
            if stale:
                self._save({k: v for k, v in options.items() if k not in stale})
            return stale

    def _response(self, host: dict[str, Any]) -> Any:
        if self.cache is None:
            return None
        return self.cache.get(host_id(host["address"], host["port"])).get("response")

    def shows_cards(self, host: dict[str, Any]) -> bool:
        """False for a stored machine whose every card is removed."""
        return not every_card_removed(host, self._response(host))

    def _repair(self, options: dict[str, Any]) -> dict[str, Any]:
        """Options read now, without the stored machines whose every card is
        removed, such as one left behind when its last drive went while a
        FILESYSTEM card was still counted. They go in one write, with their
        card ids and their cache entries."""
        hosts = _normalize_hosts(options.get("hosts"))
        if all(self.shows_cards(h) for h in hosts):
            return options
        dropped: list[str] = []

        def change(state: dict[str, Any]) -> dict[str, Any] | None:
            dropped.clear()
            dropped.extend(host_id(h["address"], h["port"]) for h in state["hosts"]
                           if not self.shows_cards(h))
            if not dropped:
                return None
            state["hosts"] = [h for h in state["hosts"]
                              if host_id(h["address"], h["port"]) not in dropped]
            for key in ("card_order", "hidden_cards"):
                state[key] = [i for i in state[key] if split_card_id(i)[0] not in dropped]
            state["thresholds"] = {k: v for k, v in state["thresholds"].items()
                                   if split_card_id(k)[0] not in dropped}
            state["card_names"] = {k: v for k, v in state["card_names"].items()
                                   if split_card_id(k)[0] not in dropped}
            state["host_names"] = {k: v for k, v in state["host_names"].items()
                                   if k not in dropped}
            return state

        state = self.write_all(change)
        for key in dropped:
            if self.cache is not None:
                self.cache.drop(key)
            log(f"Field agent {key} dropped: every card of it was removed")
        return {**options, "hosts": [_serialize_host(h) for h in state["hosts"]],
                "card_order": state["card_order"], "hidden_cards": state["hidden_cards"],
                "thresholds": _serialize_thresholds(state["thresholds"]),
                "card_names": _serialize_names(state["card_names"], "card"),
                "host_names": _serialize_names(state["host_names"], "host")}

    def hosts(self) -> list[dict[str, Any]]:
        options = self._repair(self._options())
        hosts = _normalize_hosts(options.get("hosts"))
        self.last_known = hosts
        self.last_thresholds = _normalize_thresholds(options.get("thresholds"))
        self._memo_at = time.monotonic()
        return hosts

    def thresholds_quick(self) -> dict[str, dict[str, int]]:
        """The app's own thresholds by card id, read again at most every
        MEMO_SECONDS; the last known (or none) when the Supervisor cannot be
        read."""
        if self.last_thresholds is not None and time.monotonic() - self._memo_at < self.MEMO_SECONDS:
            return self.last_thresholds
        try:
            self.hosts()
        except ApiError:
            pass
        return self.last_thresholds or {}

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
        def whole(state: dict[str, Any]) -> dict[str, Any] | None:
            hosts = change(state["hosts"])
            return None if hosts is None else {**state, "hosts": hosts}

        return self.write_all(whole)["hosts"]

    def cards(self) -> dict[str, Any]:
        """The stored hosts, card order and hidden cards, read now."""
        options = self._repair(self._options())
        hosts = _normalize_hosts(options.get("hosts"))
        self.last_known = hosts
        self.last_thresholds = _normalize_thresholds(options.get("thresholds"))
        self._memo_at = time.monotonic()
        return {"hosts": hosts, "card_order": _str_list(options.get("card_order")),
                "hidden_cards": _str_list(options.get("hidden_cards")),
                "card_names": _normalize_names(options.get("card_names"), "card"),
                "host_names": _normalize_names(options.get("host_names"), "host")}

    def write_all(self, change) -> dict[str, Any]:
        """One options write for hosts, card_order, hidden_cards, the app's
        thresholds and the names given to cards and machines together.

        change() gets {"hosts", "card_order", "hidden_cards", "thresholds",
        "card_names", "host_names"} (copies; thresholds as {card id: {label:
        int}}, names as {id: name}) and returns the new state, or None for
        nothing to save. Every other option is written back as it was read."""
        with self.lock:
            options = self._options()
            current = {"hosts": _normalize_hosts(options.get("hosts")),
                       "card_order": _str_list(options.get("card_order")),
                       "hidden_cards": _str_list(options.get("hidden_cards")),
                       "thresholds": _normalize_thresholds(options.get("thresholds")),
                       "card_names": _normalize_names(options.get("card_names"), "card"),
                       "host_names": _normalize_names(options.get("host_names"), "host")}
            state = change({"hosts": [dict(h) for h in current["hosts"]],
                            "card_order": list(current["card_order"]),
                            "hidden_cards": list(current["hidden_cards"]),
                            "thresholds": {k: dict(v) for k, v in current["thresholds"].items()},
                            "card_names": dict(current["card_names"]),
                            "host_names": dict(current["host_names"])})
            if state is None:
                state = current
            else:
                state.setdefault("thresholds", current["thresholds"])
                state.setdefault("card_names", current["card_names"])
                state.setdefault("host_names", current["host_names"])
                saved = {**options, "hosts": [_serialize_host(h) for h in state["hosts"]]}
                for key in ("card_order", "hidden_cards"):
                    if state[key] or key in options:
                        saved[key] = _str_list(state[key])
                thresholds = {k: v for k, v in state["thresholds"].items() if v}
                state["thresholds"] = thresholds
                if thresholds or "thresholds" in options:
                    saved["thresholds"] = _serialize_thresholds(thresholds)
                for key, field in (("card_names", "card"), ("host_names", "host")):
                    names = {k: clean_name(v) for k, v in state[key].items() if clean_name(v)}
                    state[key] = names
                    if names or key in options:
                        saved[key] = _serialize_names(names, field)
                self._save(saved)
                log(f"Field agents and cards saved ({len(state['hosts'])} agents stored)")
            self.last_known = state["hosts"]
            self.last_thresholds = state["thresholds"]
            self._memo_at = time.monotonic()
            return state


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


def judge_drive(rules: Rules, drive: dict[str, Any], rules_ok: bool,
                thresholds: dict[str, int] | None = None) -> tuple[Any, Any]:
    if not rules_ok:
        return None, drive_view(drive, None)
    work = dict(drive)
    verdict = rules.evaluate(work, thresholds)
    return verdict, drive_view(work, verdict)


def judge_pool(rules: Rules, pool: dict[str, Any], lab: dict[str, Any] | None,
               rules_ok: bool) -> tuple[Any, Any]:
    verdict = None
    if rules_ok and rules.pool_health is not None:
        verdict = pool_verdict(pool, rules)
    return verdict, pool_view(pool, verdict, lab)


def _flow_message(step: Any, status: int | None) -> str:
    """What a Home Assistant flow step says went wrong."""
    if status is None:
        return "Home Assistant is not answering"
    if isinstance(step, dict):
        if step.get("type") == "abort":
            return str(step.get("reason") or "aborted")
        errors = step.get("errors")
        if isinstance(errors, dict) and errors:
            return str(errors.get("base") or next(iter(errors.values())))
        if step.get("message"):
            return str(step["message"])
        if step.get("type") in ("form", "menu"):
            return f"unexpected step {step.get('step_id')}"
    return f"HTTP {status}"


# ── Request handler ─────────────────────────────────────────────────────────

# Path prefixes that belong to the proxy itself: anything under them that no
# route matches is a JSON 404, never the page.
_APP_PREFIXES = ("/app/", "/evaluate", "/fleet/", "/ha/", "/lab/", "/replicas", "/cards")


class ProxyHandler(BaseHTTPRequestHandler):
    """Serves static files, proxies API requests, answers the app routes."""

    agent_port: int = 9099
    mock_port: int = 9100
    static_dir: str = STATIC_DIR
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
    # Entry ids whose diagnostics failed; kept for the process life.
    _diagnostics_failed: set[str] = set()
    # The integration's thresholds read: entry id -> (time, {drive id:
    # {label: int}} or None when its diagnostics have none), and (entry id,
    # drive id) -> (time, {label: int}) read through the options flow.
    _thr_cache: dict[str, tuple[float, dict[str, dict[str, int]] | None]] = {}
    _thr_flow_cache: dict[tuple[str, str], tuple[float, dict[str, int]]] = {}

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
        ("GET", r"/cards", "_route_cards"),
        ("PUT", r"/cards/order", "_route_cards_order"),
        ("GET", r"/cards/(?P<cid>[^/]+)/file", "_route_card_file"),
        ("GET", r"/cards/(?P<cid>[^/]+)/thresholds", "_route_thresholds_get"),
        ("PUT", r"/cards/(?P<cid>[^/]+)/thresholds", "_route_thresholds_put"),
        ("POST", r"/cards/(?P<cid>[^/]+)/thresholds/accept", "_route_thresholds_accept"),
        ("POST", r"/cards/(?P<cid>[^/]+)/thresholds/reset", "_route_thresholds_reset"),
        ("PUT", r"/cards/(?P<cid>[^/]+)/name", "_route_card_name"),
        ("PUT", r"/hosts/(?P<key>[^/]+)/name", "_route_host_name"),
        ("PATCH", r"/cards/(?P<cid>[^/]+)", "_route_card_patch"),
        ("DELETE", r"/cards/(?P<cid>[^/]+)", "_route_card_delete"),
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
        own, hassio = self.supervisor.own_addresses()
        own |= local_ipv4_addresses()
        if network:
            own.add(network["address"])

        def skip(address: str) -> bool:
            return address in own or (hassio is not None and ipaddress.IPv4Address(address) in hassio)

        scan = self.scans.start(block, port, skip, self._added_hosts(), self._own_hosts())
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
        stored = self._added_hosts()
        keys = {(h["address"], h["port"]) for h in stored}
        row["added"] = (address, port) in keys or (
            (connect_to, port) in stored_keys(stored))
        # The address it was reached at, so the page can find a typed name
        # among the scan's rows, which carry addresses.
        row["resolved"] = connect_to
        self._send_json(200, row)

    def _added_hosts(self) -> list[dict[str, Any]]:
        """The stored machines a scan or a check marks as added: those with a
        card still on the page, hidden or not."""
        return [h for h in self.host_store.hosts_quick() if self.host_store.shows_cards(h)]

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
            known = {host_id(h["address"], h["port"]): h for h in hosts}
            new: list[dict[str, Any]] = []
            for host in wanted:
                key = host_id(host["address"], host["port"])
                stored = known.get(key)
                if stored is not None and stored["removed"]:
                    # Added again: the cards removed with the trash come back.
                    stored["removed"] = []
                    added.append(stored)
                    continue
                if stored is not None:
                    skipped.append({"address": host["address"], "port": host["port"],
                                    "reason": "already_added"})
                    continue
                known[key] = host
                new.append(host)
                added.append(host)
            return hosts + new if added else None  # nothing new: no write

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

    def _target(self, key: str) -> tuple[tuple[str, str, int, str], dict[str, Any] | None]:
        """(kind, address, port, token) for self, replicas or a stored host,
        and the stored host (None for self and replicas). 404 otherwise."""
        if key == "self":
            return ("self", "127.0.0.1", self.agent_port, self.token), None
        if key == "replicas":
            return ("replicas", "127.0.0.1", self.mock_port, ""), None
        host = self.host_store.find(key)
        if host is None:
            raise ApiError(404, "not_found", "no such field agent")
        return ("field", host["address"], host["port"], host.get("password") or ""), host

    def _route_host_drives(self, _parsed, key: str) -> None:
        target, host = self._target(key)
        response = self._read_agent(key, *target)
        if host is not None:
            response = without_removed(response, host.get("removed") or [])
            if host.get("hidden"):
                self._migrate_hidden(key, response)
        self._send_json(200, response)

    def _migrate_hidden(self, key: str, response: dict[str, Any]) -> None:
        """A machine hidden as a whole (0.3.0 before cards) hides each of its
        cards instead, in one write; the machine's own hidden flag goes."""
        ids = [c["card_id"] for c in card_list(response)] or [key]
        try:
            def change(state: dict[str, Any]) -> dict[str, Any]:
                for host in state["hosts"]:
                    if host_id(host["address"], host["port"]) == key:
                        host["hidden"] = False
                state["hidden_cards"] += [i for i in ids if i not in state["hidden_cards"]]
                return state

            self.host_store.write_all(change)
            self.cache.update(key, hidden_since=None)
        except ApiError as err:
            log(f"Could not move {key}'s hidden flag to its cards: {err.message or err.code}")

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
        """One read of an agent: health, drive list, each drive, pools,
        filesystems; every drive and pool judged, every card given its id.
        On failure the last good read, marked stale."""
        def call(path: str, timeout: float = AGENT_READ_TIMEOUT) -> AgentReply:
            return agent_call(address, port, "GET", path, token=token,
                              connect_timeout=AGENT_HEALTH_TIMEOUT, read_timeout=timeout)

        health = call("/api/health", AGENT_HEALTH_TIMEOUT)
        if health.status == 401:
            return self._stale(key, kind, "password_refused" if token else "password_needed")
        if not _is_agent_health(health):
            return self._stale(key, kind, "unreachable")
        info = health.body
        password_status = "password_refused" if token else "password_needed"

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
                # The agent answered its health: the pending card (board 3.6)
                # shows its version and drive count from it.
                return self._stale(key, kind, password_status, live=info, address=address)
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
        resolve = self._threshold_resolver(key)
        for drive, lab in payloads:
            drive_id = str(drive.get("id"))
            if kind != "replicas" and drive_id in failed_ids:
                items.append({"type": "drive", "id": drive_id, "drive": drive, "verdict": None,
                              "view": None, "lab": None, "error": "not_answering"})
                continue
            source, overrides = "app", {}
            if rules_ok:
                source, overrides, _entry = resolve(card_id(key, drive_card_key(drive, drive_id)),
                                                    drive_id)
            verdict, view = judge_drive(self.rules, drive, rules_ok, overrides)
            items.append({"type": "drive", "id": drive_id, "drive": drive, "verdict": verdict,
                          "view": view, "lab": lab if kind == "replicas" else None,
                          "thresholds_source": source})

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
        for item in items:
            card_key = (f"pool:{item['pool'].get('name')}" if item["type"] == "pool"
                        else drive_card_key(item["drive"], item["id"]))
            item["card_id"] = card_id(key, card_key)

        # Disk space: each filesystem on the card it lives on, the rest as
        # FILESYSTEM cards. Read only when the agent lists the endpoint.
        filesystems: list[dict[str, Any]] = []
        filesystems_status = "none"
        if "/api/filesystems" in endpoints:
            reply = call("/api/filesystems")
            if reply.status == 200 and isinstance(reply.body, list):
                filesystems_status = "ok"
                filesystems = [f for f in reply.body if isinstance(f, dict)]
            else:
                filesystems_status = "failed"
        fs_cards = attach_filesystems(key, items, filesystems)

        now = utc_now()
        response = {
            "id": key, "kind": kind, "status": "ok", "read_at": now, "last_answer": now,
            "agent": self._agent_info(kind, address, info,
                                      sum(1 for i in items if i["type"] == "drive"),
                                      sum(1 for i in items if i["type"] == "pool")),
            "summary": summarize(items, True),
            "items": items,
            "filesystem_cards": fs_cards,
            "pools_status": pools_status,
            "filesystems_status": filesystems_status,
            "rules_version": self.rules.version if rules_ok else None,
            "stale": False,
        }
        self.cache.update(key, response=response, last_answer=now)
        return response

    def _agent_info(self, kind: str, address: str, info: dict[str, Any],
                    drives: Any, pools: Any) -> dict[str, Any]:
        """The response's agent block, from the agent's health answer."""
        return {
            "hostname": self._agent_hostname(kind, address, info),
            "version": info.get("version"), "os": info.get("os"),
            "os_name": info.get("os_name"), "uptime_seconds": info.get("uptime_seconds"),
            "drives": drives, "pools": pools,
        }

    def _stale(self, key: str, kind: str, status: str, live: dict[str, Any] | None = None,
               address: str = "") -> dict[str, Any]:
        """The last good read of a host that did not answer as it should.
        live: the health answer of an agent that wants a password; the agent
        block then comes from it (drive and pool counts as health gives them)."""
        cached = self.cache.get(key)
        last = cached.get("response") if isinstance(cached.get("response"), dict) else {}
        items = [{**item, "stale": True} for item in last.get("items") or []]
        for item in items:  # a read cached before cards had ids
            if not item.get("card_id"):
                item["card_id"] = card_id(key, f"pool:{(item.get('pool') or {}).get('name')}"
                                          if item.get("type") == "pool"
                                          else drive_card_key(item.get("drive") or {}, str(item.get("id"))))
        fs_cards = [{**c, "stale": True} for c in last.get("filesystem_cards") or []]
        agent = last.get("agent")
        if live is not None:
            counts = [live.get(k) if isinstance(live.get(k), int) and not isinstance(live.get(k), bool)
                      else (agent or {}).get(k) for k in ("drives", "pools")]
            agent = self._agent_info(kind, address, live, *counts)
        return {
            "id": key, "kind": kind, "status": status, "read_at": utc_now(),
            "last_answer": cached.get("last_answer"),
            "agent": agent,
            "summary": summarize(items, False),
            "items": items,
            "filesystem_cards": fs_cards,
            "pools_status": last.get("pools_status"),
            "filesystems_status": last.get("filesystems_status"),
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
        self._send_file(reply, card_id)

    def _send_file(self, reply: AgentReply | bytes, name: str) -> None:
        """A JSON file as a download: the replica agent's answer (with its
        own file name when it gives one) or bytes."""
        raw = reply if isinstance(reply, bytes) else reply.raw
        given = None if isinstance(reply, bytes) else reply.headers.get("content-disposition")
        disposition = given or (
            f'attachment; filename="{re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "card"}.json"')
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

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
        entry = self._save_replica_file(source, drive_id)
        added = self._mock_or_down("POST", "/api/drives", body={"replica": entry.get("id")})
        card = self._card_id(added.body) if added.status == 201 and isinstance(added.body, dict) else None
        self._send_json(201, {"file": entry, "card_id": card})

    def _save_replica_file(self, source: str, drive_id: str) -> dict[str, Any]:
        """Read a real drive (self or a stored field agent), judge it and save
        it as a replica file in the user folder, serial and WWN replaced by
        the replica agent. Returns the file's listing entry."""
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
            body = saved.body if isinstance(saved.body, dict) else {}
            extra = {k: v for k, v in body.items() if k not in ("error", "message")}
            raise ApiError(saved.status or 502, str(body.get("error") or "replica_agent_failed"),
                           body.get("message") or f"HTTP {saved.status}", **extra)
        log(f"Saved a replica of drive {drive_id}")
        return saved.body

    # ── /cards ──

    def _known_cards(self, hosts: list[dict[str, Any]]) -> set[str]:
        """Every card id the last reads showed, and each stored machine's own
        card (its password placeholder)."""
        keys = ["self", "replicas"] + [host_id(h["address"], h["port"]) for h in hosts]
        known = {k for k in keys[2:]}
        for key in keys:
            response = self.cache.get(key).get("response")
            if isinstance(response, dict):
                known |= {c["card_id"] for c in card_list(response)}
        return known

    def _route_cards(self, _parsed) -> None:
        state = self.host_store.cards()
        self._send_json(200, {"order": state["card_order"], "hidden": state["hidden_cards"],
                              "names": state["card_names"], "host_names": state["host_names"]})

    def _route_cards_order(self, _parsed) -> None:
        body = self._read_json()
        order = body.get("order") if isinstance(body, dict) else None
        if not isinstance(order, list):
            raise ApiError(400, "bad_request", 'give {"order": ["<card id>", ...]}')

        def change(state: dict[str, Any]) -> dict[str, Any]:
            hosts = {"self", "replicas"} | {host_id(h["address"], h["port"]) for h in state["hosts"]}
            known = self._known_cards(state["hosts"])
            wanted = [i for i in _str_list(order) if i in known]
            # Cards the page did not send (a machine it could not read) keep
            # their place after the ones it did, while their machine is stored.
            kept = [i for i in state["card_order"]
                    if i not in wanted and split_card_id(i)[0] in hosts]
            state["card_order"] = wanted + kept
            return state

        state = self.host_store.write_all(change)
        self._send_json(200, {"order": state["card_order"]})

    def _check_card_host(self, cid: str, hosts: list[dict[str, Any]]) -> tuple[str, str | None]:
        host_key, key = split_card_id(cid)
        stored = {host_id(h["address"], h["port"]) for h in hosts}
        if host_key not in ("self", "replicas") and host_key not in stored:
            raise ApiError(404, "not_found", "no such card")
        if host_key in ("self", "replicas") and key is None:
            raise ApiError(404, "not_found", "no such card")
        return host_key, key

    def _route_card_patch(self, _parsed, cid: str) -> None:
        body = self._read_json()
        hidden = body.get("hidden") if isinstance(body, dict) else None
        if not isinstance(hidden, bool):
            raise ApiError(400, "bad_request", 'give {"hidden": true or false}')
        self._check_card_host(cid, self.host_store.cards()["hosts"])

        def change(state: dict[str, Any]) -> dict[str, Any] | None:
            if (cid in state["hidden_cards"]) == hidden:
                return None  # already so: no write
            if hidden:
                state["hidden_cards"].append(cid)
            else:
                state["hidden_cards"].remove(cid)
            return state

        state = self.host_store.write_all(change)
        self._send_json(200, {"id": cid, "hidden": hidden, "hidden_cards": state["hidden_cards"]})

    # ── Names: /cards/<id>/name and /hosts/<id>/name ──
    #
    # A card's name is kept in the app options (card_names). For a drive or
    # pool whose device is in Home Assistant it is also written to that
    # device's name_by_user, which the page shows first when it is set, so a
    # rename made in Home Assistant shows here on the next refresh.

    @staticmethod
    def _name_body(body: Any) -> str:
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str):
            raise ApiError(400, "bad_request", 'give {"name": "<text>"}, empty for the original')
        return clean_name(name)

    def _ha_device(self, host_key: str, key: str) -> str | None:
        """The Home Assistant device id of a card, or None when it has none."""
        if not self.supervisor.available:
            return None
        lookup = self._ha_lookup()
        if not lookup.get("reachable") or lookup.get("installed") is False:
            return None
        target = (lookup.get("targets") or {}).get(host_key) or {}
        device = (target.get("devices") or {}).get(key)
        return device if isinstance(device, str) else None

    def _route_card_name(self, _parsed, cid: str) -> None:
        name = self._name_body(self._read_json())
        host_key, key = self._check_card_host(cid, self.host_store.cards()["hosts"])
        if key is None:
            raise ApiError(404, "not_found", "a machine is renamed at /hosts/<id>/name")

        def change(state: dict[str, Any]) -> dict[str, Any] | None:
            if state["card_names"].get(cid, "") == name:
                return None  # already so: no write
            if name:
                state["card_names"][cid] = name
            else:
                state["card_names"].pop(cid, None)
            return state

        self.host_store.write_all(change)
        ha = None
        device = self._ha_device(host_key, key)
        if device is not None:
            ha = "saved" if self.supervisor.set_device_name(device, name or None) else "failed"
            if ha == "saved":
                type(self)._ha_cache = None  # the next /ha/devices reads the new name
        self._send_json(200, {"id": cid, "name": name, "home_assistant": ha})

    def _route_host_name(self, _parsed, key: str) -> None:
        name = self._name_body(self._read_json())
        if key in ("self", "replicas"):
            raise ApiError(409, "fixed_name", "HAOS and REPLICA keep their names")

        def change(state: dict[str, Any]) -> dict[str, Any] | None:
            if not any(host_id(h["address"], h["port"]) == key for h in state["hosts"]):
                raise ApiError(404, "not_found", "no such field agent")
            if state["host_names"].get(key, "") == name:
                return None
            if name:
                state["host_names"][key] = name
            else:
                state["host_names"].pop(key, None)
            return state

        self.host_store.write_all(change)
        self._send_json(200, {"id": key, "name": name})

    def _find_card(self, host_key: str, cid: str) -> dict[str, Any] | None:
        """A card of the last read of its machine; read again when the last
        read does not have it."""
        response = self.cache.get(host_key).get("response")
        found = next((c for c in card_list(response) if c["card_id"] == cid), None) \
            if isinstance(response, dict) else None
        if found is None:
            target, _host = self._target(host_key)
            found = next((c for c in card_list(self._read_agent(host_key, *target))
                          if c["card_id"] == cid), None)
        return found

    def _route_card_delete(self, _parsed, cid: str) -> None:
        host_key, key = self._check_card_host(cid, self.host_store.cards()["hosts"])
        if host_key == "self":
            raise ApiError(409, "cannot_remove", "A drive of this machine can be hidden, not removed.")
        host_removed = False
        if host_key == "replicas":
            item = self._find_card("replicas", cid)
            if item is None or item.get("type") == "filesystem":
                raise ApiError(404, "not_found", "no such replica")
            reply = self._mock_or_down("DELETE", self._replica_path(str(item["id"])))
            if reply.status != 200:
                raise ApiError(404, "not_found", "no such replica")
            gone = {cid}
        else:
            response = self.cache.get(host_key).get("response")
            if key is not None and key not in card_keys(host_key, response):
                # The last read does not have this card (or there is none):
                # read the machine, so every card it has is counted.
                target, _host = self._target(host_key)
                self._read_agent(host_key, *target)
                response = self.cache.get(host_key).get("response")
            outcome: dict[str, Any] = {}

            def change_host(state: dict[str, Any]) -> dict[str, Any]:
                host = next((h for h in state["hosts"]
                             if host_id(h["address"], h["port"]) == host_key), None)
                if host is None:
                    raise ApiError(404, "not_found", "no such field agent")
                if key is not None and key not in host["removed"]:
                    host["removed"].append(key)
                # The last card of a machine takes the machine with it:
                # drives, pools and FILESYSTEM cards all count, hidden or
                # not, and those removed before are gone.
                outcome["host"] = key is None or every_card_removed(host, response)
                if outcome["host"]:
                    state["hosts"] = [h for h in state["hosts"] if h is not host]
                return state

            gone = {cid}
            self.host_store.write_all(lambda state: _drop_cards(change_host(state), host_key,
                                                                cid, outcome))
            host_removed = bool(outcome.get("host"))
            if host_removed:
                self.cache.drop(host_key)
                log(f"Field agent {host_key} removed with its last card")
            self._send_json(200, {"removed": cid, "host_removed": host_removed})
            return
        # A replica's id leaves the order and the hidden list, if it was there.
        self.host_store.write_all(lambda state: _drop_ids(state, gone))
        self._send_json(200, {"removed": cid, "host_removed": host_removed})

    def _route_card_file(self, _parsed, cid: str) -> None:
        host_key, key = self._check_card_host(cid, self.host_store.hosts_quick())
        if key is None:
            raise ApiError(404, "not_found", "no such card")
        item = self._find_card(host_key, cid)
        if item is None or item.get("type") == "filesystem":
            raise ApiError(404, "not_found", "no such card")
        if host_key == "replicas":
            reply = self._mock_or_down("GET", self._replica_path(str(item["id"])) + "/replica")
            if reply.status != 200:
                raise ApiError(404, "not_found", "no such replica")
            self._send_file(reply, str(item["id"]))
            return
        if item.get("type") == "pool":
            # A real agent reports a pool without its disk layout, which a
            # replica file needs, so the file is the pool as the agent sent it.
            pool = item.get("pool") or {}
            doc = {"pool": pool, "filesystems": item.get("filesystems") or []}
            self._send_file(json.dumps(doc, indent=2).encode(), f"pool-{pool.get('name')}")
            return
        # A real drive: saved as a replica file (serial and WWN replaced), and
        # that file is the download.
        entry = self._save_replica_file(host_key, str(item["id"]))
        reply = self._mock_or_down("GET", f"/api/replicas/{quote(str(entry.get('id')), safe='')}")
        if reply.status != 200:
            raise ApiError(502, "replica_agent_failed", "the saved file could not be read back")
        self._send_file(reply, str(entry.get("id")))

    # ── /cards/<id>/thresholds ──
    #
    # One source of truth per drive: when the drive is in Home Assistant
    # (the integration has its device: the blue exit icon) the integration's
    # stored thresholds, read from the entry's diagnostics, and written
    # through its options flow; otherwise the app's own, in the app options.

    def _threshold_resolver(self, host_key: str) -> Callable[[str, str], tuple[str, dict[str, int], str | None]]:
        """For one read of a machine: (card id, drive id) -> (source,
        overrides, entry id). Home Assistant is looked up once, on the first
        drive that needs it; each entry's thresholds are read once."""
        memo: dict[str, Any] = {}

        def resolve(cid: str, drive_id: str) -> tuple[str, dict[str, int], str | None]:
            if "lookup" not in memo:
                memo["lookup"] = self._ha_lookup() if self.supervisor.available else None
            entry_id = self._entry_for_card(memo["lookup"], host_key, cid)
            if entry_id is not None:
                return "integration", self._integration_thresholds(entry_id, drive_id), entry_id
            return "app", dict(self.host_store.thresholds_quick().get(cid) or {}), None

        return resolve

    @staticmethod
    def _entry_for_card(lookup: dict[str, Any] | None, host_key: str, cid: str) -> str | None:
        """The integration entry that has this card's device, or None when
        the card is not in Home Assistant."""
        if not lookup or not lookup.get("reachable") or lookup.get("installed") is False:
            return None
        target = (lookup.get("targets") or {}).get(host_key)
        if not target:
            return None
        key = split_card_id(cid)[1]
        if key and (key in (target.get("devices") or {}) or lookup.get("lookup") == "entries_only"):
            return target.get("entry_id")
        return None

    def _thr_fresh(self, at: float) -> bool:
        """Home Assistant's thresholds are kept as long as its devices are,
        and at least a few seconds, so one request reads them once."""
        return time.monotonic() - at < max(type(self).ha_cache_seconds, THRESHOLD_MIN_SECONDS)

    def _integration_thresholds(self, entry_id: str, drive_id: str) -> dict[str, int]:
        """The overrides the integration stores for this drive: from the
        entry's diagnostics, which carry them per drive; when they do not,
        from its options flow's threshold form, left without saving."""
        cls = type(self)
        cached = cls._thr_cache.get(entry_id)
        if cached is None or not self._thr_fresh(cached[0]):
            cached = (time.monotonic(), self._diagnostics_thresholds(entry_id))
            cls._thr_cache[entry_id] = cached
        drives = cached[1]
        if drives is not None and drive_id in drives:
            return dict(drives[drive_id])
        flow = cls._thr_flow_cache.get((entry_id, drive_id))
        if flow is None or not self._thr_fresh(flow[0]):
            try:
                values = self._flow_read_thresholds(entry_id, drive_id)
            except ApiError as err:
                log(f"Could not read the thresholds of {drive_id} from Home Assistant: {err.message}")
                return {}
            flow = (time.monotonic(), values)
            cls._thr_flow_cache[(entry_id, drive_id)] = flow
        return dict(flow[1])

    def _diagnostics_thresholds(self, entry_id: str) -> dict[str, dict[str, int]] | None:
        """{drive id: overrides} from the entry's diagnostics, for the drives
        whose attention block names its thresholds (integration 0.7 and
        later); None when the diagnostics cannot be read."""
        if entry_id in self._diagnostics_failed:
            return None
        status, diag = self.supervisor.core(
            "GET", f"/diagnostics/config_entry/{quote(entry_id, safe='')}")
        if status != 200 or not isinstance(diag, dict):
            self._diagnostics_failed.add(entry_id)
            log(f"No diagnostics for entry {entry_id}; not asked again")
            return None
        data = diag.get("data") if isinstance(diag.get("data"), dict) else diag
        out: dict[str, dict[str, int]] = {}
        drives = data.get("drives") if isinstance(data, dict) else None
        for drive_id, block in (drives.items() if isinstance(drives, dict) else []):
            attention = block.get("attention") if isinstance(block, dict) else None
            if isinstance(attention, dict) and "thresholds" in attention:
                out[str(drive_id)] = clean_overrides(attention.get("thresholds"))
        return out

    def _defaults(self) -> Callable[[str], int | None]:
        attention = self.rules.attention if self.rules.load() else None
        if attention is None:
            return lambda _label: None
        return lambda label: int(attention.default_threshold(label))

    def _options_flow(self, entry_id: str, drive_id: str) -> tuple[str, dict[str, Any]]:
        """Start the integration's options flow for this entry and walk it
        to the threshold form of this drive: (flow path, the form step).
        ApiError 502 with the flow's own words when it goes elsewhere; the
        flow is then aborted."""
        sv = self.supervisor
        status, step = sv.core("POST", "/config/config_entries/options/flow",
                               {"handler": entry_id, "show_advanced_options": False})
        if status != 200 or not isinstance(step, dict) or not step.get("flow_id"):
            raise ApiError(502, "integration_failed", _flow_message(step, status))
        path = f"/config/config_entries/options/flow/{quote(str(step['flow_id']), safe='')}"
        try:
            if step.get("type") == "menu":
                status, step = sv.core("POST", path, {"next_step_id": "thresholds"})
            if (status == 200 and isinstance(step, dict) and step.get("type") == "form"
                    and step.get("step_id") == "thresholds"):
                status, step = sv.core("POST", path, {"drive_id": drive_id})
            if not (status == 200 and isinstance(step, dict) and step.get("type") == "form"
                    and step.get("step_id") == "threshold_form"):
                raise ApiError(502, "integration_failed", _flow_message(step, status))
        except ApiError:
            if isinstance(step, dict) and step.get("type") in ("form", "menu"):
                sv.core("DELETE", path)
            raise
        return path, step

    def _flow_read_thresholds(self, entry_id: str, drive_id: str) -> dict[str, int]:
        """The overrides as the threshold form prefills them (the value in
        force per label), the flow aborted without saving."""
        path, step = self._options_flow(entry_id, drive_id)
        self.supervisor.core("DELETE", path)
        default = self._defaults()
        out: dict[str, int] = {}
        for field in step.get("data_schema") or []:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            label, value = field["name"], field.get("default")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            base = default(label)
            if base is not None and int(value) != base:
                out[label] = int(value)
        return out

    def _flow_write_thresholds(self, entry_id: str, drive_id: str,
                               overrides: dict[str, int]) -> None:
        """Save these overrides for the drive through the integration's
        options flow: every label of its threshold form, the default where
        there is no override, so an override not wanted any more clears."""
        path, step = self._options_flow(entry_id, drive_id)
        default = self._defaults()
        values: dict[str, int] = {}
        for field in step.get("data_schema") or []:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            label = field["name"]
            base = default(label)
            if base is None:
                base = field.get("default") if isinstance(field.get("default"), (int, float)) else 0
            values[label] = int(overrides.get(label, base))
        status, result = self.supervisor.core("POST", path, values)
        if status != 200 or not isinstance(result, dict) or result.get("type") != "create_entry":
            if isinstance(result, dict) and result.get("type") in ("form", "menu"):
                self.supervisor.core("DELETE", path)
            raise ApiError(502, "integration_failed", _flow_message(result, status))
        self._forget_thresholds(entry_id)
        log(f"Thresholds of {drive_id} saved in Home Assistant ({len(overrides)} changed from the default)")

    def _forget_thresholds(self, entry_id: str | None = None) -> None:
        cls = type(self)
        if entry_id is None:
            cls._thr_cache = {}
            cls._thr_flow_cache = {}
            return
        cls._thr_cache.pop(entry_id, None)
        cls._thr_flow_cache = {k: v for k, v in cls._thr_flow_cache.items() if k[0] != entry_id}

    def _threshold_card(self, cid: str) -> tuple[str, dict[str, Any]]:
        """(host id, item) for a real or replica drive card; 404 for a pool,
        a FILESYSTEM card, a machine's own card or an unknown one. The
        stored options and Home Assistant's thresholds are read now."""
        self._forget_thresholds()
        try:
            hosts = self.host_store.hosts()
        except ApiError:
            hosts = self.host_store.hosts_quick()
        host_key, key = self._check_card_host(cid, hosts)
        if key is None:
            raise ApiError(404, "not_found", "no such drive")
        item = self._find_card(host_key, cid)
        if item is None or item.get("type") != "drive" or not isinstance(item.get("drive"), dict):
            raise ApiError(404, "not_found", "no such drive")
        if not self.rules.load():
            raise ApiError(503, "rules_unavailable", "the integration rules are not loaded")
        return host_key, item

    def _threshold_answer(self, cid: str, source: str, item: dict[str, Any],
                          overrides: dict[str, int]) -> dict[str, Any]:
        work = dict(item["drive"])
        verdict = self.rules.evaluate(work, overrides)
        return {"card_id": cid, "source": source, "labels": verdict["limits"]}

    def _route_thresholds_get(self, _parsed, cid: str) -> None:
        host_key, item = self._threshold_card(cid)
        source, overrides, _entry = self._threshold_resolver(host_key)(cid, str(item["id"]))
        self._send_json(200, self._threshold_answer(cid, source, item, overrides))

    def _save_thresholds(self, cid: str, change: Callable[[list[dict[str, Any]], dict[str, int]], dict[str, int]]) -> None:
        """Read the thresholds in force, apply change(limits, overrides) ->
        new overrides, store them where they live, and answer the result."""
        host_key, item = self._threshold_card(cid)
        drive_id = str(item["id"])
        source, overrides, entry_id = self._threshold_resolver(host_key)(cid, drive_id)
        limits = self._threshold_answer(cid, source, item, overrides)["labels"]
        wanted = change(limits, dict(overrides))
        by_label = {lim["label"]: lim for lim in limits}
        # Only genuine overrides are stored: a value equal to the default
        # clears its label, as typing the default does in Home Assistant.
        wanted = {label: int(value) for label, value in wanted.items()
                  if label in by_label and int(value) != by_label[label]["default"]}
        if source == "integration" and entry_id is not None:
            self._flow_write_thresholds(entry_id, drive_id, wanted)
        else:
            def write(state: dict[str, Any]) -> dict[str, Any] | None:
                if (state["thresholds"].get(cid) or {}) == wanted:
                    return None
                if wanted:
                    state["thresholds"][cid] = wanted
                else:
                    state["thresholds"].pop(cid, None)
                return state

            self.host_store.write_all(write)
        self._send_json(200, self._threshold_answer(cid, source, item, wanted))

    def _route_thresholds_put(self, _parsed, cid: str) -> None:
        body = self._read_json()
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict):
            raise ApiError(400, "bad_request", 'give {"values": {"<label>": <number>, ...}}')
        for label, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 \
                    or int(value) != value:
                raise ApiError(400, "bad_request", f"{label}: a whole number from 0 up")

        def change(limits: list[dict[str, Any]], overrides: dict[str, int]) -> dict[str, int]:
            known = {lim["label"] for lim in limits}
            unknown = [label for label in values if label not in known]
            if unknown:
                raise ApiError(400, "bad_request", f"this drive has no threshold {unknown[0]}")
            return {**overrides, **{label: int(v) for label, v in values.items()}}

        self._save_thresholds(cid, change)

    def _route_thresholds_accept(self, _parsed, cid: str) -> None:
        def change(limits: list[dict[str, Any]], overrides: dict[str, int]) -> dict[str, int]:
            flagged = [lim for lim in limits if lim["flagged"]]
            if not flagged:
                raise ApiError(409, "nothing_flagged", "no reading of this drive is flagged")
            for lim in flagged:
                overrides[lim["label"]] = accept_value(int(lim["reading"]), lim["compare"])
            return overrides

        self._save_thresholds(cid, change)

    def _route_thresholds_reset(self, _parsed, cid: str) -> None:
        self._save_thresholds(cid, lambda _limits, _overrides: {})

    def _copy_thresholds(self, target: str) -> list[str]:
        """After a machine is added to Home Assistant: each card of it whose
        device is now there takes its app thresholds into the integration,
        and they leave the app options. Returns the card ids copied."""
        app = {cid: v for cid, v in self.host_store.thresholds_quick().items()
               if split_card_id(cid)[0] == target and v}
        if not app:
            return []
        self._forget_thresholds()
        lookup = self._ha_lookup(fresh=True)
        response = self.cache.get(target).get("response")
        cards = {c["card_id"]: c for c in card_list(response)} if isinstance(response, dict) else {}
        copied: list[str] = []
        for cid, overrides in app.items():
            item = cards.get(cid)
            entry_id = self._entry_for_card(lookup, target, cid)
            if item is None or entry_id is None:
                continue
            try:
                self._flow_write_thresholds(entry_id, str(item.get("id")), overrides)
            except ApiError as err:
                log(f"Thresholds of {cid} stay in the app: {err.message}")
                continue
            copied.append(cid)
        if copied:
            def drop(state: dict[str, Any]) -> dict[str, Any]:
                for cid in copied:
                    state["thresholds"].pop(cid, None)
                return state

            self.host_store.write_all(drop)
            log(f"Thresholds of {len(copied)} card{'s' if len(copied) > 1 else ''} moved to Home Assistant")
        return copied

    # ── /ha/devices ──

    def _own_hosts(self) -> set[str]:
        hosts = {self.agent_host.lower()}
        try:
            hosts.add(socket.gethostname().split(".")[0].lower())
        except OSError:
            pass
        return hosts

    def _entry_config(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """host and port from an entry's diagnostics, or None.

        Only a loaded entry has diagnostics: Core answers 500 for one that
        never set up (setup_retry), so those are not asked. An entry whose
        diagnostics failed once is not asked again while the proxy runs, so
        a refresh does not fill Core's log."""
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id:
            return None
        if entry.get("state") != "loaded" or entry_id in self._diagnostics_failed:
            return None
        status, diag = self.supervisor.core(
            "GET", f"/diagnostics/config_entry/{quote(entry_id, safe='')}"
        )
        if status != 200 or not isinstance(diag, dict):
            self._diagnostics_failed.add(entry_id)
            log(f"No diagnostics for entry {entry_id}; not asked again")
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
        config = self._entry_config(entry)
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
                        "names": {},
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
                        names = {k: device.get(k) if isinstance(device.get(k), str) else None
                                 for k in ("name", "name_by_user")}

                        def put(key: str) -> None:
                            target["devices"][key] = device["id"]
                            target["names"][key] = names

                        serial = device.get("serial_number")
                        if isinstance(serial, str) and serial:
                            put(serial)
                        prefix = f"{entry_id}_zpool_"
                        for ident in device.get("identifiers") or []:
                            if not (isinstance(ident, list) and len(ident) == 2
                                    and ident[0] == INTEGRATION_DOMAIN):
                                continue
                            if str(ident[1]).startswith(prefix):
                                put(f"pool:{str(ident[1])[len(prefix):]}")
                            elif not serial and not str(ident[1]).startswith(f"{entry_id}_"):
                                # A drive with no serial is known by its drive id.
                                put(f"id:{ident[1]}")
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
        if outcome.get("result") in ("created", "reloaded", "already_added"):
            copied = self._copy_thresholds(target)
            if copied:
                outcome["thresholds_copied"] = copied
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
        """Serve a static file from static_dir.

        The page names its scripts and stylesheet with a ?v= of their
        content, so a browser fetches cc.js again the moment it changes and
        never runs an old script against a new page. Those versioned URLs
        may be cached for good; everything else is revalidated by ETag.
        """
        if path == "/" or path == "":
            path = "/index.html"

        # Security: prevent directory traversal
        root = os.path.realpath(self.static_dir)
        safe_path = os.path.normpath(path).lstrip("/")
        file_path = os.path.realpath(os.path.join(root, safe_path))
        if file_path != root and not file_path.startswith(root + os.sep):
            self.send_error(403, "Forbidden")
            return

        if not os.path.isfile(file_path):
            # Fall back to index.html for SPA routing
            file_path = os.path.join(root, "index.html")
            if not os.path.isfile(file_path):
                self.send_error(404, "Not Found")
                return
        is_page = os.path.basename(file_path) == "index.html"

        content_type, _ = mimetypes.guess_type(file_path)
        if content_type is None:
            content_type = "application/octet-stream"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
        except OSError:
            self.send_error(500, "Internal Server Error")
            return
        if is_page:
            content = _version_assets(content, root)

        etag = '"' + hashlib.md5(content).hexdigest() + '"'
        versioned = not is_page and "v=" in (urlparse(self.path).query or "")
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", _IMMUTABLE if versioned else "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", _IMMUTABLE if versioned else "no-cache")
        self.end_headers()
        self.wfile.write(content)


_IMMUTABLE = "public, max-age=31536000, immutable"
_ASSET_REF = re.compile(rb'(\s(?:src|href)=")([A-Za-z0-9_.-]+\.(?:js|css))(")')


def _version_assets(page: bytes, root: str) -> bytes:
    """Give each local script and stylesheet the page names a ?v= of its
    content, so a cached copy from an older build is never used."""
    def stamp(match: re.Match[bytes]) -> bytes:
        name = match.group(2).decode()
        try:
            with open(os.path.join(root, name), "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()[:12]
        except OSError:
            return match.group(0)
        return match.group(1) + match.group(2) + b"?v=" + digest.encode() + match.group(3)

    return _ASSET_REF.sub(stamp, page)


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
    parser.add_argument("--static-dir", type=str, default=STATIC_DIR,
                        help="Where the page, its scripts and images are")
    parser.add_argument("--data-dir", type=str, default="/data",
                        help="Where fleet-cache.json is kept (empty: memory only)")
    args = parser.parse_args()

    ProxyHandler.agent_port = args.agent_port
    ProxyHandler.mock_port = args.mock_port
    ProxyHandler.static_dir = args.static_dir
    ProxyHandler.token = args.token
    ProxyHandler.app_version = args.app_version
    ProxyHandler.agent_host = args.agent_host.strip() or DEFAULT_AGENT_HOST
    ProxyHandler.mdns_name = args.mdns_name.strip() or None
    ProxyHandler.rules = Rules(args.rules_dir)
    ProxyHandler.supervisor = Supervisor(
        args.supervisor_url, os.environ.get("SUPERVISOR_TOKEN", "")
    )
    ProxyHandler.ha_cache_seconds = args.ha_cache_seconds
    ProxyHandler.cache = FleetCache(
        os.path.join(args.data_dir, "fleet-cache.json") if args.data_dir else None
    )
    ProxyHandler.host_store = HostStore(ProxyHandler.supervisor, ProxyHandler.cache)

    server = ThreadingHTTPServer(("0.0.0.0", args.port), ProxyHandler)
    server.daemon_threads = True
    log(f"Serving on port {args.port}")
    log(f"Proxying /api/* -> 127.0.0.1:{args.agent_port}")
    log(f"Proxying /mock/* -> 127.0.0.1:{args.mock_port}")
    log(f"Integration rules: {ProxyHandler.rules.version or 'not found'}")
    log(f"Agent host for the integration: {ProxyHandler.agent_host}")
    if not ProxyHandler.supervisor.available:
        log("No Supervisor token: field agents cannot be stored and Home Assistant routes are off")
    else:
        threading.Thread(target=drop_stale_options, args=(ProxyHandler.host_store,),
                         daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
