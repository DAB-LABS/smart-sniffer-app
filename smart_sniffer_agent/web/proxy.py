#!/usr/bin/env python3
"""SMART Sniffer App: Web UI proxy server.

Serves static files from /opt/web/ at the root path and proxies:
  /api/*  -> localhost:$AGENT_PORT  (this app's Go agent, app token injected)

The page shows this box only: the drives of this app's own agent, read only,
and one card that leads to the SMART Sniffer integration's Control Center
(or says how to install or update it). Every other machine lives in the
integration. The proxy answers the page's own routes:
  GET  /app/info                 versions, ports, identity
  POST /app/notice/dismiss       close the install or the update card, for
                                 every browser ({"notice": "install"|"update"})
  GET  /fleet/hosts/self/drives  one judged read of this box's agent
  GET  /cards/<id>/file          one of this box's cards as a JSON file
  GET  /ha/integration           the integration's state for the page: loaded
                                 (v0.9+), no entries, restart pending, update
                                 (0.8.x), missing; whether HAOS is an entry
  POST /ha/add                   create the integration entry for this box
                                 ({"target": "self"})
  POST /app/handover             send the field machines to the integration
                                 again now (no button; a manual retry)

It also tells the integration where this box's agent is, through the
Supervisor's discovery (service smart_sniffer, host and port only), but only
while integration v0.9 or later is installed. The previous message is
deleted on every start and a new one posted, so Home Assistant always hears
of it once per run.

The verdict comes from the integration's own rule modules (attention.py,
devstat.py, extract.py, pool_health.py), which the Dockerfile takes from the
smart-sniffer release this app is built against. They import nothing from
Home Assistant.

drive_view(), pool_verdict(), pool_view() and Rules are importable without
side effects, so the page's dev harness can use them.

The Home Assistant routes talk to the Supervisor (and, through it, to Home
Assistant Core) with the SUPERVISOR_TOKEN the Supervisor puts in the app's
environment. The token is never logged and never sent to the browser.

The field machines, names, hidden cards, order and thresholds an older
version kept in the app options are handed over to the integration once it
is v0.9 or later and says it takes them (Handover), and cleared here as it
confirms each. This box's own settings are sent too and kept: its per-drive
thresholds are still read, so its verdicts match what 0.3.x showed.

Runs on the ingress port (8099) so Home Assistant can embed the page in the
sidebar. Every request from the page goes to the same origin, so there is no
CORS. The port listens on every interface of the container, but only Home
Assistant's ingress gateway (172.30.32.2) and the container itself
(127.0.0.1) are answered; anyone else gets 403 for every request. The agent
token comes from the AGENT_TOKEN environment variable, never the command line.
Python standard library only.
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

# The hostname Home Assistant uses to reach this app's agent from the
# integration. run.sh passes the container's real hostname with --agent-host
# (it follows the slug, so a local install is local-smart-sniffer-agent); this
# default, the published app's hostname, is only for running the proxy by hand.
DEFAULT_AGENT_HOST = "0449a086-smart-sniffer-agent"

INTEGRATION_DOMAIN = "smart_sniffer"
SUPERVISOR_TIMEOUT = 5  # seconds, every Supervisor and Core call
MAX_BODY = 64 * 1024  # largest request body taken, for every route and /api/*
# The only clients the proxy answers: Home Assistant's ingress gateway, which
# every page request comes through, and this container itself.
ALLOWED_CLIENTS = frozenset(ipaddress.ip_address(a) for a in ("172.30.32.2", "127.0.0.1", "::1"))
HA_API_CACHE_SECONDS = 30
NOTICE_DISMISSED = "integration_notice_dismissed"  # app-state.json: the install card closed
UPDATE_DISMISSED = "integration_update_dismissed"  # app-state.json: the update card closed
DISCOVERY_UUID = "discovery_uuid"  # app-state.json: this app's last discovery message
DISCOVERY_SERVICE = "smart_sniffer"
# The integration's first version with the Control Center panel; from it on
# the app posts its discovery and leads to the panel.
PANEL_VERSION = (0, 9)
DEFAULT_PANEL_PATH = "/smart-sniffer"
# How often the integration is looked up while no page asks: discovery
# follows an install or an update of the integration within this time.
WATCH_SECONDS = 300.0
WATCH_RETRY_SECONDS = 30.0  # sooner while Home Assistant does not answer

INTEGRATION_URL = "/config/integrations/integration/smart_sniffer"


def client_allowed(address: str) -> bool:
    """Whether a request from this address is answered: the ingress gateway
    or loopback, also when written as an IPv4-mapped IPv6 address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip in ALLOWED_CLIENTS


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
# The integration's own commands (smart_sniffer/info) and manifest/get are
# only on Home Assistant's websocket API, reached through this client: the HTTP upgrade, masked
# text frames out, text frames in (continuations joined), ping answered with
# pong, and close. No extensions, no TLS: the Supervisor's proxy to Core is
# plain ws:// on the internal network.

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_MESSAGE = 4 * 1024 * 1024


class WebSocketError(Exception):
    """The websocket could not be opened, or broke, or closed. ``reason`` is
    Home Assistant's own message when it refused a command, else the error;
    ``code`` its error code (unknown_command, not_found), None when Home
    Assistant gave no answer to the command at all."""

    def __init__(self, message: str, reason: str | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or message
        self.code = code


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
            reason = str(error.get("message") or error.get("code") or "no reason")
            code = error.get("code")
            raise WebSocketError(f"Home Assistant refused {command.get('type')}: {reason}", reason,
                                 str(code) if code else "unknown_error")
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

    def ws(self, command: dict[str, Any]) -> Any:
        """One Core websocket command; its result. Raises WebSocketError
        (with Home Assistant's error code in .code when it refused it)."""
        if not self.token:
            raise WebSocketError("the app has no Supervisor token")
        return ha_ws_command(self.ws_url(), self.token, command).get("result")

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
        None when Core cannot be reached."""
        if not self.available:
            return None
        if entries is None:
            entries = self.entries()
            if entries is None:
                return None
        if entries:
            return True
        status, config = self.core("GET", "/config")
        if status == 200 and isinstance(config, dict) and \
                INTEGRATION_DOMAIN in (config.get("components") or []):
            return True
        status, handlers = self.core("GET", "/config/config_entries/flow_handlers")
        if status == 200 and isinstance(handlers, list):
            return INTEGRATION_DOMAIN in handlers
        return None

    def hacs_installed(self) -> bool | None:
        """Whether HACS is set up in Home Assistant: hacs among the loaded
        components of Core's /api/config. None when Core does not say. A
        HACS that was downloaded but never set up is not loaded, and counts
        as missing: it still has to be set up before it can install the
        integration."""
        if not self.available:
            return None
        status, config = self.core("GET", "/config")
        if status != 200 or not isinstance(config, dict) or not isinstance(config.get("components"), list):
            return None
        return "hacs" in config["components"]

    def discovery_post(self, config: dict[str, Any]) -> str | None:
        """Post this app's discovery message; its uuid, or None when the
        Supervisor did not take it."""
        status, reply = self.call("POST", "/discovery",
                                  {"service": DISCOVERY_SERVICE, "config": config})
        data = reply.get("data") if isinstance(reply, dict) else None
        uuid = data.get("uuid") if isinstance(data, dict) else None
        if status == 200 and isinstance(uuid, str) and uuid:
            return uuid
        return None

    def discovery_delete(self, uuid: str) -> bool:
        """Delete one of this app's discovery messages. True when it is gone,
        also when the Supervisor no longer had it."""
        status, _ = self.call("DELETE", f"/discovery/{quote(uuid, safe='')}")
        return status in (200, 404)


# ── The integration, as this page needs it ──────────────────────────────────
#
# Looked up through the Supervisor's Core websocket:
#   1. smart_sniffer/info answers: integration v0.9 or later is loaded. It
#      gives whether HAOS is one of its machines and the panel's path.
#   2. Unknown command: manifest/get gives the installed version, with the
#      entries and the older installed check beside it.
#        0.9 or later, entries exist:  installed, Home Assistant not restarted
#        0.9 or later, no entries:     installed, no machine yet (not loaded)
#        older:                        0.8.x, to be updated
#        not there:                    missing
# A lookup that cannot finish keeps the state unknown (None); nothing on the
# page or in the discovery changes on an unknown.

STATE_LOADED = "loaded"
STATE_NO_ENTRIES = "no_entries"
STATE_RESTART = "restart"
STATE_UPDATE = "update"
STATE_MISSING = "missing"
PANEL_STATES = (STATE_LOADED, STATE_NO_ENTRIES, STATE_RESTART)
_PANEL_PATH_RE = re.compile(r"^/[a-z0-9][a-z0-9_-]{0,63}$")


def version_tuple(text: Any) -> tuple[int, ...] | None:
    """(0, 9, 2) from "0.9.2", "v0.9.0-beta.1" or "0.10"; None when it is
    not a version."""
    if not isinstance(text, str):
        return None
    match = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", text.strip())
    if not match:
        return None
    return tuple(int(g) for g in match.groups() if g is not None)


def panel_capable(version: Any) -> bool:
    found = version_tuple(version)
    return found is not None and found[:2] >= PANEL_VERSION


def detect_integration(sv: Supervisor) -> dict[str, Any]:
    """One lookup: {"state", "version", "panel", "panel_path", "haos_entry",
    "app_import"} (app_import: the handover version it takes, or None).
    state None when it could not be told."""
    out: dict[str, Any] = {"state": None, "version": None, "panel": None,
                           "panel_path": DEFAULT_PANEL_PATH, "haos_entry": None,
                           "app_import": None}
    if not sv.available:
        return out
    try:
        info = sv.ws({"type": f"{INTEGRATION_DOMAIN}/info"})
    except WebSocketError as err:
        if err.code is None:
            return out  # Core not answering: unknown
        if err.code != "unknown_command":
            # The command is there but refused: v0.9+ is loaded; what it
            # knows is not.
            log(f"smart_sniffer/info refused: {err.reason}")
            out.update(state=STATE_LOADED, panel=True)
            return out
    else:
        info = info if isinstance(info, dict) else {}
        path = info.get("panel_path")
        out.update(
            state=STATE_LOADED,
            version=info.get("version") if isinstance(info.get("version"), str) else None,
            panel=info.get("panel") is not False,
            panel_path=path if isinstance(path, str) and _PANEL_PATH_RE.match(path) else DEFAULT_PANEL_PATH,
            haos_entry=info.get("haos_entry") if isinstance(info.get("haos_entry"), bool) else None,
            app_import=info.get("app_import") if isinstance(info.get("app_import"), int) else None,
        )
        return out
    # Not loaded, or older than 0.9: the version on disk.
    version: str | None = None
    missing = False
    try:
        manifest = sv.ws({"type": "manifest/get", "integration": INTEGRATION_DOMAIN})
        if isinstance(manifest, dict) and isinstance(manifest.get("version"), str):
            version = manifest["version"]
    except WebSocketError as err:
        if err.code is None:
            return out
        missing = err.code == "not_found"
    entries = sv.entries()
    if entries is None:
        return out
    out["version"] = version
    if version is None:
        installed = False if missing and not entries else sv.integration_installed(entries)
        if installed is None:
            return out
        # Installed but no version to tell by: treated as an older one.
        out["state"] = STATE_UPDATE if installed else STATE_MISSING
        return out
    if panel_capable(version):
        out["state"] = STATE_RESTART if entries else STATE_NO_ENTRIES
        out["haos_entry"] = False if not entries else None
    else:
        out["state"] = STATE_UPDATE
    return out


class Discovery:
    """This app's Supervisor discovery message for the integration.

    Posted only while integration v0.9 or later is installed (older versions
    do not know the service and would show an empty Discovered card), and
    deleted otherwise. The first sync of a run deletes the message of the
    previous run, whose uuid the app keeps, and posts a new one: the new uuid
    always reaches Core, so the integration hears of the agent once per run.
    The message names the agent's host and port only; no token."""

    def __init__(self, sv: Supervisor, state: AppState, host: str, port: int) -> None:
        self.sv = sv
        self.state = state
        self.host = host
        self.port = port
        self.lock = threading.Lock()
        self.started = False

    def config(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port}

    def _delete(self) -> None:
        uuid = self.state.get(DISCOVERY_UUID)
        if not isinstance(uuid, str) or not uuid:
            return
        if self.sv.discovery_delete(uuid):
            self.state.set(DISCOVERY_UUID, None)
            log("Discovery for the integration withdrawn")

    def sync(self, integration_state: str | None) -> None:
        if not self.sv.available:
            return
        with self.lock:
            if not self.started:
                self._delete()
                if self.state.get(DISCOVERY_UUID):
                    return  # the old one could not be deleted: try again next time
                self.started = True
            if integration_state is None:
                return
            if integration_state in PANEL_STATES:
                if not self.state.get(DISCOVERY_UUID):
                    uuid = self.sv.discovery_post(self.config())
                    if uuid:
                        self.state.set(DISCOVERY_UUID, uuid)
                        log(f"Discovery posted for the integration: {self.host}:{self.port}")
                    else:
                        log("The Supervisor did not take the discovery for the integration")
            else:
                self._delete()


class IntegrationWatch:
    """The integration's state, read at most every ha_cache_seconds; one
    lookup at a time. Every lookup also keeps the discovery in step."""

    def __init__(self, sv: Supervisor, discovery: Discovery | None,
                 cache_seconds: float = HA_API_CACHE_SECONDS) -> None:
        self.sv = sv
        self.discovery = discovery
        self.cache_seconds = cache_seconds
        self.lock = threading.Lock()
        self.cached: tuple[float, dict[str, Any]] | None = None
        self.last_known: dict[str, Any] | None = None

    def get(self, fresh: bool = False) -> dict[str, Any]:
        started = time.monotonic()
        with self.lock:
            cached = self.cached
            if cached and (cached[0] >= started or
                           (not fresh and time.monotonic() - cached[0] < self.cache_seconds)):
                return cached[1]
            result = detect_integration(self.sv)
            if result["state"] is None and self.last_known is not None:
                # Unknown now (Core restarting): the page keeps its last state.
                result = {**self.last_known, "stale": True}
            elif result["state"] is not None:
                self.last_known = result
            self.cached = (time.monotonic(), result)
        if self.discovery is not None and not result.get("stale"):
            try:
                self.discovery.sync(result["state"])
            except Exception as err:  # noqa: BLE001 -- the page answers anyway
                log(f"Discovery failed: {type(err).__name__}: {err}")
        return result

    def forget(self) -> None:
        with self.lock:
            self.cached = None

    def peek(self) -> dict[str, Any] | None:
        """The last lookup's result, without a lookup of its own."""
        cached = self.cached
        return cached[1] if cached else None

    def run(self) -> None:
        """The background watch: a lookup at start, then every
        WATCH_SECONDS (WATCH_RETRY_SECONDS while the state is unknown)."""
        while True:
            try:
                result = self.get(fresh=True)
                wait = WATCH_RETRY_SECONDS if result.get("state") is None else WATCH_SECONDS
            except Exception as err:  # noqa: BLE001 -- the watch never stops
                log(f"Integration lookup failed: {type(err).__name__}: {err}")
                wait = WATCH_RETRY_SECONDS
            time.sleep(wait)


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
    """One HTTP request to an agent. Never raises.
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



def _is_agent_health(reply: AgentReply) -> bool:
    body = reply.body
    return (
        reply.status == 200
        and isinstance(body, dict)
        and isinstance(body.get("version"), str)
        and isinstance(body.get("endpoints"), list)
        and "/api/drives" in body["endpoints"]
    )


# ── Small stores in /data, and the options of older versions ────────────────

class AppState:
    """/data/app-state.json: small page settings that follow the user to every
    browser, such as the closed install card, and the uuid of this app's
    discovery message. Written atomically."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.values: dict[str, Any] = {}
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    self.values = data
            except (OSError, ValueError):
                pass

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self.lock:
            if self.values.get(key) == value:
                return
            self.values[key] = value
            if not self.path:
                return
            tmp = f"{self.path}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(self.values, handle)
                os.replace(tmp, self.path)
            except OSError as err:
                log(f"Cannot write {self.path}: {err.strerror or err}; kept in memory")


class ReadCache:
    """The last good read of this box's agent, so the page shows it (marked
    stale) while the agent does not answer. Kept in memory. An older
    version's /data/fleet-cache.json is read once at start for this box's
    entry and never written; the Handover removes it once the integration
    has taken every field machine."""

    def __init__(self, legacy_path: str | None = None) -> None:
        self.lock = threading.Lock()
        self.hosts: dict[str, dict[str, Any]] = {}
        if legacy_path:
            try:
                with open(legacy_path, encoding="utf-8") as handle:
                    data = json.load(handle)
                hosts = data.get("hosts") if isinstance(data, dict) else None
                own = hosts.get("self") if isinstance(hosts, dict) else None
                if isinstance(own, dict):
                    self.hosts["self"] = {k: own[k] for k in ("response", "last_answer") if k in own}
            except (OSError, ValueError):
                pass

    def get(self, key: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.hosts.get(key) or {})

    def update(self, key: str, **fields: Any) -> None:
        with self.lock:
            self.hosts.setdefault(key, {}).update(fields)


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


# Options of older versions, removed at start when the Supervisor does not
# report the schema to compare with.
REMOVED_OPTIONS = ("mock_mode", "mock_port")


def _schema_names(schema: Any) -> set[str]:
    """Top-level option names from the Supervisor's schema: a list of
    {"name": ...} (its UI form) or a mapping. Empty when unknown."""
    if isinstance(schema, dict):
        return {str(k) for k in schema}
    if isinstance(schema, list):
        return {str(item["name"]) for item in schema if isinstance(item, dict) and item.get("name")}
    return set()


class LegacyOptions:
    """The app options as 0.3.x left them. Read only here, except that
    options the schema no longer has (mock_port, mock_mode) are dropped once
    at start; every other option is written back exactly as it was read.
    The field machines and their settings leave through the Handover, which
    shares this lock."""

    MEMO_SECONDS = 60

    def __init__(self, supervisor: Supervisor) -> None:
        self.sv = supervisor
        self.lock = threading.Lock()
        self._thresholds: dict[str, dict[str, int]] | None = None
        self._memo_at = 0.0

    def _info(self) -> dict[str, Any]:
        if not self.sv.available:
            raise ApiError(503, "supervisor_unavailable", "the Supervisor is not available")
        status, info = self.sv.call("GET", "/addons/self/info")
        data = info.get("data") if isinstance(info, dict) else None
        options = data.get("options") if isinstance(data, dict) else None
        if status != 200 or not isinstance(options, dict):
            raise ApiError(502, "supervisor_failed", "could not read the app options")
        return data  # type: ignore[return-value]

    def drop_stale_options(self) -> list[str]:
        with self.lock:
            data = self._info()
            options = data["options"]
            names = _schema_names(data.get("schema"))
            stale = [key for key in options
                     if (key not in names if names else key in REMOVED_OPTIONS)]
            if stale:
                kept = {k: v for k, v in options.items() if k not in stale}
                status, reply = self.sv.call("POST", "/addons/self/options", {"options": kept})
                if status != 200:
                    message = reply.get("message") if isinstance(reply, dict) else None
                    raise ApiError(502, "supervisor_failed", message or "could not save the app options")
            return stale

    def thresholds(self) -> dict[str, dict[str, int]]:
        """The per-card thresholds 0.3.x kept in the options, by card id, read
        again at most every MEMO_SECONDS; the last known (or none) when the
        Supervisor cannot be read."""
        if self._thresholds is not None and time.monotonic() - self._memo_at < self.MEMO_SECONDS:
            return self._thresholds
        try:
            self._thresholds = _normalize_thresholds(self._info()["options"].get("thresholds"))
        except ApiError:
            pass
        self._memo_at = time.monotonic()
        return self._thresholds or {}


def drop_stale_options(store: LegacyOptions) -> None:
    """Run once at start: remove options left from an older version."""
    try:
        stale = store.drop_stale_options()
    except ApiError as err:
        log(f"Could not check the app options for old settings: {err.message or err.code}")
        return
    if stale:
        log(f"Removed the old option{'s' if len(stale) > 1 else ''} {', '.join(stale)} from the app options")


# ── Handing the field machines over to the integration ──────────────────────
#
# App 0.3.x kept its field machines (address, port, password), their labels,
# card names, hidden cards, the card order and per-card thresholds in the
# app options. The SMART Sniffer integration (v0.9 and later) owns all of
# that now. Once its smart_sniffer/info says it takes a handover
# ("app_import"), the app sends everything once over the Supervisor's Core
# websocket (smart_sniffer/app/import starts a job; .../result is polled),
# then clears from its options exactly what the integration confirmed. This
# box's own settings (its thresholds, names, hidden cards) are sent too, so
# the integration has them for HAOS, and kept here: this page still uses
# them. Nothing is sent while the integration is missing or older.
#
# The integration keeps a ledger of what it did, so a lost answer, a retry
# or a second start never makes a machine twice or brings back one the user
# deleted. A machine it could not take stays here, with its password, and is
# sent again later (every HANDOVER_RETRY_SECONDS) until it is taken or the
# user discards it in the integration's panel.

HANDOVER_DONE = "handover_done"  # app-state.json: item ids of this box's settings sent
HANDOVER_RETRY_SECONDS = 600.0  # while something is left to send
HANDOVER_WAIT_SECONDS = 30.0  # while the integration cannot take it, or an answer was lost
HANDOVER_POLL_SECONDS = 2.0
HANDOVER_FIRST_SECONDS = 5.0  # after start, before the first look
HANDOVER_POLL_LIMIT = 300.0  # seconds a job is waited for
HANDOVER_FINAL = ("created", "created_offline", "matched", "applied", "pending", "kept",
                  "replicas_dropped", "discarded")
HANDOVER_SELF_ID = "self"
HANDOVER_ORDER_ID = "card_order"
HANDOVER_REPLICAS_ID = "replicas"
REPLICA_HOST_KEY = "replicas"
OPTION_LISTS = ("card_order", "hidden_cards", "thresholds", "card_names", "host_names")


def handover_host_key(address: str, port: int) -> str:
    """0.3.x's id of a field machine: "<address>:<port>", lower case."""
    return f"{address.lower()}:{int(port)}"


def handover_item_id(address: str, port: int) -> str:
    """A field machine's item id: the same for the same machine on every
    try, so the integration's ledger knows it again."""
    return hashlib.sha256(handover_host_key(address, port).encode()).hexdigest()


def _option_hosts(raw: Any) -> list[dict[str, Any]]:
    """The field machines in the options, as 0.3.x wrote them; anything
    malformed is left out (and left in the options)."""
    hosts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        port = item.get("port")
        if not isinstance(address, str) or not address.strip() or isinstance(port, bool) \
                or not isinstance(port, int) or not 0 < port < 65536:
            continue
        key = handover_host_key(address.strip(), port)
        if key in seen:
            continue
        seen.add(key)
        password = item.get("password")
        hosts.append({"address": address.strip(), "port": port, "key": key,
                      "password": password if isinstance(password, str) else "",
                      "hidden": item.get("hidden") is True,
                      "removed": _str_list(item.get("removed"))})
    return hosts


def _option_names(raw: Any, field: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get(field), str) and item[field] \
                and isinstance(item.get("name"), str) and item["name"].strip():
            out[item[field]] = " ".join(item["name"].split())[:100]
    return out


def _host_of(card: str) -> str:
    return split_card_id(card)[0].lower()


def build_handover(options: dict[str, Any], cache_hosts: dict[str, Any] | None = None,
                   sent: list[str] | None = None) -> list[dict[str, Any]]:
    """The items to send, from the options (and the old fleet cache's host
    names): one per field machine, this box's settings, the card order, and
    the old Replica settings to drop. Items already sent and kept here on
    purpose (``sent``: this box's settings, the order) are left out."""
    sent = sent or []
    hosts = _option_hosts(options.get("hosts"))
    names = _option_names(options.get("card_names"), "card")
    labels = _option_names(options.get("host_names"), "host")
    hidden = _str_list(options.get("hidden_cards"))
    order = _str_list(options.get("card_order"))
    thresholds = _normalize_thresholds(options.get("thresholds"))
    cards: dict[str, dict[str, dict[str, Any]]] = {}

    def card(cid: str) -> dict[str, Any] | None:
        host, key = split_card_id(cid)
        if not key:
            return None
        return cards.setdefault(host.lower(), {}).setdefault(key, {})

    for cid, name in names.items():
        found = card(cid)
        if found is not None:
            found["name"] = name
    for cid in hidden:
        found = card(cid)
        if found is not None:
            found["hidden"] = True
    for cid, values in thresholds.items():
        found = card(cid)
        if found is not None and values:
            found["thresholds"] = dict(values)
    items: list[dict[str, Any]] = []
    for host in hosts:
        item: dict[str, Any] = {
            "item_id": handover_item_id(host["address"], host["port"]), "kind": "field",
            "address": host["address"], "port": host["port"], "password": host["password"],
            "hidden": host["hidden"], "removed": host["removed"],
            "cards": cards.get(host["key"], {}),
        }
        if labels.get(host["key"]):
            item["name"] = labels[host["key"]]
        cached = ((cache_hosts or {}).get(host["key"]) or {}).get("response")
        agent = cached.get("agent") if isinstance(cached, dict) else None
        if isinstance(agent, dict):
            if isinstance(agent.get("hostname"), str) and agent["hostname"]:
                item["hostname"] = agent["hostname"][:253]
            if isinstance(agent.get("agent_id"), str) and agent["agent_id"]:
                item["agent_id"] = agent["agent_id"][:64]
        items.append(item)
    if cards.get("self") and HANDOVER_SELF_ID not in sent:
        items.append({"item_id": HANDOVER_SELF_ID, "kind": "self", "cards": cards["self"]})
    kept_order = [c for c in order if _host_of(c) != REPLICA_HOST_KEY]
    if kept_order and HANDOVER_ORDER_ID not in sent:
        items.append({"item_id": HANDOVER_ORDER_ID, "kind": "order", "order": kept_order})
    replica_keys = [c for c in [*names, *hidden, *order, *thresholds]
                    if _host_of(c) == REPLICA_HOST_KEY] + \
        [k for k in labels if k.lower() == REPLICA_HOST_KEY]
    if replica_keys:
        items.append({"item_id": HANDOVER_REPLICAS_ID, "kind": "replicas"})
    return items


def clear_handed_over(options: dict[str, Any], done_hosts: set[str],
                      drop_replicas: bool) -> dict[str, Any]:
    """The options without what the integration confirmed: those field
    machines (passwords included) and every name, hidden mark, order place
    and threshold of theirs; the old Replica settings when confirmed
    dropped. Settings of a machine no longer in the options (left by an
    older version) go once no field machine is left. This box's own
    settings and every other option stay as they are."""
    out = dict(options)
    hosts = options.get("hosts") if isinstance(options.get("hosts"), list) else []
    kept_hosts = [h for h in hosts if not (
        isinstance(h, dict) and isinstance(h.get("address"), str) and isinstance(h.get("port"), int)
        and handover_host_key(h["address"].strip(), h["port"]) in done_hosts)]
    out["hosts"] = kept_hosts
    remaining = {k["key"] for k in _option_hosts(kept_hosts)}

    def gone(host: str) -> bool:
        host = host.lower()
        if host == "self":
            return False
        if host == REPLICA_HOST_KEY:
            return drop_replicas
        return host in done_hosts or (not kept_hosts and host not in remaining)

    for name in OPTION_LISTS:
        raw = options.get(name)
        if not isinstance(raw, list):
            continue
        kept = []
        for item in raw:
            if name in ("card_order", "hidden_cards"):
                host = _host_of(item) if isinstance(item, str) else ""
            elif name == "host_names":
                host = str(item.get("host") or "") if isinstance(item, dict) else ""
            else:
                host = _host_of(str(item.get("card") or "")) if isinstance(item, dict) else ""
            if not gone(host):
                kept.append(item)
        out[name] = kept
    return out


class Handover:
    """Sends the field machines to the integration and clears what it took."""

    def __init__(self, sv: Supervisor, state: AppState, watch: IntegrationWatch,
                 data_dir: str | None, options_lock: Any = None) -> None:
        self.sv = sv
        # Shared with LegacyOptions: one read-modify-write of the options at
        # a time.
        self.options_lock = options_lock or threading.Lock()
        self.state = state
        self.watch = watch
        self.data_dir = data_dir
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.last: dict[str, Any] | None = None

    def ready(self, integration: dict[str, Any]) -> bool:
        """Integration v0.9 or later loaded, and it takes a handover."""
        version = integration.get("app_import")
        return (integration.get("state") == STATE_LOADED and not integration.get("stale")
                and panel_capable(integration.get("version"))
                and isinstance(version, int) and not isinstance(version, bool) and version >= 1)

    def _options(self) -> dict[str, Any] | None:
        status, info = self.sv.call("GET", "/addons/self/info")
        data = info.get("data") if isinstance(info, dict) else None
        options = data.get("options") if isinstance(data, dict) else None
        return options if status == 200 and isinstance(options, dict) else None

    def _cache_hosts(self) -> dict[str, Any]:
        if not self.data_dir:
            return {}
        try:
            with open(os.path.join(self.data_dir, "fleet-cache.json"), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        hosts = data.get("hosts") if isinstance(data, dict) else None
        return hosts if isinstance(hosts, dict) else {}

    def pending(self) -> list[dict[str, Any]] | None:
        """What is left to send; None when the options cannot be read."""
        options = self._options()
        if options is None:
            return None
        return build_handover(options, self._cache_hosts(), list(self.state.get(HANDOVER_DONE) or []))

    def _ask(self, command: dict[str, Any]) -> Any:
        return self.sv.ws(command)

    def run_once(self) -> str:
        """One handover: "nothing", "done", "partial", "lost" (no answer:
        tried again soon; the integration's ledger keeps what it did) or
        "unavailable"."""
        with self.lock:
            items = self.pending()
            if items is None:
                return "unavailable"
            if not items:
                return "nothing"
            try:
                started = self._ask({"type": f"{INTEGRATION_DOMAIN}/app/import", "items": items})
                job_id = started.get("job_id") if isinstance(started, dict) else None
            except WebSocketError as err:
                log(f"Handover to the integration not started: {err.code or 'no answer'}")
                return "lost"
            if not isinstance(job_id, str):
                return "lost"
            report = self._wait(job_id)
            if report is None:
                return "lost"
            return self._apply(items, report)

    def _wait(self, job_id: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + HANDOVER_POLL_LIMIT
        while time.monotonic() < deadline:
            time.sleep(HANDOVER_POLL_SECONDS)
            try:
                answer = self._ask({"type": f"{INTEGRATION_DOMAIN}/app/import/result",
                                    "job_id": job_id})
            except WebSocketError as err:
                if err.code == "not_found":
                    return None  # Home Assistant restarted: sent again later
                continue  # a lost poll: the job goes on
            state = answer.get("state") if isinstance(answer, dict) else None
            if state == "done" and isinstance(answer.get("report"), dict):
                return answer["report"]
            if state != "running":
                return None
        return None

    def _apply(self, items: list[dict[str, Any]], report: dict[str, Any]) -> str:
        outcomes = report.get("items") if isinstance(report.get("items"), dict) else {}

        def final(item_id: str) -> bool:
            row = outcomes.get(item_id)
            return isinstance(row, dict) and row.get("outcome") in HANDOVER_FINAL

        done_hosts = {handover_host_key(i["address"], i["port"])
                      for i in items if i["kind"] == "field" and final(i["item_id"])}
        drop_replicas = final(HANDOVER_REPLICAS_ID)
        sent = list(self.state.get(HANDOVER_DONE) or [])
        for item_id in (HANDOVER_SELF_ID, HANDOVER_ORDER_ID):
            if final(item_id) and item_id not in sent:
                sent.append(item_id)
        left = [i for i in items if not final(i["item_id"])]
        if done_hosts or drop_replicas:
            with self.options_lock:
                options = self._options()
                if options is None:
                    return "lost"
                cleared = clear_handed_over(options, done_hosts, drop_replicas)
                if cleared != options:
                    status, reply = self.sv.call("POST", "/addons/self/options",
                                                 {"options": cleared})
                    if status != 200:
                        message = reply.get("message") if isinstance(reply, dict) else None
                        log(f"Handover: the app options could not be cleared "
                            f"({message or status}); sent again later")
                        return "lost"
        self.state.set(HANDOVER_DONE, sent)
        counts = {"machines": len(done_hosts), "left": sum(1 for i in left if i["kind"] == "field")}
        self.last = {"at": utc_now(), **counts}
        log(f"Handover to the integration: {counts['machines']} field machine"
            f"{'s' if counts['machines'] != 1 else ''} taken, {counts['left']} left")
        if not left:
            self._finish(items, outcomes)
            return "done"
        return "partial"

    def _finish(self, items: list[dict[str, Any]], outcomes: dict[str, Any]) -> None:
        """Nothing left: the old fleet cache goes, and a note of the handover
        (no password) is kept in /data/migration.json."""
        if not self.data_dir:
            return
        try:
            os.remove(os.path.join(self.data_dir, "fleet-cache.json"))
        except OSError:
            pass
        note = {"at": utc_now(), "items": {
            i["item_id"]: {"kind": i["kind"],
                           **({"address": i["address"], "port": i["port"]}
                              if i["kind"] == "field" else {}),
                           "outcome": (outcomes.get(i["item_id"]) or {}).get("outcome")}
            for i in items}}
        path = os.path.join(self.data_dir, "migration.json")
        try:
            with open(f"{path}.tmp", "w", encoding="utf-8") as handle:
                json.dump(note, handle)
            os.replace(f"{path}.tmp", path)
        except OSError as err:
            log(f"Cannot write {path}: {err.strerror or err}")

    def trigger(self) -> None:
        self.wake.set()

    def step(self) -> float:
        """One turn of the background loop; the seconds until the next. It
        uses the watch's last lookup (the watch looks at start and every
        WATCH_SECONDS), so it adds no lookups of its own."""
        integration = self.watch.peek() or {}
        if not self.ready(integration):
            return HANDOVER_WAIT_SECONDS
        outcome = self.run_once()
        if outcome == "partial":
            return HANDOVER_RETRY_SECONDS
        if outcome in ("lost", "unavailable"):
            return HANDOVER_WAIT_SECONDS
        return HANDOVER_RETRY_SECONDS

    def run(self) -> None:
        self.wake.wait(HANDOVER_FIRST_SECONDS)  # the watch's first lookup
        self.wake.clear()
        while True:
            try:
                wait = self.step()
            except Exception as err:  # noqa: BLE001 -- the loop never stops
                log(f"Handover failed: {type(err).__name__}: {err}")
                wait = HANDOVER_WAIT_SECONDS
            self.wake.wait(wait)
            self.wake.clear()


# ── Cards ───────────────────────────────────────────────────────────────────
#
# Every card on the page has an id that stays the same across reloads:
# "self", a "|", then the key the integration also uses for the drive: its
# serial, else "id:<drive id>"; "pool:<name>" for a pool; "fs:<filesystem
# id>" for a filesystem that belongs to no drive or pool.

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
    on the drive the agent names in drive_id (agent 0.8.3 on macOS, where an
    APFS volume's device says nothing about its drive), else a block
    device's on the drive whose path starts it, a ZFS dataset on its pool's
    card. Returns the rest as FILESYSTEM cards."""
    for item in items:
        item["filesystems"] = []
    drives = [i for i in items if i.get("type") == "drive"]
    by_id = {str(d.get("id")): d for d in drives}
    pools = {str((i.get("pool") or {}).get("name")): i for i in items if i.get("type") == "pool"}
    leftover: list[dict[str, Any]] = []
    for fs in filesystems:
        device = str(fs.get("device") or "")
        drive_id = fs.get("drive_id")
        owner = None
        if isinstance(drive_id, str) and drive_id in by_id:
            owner = by_id[drive_id]
        elif str(fs.get("fstype") or "").lower() == "zfs":
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


def card_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Every card of a drives read: its items, then its FILESYSTEM cards."""
    return [c for c in (response.get("items") or []) + (response.get("filesystem_cards") or [])
            if isinstance(c, dict) and c.get("card_id")]


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


# ── Request handler ─────────────────────────────────────────────────────────

# Path prefixes that belong to the proxy itself: anything under them that no
# route matches is a JSON 404, never the page. The routes of 0.3.x (field
# machines, replicas, card writes, thresholds, Agentic access) answer 404.
_APP_PREFIXES = ("/app/", "/evaluate", "/fleet/", "/ha/", "/lab/", "/replicas", "/cards",
                 "/hosts/", "/agentic-access", "/mock/")


class ProxyHandler(BaseHTTPRequestHandler):
    """Serves static files, proxies API requests, answers the app routes."""

    agent_port: int = 9099
    static_dir: str = STATIC_DIR
    token: str = ""
    app_version: str | None = None
    agent_host: str = DEFAULT_AGENT_HOST
    mdns_name: str | None = None
    rules: Rules = Rules(DEFAULT_RULES_DIR)
    supervisor: Supervisor = Supervisor("http://supervisor", "")
    options: LegacyOptions = LegacyOptions(supervisor)
    cache: ReadCache = ReadCache()
    app_state: AppState = AppState(None)
    watch: IntegrationWatch = IntegrationWatch(supervisor, None)
    handover: Handover | None = None

    # The routes answered by the proxy itself: (method, path pattern, handler
    # name). Path parameters are named groups, percent-decoded before use.
    ROUTES: list[tuple[str, str, str]] = [
        ("GET", r"/app/info", "_route_app_info"),
        ("POST", r"/app/notice/dismiss", "_route_notice_dismiss"),
        ("GET", r"/fleet/hosts/self/drives", "_route_self_drives"),
        ("GET", r"/ha/integration", "_route_ha_integration"),
        ("POST", r"/ha/add", "_route_ha_add"),
        ("GET", r"/cards/(?P<cid>[^/]+)/file", "_route_card_file"),
        ("POST", r"/app/handover", "_route_handover"),
    ]

    def log_message(self, format, *args):
        """Send access logs to stdout for the app's log."""
        print(f"[webui] {self.address_string()} - {format % args}", flush=True)

    def parse_request(self) -> bool:
        """Read the request line and headers, then refuse, before anything
        else is done: any client but the ingress gateway and loopback (403),
        and a body over MAX_BODY (413, the body is never read)."""
        if not super().parse_request():
            return False
        if not client_allowed(self.client_address[0]):
            self.close_connection = True
            self._send_json(403, {"error": "forbidden", "message": None})
            return False
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._send_json(400, {"error": "bad_request", "message": "bad Content-Length"})
            return False
        if length > MAX_BODY:
            self.close_connection = True
            self._send_json(413, {"error": "too_large", "message": "the request body is too large"})
            return False
        return True

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

    # ── /app/info ──

    def _route_app_info(self, _parsed) -> None:
        sv = self.supervisor
        ha_api = sv.available and sv.core_reachable_cached()
        slug = sv.app_slug()
        self._send_json(200, {
            "app_version": self.app_version,
            "rules_version": self.rules.version,
            "agent_port": self.agent_port,
            "hostname": self.agent_host,
            "slug": slug,
            "mdns_name": self.mdns_name,
            "ha_version": sv.ha_version(),
            "ha_api": ha_api,
            "supervisor": slug is not None,
        })

    # ── /fleet/hosts/self/drives ──

    def _route_self_drives(self, _parsed) -> None:
        self._send_json(200, self._read_self())

    def _read_self(self) -> dict[str, Any]:
        return self._read_agent("self", "127.0.0.1", self.agent_port, self.token)

    def _read_agent(self, key: str, address: str, port: int, token: str) -> dict[str, Any]:
        """One read of this box's agent: health, drive list, each drive,
        pools, filesystems; every drive and pool judged, every card given its
        id. On failure the last good read, marked stale."""
        def call(path: str, timeout: float = AGENT_READ_TIMEOUT) -> AgentReply:
            return agent_call(address, port, "GET", path, token=token,
                              connect_timeout=AGENT_HEALTH_TIMEOUT, read_timeout=timeout)

        health = call("/api/health", AGENT_HEALTH_TIMEOUT)
        if health.status == 401:
            return self._stale(key, "password_refused" if token else "password_needed")
        if not _is_agent_health(health):
            return self._stale(key, "unreachable")
        info = health.body
        listing = call("/api/drives")
        if listing.status == 401:
            return self._stale(key, "password_refused" if token else "password_needed")
        if listing.status != 200 or not isinstance(listing.body, list):
            return self._stale(key, "unreachable")
        summaries = [d for d in listing.body if isinstance(d, dict) and d.get("id")]

        def read_one(summary: dict[str, Any]) -> dict[str, Any]:
            reply = call(f"/api/drives/{quote(str(summary['id']), safe='')}")
            if reply.status == 200 and isinstance(reply.body, dict):
                return reply.body
            return summary

        with ThreadPoolExecutor(max_workers=DRIVE_READERS) as pool:
            payloads = list(pool.map(read_one, summaries))
        # A drive whose read failed is kept as its summary, unjudged.
        failed_ids = {str(d.get("id")) for d in payloads if "smart_data" not in d}

        rules_ok = self.rules.load()
        saved = self.options.thresholds() if rules_ok else {}
        items: list[dict[str, Any]] = []
        for drive in payloads:
            drive_id = str(drive.get("id"))
            if drive_id in failed_ids:
                items.append({"type": "drive", "id": drive_id, "drive": drive, "verdict": None,
                              "view": None, "lab": None, "error": "not_answering"})
                continue
            overrides = dict(saved.get(card_id(key, drive_card_key(drive, drive_id))) or {})
            verdict, view = judge_drive(self.rules, drive, rules_ok, overrides)
            items.append({"type": "drive", "id": drive_id, "drive": drive, "verdict": verdict,
                          "view": view, "lab": None, "thresholds_source": "app"})

        endpoints = info.get("endpoints") or []
        pools_status = "none"
        pool_list: list[dict[str, Any]] = []
        if "/api/pools" in endpoints:
            reply = call("/api/pools")
            if reply.status == 200 and isinstance(reply.body, list):
                pools_status = "ok"
                pool_list = [p for p in reply.body if isinstance(p, dict) and p.get("name")]
            else:
                pools_status = "failed"
        for pool_payload in pool_list:
            verdict, view = judge_pool(self.rules, pool_payload, None, rules_ok)
            items.append({"type": "pool", "id": f"pool.{pool_payload.get('name')}",
                          "pool": pool_payload, "verdict": verdict, "view": view, "lab": None})
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
            "id": key, "kind": "self", "status": "ok", "read_at": now, "last_answer": now,
            "agent": self._agent_info(info,
                                      sum(1 for i in items if i["type"] == "drive"),
                                      sum(1 for i in items if i["type"] == "pool")),
            "summary": summarize(items, True),
            "items": items,
            "filesystem_cards": fs_cards,
            "pools_status": pools_status,
            "filesystems_status": filesystems_status,
            "rules_version": self.rules.version if rules_ok else None,
            "stale": False,
            "health": info,
        }
        self.cache.update(key, response=response, last_answer=now)
        return response

    def _agent_info(self, info: dict[str, Any], drives: Any, pools: Any) -> dict[str, Any]:
        """The response's agent block, from the agent's health answer."""
        return {
            "hostname": self.agent_host,
            "version": info.get("version"), "os": info.get("os"),
            "os_name": info.get("os_name"), "uptime_seconds": info.get("uptime_seconds"),
            "drives": drives, "pools": pools,
        }

    def _stale(self, key: str, status: str) -> dict[str, Any]:
        """The last good read of an agent that did not answer as it should."""
        cached = self.cache.get(key)
        last = cached.get("response") if isinstance(cached.get("response"), dict) else {}
        items = [{**item, "stale": True} for item in last.get("items") or []]
        for item in items:  # a read cached before cards had ids
            if not item.get("card_id"):
                item["card_id"] = card_id(key, f"pool:{(item.get('pool') or {}).get('name')}"
                                          if item.get("type") == "pool"
                                          else drive_card_key(item.get("drive") or {}, str(item.get("id"))))
        fs_cards = [{**c, "stale": True} for c in last.get("filesystem_cards") or []]
        return {
            "id": key, "kind": "self", "status": status, "read_at": utc_now(),
            "last_answer": cached.get("last_answer"),
            "agent": last.get("agent"),
            "summary": summarize(items, False),
            "items": items,
            "filesystem_cards": fs_cards,
            "pools_status": last.get("pools_status"),
            "filesystems_status": last.get("filesystems_status"),
            "rules_version": last.get("rules_version") or self.rules.version,
            "stale": True,
            "health": last.get("health"),
        }

    # ── /cards/<id>/file ──

    def _route_card_file(self, _parsed, cid: str) -> None:
        """One of this box's drive or pool cards as a JSON file: what the
        agent sent for it, with the filesystems on it."""
        host_key, key = split_card_id(cid)
        if host_key != "self" or not key:
            raise ApiError(404, "not_found", "no such card")
        response = self.cache.get("self").get("response")
        item = next((c for c in card_list(response) if c["card_id"] == cid), None) \
            if isinstance(response, dict) else None
        if item is None:
            item = next((c for c in card_list(self._read_self()) if c["card_id"] == cid), None)
        if item is None or item.get("type") not in ("drive", "pool"):
            raise ApiError(404, "not_found", "no such card")
        if item["type"] == "pool":
            pool = item.get("pool") or {}
            doc = {"pool": pool, "filesystems": item.get("filesystems") or []}
            name = f"pool-{pool.get('name')}"
        else:
            drive = item.get("drive") or {}
            doc = {"drive": drive, "filesystems": item.get("filesystems") or []}
            name = f"drive-{drive.get('model') or item.get('id')}"
        self._send_file(json.dumps(doc, indent=2).encode(), name)

    def _send_file(self, raw: bytes, name: str) -> None:
        """A JSON file as a download."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "card"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition", f'attachment; filename="{safe}.json"')
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    # ── /ha/integration, /app/notice/dismiss ──

    def _route_ha_integration(self, _parsed) -> None:
        self._send_json(200, self._integration())

    def _integration(self, fresh: bool = False) -> dict[str, Any]:
        """What the page shows about the integration: the full-width card's
        state, the Create line, and the closed cards."""
        sv = self.supervisor
        result: dict[str, Any] = {
            "reachable": False, "state": None, "version": None, "panel": None,
            "panel_path": DEFAULT_PANEL_PATH, "haos_entry": None, "create": False,
            "hacs": None, "integration_url": INTEGRATION_URL,
            "dismissed": {"install": False, "update": False},
        }
        if sv.available and sv.core_reachable_cached():
            result["reachable"] = True
            found = self.watch.get(fresh=fresh)
            result.update({k: found.get(k) for k in
                           ("state", "version", "panel", "panel_path", "haos_entry")})
            state = result["state"]
            if state == STATE_MISSING:
                # The install guide names HACS first when it is missing too.
                result["hacs"] = sv.hacs_installed()
            elif state is not None:
                # Seen installed: a closed install card may show again if the
                # integration is ever removed.
                self.app_state.set(NOTICE_DISMISSED, False)
            if state in PANEL_STATES:
                # Seen at 0.9 or later: a closed update card may show again
                # after a downgrade.
                self.app_state.set(UPDATE_DISMISSED, False)
            result["create"] = ((state == STATE_LOADED and result["haos_entry"] is False)
                                or state == STATE_NO_ENTRIES)
        result["dismissed"] = {"install": bool(self.app_state.get(NOTICE_DISMISSED)),
                               "update": bool(self.app_state.get(UPDATE_DISMISSED))}
        return result

    def _route_handover(self, _parsed) -> None:
        """Send the field machines to the integration again now (a manual
        retry; the page has no button for it). Only once the integration
        takes a handover."""
        if self.handover is None:
            raise ApiError(503, "supervisor_unavailable", "the Supervisor is not available")
        self.handover.trigger()
        self._send_json(202, {"queued": True})

    def _route_notice_dismiss(self, _parsed) -> None:
        """The install card (no integration) or the update card (0.8.x),
        closed for every browser until the integration is seen installed, or
        seen at 0.9 or later."""
        try:
            body = self._read_json()
        except ApiError:
            body = {}
        which = body.get("notice", "install") if isinstance(body, dict) else "install"
        if which not in ("install", "update"):
            raise ApiError(400, "bad_request", 'notice must be "install" or "update"')
        self.app_state.set(NOTICE_DISMISSED if which == "install" else UPDATE_DISMISSED, True)
        self._send_json(200, {"notice": which, "dismissed": True,
                              "notice_dismissed": bool(self.app_state.get(NOTICE_DISMISSED))})

    # ── /ha/add ──

    def _route_ha_add(self, _parsed) -> None:
        body = self._read_json()
        target = body.get("target") if isinstance(body, dict) else None
        if target != "self":
            raise ApiError(400, "bad_target", 'target must be "self"')
        try:
            outcome, code = self._ha_add(self.agent_host, self.agent_port, self.token)
        finally:
            self.watch.forget()
        self._send_json(code, outcome)

    def _ha_add(self, host: str, port: int, token: str) -> tuple[dict[str, Any], int]:
        def failed(message: str) -> tuple[dict[str, Any], int]:
            return {"error": "add_failed", "result": "error", "message": message}, 502

        sv = self.supervisor
        if not (sv.available and sv.core_reachable()):
            return failed("unreachable")
        found = self.watch.get(fresh=True)
        if found.get("state") == STATE_LOADED and found.get("haos_entry") is True:
            return {"result": "already_added", "entry_id": None}, 200

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
            log("Integration entry added for this box")
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
            # parse_request() checked it: a number from 0 to MAX_BODY
            content_length = int(self.headers.get("Content-Length", 0) or 0)
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
            # parse_request() has already refused every client but the
            # ingress gateway and loopback, so no one else gets the token's
            # access this way.
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
    parser.add_argument("--app-version", type=str, default=os.environ.get("APP_VERSION") or None,
                        help="App version shown on the page (default: $APP_VERSION)")
    parser.add_argument("--rules-dir", type=str, default=DEFAULT_RULES_DIR,
                        help="Directory holding smart_sniffer/attention.py; VERSION sits beside it")
    parser.add_argument("--supervisor-url", type=str, default="http://supervisor",
                        help="Supervisor API base URL")
    parser.add_argument("--agent-host", type=str, default=DEFAULT_AGENT_HOST,
                        help="Hostname Home Assistant uses to reach this app's agent "
                             "(run.sh passes the container hostname)")
    parser.add_argument("--mdns-name", type=str, default="",
                        help="The agent's mDNS name, shown on the page (empty: none)")
    parser.add_argument("--ha-cache-seconds", type=float, default=HA_API_CACHE_SECONDS,
                        help="How long one lookup of the integration is used")
    parser.add_argument("--static-dir", type=str, default=STATIC_DIR,
                        help="Where the page, its scripts and images are")
    parser.add_argument("--data-dir", type=str, default="/data",
                        help="Where app-state.json is kept (empty: memory only)")
    args = parser.parse_args()

    ProxyHandler.agent_port = args.agent_port
    ProxyHandler.static_dir = args.static_dir
    # The agent token, from the environment so it never shows on a command line
    ProxyHandler.token = os.environ.get("AGENT_TOKEN", "")
    ProxyHandler.app_version = args.app_version
    ProxyHandler.agent_host = args.agent_host.strip() or DEFAULT_AGENT_HOST
    ProxyHandler.mdns_name = args.mdns_name.strip() or None
    ProxyHandler.rules = Rules(args.rules_dir)
    ProxyHandler.supervisor = Supervisor(
        args.supervisor_url, os.environ.get("SUPERVISOR_TOKEN", "")
    )
    ProxyHandler.options = LegacyOptions(ProxyHandler.supervisor)
    ProxyHandler.cache = ReadCache(
        os.path.join(args.data_dir, "fleet-cache.json") if args.data_dir else None
    )
    ProxyHandler.app_state = AppState(
        os.path.join(args.data_dir, "app-state.json") if args.data_dir else None
    )
    discovery = Discovery(ProxyHandler.supervisor, ProxyHandler.app_state,
                          ProxyHandler.agent_host, ProxyHandler.agent_port)
    ProxyHandler.watch = IntegrationWatch(ProxyHandler.supervisor, discovery,
                                          args.ha_cache_seconds)
    ProxyHandler.handover = Handover(ProxyHandler.supervisor, ProxyHandler.app_state,
                                     ProxyHandler.watch, args.data_dir or None,
                                     ProxyHandler.options.lock)

    server = ThreadingHTTPServer(("0.0.0.0", args.port), ProxyHandler)
    server.daemon_threads = True
    log(f"Serving on port {args.port}")
    log(f"Proxying /api/* -> 127.0.0.1:{args.agent_port}")
    log(f"Integration rules: {ProxyHandler.rules.version or 'not found'}")
    log(f"Agent host for the integration: {ProxyHandler.agent_host}")
    if not ProxyHandler.supervisor.available:
        log("No Supervisor token: the Home Assistant routes and the discovery are off")
    else:
        threading.Thread(target=drop_stale_options, args=(ProxyHandler.options,),
                         daemon=True).start()
        threading.Thread(target=ProxyHandler.watch.run, daemon=True).start()
        threading.Thread(target=ProxyHandler.handover.run, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
