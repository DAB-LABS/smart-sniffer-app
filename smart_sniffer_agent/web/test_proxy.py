"""Tests for the web UI proxy's own routes (proxy.py).

The proxy runs as a real subprocess on a free port. Two small HTTP servers in
this process stand in for its backends: one for the Go agent, one for the
Supervisor (including Home Assistant Core behind /core/api). The proxy reaches
the second through --supervisor-url.

/evaluate needs the integration's rule modules. Point SS_RULES_DIR at a
smart-sniffer checkout's custom_components directory (default: a
smart-sniffer checkout beside this repository); the drive fixtures come from the same
checkout's tests/fixtures, or from SS_FIXTURES. The /evaluate tests are
skipped when the rules are absent.

drive_view(), pool_verdict() and Rules are also tested directly, imported
from proxy.py without starting it.

Run with:
  SS_RULES_DIR=../smart-sniffer/custom_components \
    python3 -m pytest smart_sniffer_agent/web/test_proxy.py
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import pytest

PROXY = Path(__file__).resolve().parent / "proxy.py"
# Default: a smart-sniffer checkout next to this repository's checkout.
_SIBLING = Path(__file__).resolve().parents[3] / "smart-sniffer" / "custom_components"
RULES_DIR = Path(os.environ.get("SS_RULES_DIR", _SIBLING))
FIXTURES = Path(os.environ.get("SS_FIXTURES", RULES_DIR.parent / "tests" / "fixtures"))
# The replica agent: SS_MOCK, else the rules checkout's tools/mock-agent.py,
# else the first smart-sniffer checkout beside this one whose mock serves
# replica files. The replica tests are skipped when none is found.


def _find_mock() -> Path:
    if os.environ.get("SS_MOCK"):
        return Path(os.environ["SS_MOCK"])
    candidates = [RULES_DIR.parent / "tools" / "mock-agent.py"]
    candidates += sorted(Path(__file__).resolve().parents[3].glob("*/tools/mock-agent.py"))
    for candidate in candidates:
        if _serves_replicas(candidate):
            return candidate
    return candidates[0]


def _serves_replicas(mock: Path) -> bool:
    try:
        text = mock.read_text(encoding="utf-8")
    except OSError:
        return False
    return "--user-replicas" in text and (mock.parent / "replicas").is_dir()


MOCK_AGENT = _find_mock()
TOKEN = "test-supervisor-token"
# The app installed as a local app: run.sh passes the container hostname and
# the Supervisor reports the slug.
AGENT_HOST = "local-smart-sniffer-agent"
SLUG = "local_smart_sniffer_agent"
MDNS_NAME = "smartha-homeassistant"
# Home Assistant 2026 moved the app pages to /config/app/<slug>/...; the page
# picks its link paths from this version.
HA_VERSION = "2026.7.2"

def _mock_has_replicas() -> bool:
    return _serves_replicas(MOCK_AGENT)


needs_mock = pytest.mark.skipif(
    not _mock_has_replicas(),
    reason=f"replica mock agent not found ({MOCK_AGENT}); set SS_MOCK",
)

needs_rules = pytest.mark.skipif(
    not (RULES_DIR / "smart_sniffer" / "attention.py").is_file()
    or not (FIXTURES / "nvme_healthy.json").is_file(),
    reason=f"integration rules or fixtures not found ({RULES_DIR}, {FIXTURES})",
)


# ── Fake backends ───────────────────────────────────────────────────────────

class FakeState:
    """What the fake Supervisor answers, changed per test."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.core_status = 200  # 401 makes Core unreachable
        self.ha_version = HA_VERSION
        self.entries: list[dict[str, Any]] = []
        self.components: list[str] = ["sensor", "zeroconf"]
        self.flow_handlers: list[str] = ["met", "zha"]
        self.flow_step: dict[str, Any] = {"type": "create_entry"}
        self.handler_known = True  # False: the integration is not installed
        self.options: dict[str, Any] = {
            "port": 9099, "token": "", "scan_interval": 60,
            "mock_port": 9100, "hosts": [],
        }
        self.options_status = 200  # 400 makes the Supervisor refuse a write
        # The schema as the Supervisor's app info reports it (its UI form);
        # None when it reports none.
        self.schema: list[dict[str, Any]] | None = [
            {"name": "port", "type": "integer"}, {"name": "token", "type": "string"},
            {"name": "scan_interval", "type": "integer"}, {"name": "mock_port", "type": "integer"},
            {"name": "hosts", "type": "schema", "multiple": True},
        ]
        self.network: dict[str, Any] = {"interfaces": [
            {"interface": "lo0", "primary": False, "connected": True,
             "ipv4": {"address": ["192.168.99.2/24"]}},
            {"interface": "enp0s18", "primary": True, "connected": True,
             "ipv4": {"address": ["127.0.0.1/24"]}},
        ]}
        self.diagnostics: dict[str, Any] = {}  # entry_id -> port, or {"host", "port"}
        self.diagnostics_broken: set[str] = set()  # entry ids Core answers 500 for
        self.devices: list[dict[str, Any]] | None = []  # None: the template call fails
        self.calls: list[tuple[str, str, Any]] = []
        self.restarted = threading.Event()
        # The integration's thresholds behind its options flow and its
        # diagnostics: entry id -> drive id -> the labels of the threshold
        # form, and entry id -> drive id -> the stored overrides.
        self.int_labels: dict[str, dict[str, list[str]]] = {}
        self.int_thresholds: dict[str, dict[str, dict[str, int]]] = {}
        self.diag_thresholds = True  # False: diagnostics without them (older integration)
        self.options_flow_abort: str | None = None  # the options flow aborts with this reason
        self.options_flow: dict[str, Any] = {}
        # Added to entries and devices when the config flow creates an entry.
        self.on_create: tuple[dict[str, Any], list[dict[str, Any]]] | None = None
        # Core's websocket behind the Supervisor: "ok", "refuse" (the
        # command fails), "auth" (the token is refused), "down" (no upgrade),
        # "drop" (the connection closes after auth). The fake pings before
        # auth_required and splits its result into two frames, so the
        # client's pong and continuation handling are used every time.
        self.ws_mode = "ok"
        self.ws_commands: list[dict[str, Any]] = []
        self.ws_pongs: list[bytes] = []
        self.ws_closed_cleanly: list[bool] = []
        self.ws_error: dict[str, Any] = {"code": "not_found", "message": "Device not found"}
        self.entry_delete_status = 200  # else Core refuses to delete an entry
        # device id -> the entity ids with a state, for the replica removal's poll.
        self.device_entities: dict[str, list[str]] = {}
        # How many remove_config_entry commands the integration refuses
        # first, as it does while its last poll still has the drive.
        self.remove_refusals = 0


STATE = FakeState()


def _json_reply(handler: BaseHTTPRequestHandler, code: int, data: Any) -> None:
    body = json.dumps(data).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class FakeSupervisor(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def _dispatch(self, method: str) -> None:
        if self.path == "/core/websocket" and self.headers.get("Upgrade", "").lower() == "websocket":
            STATE.calls.append(("WS", self.path, None))
            _fake_core_websocket(self)
            return
        body = self._body()
        path = self.path
        STATE.calls.append((method, path, body))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            _json_reply(self, 401, {"message": "unauthorized"})
            return

        if path == "/addons/self/info" and method == "GET":
            _json_reply(self, 200, {"result": "ok", "data": {
                "slug": SLUG, "hostname": AGENT_HOST, "options": STATE.options,
                "schema": STATE.schema,
            }})
            return
        if path == "/addons/self/options" and method == "POST":
            if STATE.options_status != 200:
                _json_reply(self, STATE.options_status, {
                    "result": "error", "message": "Option 'hosts' does not match the schema",
                })
                return
            STATE.options = body["options"]
            _json_reply(self, 200, {"result": "ok", "data": {}})
            return
        if path == "/network/info" and method == "GET":
            _json_reply(self, 200, {"result": "ok", "data": STATE.network})
            return
        if path == "/addons/self/restart" and method == "POST":
            STATE.restarted.set()
            _json_reply(self, 200, {"result": "ok", "data": {}})
            return

        if not path.startswith("/core/api/"):
            _json_reply(self, 404, {"message": "not found"})
            return
        if STATE.core_status != 200:
            _json_reply(self, STATE.core_status, {"message": "unauthorized"})
            return
        core = path[len("/core/api"):]
        parsed = urlparse(core)

        if core == "/" and method == "GET":
            _json_reply(self, 200, {"message": "API running."})
        elif core == "/config" and method == "GET":
            _json_reply(self, 200, {"components": STATE.components,
                                    "version": STATE.ha_version})
        elif core == "/config/config_entries/flow_handlers" and method == "GET":
            _json_reply(self, 200, STATE.flow_handlers)
        elif parsed.path == "/config/config_entries/entry" and method == "GET":
            assert parsed.query == "domain=smart_sniffer"
            _json_reply(self, 200, STATE.entries)
        elif core.startswith("/config/config_entries/entry/") and method == "DELETE":
            entry_id = core.rsplit("/", 1)[1]
            if STATE.entry_delete_status != 200:
                _json_reply(self, STATE.entry_delete_status, {"message": "Config entry not found."})
                return
            STATE.entries = [e for e in STATE.entries if e["entry_id"] != entry_id]
            _json_reply(self, 200, {"require_restart": False})
        elif core == "/config/config_entries/flow" and method == "POST":
            if body.get("handler") != "smart_sniffer" or not STATE.handler_known:
                _json_reply(self, 404, {"message": "Invalid handler specified"})
                return
            _json_reply(self, 200, {
                "type": "form", "flow_id": "flow123", "handler": "smart_sniffer",
                "step_id": "user", "errors": {}, "data_schema": [],
            })
        elif core == "/config/config_entries/flow/flow123" and method == "POST":
            if STATE.flow_step.get("type") == "create_entry" and STATE.on_create:
                entry, devices = STATE.on_create
                STATE.entries.append(entry)
                STATE.devices = (STATE.devices or []) + devices
            _json_reply(self, 200, {"flow_id": "flow123", **STATE.flow_step})
        elif core == "/config/config_entries/options/flow" and method == "POST":
            entry_id = body.get("handler")
            if entry_id not in STATE.int_labels:
                _json_reply(self, 404, {"message": "Invalid handler specified"})
                return
            STATE.options_flow = {"entry": entry_id, "drive": None}
            if STATE.options_flow_abort:
                _json_reply(self, 200, {"type": "abort", "flow_id": "opt1",
                                        "reason": STATE.options_flow_abort})
                return
            _json_reply(self, 200, {"type": "menu", "flow_id": "opt1", "step_id": "init",
                                    "menu_options": ["settings", "thresholds"]})
        elif core == "/config/config_entries/options/flow/opt1" and method == "POST":
            _json_reply(self, 200, _options_step(body))
        elif core == "/config/config_entries/options/flow/opt1" and method == "DELETE":
            STATE.options_flow = {}
            _json_reply(self, 200, {"message": "Flow aborted"})
        elif core == "/config/config_entries/flow/flow123" and method == "DELETE":
            _json_reply(self, 200, {"message": "Flow aborted"})
        elif core.startswith("/diagnostics/config_entry/") and method == "GET":
            entry_id = core.rsplit("/", 1)[1]
            if entry_id in STATE.diagnostics_broken:
                _json_reply(self, 500, {"message": "500 Internal Server Error"})
                return
            if entry_id not in STATE.diagnostics and entry_id not in STATE.int_labels:
                _json_reply(self, 404, {"message": "not found"})
                return
            config = STATE.diagnostics.get(entry_id, {"host": "172.30.33.5", "port": 9099})
            if not isinstance(config, dict):
                config = {"host": "172.30.33.5", "port": config}
            drives = {}
            for drive_id in STATE.int_labels.get(entry_id, {}):
                attention: dict[str, Any] = {"state": "NO", "severity": "none", "reasons": []}
                if STATE.diag_thresholds:
                    attention["thresholds"] = dict(
                        STATE.int_thresholds.get(entry_id, {}).get(drive_id) or {})
                drives[drive_id] = {"summary": {"model": "x"}, "attention": attention}
            _json_reply(self, 200, {"data": {"config_entry": config, "drives": drives}})
        elif core == "/template" and method == "POST" and "device_entities(" in body["template"]:
            device = re.search(r'device_entities\("([^"]+)"\)', body["template"]).group(1)
            _json_reply(self, 200, STATE.device_entities.get(device, []))
        elif core == "/services/homeassistant/update_entity" and method == "POST":
            _json_reply(self, 200, [])
        elif core == "/template" and method == "POST":
            if STATE.devices is None:
                _json_reply(self, 400, {"message": "Error rendering template"})
                return
            # Core answers the rendered text, here the JSON the template prints.
            text = json.dumps(STATE.devices).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)
        elif (core.startswith("/config/config_entries/entry/") and core.endswith("/reload")
              and method == "POST"):
            _json_reply(self, 200, {"require_restart": False})
        else:
            _json_reply(self, 404, {"message": f"no fake for {method} {core}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


# ── A fake of Core's websocket API (server side of RFC 6455) ────────────────

def _ws_send(handler: BaseHTTPRequestHandler, opcode: int, payload: bytes, fin: bool = True) -> None:
    size = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    if size < 126:
        head += bytes([size])
    elif size < 65536:
        head += bytes([126]) + struct.pack("!H", size)
    else:
        head += bytes([127]) + struct.pack("!Q", size)
    handler.wfile.write(head + payload)
    handler.wfile.flush()


def _ws_read(handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
    first, second = handler.rfile.read(2)
    assert second & 0x80, "a client frame must be masked"
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", handler.rfile.read(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", handler.rfile.read(8))[0]
    mask = handler.rfile.read(4)
    data = handler.rfile.read(size)
    return first & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _ws_message(handler: BaseHTTPRequestHandler) -> Any:
    while True:
        opcode, payload = _ws_read(handler)
        if opcode == 0xA:
            STATE.ws_pongs.append(payload)
            continue
        if opcode == 0x8:
            return None
        return json.loads(payload)


def _fake_core_websocket(handler: BaseHTTPRequestHandler) -> None:
    if STATE.ws_mode == "down":
        _json_reply(handler, 502, {"message": "Bad Gateway"})
        return
    key = handler.headers.get("Sec-WebSocket-Key", "")
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    handler.wfile.flush()
    send = lambda data: _ws_send(handler, 0x1, json.dumps(data).encode())  # noqa: E731
    _ws_send(handler, 0x9, b"hello")
    send({"type": "auth_required", "ha_version": STATE.ha_version})
    auth = _ws_message(handler)
    if STATE.ws_mode == "auth" or not isinstance(auth, dict) or auth.get("access_token") != TOKEN:
        send({"type": "auth_invalid", "message": "Invalid access token or password"})
        _ws_send(handler, 0x8, struct.pack("!H", 1000))
        return
    send({"type": "auth_ok", "ha_version": STATE.ha_version})
    if STATE.ws_mode == "drop":
        _ws_send(handler, 0x8, struct.pack("!H", 1011))
        return
    command = _ws_message(handler)
    STATE.ws_commands.append(command)
    if command.get("type") == "test/echo":
        reply = {"id": command["id"], "type": "result", "success": True, "result": command.get("data")}
    elif STATE.ws_mode == "refuse" or command.get("type") not in (
            "config/device_registry/update", "config/device_registry/remove_config_entry"):
        reply = {"id": command["id"], "type": "result", "success": False, "error": STATE.ws_error}
    elif command["type"] == "config/device_registry/remove_config_entry" and STATE.remove_refusals:
        STATE.remove_refusals -= 1
        reply = {"id": command["id"], "type": "result", "success": False, "error": STATE.ws_error}
    elif command["type"] == "config/device_registry/remove_config_entry":
        reply = {"id": command["id"], "type": "result", "success": True, "result": None}
        STATE.devices = [d for d in STATE.devices or [] if d["id"] != command["device_id"]]
    else:
        changes = {k: command[k] for k in ("name_by_user", "disabled_by") if k in command}
        reply = {"id": command["id"], "type": "result", "success": True,
                 "result": {"id": command["device_id"], **changes}}
        for device in STATE.devices or []:
            if device["id"] == command["device_id"]:
                device.update(changes)
    # An event for another subscription first, then the result in two frames.
    send({"id": 99, "type": "event", "event": {}})
    text = json.dumps(reply).encode()
    _ws_send(handler, 0x1, text[:5], fin=False)
    _ws_send(handler, 0x0, text[5:])
    opcode, _payload = _ws_read(handler)
    STATE.ws_closed_cleanly.append(opcode == 0x8)
    if opcode == 0x8:
        _ws_send(handler, 0x8, struct.pack("!H", 1000))


# The integration's threshold form as Home Assistant serves it: one number
# field per label, prefilled with the value in force; a value equal to the
# default clears the override.
INT_DEFAULTS = {"Command Timeout": 100, "SSD Wear Percent Used": 90, "NVMe Spare Warn Below": 20}
ATA_LABELS = ["Reallocated Sector Count", "Current Pending Sector Count", "Command Timeout",
              "Offline Uncorrectable Errors", "Reallocated Event Count",
              "Reported Uncorrectable Errors", "Spin Retry Count", "Total Offline Uncorrectable",
              "Uncorrectable Error Count", "SSD Wear Percent Used"]


def _threshold_form(entry_id: str, drive_id: str) -> dict[str, Any]:
    stored = STATE.int_thresholds.get(entry_id, {}).get(drive_id) or {}
    return {"type": "form", "flow_id": "opt1", "handler": entry_id, "step_id": "threshold_form",
            "errors": None, "description_placeholders": {"drive": drive_id},
            "data_schema": [{"name": label, "optional": True,
                             "default": stored.get(label, INT_DEFAULTS.get(label, 0)),
                             "selector": {"number": {"min": 0, "step": 1, "mode": "box"}}}
                            for label in STATE.int_labels[entry_id][drive_id]]}


def _options_step(body: dict[str, Any]) -> dict[str, Any]:
    flow = STATE.options_flow
    entry_id = flow.get("entry")
    if entry_id is None:
        return {"type": "abort", "reason": "unknown_flow"}
    drives = STATE.int_labels[entry_id]
    if body.get("next_step_id") == "thresholds":
        if len(drives) == 1:
            flow["drive"] = next(iter(drives))
            return _threshold_form(entry_id, flow["drive"])
        return {"type": "form", "flow_id": "opt1", "step_id": "thresholds", "errors": None,
                "data_schema": [{"name": "drive_id", "required": True,
                                 "selector": {"select": {"options": [
                                     {"value": d, "label": d} for d in drives]}}}]}
    if "drive_id" in body:
        if body["drive_id"] not in drives:
            return {"type": "form", "flow_id": "opt1", "step_id": "thresholds",
                    "errors": {"drive_id": "invalid_drive"}, "data_schema": []}
        flow["drive"] = body["drive_id"]
        return _threshold_form(entry_id, flow["drive"])
    drive_id = flow.get("drive")
    labels = drives[drive_id]
    chosen = {label: int(body[label]) for label in labels
              if label in body and int(body[label]) != INT_DEFAULTS.get(label, 0)}
    STATE.int_thresholds.setdefault(entry_id, {})[drive_id] = chosen
    STATE.options_flow = {}
    return {"type": "create_entry", "flow_id": "opt1", "handler": entry_id, "title": "",
            "result": True}


APP_TOKEN = "app-token-123"  # this app's agent token (the token option)
FIELD_PASSWORD = "fieldpass"


def simple_drive(drive_id: str, serial: str, attrs: dict[str, int] | None = None,
                 passed: bool = True, smart: bool = True) -> dict[str, Any]:
    """An ATA drive as the agent's /api/drives/{id} returns it."""
    table = [
        {"id": i, "name": n, "value": 100, "worst": 100, "thresh": 0,
         "raw": {"value": (attrs or {}).get(n, 0), "string": str((attrs or {}).get(n, 0))}}
        for i, n in ((5, "Reallocated_Sector_Ct"), (188, "Command_Timeout"),
                     (197, "Current_Pending_Sector"))
    ]
    return {
        "id": drive_id, "device_path": f"/dev/{drive_id}", "model": f"Model {drive_id}",
        "serial": serial, "protocol": "ATA", "readable": True,
        "smart_data": {
            "smart_status": {"passed": passed}, "temperature": {"current": 35},
            "power_on_time": {"hours": 1000}, "power_cycle_count": 10,
            "ata_smart_attributes": {"table": table},
        } if smart else {},
    }


class AgentState:
    """A fake agent: what it answers and what it was asked.

    mode: "agent" (an agent; a token makes it want a password), "404",
    "notagent" (a JSON answer that is not an agent), "slow" (answers after
    3 s), "broken" (HTTP 500)."""

    def __init__(self, mode: str = "agent", token: str = "", hostname: str | None = None,
                 drives: list[dict[str, Any]] | None = None,
                 pools: list[dict[str, Any]] | None = None, version: str = "0.8.1",
                 filesystems: list[dict[str, Any]] | None = None) -> None:
        self.mode = mode
        self.token = token
        self.hostname = hostname
        self.drives = drives if drives is not None else [simple_drive("sda", "SER-A")]
        self.pools = pools  # None: /api/pools not advertised
        self.filesystems = filesystems  # None: /api/filesystems not advertised
        self.version = version
        self.requests: list[tuple[str, str, str | None]] = []


def agent_handler(state: AgentState):
    class FakeAgent(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state.requests.append(("GET", self.path, self.headers.get("Authorization")))
            if state.mode == "slow":
                time.sleep(3)
                _json_reply(self, 200, {})
                return
            if state.mode == "404":
                _json_reply(self, 404, {"error": "not found"})
                return
            if state.mode == "notagent":
                _json_reply(self, 200, {"status": "ok", "app": "printer"})
                return
            if state.mode == "broken":
                _json_reply(self, 500, {"error": "boom"})
                return
            if self.path == "/api/health":
                endpoints = ["/api/health", "/api/drives", "/api/drives/{id}"]
                if state.pools is not None:
                    endpoints.append("/api/pools")
                if state.filesystems is not None:
                    endpoints.append("/api/filesystems")
                health = {"status": "ok", "version": state.version, "os": "linux",
                          "uptime_seconds": 3542400, "endpoints": endpoints,
                          "drives": len(state.drives),
                          "filesystems": len(state.filesystems or [])}
                if state.hostname:
                    health["hostname"] = state.hostname
                _json_reply(self, 200, health)
                return
            if state.token and self.headers.get("Authorization") != f"Bearer {state.token}":
                _json_reply(self, 401, {"error": "unauthorized"})
                return
            if self.path == "/api/drives":
                _json_reply(self, 200, [{k: d[k] for k in ("id", "device_path", "model", "serial",
                                                           "protocol", "readable")}
                                        for d in state.drives])
            elif self.path.startswith("/api/drives/"):
                found = [d for d in state.drives if d["id"] == self.path.rsplit("/", 1)[1]]
                _json_reply(self, 200 if found else 404, found[0] if found else {"error": "nf"})
            elif self.path == "/api/pools" and state.pools is not None:
                _json_reply(self, 200, state.pools)
            elif self.path == "/api/filesystems" and state.filesystems is not None:
                _json_reply(self, 200, state.filesystems)
            else:
                _json_reply(self, 404, {"error": "not found"})

    return FakeAgent


SELF_AGENT = AgentState(token=APP_TOKEN)


def _serve(handler, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ── Proxy under test ────────────────────────────────────────────────────────

class Proxy:
    def __init__(self, base: str) -> None:
        self.base = base

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, raw.decode(errors="replace")

    def get(self, path: str) -> tuple[int, Any]:
        return self.request("GET", path)

    def post(self, path: str, body: Any = None) -> tuple[int, Any]:
        return self.request("POST", path, body if body is not None else {})


@pytest.fixture(scope="session")
def backends():
    supervisor = _serve(FakeSupervisor)
    agent = _serve(agent_handler(SELF_AGENT))
    yield supervisor, agent
    supervisor.shutdown()
    agent.shutdown()


def _start_proxy(args: list[str], token: str) -> tuple[subprocess.Popen, Proxy]:
    port = _free_port()
    env = {**os.environ, "SUPERVISOR_TOKEN": token}
    env.pop("APP_VERSION", None)
    proc = subprocess.Popen(
        [sys.executable, str(PROXY), f"--port={port}", *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 10
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                proc.kill()
                pytest.fail(f"proxy did not start:\n{out}")
            time.sleep(0.05)
    return proc, Proxy(f"http://127.0.0.1:{port}")


def _stop_proxy(proc: subprocess.Popen) -> str:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out.decode(errors="replace")


@pytest.fixture(scope="session")
def mock_agent():
    """The replica agent from the smart-sniffer checkout, with the shipped
    replica folder and a temporary user folder. None when it is not there;
    the replica tests skip, the rest run against a closed port."""
    if not _mock_has_replicas():
        yield None
        return
    work = Path(tempfile.mkdtemp(prefix="ss-mock-"))
    user = work / "user"
    user.mkdir()
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_AGENT), f"--port={port}", "--host=127.0.0.1",
         "--no-mdns", "--replicas", str(MOCK_AGENT.parent / "replicas"),
         "--user-replicas", str(user)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _wait_for_port(port, proc)
    yield {"port": port, "user": user, "proc": proc}
    proc.terminate()
    proc.wait(timeout=5)
    shutil.rmtree(work, ignore_errors=True)


def _wait_for_port(port: int, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 10
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                pytest.fail(f"process on port {port} did not start")
            time.sleep(0.05)


@pytest.fixture(scope="session")
def data_dir():
    path = Path(tempfile.mkdtemp(prefix="ss-data-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def proxy(backends, mock_agent, data_dir):
    supervisor, agent = backends
    agent_port = agent.server_address[1]
    mock_port = mock_agent["port"] if mock_agent else _free_port()
    proc, client = _start_proxy([
        f"--agent-port={agent_port}",
        f"--mock-port={mock_port}",
        "--app-version=0.3.0",
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{supervisor.server_address[1]}",
        f"--agent-host={AGENT_HOST}",
        f"--mdns-name={MDNS_NAME}",
        f"--token={APP_TOKEN}",
        f"--data-dir={data_dir}",
        "--ha-cache-seconds=0",
        "--ha-replica-seconds=2",
    ], TOKEN)
    client.agent_port = agent_port  # type: ignore[attr-defined]
    client.mock_port = mock_port  # type: ignore[attr-defined]
    yield client
    # The Supervisor token and stored passwords must never reach the log.
    output = _stop_proxy(proc)
    assert TOKEN not in output
    assert FIELD_PASSWORD not in output and "wrong-pass" not in output


@pytest.fixture(autouse=True)
def fresh_state():
    STATE.reset()
    SELF_AGENT.__init__(token=APP_TOKEN)
    yield


def _drive(fixture: str) -> dict[str, Any]:
    """One drive the way the agent's /api/drives/{id} returns it."""
    with (FIXTURES / f"{fixture}.json").open(encoding="utf-8") as handle:
        smart_data = json.load(handle)
    device = smart_data.get("device") or {}
    return {
        "id": f"fx-{fixture.replace('_', '-')}",
        "device_path": "/dev/fixture",
        "model": smart_data.get("model_name", "Unknown Drive"),
        "serial": smart_data.get("serial_number", ""),
        "protocol": device.get("protocol", ""),
        "readable": True,
        "last_updated": "2026-10-01T12:00:00Z",
        "smart_data": copy.deepcopy(smart_data),
    }


def _entry(entry_id: str, title: str, state: str = "loaded") -> dict[str, Any]:
    return {
        "entry_id": entry_id, "domain": "smart_sniffer", "title": title,
        "source": "user", "state": state,
    }


# ── proxy.py imported as a module ───────────────────────────────────────────

@pytest.fixture(scope="session")
def mod():
    """proxy.py imported without starting it, its rules loaded."""
    spec = importlib.util.spec_from_file_location("webui_proxy_under_test", PROXY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    rules = module.Rules(str(RULES_DIR))
    module.loaded_rules = rules if rules.load() else None
    return module


def _devstat_drive(name: str) -> dict[str, Any]:
    """A v0.8.0 agent's drive from the integration's devstat fixtures."""
    with (FIXTURES / "devstat" / f"{name}.a.json").open(encoding="utf-8") as handle:
        smart_data = json.load(handle)
    with (FIXTURES / "devstat" / f"{name}.devstat.json").open(encoding="utf-8") as handle:
        stats = json.load(handle)
    return {
        "id": name, "device_path": "/dev/fixture",
        "model": smart_data.get("model_name", ""), "serial": smart_data.get("serial_number", ""),
        "protocol": "ATA", "readable": True,
        "smart_data": smart_data,
        "device_statistics": {
            "status": "present", "complete": True, "exit_status": 0,
            "logical_block_size": 512, "pages": stats["ata_device_statistics"]["pages"],
        },
    }


def _ata_drive(attrs: dict[str, int], passed: bool = True, wear: int | None = None) -> dict[str, Any]:
    """A small ATA drive: the given attribute raw values."""
    ids = {"Reallocated_Sector_Ct": 5, "Spin_Retry_Count": 10, "Reported_Uncorrect": 187,
           "Command_Timeout": 188, "Reallocated_Event_Count": 196,
           "Current_Pending_Sector": 197, "Offline_Uncorrectable": 198}
    table = [
        {"id": ids[name], "name": name, "value": 100, "worst": 100, "thresh": 0,
         "raw": {"value": raw, "string": str(raw)}}
        for name, raw in attrs.items()
    ]
    if wear is not None:
        table.append({"id": 177, "name": "Wear_Leveling_Count", "value": 100 - wear,
                      "worst": 100 - wear, "thresh": 0, "flags": {"value": 19},
                      "raw": {"value": 10, "string": "10"}})
    return {
        "id": "sda", "model": "Test ATA", "serial": "T1", "protocol": "ATA",
        "smart_data": {
            "smart_status": {"passed": passed},
            "temperature": {"current": 38},
            "power_on_time": {"hours": 31502},
            "power_cycle_count": 1204,
            "ata_smart_attributes": {"table": table},
        },
        "derived": {"host_writes": {"bytes": 61200000000000, "source": "ata_attribute",
                                    "attribute_id": 241, "attribute_name": "Total_LBAs_Written"},
                    "host_reads_omitted": "no_source"},
        "device_statistics": {"status": "absent"},
    }


def _view(mod, drive: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    verdict = mod.loaded_rules.evaluate(drive)
    return verdict, mod.drive_view(drive, verdict)


def _rows(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in view["rows"]}


@needs_rules
def test_view_ata_critical(mod):
    verdict, view = _view(mod, _ata_drive({
        "Reallocated_Sector_Ct": 24, "Current_Pending_Sector": 3,
        "Offline_Uncorrectable": 0, "Reallocated_Event_Count": 0,
    }))
    assert verdict["state"] == "YES"
    assert view["header"] == "attention" and view["health_failed"] is False
    rows = _rows(view)
    assert rows["reallocated"] == {"key": "reallocated", "name": "Reallocated sectors",
                                   "value": "24", "mark": "crit", "expect": "expected 0",
                                   "note": None}
    assert rows["pending"]["mark"] == "crit" and rows["pending"]["value"] == "3"
    assert rows["offline_unc"]["mark"] is None and rows["offline_unc"]["expect"] is None
    assert [r["key"] for r in view["rows"]] == [
        "reallocated", "pending", "offline_unc", "realloc_events"]
    assert view["stats"] == {"temp_c": 38, "power_on_hours": 31502, "power_cycles": 1204}
    assert view["volumes"] == {
        "written": {"bytes": 61200000000000, "source": "attribute", "attribute_id": 241},
        "read": {"omitted": "no_source"},
    }
    assert view["devstat"] == "absent"
    assert view["report"] == {"kind": "ata", "count": 4}
    assert view["can_save"] is True


@needs_rules
def test_view_ata_warning(mod):
    verdict, view = _view(mod, _ata_drive({"Command_Timeout": 120, "Reallocated_Event_Count": 1}))
    assert verdict["state"] == "MAYBE"
    assert view["header"] == "maybe"
    rows = _rows(view)
    assert rows["cmd_timeout"]["mark"] == "warn"
    assert rows["cmd_timeout"]["expect"] == "warns above 100"
    assert rows["realloc_events"]["mark"] == "warn"
    assert rows["realloc_events"]["expect"] == "expected 0"


@needs_rules
def test_view_smart_failed(mod):
    _, view = _view(mod, _ata_drive({"Reallocated_Sector_Ct": 24}, passed=False))
    assert view["header"] == "attention"
    assert view["health_failed"] is True
    # Health FAILED is on the header line; no row repeats it.
    assert all(row["key"] not in ("smart_status", "other") for row in view["rows"])


@needs_rules
def test_view_ata_wear(mod):
    _, view = _view(mod, _ata_drive({"Reallocated_Sector_Ct": 0}, wear=95))
    rows = _rows(view)
    assert rows["wear"] == {"key": "wear", "name": "Wear used", "value": "95%",
                            "mark": "warn", "expect": "warns at 90%", "note": None}


def _nvme(**changes: Any) -> dict[str, Any]:
    drive = _drive("nvme_healthy")
    drive["smart_data"]["nvme_smart_health_information_log"].update(changes)
    return drive


@needs_rules
def test_view_nvme_spare_warning(mod):
    verdict, view = _view(mod, _nvme(available_spare=15))
    assert verdict["state"] == "MAYBE"
    rows = _rows(view)
    assert rows["available_spare"]["value"] == "15%"
    assert rows["available_spare"]["mark"] == "warn"
    assert rows["available_spare"]["expect"] == "warns below 20%"
    assert [r["key"] for r in view["rows"]] == [
        "critical_warning", "media_errors", "available_spare", "wear"]
    assert view["report"]["kind"] == "nvme"


@needs_rules
def test_view_nvme_spare_critical(mod):
    verdict, view = _view(mod, _nvme(available_spare=8))
    assert verdict["state"] == "YES"
    spare = _rows(view)["available_spare"]
    assert spare["mark"] == "crit" and spare["value"] == "8%"
    assert spare["expect"] == "at or below the drive's own 10%"


@needs_rules
def test_view_nvme_wear_and_critical_warning(mod):
    _, view = _view(mod, _nvme(percentage_used=92, critical_warning=4, media_errors=2))
    rows = _rows(view)
    assert rows["wear"]["mark"] == "warn" and rows["wear"]["expect"] == "warns at 90%"
    assert rows["critical_warning"]["value"] == "0x04"
    assert rows["critical_warning"]["mark"] == "crit"
    assert rows["media_errors"]["mark"] == "crit"
    assert rows["media_errors"]["expect"] == "expected 0"


@needs_rules
def test_view_devstat_gap(mod):
    drive = _devstat_drive("ata_devstat_wdc_unc")
    verdict, view = _view(mod, drive)
    assert verdict["state"] == "YES"
    keys = [r["key"] for r in view["rows"]]
    # The attribute row is a ghost, the page 4 count stands in right after it.
    i = keys.index("reported_unc")
    assert keys[i + 1] == "reported_unc_devstat"
    assert view["rows"][i] == {"key": "reported_unc", "name": "Reported uncorrectable",
                               "value": "not known", "mark": "unk", "expect": None,
                               "note": "no attribute 187"}
    assert view["rows"][i + 1] == {"key": "reported_unc_devstat", "name": "Uncorrectable, page 4",
                                   "value": "18", "mark": "crit",
                                   "expect": "expected 0, from device statistics", "note": None}
    assert view["devstat"] == "present"
    assert "reallocated_devstat" not in keys  # attribute 5 is there


@needs_rules
def test_view_unsupported(mod):
    drive = {"id": "sdz", "model": "USB bridge", "protocol": "ATA", "smart_data": {}}
    verdict, view = _view(mod, drive)
    assert verdict["state"] == "UNSUPPORTED"
    assert view["header"] == "unknown"
    assert view["can_save"] is False
    assert [(r["key"], r["value"], r["mark"]) for r in view["rows"]] == [
        ("reallocated", "not known", "unk"), ("pending", "not known", "unk")]


def _board_drive(state: str) -> dict[str, Any]:
    if state == "2.1":
        return _drive("nvme_healthy")
    if state == "2.3":
        return {"id": "sdz", "model": "QEMU HARDDISK", "protocol": "ATA", "smart_data": {}}
    attrs = {"2.2": {"Reallocated_Sector_Ct": 24, "Current_Pending_Sector": 3,
                     "Offline_Uncorrectable": 0, "Reported_Uncorrect": 0, "Command_Timeout": 0},
             "3.1": {"Reallocated_Sector_Ct": 0, "Current_Pending_Sector": 0,
                     "Reallocated_Event_Count": 1},
             "3.10": {"Reallocated_Sector_Ct": 0, "Offline_Uncorrectable": 8},
             "3.12": {"Reallocated_Sector_Ct": 0, "Current_Pending_Sector": 2}}[state]
    return _ata_drive(attrs)


@needs_rules
@pytest.mark.parametrize("board,header,marks", [
    ("2.1", "ok", {"critical_warning": None, "media_errors": None, "available_spare": None,
                   "wear": None}),
    ("2.2", "attention", {"reallocated": "crit", "pending": "crit", "offline_unc": None,
                          "reported_unc": None, "cmd_timeout": None}),
    ("2.3", "unknown", {"reallocated": "unk", "pending": "unk"}),
    ("3.1", "maybe", {"reallocated": None, "pending": None, "realloc_events": "warn"}),
    ("3.10", "attention", {"reallocated": None, "offline_unc": "crit"}),
    ("3.12", "attention", {"reallocated": None, "pending": "crit"}),
])
def test_view_board_states(mod, board, header, marks):
    """The header word and every row mark of the drives drawn in board parts
    2 and 3, with "expected 0" inline on each marked counter."""
    _, view = _view(mod, _board_drive(board))
    assert view["header"] == header
    assert {row["key"]: row["mark"] for row in view["rows"]} == marks
    for row in view["rows"]:
        if row["mark"] in ("crit", "warn"):
            assert row["expect"] == "expected 0", row
        else:
            assert row["expect"] is None, row


def test_view_without_verdict(mod):
    view = mod.drive_view(_ata_drive({"Reallocated_Sector_Ct": 5}), None)
    assert view["header"] == "unknown"
    assert all(row["mark"] is None for row in view["rows"])


def test_view_unmatched_reason_becomes_other(mod):
    verdict = {"state": "MAYBE", "severity": "warning", "reasons": ["Something new: 3"]}
    view = mod.drive_view(_ata_drive({}), verdict)
    assert view["rows"][-1] == {"key": "other", "name": "Something new: 3", "value": "",
                                "mark": "warn", "expect": None, "note": None}


def _fixture_drives() -> list[dict[str, Any]]:
    drives = [_drive(p.stem) for p in sorted(FIXTURES.glob("*.json"))]
    drives += [_devstat_drive(p.name[:-len(".a.json")])
               for p in sorted((FIXTURES / "devstat").glob("*.a.json"))
               if (FIXTURES / "devstat" / p.name.replace(".a.json", ".devstat.json")).is_file()]
    return drives


@needs_rules
def test_every_fixture_reason_maps_to_a_row(mod):
    reasons: set[str] = set()
    for drive in _fixture_drives():
        verdict, view = _view(mod, drive)
        reasons.update(verdict["reasons"])
        assert all(row["key"] != "other" for row in view["rows"]), drive["id"]
    golden = json.loads((FIXTURES / "golden" / "v070-attention.json").read_text(encoding="utf-8"))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("default", "overrides") and isinstance(value, list) and len(value) == 4:
                    reasons.update(value[2])
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(golden)
    # Every label the rule module knows, worded as it words a breach.
    attention = mod.loaded_rules.attention
    for label in set(attention._CRITICAL_ATA.values()) | set(attention._WARNING_ATA.values()):
        reasons.add(f"{label}: 7 (expected 0)")
        reasons.add(f"{label}: 7 (expected 0; from device statistics)")
    reasons.update({
        "SMART overall status: FAILED",
        "NVMe critical warning flag set (0x04)",
        "NVMe media errors: 3 (expected 0)",
        "NVMe available spare (5%) at or below drive threshold (10%)",
        "NVMe available spare low: 15% remaining",
        "NVMe drive wear at 92% of rated life \u2014 consider scheduling replacement",
        "SSD wear at 95% of rated life -- consider scheduling replacement",
        "SSD wear at 95% of rated life -- consider scheduling replacement (from device statistics)",
        "Command Timeout: 120 (threshold 100)",
    })
    assert len(reasons) > 20
    for reason in sorted(reasons):
        assert mod.classify_reason(reason) is not None, reason


@needs_rules
def test_pool_verdict(mod):
    pools = json.loads((FIXTURES / "pools" / "degraded-faulted.json").read_text(encoding="utf-8"))
    verdict = mod.pool_verdict(pools[0], mod.loaded_rules)
    assert verdict["state"] == "YES"
    assert verdict["problems"][0] == "State: DEGRADED"
    healthy = json.loads((FIXTURES / "pools" / "never-scrubbed.json").read_text(encoding="utf-8"))
    assert mod.pool_verdict(healthy[0]) == {"state": "NO", "problems": []}


def test_pool_view_replica(mod):
    lab = {"vanished": False, "vdev": {"name": "mirror-0", "type": "mirror"},
           "devices": [{"name": "MOCK0001", "state": "ONLINE", "read": 0, "write": 0, "cksum": 0},
                       {"name": "MOCK0002", "state": "FAULTED", "read": 12, "write": 0, "cksum": 0}]}
    view = mod.pool_view({"name": "tank", "state": "DEGRADED"}, {"state": "YES"}, lab)
    assert view["header"] == "attention"
    assert view["layout"] == "two-disk mirror"
    assert view["state"] == "DEGRADED"
    assert [d["mark"] for d in view["disks"]] == [None, "crit"]
    real = mod.pool_view({"name": "tank", "state": "ONLINE"}, {"state": "NO"})
    assert real == {"header": "ok", "layout": None, "state": "ONLINE", "disks": None,
                    "vanished": False}
    gone = mod.pool_view({"name": "tank", "state": "ONLINE"}, {"state": "NO"}, {"vanished": True})
    assert gone["header"] == "unknown" and gone["vanished"] is True


# ── /app/info ───────────────────────────────────────────────────────────────

def test_app_info(proxy, mock_agent):
    STATE.flow_handlers.append("smart_sniffer")
    status, info = proxy.get("/app/info")
    assert status == 200
    replicas = info.pop("replicas")
    assert info == {
        "app_version": "0.3.0",
        "rules_version": info["rules_version"],  # from VERSION beside the rules, if any
        "agent_port": proxy.agent_port,
        "mock_port": proxy.mock_port,
        "hostname": AGENT_HOST,
        "slug": SLUG,
        "mdns_name": MDNS_NAME,
        "ha_version": HA_VERSION,
        "ha_api": True,
        "supervisor": True,
        "integration_installed": info["integration_installed"],  # cached 30 s, see below
        "network": {"block": "127.0.0.0/24", "address": "127.0.0.1", "prefix": 24,
                    "source": "supervisor"},
        "refresh_minutes": 15,
    }
    assert "mock_enabled" not in info
    if mock_agent:
        assert replicas == {"agent": "ok", "folder": "ok", "path": str(mock_agent["user"]),
                            "shipped": 6, "user": replicas["user"]}
    else:
        assert replicas["agent"] == "down"


def test_app_info_slug_is_cached(proxy):
    proxy.get("/app/info")
    STATE.calls.clear()
    _, info = proxy.get("/app/info")
    assert info["slug"] == SLUG
    assert info["ha_version"] == HA_VERSION
    assert not any(c[1] == "/addons/self/info" for c in STATE.calls)
    assert not any(c[1] == "/core/api/config" for c in STATE.calls)
    assert not any(c[1] == "/network/info" for c in STATE.calls)


def _app_info_from_fresh_proxy(supervisor, *extra: str) -> dict[str, Any]:
    """/app/info from a proxy of its own, so nothing is cached yet."""
    proc, client = _start_proxy([
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{supervisor.server_address[1]}",
        f"--agent-host={AGENT_HOST}",
        f"--mock-port={_free_port()}",
        *extra,
    ], TOKEN)
    try:
        status, info = client.get("/app/info")
    finally:
        _stop_proxy(proc)
    assert status == 200
    return info


def test_app_info_older_home_assistant(backends):
    # Before 2026 the app pages live under /hassio/addon/<slug>/; the page
    # reads the year from this version.
    STATE.ha_version = "2025.12.0"
    info = _app_info_from_fresh_proxy(backends[0])
    assert info["ha_version"] == "2025.12.0"
    assert info["slug"] == SLUG


def test_app_info_core_unreachable(backends):
    # The Supervisor answers but Core does not: the slug is known, the
    # version and the integration are null and the page falls back to the
    # newer paths.
    STATE.core_status = 401
    info = _app_info_from_fresh_proxy(backends[0])
    assert info["ha_api"] is False
    assert info["slug"] == SLUG
    assert info["ha_version"] is None
    assert info["integration_installed"] is None


def test_app_info_integration_installed(backends):
    assert _app_info_from_fresh_proxy(backends[0])["integration_installed"] is False
    STATE.entries = [_entry("e1", "SMART Sniffer (nas.local:9099)")]
    assert _app_info_from_fresh_proxy(backends[0])["integration_installed"] is True
    STATE.entries = []
    STATE.components.append("smart_sniffer")
    assert _app_info_from_fresh_proxy(backends[0])["integration_installed"] is True


def test_app_info_network_first_primary_connected(backends):
    STATE.network = {"interfaces": [
        {"interface": "wlan0", "primary": True, "connected": False,
         "ipv4": {"address": ["10.9.9.9/24"]}},
        {"interface": "eth0", "primary": True, "connected": True,
         "ipv4": {"address": ["10.0.10.5/22", "10.0.20.5/24"]}},
    ]}
    info = _app_info_from_fresh_proxy(backends[0])
    assert info["network"] == {"block": "10.0.8.0/22", "address": "10.0.10.5", "prefix": 22,
                               "source": "supervisor"}
    STATE.network = {"interfaces": [{"interface": "eth0", "primary": True, "connected": True,
                                     "ipv4": {"address": []}}]}
    assert _app_info_from_fresh_proxy(backends[0])["network"] is None


def test_app_info_replica_agent_down(backends):
    info = _app_info_from_fresh_proxy(backends[0])
    assert info["replicas"] == {"agent": "down", "folder": None, "path": None,
                                "shipped": None, "user": None}


def test_app_info_version_missing_from_config(backends):
    # A Core that leaves the field out: null, never a made-up version.
    STATE.ha_version = None
    info = _app_info_from_fresh_proxy(backends[0])
    assert info["ha_version"] is None


def test_app_info_without_supervisor(backends):
    # The Supervisor refuses the token, run.sh found no mDNS name, and no
    # --agent-host was given: slug, network and mdns_name are null, the host
    # is the proxy's default.
    supervisor, _ = backends
    spec = importlib.util.spec_from_file_location("webui_proxy", PROXY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    proc, client = _start_proxy([
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{supervisor.server_address[1]}",
        "--mdns-name=",
    ], "not-the-token")
    try:
        status, info = client.get("/app/info")
    finally:
        _stop_proxy(proc)
    assert status == 200
    assert info["ha_api"] is False
    assert info["supervisor"] is False
    assert info["slug"] is None
    assert info["network"] is None
    assert info["integration_installed"] is None
    assert info["mdns_name"] is None
    assert info["ha_version"] is None
    assert info["hostname"] == module.DEFAULT_AGENT_HOST


def test_wrong_method_is_405(proxy):
    status, body = proxy.get("/evaluate")
    assert status == 405 and body["error"] == "method_not_allowed"


def test_agent_proxy_still_works(proxy):
    status, health = proxy.get("/api/health")
    assert status == 200 and health["status"] == "ok"


# ── /evaluate ───────────────────────────────────────────────────────────────

@needs_rules
def test_evaluate_healthy_nvme(proxy):
    status, verdict = proxy.post("/evaluate", _drive("nvme_healthy"))
    assert status == 200
    assert verdict["state"] == "NO"
    assert verdict["severity"] == "none"
    assert verdict["reasons"] == []
    assert verdict["accepted"] == []
    assert verdict["reasons_text"] == "No issues detected"
    assert verdict["readings"]["NVMe Media Errors"] == 0
    assert "rules_version" in verdict
    assert verdict["view"]["header"] == "ok"


@needs_rules
def test_evaluate_nvme_media_errors(proxy):
    drive = _drive("nvme_healthy")
    drive["smart_data"]["nvme_smart_health_information_log"]["media_errors"] = 3
    status, verdict = proxy.post("/evaluate", drive)
    assert status == 200
    assert verdict["state"] == "YES"
    assert verdict["severity"] == "critical"
    assert verdict["reasons"] == ["NVMe media errors: 3 (expected 0)"]
    assert verdict["reasons_text"] == "NVMe media errors: 3 (expected 0)"
    assert verdict["readings"]["NVMe Media Errors"] == 3
    rows = {r["key"]: r for r in verdict["view"]["rows"]}
    assert rows["media_errors"]["mark"] == "crit"


@needs_rules
def test_evaluate_pool(proxy):
    pools = json.loads((FIXTURES / "pools" / "degraded-faulted.json").read_text(encoding="utf-8"))
    status, body = proxy.post("/evaluate", {"pool": pools[0]})
    assert status == 200
    assert body["state"] == "YES" and "State: DEGRADED" in body["problems"]
    healthy = json.loads((FIXTURES / "pools" / "never-scrubbed.json").read_text(encoding="utf-8"))
    assert proxy.post("/evaluate", {"pool": healthy[0]}) == (200, {"state": "NO", "problems": []})


def test_evaluate_rejects_non_object(proxy):
    status, body = proxy.post("/evaluate", [1, 2])
    assert status == 400 and body["error"] == "bad_request"


def test_evaluate_without_rules(backends):
    proc, client = _start_proxy([
        "--rules-dir=/nonexistent",
        f"--supervisor-url=http://127.0.0.1:{backends[0].server_address[1]}",
    ], TOKEN)
    try:
        status, body = client.post("/evaluate", {"id": "sda", "smart_data": {}})
    finally:
        _stop_proxy(proc)
    assert status == 503 and body["error"] == "rules_unavailable"


# ── Removed routes ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("POST", "/lab/on"), ("POST", "/lab/off"),
])
def test_removed_routes_are_404(proxy, method, path):
    status, body = proxy.request(method, path, {})
    assert status == 404 and body["error"] == "not_found"
    assert not STATE.restarted.is_set()
    assert not any(c[1] == "/addons/self/options" for c in STATE.calls)


# ── Field agents ────────────────────────────────────────────────────────────
#
# Fake agents on loopback addresses, all on one port, so a scan of
# 127.0.0.0/29 finds them. 127.0.0.1 is this app's own address (the fake
# Supervisor's network info says so) and must be skipped.

FIELD = {
    "127.0.0.1": AgentState(hostname="own-host"),
    "127.0.0.2": AgentState(hostname="office-nuc",
                            drives=[simple_drive("sda", "NUC-1"), simple_drive("sdb", "NUC-2")]),
    "127.0.0.3": AgentState(token=FIELD_PASSWORD, hostname="backup-pi"),
    "127.0.0.4": AgentState(mode="404"),
    "127.0.0.5": AgentState(mode="notagent"),
    # 127.0.0.6: nothing listens
}


@pytest.fixture(scope="session")
def field():
    """Start the fake field agents; returns their common port."""
    for _ in range(20):
        port = _free_port()
        servers = []
        try:
            for address, state in FIELD.items():
                servers.append(_serve(agent_handler(state), address, port))
        except OSError:
            for server in servers:
                server.shutdown()
            continue
        break
    else:
        pytest.skip("no common free port on the loopback addresses")
    yield port
    for server in servers:
        server.shutdown()


@pytest.fixture(autouse=True)
def fresh_field():
    for address, state in FIELD.items():
        state.requests.clear()
        state.mode = "agent" if address not in ("127.0.0.4", "127.0.0.5") else state.mode
        state.pools = None
        state.filesystems = None
        if address == "127.0.0.2":
            state.drives = [simple_drive("sda", "NUC-1"), simple_drive("sdb", "NUC-2")]
        if address == "127.0.0.3":
            state.token = FIELD_PASSWORD
    yield


def _wait_scan(proxy, scan_id: str, timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        status, body = proxy.get(f"/fleet/scan/{scan_id}")
        assert status == 200
        if body["state"] != "running" or time.monotonic() > deadline:
            return body
        time.sleep(0.1)


def _store(*hosts: dict[str, Any]) -> None:
    STATE.options["hosts"] = [dict(h) for h in hosts]


def _option_writes() -> list[Any]:
    return [c[2] for c in STATE.calls if c[1] == "/addons/self/options"]


def _refresh_hosts(proxy) -> list[dict[str, Any]]:
    status, body = proxy.get("/fleet/hosts")
    assert status == 200
    return body["hosts"]


def test_scan_finds_agents(proxy, field):
    _store({"address": "127.0.0.3", "port": field, "password": FIELD_PASSWORD})
    _refresh_hosts(proxy)
    status, started = proxy.post("/fleet/scan", {"block": "127.0.0.0/29", "port": field})
    assert status == 202
    assert started["total"] == 6
    body = _wait_scan(proxy, started["scan_id"])
    assert body["state"] == "done"
    assert body["probed"] == body["total"] == 6
    assert body["block"] == "127.0.0.0/29" and body["port"] == field
    assert body["finished_at"] and body["started_at"]
    rows = {row["address"]: row for row in body["found"]}
    assert set(rows) == {"127.0.0.2", "127.0.0.3"}  # not own, not 404, not the printer
    assert rows["127.0.0.2"] == {
        "id": f"127.0.0.2:{field}", "address": "127.0.0.2", "port": field,
        "hostname": "office-nuc", "version": "0.8.1", "drives": 2, "pools": 0,
        "auth": "open", "added": False,
    }
    assert rows["127.0.0.3"]["auth"] == "password"
    assert rows["127.0.0.3"]["added"] is True
    assert rows["127.0.0.3"]["drives"] == 1  # from health; the list wants a password
    # Only GET /api/health and GET /api/drives, never with credentials.
    for state in FIELD.values():
        for method, path, auth in state.requests:
            assert method == "GET" and path in ("/api/health", "/api/drives")
            assert auth is None
    assert FIELD["127.0.0.1"].requests == []


@pytest.mark.parametrize("block,total", [
    ("127.0.0.0/24", 254), ("127.0.0.9", 1), ("127.0.0.9/32", 1), ("127.0.0.0/22", 1022),
])
def test_scan_blocks_accepted(proxy, block, total):
    status, body = proxy.post("/fleet/scan", {"block": block, "port": _free_port()})
    assert status == 202 and body["total"] == total
    proxy.request("DELETE", f"/fleet/scan/{body['scan_id']}")


def _fresh_proxy(supervisor, *extra: str) -> tuple[subprocess.Popen, Proxy]:
    return _start_proxy([
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{supervisor.server_address[1]}",
        f"--agent-host={AGENT_HOST}",
        f"--mock-port={_free_port()}",
        *extra,
    ], TOKEN)


def test_scan_skips_every_address_of_this_machine(backends):
    # The host's addresses on every interface, its side of the hassio
    # network, every address in that network (other apps' containers) and
    # an agent whose health names this app's host: nothing found, and
    # nothing but the health read sent to the last.
    STATE.network = {
        "interfaces": [
            {"interface": "eth0", "primary": True, "connected": True,
             "ipv4": {"address": ["10.255.0.5/24"]}},
            {"interface": "wlan0", "primary": False, "connected": False,
             "ipv4": {"address": ["127.0.2.9/24"]}},
        ],
        "docker": {"interface": "hassio", "address": "127.0.3.0/24", "gateway": "127.0.2.10"},
    }
    agents = {
        "127.0.2.9": AgentState(hostname="host-wlan"),
        "127.0.2.10": AgentState(hostname="host-hassio"),
        "127.0.2.11": AgentState(hostname=AGENT_HOST.upper() + ".local"),
        "127.0.2.12": AgentState(hostname="office-nuc"),
        "127.0.3.5": AgentState(hostname="published-app"),
    }
    port = _free_port()
    servers = [_serve(agent_handler(state), address, port) for address, state in agents.items()]
    proc, client = _fresh_proxy(backends[0])
    try:
        _, started = client.post("/fleet/scan", {"block": "127.0.2.8/29", "port": port})
        body = _wait_scan(client, started["scan_id"])
        assert body["state"] == "done" and body["probed"] == body["total"] == 6
        assert [row["address"] for row in body["found"]] == ["127.0.2.12"]
        _, started = client.post("/fleet/scan", {"block": "127.0.3.4/30", "port": port})
        body = _wait_scan(client, started["scan_id"])
        assert body["state"] == "done" and body["found"] == []
    finally:
        _stop_proxy(proc)
        for server in servers:
            server.shutdown()
    for address in ("127.0.2.9", "127.0.2.10", "127.0.3.5"):
        assert agents[address].requests == [], address
    assert [r[1] for r in agents["127.0.2.11"].requests] == ["/api/health"]


def test_scan_skips_the_container_own_address(backends, field, mod):
    # The Supervisor lists none of the loopback addresses; the container's
    # own address (here what this machine's host name resolves to) is still
    # skipped.
    if "127.0.0.1" not in mod.local_ipv4_addresses():
        pytest.skip("this machine's host name does not resolve to 127.0.0.1")
    STATE.network = {"interfaces": [{"interface": "eth0", "primary": True, "connected": True,
                                     "ipv4": {"address": ["10.255.0.5/24"]}}]}
    proc, client = _fresh_proxy(backends[0])
    try:
        _, started = client.post("/fleet/scan", {"block": "127.0.0.0/30", "port": field})
        body = _wait_scan(client, started["scan_id"])
    finally:
        _stop_proxy(proc)
    assert [row["address"] for row in body["found"]] == ["127.0.0.2"]
    assert FIELD["127.0.0.1"].requests == []


def test_route_targets_and_local_addresses(mod, tmp_path):
    table = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010200C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000200C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
        "hassio\t0000000A\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "broken\tzzzz\n"
    )
    if sys.byteorder == "little":
        assert mod._route_targets(table) == ["192.0.2.1", "192.0.2.1", "10.0.0.1"]
    route = tmp_path / "route"
    route.write_text(table)
    for found in (mod.local_ipv4_addresses(str(route)),
                  mod.local_ipv4_addresses(str(tmp_path / "missing"))):
        assert all(mod.ipaddress.IPv4Address(a) for a in found)
        assert "0.0.0.0" not in found


def test_scan_too_wide(proxy):
    STATE.calls.clear()
    status, body = proxy.post("/fleet/scan", {"block": "10.0.0.0/21", "port": 9099})
    assert status == 422
    assert body["error"] == "too_wide"
    assert body["addresses"] == 2046
    assert body["suggest"] == "127.0.0.0/24"  # the /24 holding this app's address
    status, body = proxy.post("/fleet/scan", {"block": "10.0.0.0/16", "port": 9099})
    assert body["addresses"] == 65534


@pytest.mark.parametrize("body,code", [
    ({"block": "nonsense", "port": 9099}, "bad_block"),
    ({"block": "fe80::/120", "port": 9099}, "bad_block"),
    ({"block": "", "port": 9099}, "bad_block"),
    ({"block": "10.0.0.0/24", "port": 0}, "bad_port"),
    ({"block": "10.0.0.0/24", "port": "x"}, "bad_port"),
])
def test_scan_bad_input(proxy, body, code):
    status, reply = proxy.post("/fleet/scan", body)
    assert status == 400 and reply["error"] == code


def test_new_scan_cancels_old_and_delete(proxy):
    slow = [_serve(agent_handler(AgentState(mode="slow")), f"127.0.1.{i}", 0) for i in (1,)]
    port = slow[0].server_address[1]
    try:
        _, first = proxy.post("/fleet/scan", {"block": "127.0.1.0/30", "port": port})
        _, second = proxy.post("/fleet/scan", {"block": "127.0.1.0/30", "port": port})
        status, body = proxy.get(f"/fleet/scan/{first['scan_id']}")
        assert status == 200 and body["state"] == "cancelled"
        status, body = proxy.request("DELETE", f"/fleet/scan/{second['scan_id']}")
        assert status == 200 and body == {"state": "cancelled"}
        assert proxy.get(f"/fleet/scan/{second['scan_id']}")[1]["state"] == "cancelled"
    finally:
        for server in slow:
            server.shutdown()
    assert proxy.get("/fleet/scan/nope")[0] == 404
    assert proxy.request("DELETE", "/fleet/scan/nope")[0] == 404


def test_scan_expires(mod):
    import ipaddress as ip

    scans = mod.ScanManager()
    scan = mod.Scan("abc", ip.ip_network("127.0.9.0/30"), 1)
    scans.scans["abc"] = scan
    scan.finish("done")
    assert scans.get("abc") is scan
    scan.finished_mono -= mod.SCAN_KEEP_SECONDS + 1
    assert scans.get("abc") is None


def test_probe_address_and_name(proxy, field):
    status, row = proxy.post("/fleet/probe", {"host": "127.0.0.2", "port": field})
    assert status == 200
    assert row["hostname"] == "office-nuc" and row["auth"] == "open" and row["added"] is False
    status, row = proxy.post("/fleet/probe", {"host": "localhost", "port": field})
    assert status == 200
    assert row["address"] == "localhost"  # what was typed
    assert row["id"] == f"localhost:{field}"
    assert row["resolved"] == "127.0.0.1"
    assert row["hostname"] == "own-host"
    status, row = proxy.post("/fleet/probe", {"host": "127.0.0.3", "port": field})
    assert row["auth"] == "password"


def test_probe_failures(proxy, field):
    assert proxy.post("/fleet/probe", {"host": "127.0.0.6", "port": field})[1]["error"] == "not_answering"
    assert proxy.post("/fleet/probe", {"host": "127.0.0.6", "port": field})[0] == 504
    status, body = proxy.post("/fleet/probe", {"host": "127.0.0.4", "port": field})
    assert status == 404 and body["error"] == "not_an_agent"
    status, body = proxy.post("/fleet/probe", {"host": "127.0.0.5", "port": field})
    assert status == 404 and body["error"] == "not_an_agent"
    status, body = proxy.post("/fleet/probe", {"host": "no-such-host.invalid", "port": field})
    assert status == 400 and body["error"] == "cannot_resolve"
    status, body = proxy.post("/fleet/probe", {"host": "bad host!", "port": field})
    assert status == 400 and body["error"] == "bad_request"


def test_hosts_add_one_write(proxy, field):
    STATE.options["token"] = "agent-secret"
    _store({"address": "nas.lan", "port": 9099, "password": "keep-me"})
    STATE.calls.clear()
    status, body = proxy.post("/fleet/hosts", {"hosts": [
        {"address": "127.0.0.2", "port": field},
        {"address": "Office-NUC.lan", "port": 9099},
        {"address": "NAS.lan", "port": 9099},
    ]})
    assert status == 201
    assert [h["id"] for h in body["added"]] == [f"127.0.0.2:{field}", "office-nuc.lan:9099"]
    assert body["skipped"] == [{"address": "nas.lan", "port": 9099, "reason": "already_added"}]
    assert body["added"][0] == {
        "id": f"127.0.0.2:{field}", "address": "127.0.0.2", "port": field,
        "has_password": False, "hidden": False, "hidden_since": None,
        "added_at": body["added"][0]["added_at"],
    }
    assert body["added"][0]["added_at"].endswith("Z")
    writes = _option_writes()
    assert len(writes) == 1
    # The whole options object goes back, with only hosts changed.
    assert writes[0] == {"options": {
        "port": 9099, "token": "agent-secret", "scan_interval": 60, "mock_port": 9100,
        "hosts": [{"address": "nas.lan", "port": 9099, "password": "keep-me"},
                  {"address": "127.0.0.2", "port": field},
                  {"address": "office-nuc.lan", "port": 9099}],
    }}
    assert not STATE.restarted.is_set()
    assert not any("restart" in c[1] for c in STATE.calls)
    # No password in any answer.
    hosts = _refresh_hosts(proxy)
    assert "keep-me" not in json.dumps(hosts)
    assert [h["has_password"] for h in hosts] == [True, False, False]


def test_hosts_add_only_duplicates_writes_nothing(proxy):
    _store({"address": "nas.lan", "port": 9099})
    STATE.calls.clear()
    status, body = proxy.post("/fleet/hosts", {"hosts": [{"address": "nas.lan", "port": 9099}]})
    assert status == 201 and body["added"] == [] and len(body["skipped"]) == 1
    assert _option_writes() == []


@pytest.mark.parametrize("hosts", [
    [{"address": "not a host", "port": 9099}],
    [{"address": "10.0.0.300", "port": 9099}],
    [{"address": "nas.lan", "port": 70000}],
    [],
])
def test_hosts_add_bad_input(proxy, hosts):
    status, body = proxy.post("/fleet/hosts", {"hosts": hosts})
    assert status == 400 and body["error"] == "bad_request"
    assert _option_writes() == []


def _wait_for(check, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


@pytest.mark.parametrize("schema", ["reported", "not_reported"])
def test_start_removes_options_the_schema_no_longer_has(backends, schema):
    # A 0.2.x install keeps mock_mode after the update until the options are
    # written; the proxy writes them once at start without it.
    hosts = [{"address": "nas.lan", "port": 9099, "password": FIELD_PASSWORD}]
    STATE.options = {**STATE.options, "mock_mode": True, "future": 1, "hosts": hosts}
    expected = {"port": 9099, "token": "", "scan_interval": 60, "mock_port": 9100, "hosts": hosts}
    if schema == "not_reported":
        # Without the schema only the known old option goes.
        STATE.schema = None
        expected["future"] = 1
    proc, _ = _fresh_proxy(backends[0])
    try:
        _wait_for(lambda: _option_writes())
        time.sleep(0.3)
    finally:
        output = _stop_proxy(proc)
    assert _option_writes() == [{"options": expected}]
    assert STATE.options == expected
    removed = [line for line in output.splitlines() if "Removed the old option" in line]
    assert len(removed) == 1 and "mock_mode" in removed[0]
    assert FIELD_PASSWORD not in output


def test_start_writes_nothing_when_the_options_are_current(backends):
    proc, _ = _fresh_proxy(backends[0])
    try:
        _wait_for(lambda: any(c[1] == "/addons/self/info" for c in STATE.calls))
        time.sleep(0.3)
    finally:
        output = _stop_proxy(proc)
    assert _option_writes() == []
    assert "Removed the old option" not in output


def test_hosts_supervisor_refuses(proxy):
    STATE.options_status = 400
    status, body = proxy.post("/fleet/hosts", {"hosts": [{"address": "nas.lan", "port": 9099}]})
    assert status == 502
    assert body == {"error": "supervisor_failed",
                    "message": "Option 'hosts' does not match the schema"}


def test_hosts_without_supervisor(backends):
    proc, client = _start_proxy([
        f"--supervisor-url=http://127.0.0.1:{backends[0].server_address[1]}",
        "--data-dir=",
    ], "")
    try:
        assert client.get("/fleet/hosts")[1]["error"] == "supervisor_unavailable"
        status, body = client.post("/fleet/hosts", {"hosts": [{"address": "nas.lan", "port": 1}]})
        assert status == 503 and body["error"] == "supervisor_unavailable"
    finally:
        _stop_proxy(proc)


def test_patch_password(proxy, field):
    key = f"127.0.0.3:{field}"
    _store({"address": "127.0.0.3", "port": field})
    STATE.calls.clear()
    status, body = proxy.request("PATCH", f"/fleet/hosts/{quote(key, safe='')}",
                                 {"password": "wrong-pass"})
    assert status == 422 and body["error"] == "password_refused"
    assert _option_writes() == []
    status, body = proxy.request("PATCH", f"/fleet/hosts/{quote(key, safe='')}",
                                 {"password": FIELD_PASSWORD})
    assert status == 200
    assert body["host"]["has_password"] is True
    assert FIELD_PASSWORD not in json.dumps(body)
    assert _option_writes() == [{"options": {**STATE.options}}]
    assert STATE.options["hosts"] == [{"address": "127.0.0.3", "port": field,
                                       "password": FIELD_PASSWORD}]
    # The check sent the password to that agent only, as a bearer token.
    assert ("GET", "/api/drives", f"Bearer {FIELD_PASSWORD}") in FIELD["127.0.0.3"].requests
    # An empty string clears it.
    status, body = proxy.request("PATCH", f"/fleet/hosts/{quote(key, safe='')}", {"password": ""})
    assert status == 200 and body["host"]["has_password"] is False
    assert STATE.options["hosts"] == [{"address": "127.0.0.3", "port": field}]


def test_patch_password_not_answering(proxy, field):
    _store({"address": "127.0.0.6", "port": field})
    status, body = proxy.request("PATCH", f"/fleet/hosts/127.0.0.6:{field}", {"password": "x"})
    assert status == 504 and body["error"] == "not_answering"
    assert _option_writes() == []


def test_patch_hidden(proxy):
    _store({"address": "nas.lan", "port": 9099})
    STATE.calls.clear()
    status, body = proxy.request("PATCH", "/fleet/hosts/nas.lan%3A9099", {"hidden": True})
    assert status == 200
    assert body["host"]["hidden"] is True and body["host"]["hidden_since"].endswith("Z")
    assert len(_option_writes()) == 1
    assert STATE.options["hosts"] == [{"address": "nas.lan", "port": 9099, "hidden": True}]
    assert _refresh_hosts(proxy)[0]["hidden_since"] == body["host"]["hidden_since"]
    status, body = proxy.request("PATCH", "/fleet/hosts/nas.lan%3A9099", {"hidden": False})
    assert status == 200 and body["host"]["hidden"] is False
    assert body["host"]["hidden_since"] is None
    assert len(_option_writes()) == 2


def test_patch_and_delete_unknown(proxy):
    assert proxy.request("PATCH", "/fleet/hosts/nope:1", {"hidden": True})[0] == 404
    assert proxy.request("DELETE", "/fleet/hosts/nope:1")[0] == 404
    assert proxy.request("PATCH", "/fleet/hosts/nope:1", {})[0] == 400


def test_delete_host(proxy, field, data_dir):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    assert proxy.get(f"/fleet/hosts/{key}/drives")[1]["status"] == "ok"
    cache = json.loads((data_dir / "fleet-cache.json").read_text(encoding="utf-8"))
    assert key in cache["hosts"]
    STATE.calls.clear()
    status, body = proxy.request("DELETE", f"/fleet/hosts/{key}")
    assert status == 200 and body == {"removed": key}
    assert len(_option_writes()) == 1 and STATE.options["hosts"] == []
    cache = json.loads((data_dir / "fleet-cache.json").read_text(encoding="utf-8"))
    assert key not in cache["hosts"]


@needs_rules
def test_drives_self_uses_app_token(proxy):
    SELF_AGENT.drives = [simple_drive("sda", "SELF-1", {"Reallocated_Sector_Ct": 4})]
    status, body = proxy.get("/fleet/hosts/self/drives")
    assert status == 200
    assert body["kind"] == "self" and body["status"] == "ok" and body["stale"] is False
    assert body["agent"]["hostname"] == AGENT_HOST
    assert body["agent"]["version"] == "0.8.1"
    assert body["summary"] == {"header": "attention", "attention": 1, "maybe": 0,
                               "unsupported": 0, "drives": 1, "pools": 0}
    item = body["items"][0]
    assert item["type"] == "drive" and item["id"] == "sda"
    assert item["verdict"]["state"] == "YES"
    assert item["view"]["rows"][0]["mark"] == "crit"
    assert "_devstat" not in item["drive"]
    assert body["pools_status"] == "none"
    assert ("GET", "/api/drives", f"Bearer {APP_TOKEN}") in SELF_AGENT.requests
    assert ("GET", "/api/drives/sda", f"Bearer {APP_TOKEN}") in SELF_AGENT.requests


@needs_rules
def test_drives_field_with_password_and_pools(proxy, field):
    key = f"127.0.0.3:{field}"
    pools = json.loads((FIXTURES / "pools" / "degraded-faulted.json").read_text(encoding="utf-8"))
    FIELD["127.0.0.3"].pools = pools[:1]
    _store({"address": "127.0.0.3", "port": field, "password": FIELD_PASSWORD})
    _refresh_hosts(proxy)
    status, body = proxy.get(f"/fleet/hosts/{key}/drives")
    assert status == 200 and body["status"] == "ok" and body["kind"] == "field"
    assert body["agent"]["hostname"] == "backup-pi"
    assert body["pools_status"] == "ok"
    pool = body["items"][-1]
    assert pool["type"] == "pool" and pool["id"] == "pool.tank"
    assert pool["verdict"]["state"] == "YES"
    assert pool["view"]["layout"] is None and pool["view"]["header"] == "attention"
    assert body["summary"]["header"] == "attention" and body["summary"]["pools"] == 1
    assert all(auth == f"Bearer {FIELD_PASSWORD}" for m, p, auth in FIELD["127.0.0.3"].requests
               if p != "/api/health")


@needs_rules
def test_drives_pools_only_when_advertised(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    status, body = proxy.get(f"/fleet/hosts/{key}/drives")
    assert status == 200 and body["pools_status"] == "none"
    assert not any(p == "/api/pools" for _, p, _ in FIELD["127.0.0.2"].requests)
    assert body["summary"]["header"] == "ok"


@needs_rules
def test_drives_password_needed_and_refused(proxy, field):
    key = f"127.0.0.3:{field}"
    _store({"address": "127.0.0.3", "port": field})
    _refresh_hosts(proxy)
    status, body = proxy.get(f"/fleet/hosts/{key}/drives")
    assert status == 200 and body["status"] == "password_needed"
    assert body["summary"]["header"] == "unknown"
    # The pending card (board 3.6): version and drive count from the health
    # answer; rows only from an earlier good read, marked stale.
    assert body["agent"]["hostname"] == "backup-pi"
    assert body["agent"]["version"] == "0.8.1" and body["agent"]["drives"] == 1
    assert all(item["stale"] is True for item in body["items"])
    _store({"address": "127.0.0.3", "port": field, "password": "wrong-pass"})
    _refresh_hosts(proxy)
    assert proxy.get(f"/fleet/hosts/{key}/drives")[1]["status"] == "password_refused"


@needs_rules
def test_drives_unreachable_serves_cache(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    _, good = proxy.get(f"/fleet/hosts/{key}/drives")
    assert good["status"] == "ok"
    FIELD["127.0.0.2"].mode = "broken"
    status, body = proxy.get(f"/fleet/hosts/{key}/drives")
    assert status == 200
    assert body["status"] == "unreachable" and body["stale"] is True
    assert body["last_answer"] == good["last_answer"]
    assert [i["id"] for i in body["items"]] == ["sda", "sdb"]
    assert all(i["stale"] is True for i in body["items"])
    assert body["summary"]["header"] == "unknown"
    assert body["agent"]["hostname"] == "office-nuc"


def test_drives_unknown_host(proxy):
    status, body = proxy.get("/fleet/hosts/10.9.9.9:9099/drives")
    assert status == 404 and body["error"] == "not_found"


def _item(state: str | None, kind: str = "drive", vanished: bool = False) -> dict[str, Any]:
    return {"type": kind, "verdict": {"state": state} if state else None,
            "view": {"vanished": vanished}}


@pytest.mark.parametrize("states,ok,header", [
    ([_item("YES"), _item("NO")], True, "attention"),
    ([_item("NO"), _item("NO", "pool")], True, "ok"),
    ([_item("NO"), _item("YES", "pool")], True, "attention"),
    ([_item("MAYBE"), _item("NO")], True, "maybe"),
    ([_item("MAYBE"), _item("YES")], True, "attention"),
    ([_item("UNSUPPORTED"), _item("NO")], True, "ok"),
    ([_item("UNSUPPORTED"), _item("UNSUPPORTED")], True, "unknown"),
    ([], True, "unknown"),
    ([_item("YES")], False, "unknown"),
    ([_item("NO"), _item("YES", "pool", vanished=True)], True, "ok"),
])
def test_summary_header(mod, states, ok, header):
    assert mod.summarize(states, ok)["header"] == header


# ── /ha/devices ─────────────────────────────────────────────────────────────

def _device(device_id: str, entry_id: str, serial: str | None = None,
            pool: str | None = None) -> dict[str, Any]:
    identifiers = [["smart_sniffer", f"{entry_id}_{serial}" if serial else f"{entry_id}_zpool_{pool}"]]
    return {"id": device_id, "name": serial or f"ZFS pool {pool}", "serial_number": serial,
            "identifiers": identifiers, "config_entries": [entry_id]}


def test_ha_devices_core_down(proxy):
    STATE.core_status = 401
    status, body = proxy.get("/ha/devices")
    assert status == 200
    assert body == {"installed": None, "reachable": False, "lookup": None,
                    "integration_url": "/config/integrations/integration/smart_sniffer",
                    "targets": {}, "ha_removal": {}}


def test_ha_devices_not_installed(proxy):
    _, body = proxy.get("/ha/devices")
    assert body["reachable"] is True and body["installed"] is False
    assert body["targets"] == {}


def test_ha_devices_registry(proxy):
    _store({"address": "10.0.10.20", "port": 9099}, {"address": "nas.lan", "port": 9099})
    _refresh_hosts(proxy)
    STATE.entries = [
        _entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})"),
        _entry("e-lab", f"SMART Sniffer ({AGENT_HOST}:{proxy.mock_port})", state="setup_retry"),
        _entry("e-field", "SMART Sniffer (10.0.10.20:9099)"),
        _entry("e-other", "SMART Sniffer (somewhere.else:9099)"),
    ]
    STATE.devices = [
        _device("dev-self", "e-agent", serial="S5GXNF0R412233A"),
        _device("dev-drive", "e-field", serial="VK0ABCDE"),
        _device("dev-pool", "e-field", pool="cache"),
        _device("dev-elsewhere", "e-other", serial="ZZZ"),
        {"id": "dev-qemu", "name": "QEMU", "serial_number": "",
         "identifiers": [["smart_sniffer", "dev-sda"]], "config_entries": ["e-agent"]},
    ]
    status, body = proxy.get("/ha/devices")
    assert status == 200
    assert body["installed"] is True and body["reachable"] is True
    assert body["lookup"] == "registry"
    assert set(body["targets"]) == {"self", "replicas", "10.0.10.20:9099"}
    assert body["targets"]["self"] == {
        "entry_id": "e-agent",
        "entry_url": "/config/integrations/integration/smart_sniffer#config_entry=e-agent",
        "state": "loaded",
        "devices": {"S5GXNF0R412233A": "dev-self", "id:dev-sda": "dev-qemu"},
        "names": {"S5GXNF0R412233A": {"name": "S5GXNF0R412233A", "name_by_user": None},
                  "id:dev-sda": {"name": "QEMU", "name_by_user": None}},
        "identifiers": {"id:dev-sda": "dev-sda"},
    }
    assert body["targets"]["replicas"]["entry_id"] == "e-lab"
    assert body["targets"]["10.0.10.20:9099"]["devices"] == {
        "VK0ABCDE": "dev-drive", "pool:cache": "dev-pool"}
    template = [c[2] for c in STATE.calls if c[1] == "/core/api/template"][0]["template"]
    assert "integration_entities('smart_sniffer')" in template
    assert "'config_entries')" not in template  # deprecated, raises on 2026.10


def test_ha_devices_template_fails(proxy):
    STATE.entries = [_entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})")]
    STATE.devices = None
    _, body = proxy.get("/ha/devices")
    assert body["lookup"] == "entries_only"
    assert body["targets"]["self"]["devices"] == {}
    assert body["targets"]["self"]["entry_url"].endswith("#config_entry=e-agent")


def test_ha_devices_zeroconf_titles(proxy):
    # Zeroconf entries carry the hostname only. This app's own: the port
    # from the diagnostics tells the agent from the replicas, and without it
    # the entry is the agent's. A field agent: host and port from the
    # diagnostics, compared with the stored address.
    _store({"address": "localhost", "port": 9200})
    _refresh_hosts(proxy)
    STATE.entries = [
        _entry("z-agent", f"SMART Sniffer ({AGENT_HOST})"),
        _entry("z-lab", f"SMART Sniffer ({AGENT_HOST})"),
        _entry("z-field", "SMART Sniffer (proxmox-01)"),
    ]
    STATE.diagnostics = {"z-lab": proxy.mock_port,
                         "z-field": {"host": "127.0.0.1", "port": 9200}}
    _, body = proxy.get("/ha/devices")
    assert body["targets"]["self"]["entry_id"] == "z-agent"
    assert body["targets"]["replicas"]["entry_id"] == "z-lab"
    # localhost resolves to 127.0.0.1, the address in the diagnostics.
    assert body["targets"]["localhost:9200"]["entry_id"] == "z-field"


def _diagnostics_calls(entry_id: str) -> list[tuple[str, str, Any]]:
    return [c for c in STATE.calls if c[1] == f"/core/api/diagnostics/config_entry/{entry_id}"]


def test_ha_devices_no_diagnostics_for_an_entry_not_loaded(proxy):
    # A zeroconf entry that never set up: Core answers 500 for its
    # diagnostics, so the proxy does not ask.
    _store({"address": "localhost", "port": 9201})
    _refresh_hosts(proxy)
    STATE.entries = [_entry("z-retry", "SMART Sniffer (nas-retry)", state="setup_retry")]
    STATE.diagnostics = {"z-retry": {"host": "127.0.0.1", "port": 9201}}
    for _ in range(2):
        status, body = proxy.get("/ha/devices")
        assert status == 200 and body["targets"] == {}
    assert _diagnostics_calls("z-retry") == []


def test_ha_devices_failed_diagnostics_asked_once(proxy):
    # A loaded entry whose diagnostics fail is asked once, not on every refresh.
    STATE.entries = [_entry("z-broken", "SMART Sniffer (nas-broken)")]
    STATE.diagnostics_broken = {"z-broken"}
    for _ in range(3):
        status, body = proxy.get("/ha/devices")
        assert status == 200 and body["targets"] == {}
    assert len(_diagnostics_calls("z-broken")) == 1
    # A loaded entry that answers is still asked each time.
    STATE.entries.append(_entry("z-fine", "SMART Sniffer (nas-fine)"))
    STATE.diagnostics = {"z-fine": 9202}
    proxy.get("/ha/devices")
    proxy.get("/ha/devices")
    assert len(_diagnostics_calls("z-fine")) == 2
    assert len(_diagnostics_calls("z-broken")) == 1


# ── /ha/add ─────────────────────────────────────────────────────────────────

def _flow_posts() -> list[tuple[str, str, Any]]:
    return [c for c in STATE.calls
            if c[1].startswith("/core/api/config/config_entries/flow") and c[0] == "POST"]


def test_ha_add_self(proxy):
    STATE.flow_handlers.append("smart_sniffer")
    STATE.flow_step = {"type": "create_entry", "result": {"entry_id": "new-self"}}
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 200 and body == {"result": "created", "entry_id": "new-self"}
    assert _flow_posts() == [
        ("POST", "/core/api/config/config_entries/flow",
         {"handler": "smart_sniffer", "show_advanced_options": False}),
        ("POST", "/core/api/config/config_entries/flow/flow123",
         {"host": AGENT_HOST, "port": proxy.agent_port, "token": APP_TOKEN, "scan_interval": 60}),
    ]


def test_ha_add_replicas(proxy):
    STATE.flow_step = {"type": "create_entry", "result": {"entry_id": "new-lab"}}
    status, body = proxy.post("/ha/add", {"target": "replicas"})
    assert status == 200 and body["result"] == "created"
    assert _flow_posts()[1][2] == {"host": AGENT_HOST, "port": proxy.mock_port, "token": "",
                                   "scan_interval": 60}


def test_ha_add_field_host_with_password(proxy):
    _store({"address": "nas.lan", "port": 9099, "password": FIELD_PASSWORD})
    STATE.flow_step = {"type": "create_entry", "result": {"entry_id": "new-nas"}}
    status, body = proxy.post("/ha/add", {"target": "nas.lan:9099"})
    assert status == 200 and body == {"result": "created", "entry_id": "new-nas"}
    assert _flow_posts()[1][2] == {"host": "nas.lan", "port": 9099, "token": FIELD_PASSWORD,
                                   "scan_interval": 60}


def test_ha_add_already_added(proxy):
    STATE.entries = [_entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})")]
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 200 and body == {"result": "already_added", "entry_id": "e-agent"}
    assert _flow_posts() == []
    STATE.entries = []
    STATE.flow_step = {"type": "abort", "reason": "already_configured"}
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 200 and body["result"] == "already_added"


def test_ha_add_replicas_reloads(proxy):
    STATE.entries = [_entry("e-lab", f"SMART Sniffer ({AGENT_HOST}:{proxy.mock_port})")]
    status, body = proxy.post("/ha/add", {"target": "replicas"})
    assert status == 200 and body == {"result": "reloaded", "entry_id": "e-lab"}
    assert ("POST", "/core/api/config/config_entries/entry/e-lab/reload", None) in STATE.calls
    assert _flow_posts() == []


def test_ha_add_cannot_connect(proxy):
    STATE.flow_step = {"type": "form", "step_id": "user", "errors": {"base": "cannot_connect"}}
    status, body = proxy.post("/ha/add", {"target": "replicas"})
    assert status == 502
    assert body == {"error": "add_failed", "result": "error", "message": "cannot_connect"}
    # The half-finished flow is cleaned up.
    assert ("DELETE", "/core/api/config/config_entries/flow/flow123", None) in STATE.calls


def test_ha_add_errors(proxy):
    STATE.flow_step = {"type": "form", "step_id": "user", "errors": {"base": "invalid_auth"}}
    assert proxy.post("/ha/add", {"target": "self"})[1]["message"] == "invalid_auth"
    STATE.core_status = 401
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 502 and body["message"] == "unreachable"


def test_ha_add_integration_not_installed(proxy):
    # Core answers 404 for a config flow handler it does not know.
    STATE.handler_known = False
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 502 and body["message"] == "integration_not_loaded"


def test_ha_add_bad_target(proxy):
    for target in ("nas", "10.9.9.9:9099", None):
        status, body = proxy.post("/ha/add", {"target": target})
        assert status == 400 and body["error"] == "bad_target"


def test_ha_add_drops_the_cache(backends):
    proc, client = _start_proxy([
        f"--supervisor-url=http://127.0.0.1:{backends[0].server_address[1]}",
        f"--agent-host={AGENT_HOST}", "--agent-port=9099", "--data-dir=",
    ], TOKEN)
    try:
        assert client.get("/ha/devices")[1]["targets"] == {}
        STATE.entries = [_entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:9099)")]
        assert client.get("/ha/devices")[1]["targets"] == {}  # cached
        client.post("/ha/add", {"target": "self"})
        assert client.get("/ha/devices")[1]["targets"]["self"]["entry_id"] == "e-agent"
    finally:
        _stop_proxy(proc)


@pytest.mark.parametrize("method,path", [
    ("GET", "/ha/status"), ("POST", "/ha/connect"), ("POST", "/ha/disconnect"),
    ("GET", "/ha/verdict?serial=X"),
])
def test_removed_ha_routes_are_404(proxy, method, path):
    status, body = proxy.request(method, path, {} if method == "POST" else None)
    assert status == 404 and body["error"] == "not_found"


# ── /replicas ───────────────────────────────────────────────────────────────

def _raw(proxy, method: str, path: str, data: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(proxy.base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def _shipped(proxy, file_id: str) -> dict[str, Any]:
    status, doc = proxy.get(f"/mock/replicas/{file_id}")
    assert status == 200
    return doc


@needs_mock
def test_replicas_listing(proxy, mock_agent):
    status, body = proxy.get("/replicas")
    assert status == 200
    assert body["agent"] == "ok"
    assert body["folder"] == {"path": str(mock_agent["user"]), "status": "ok"}
    shipped = [f for f in body["files"] if f.get("source") == "shipped"]
    assert [f["order"] for f in shipped] == [1, 2, 3, 4, 5, 6]
    assert all(f["valid"] for f in shipped)
    assert {f["id"] for f in shipped} >= {"shipped.smart-failed", "shipped.zfs-pool-degraded"}


@needs_mock
def test_replicas_upload(proxy, mock_agent):
    doc = _shipped(proxy, "shipped.nvme-wear")
    doc["name"] = "Uploaded NVMe for the test"
    status, entry = proxy.post("/replicas", doc)
    assert status == 201
    assert entry["source"] == "user" and entry["valid"] is True
    assert entry["name"] == "Uploaded NVMe for the test"
    assert (mock_agent["user"] / f"{entry['id'][len('user.'):]}.json").is_file()
    status, body = proxy.get("/replicas")
    assert entry["id"] in {f["id"] for f in body["files"]}


@needs_mock
def test_replicas_upload_refused(proxy):
    status, body = proxy.post("/replicas", {"format": "something-else", "name": "x"})
    assert status == 422 and body["error"] == "invalid_replica" and body["message"]
    status, _, raw = _raw(proxy, "POST", "/replicas", b"{not json")
    assert status == 422 and json.loads(raw)["error"] == "invalid_replica"
    big = json.dumps({"format": "smart-sniffer-replica", "pad": "x" * 1_200_000}).encode()
    status, _, raw = _raw(proxy, "POST", "/replicas", big)
    assert status == 413 and json.loads(raw)["error"] == "too_large"


@needs_mock
def test_replicas_folder_unavailable(backends, tmp_path):
    port = _free_port()
    mock = subprocess.Popen(
        [sys.executable, str(MOCK_AGENT), f"--port={port}", "--host=127.0.0.1", "--no-mdns",
         "--replicas", str(MOCK_AGENT.parent / "replicas"),
         "--user-replicas", str(tmp_path / "missing")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _wait_for_port(port, mock)
    proc, client = _start_proxy([
        f"--mock-port={port}", "--data-dir=",
        f"--supervisor-url=http://127.0.0.1:{backends[0].server_address[1]}",
    ], TOKEN)
    try:
        assert client.get("/app/info")[1]["replicas"]["folder"] == "missing"
        doc = client.get("/mock/replicas/shipped.nvme-wear")[1]
        status, body = client.post("/replicas", doc)
        assert status == 503 and body["error"] == "replica_folder_unavailable"
    finally:
        _stop_proxy(proc)
        mock.terminate()
        mock.wait(timeout=5)


def test_replicas_agent_down(backends):
    proc, client = _start_proxy([
        f"--mock-port={_free_port()}", "--data-dir=",
        f"--supervisor-url=http://127.0.0.1:{backends[0].server_address[1]}",
    ], TOKEN)
    try:
        status, body = client.get("/replicas")
        assert status == 503 and body["error"] == "replica_agent_down"
        status, body = client.post("/replicas/add", {"files": ["shipped.nvme-wear"]})
        assert status == 503 and body["error"] == "replica_agent_down"
    finally:
        _stop_proxy(proc)


@needs_mock
@needs_rules
def test_replicas_add_read_export_delete(proxy):
    status, body = proxy.post("/replicas/add", {"files": [
        "shipped.smart-failed", "shipped.zfs-pool-degraded", "shipped.nope"]})
    assert status == 201
    cards = {c["file"]: c["card_id"] for c in body["cards"]}
    assert cards["shipped.zfs-pool-degraded"].startswith("pool.tank")
    assert body["failed"] == [{"file": "shipped.nope", "error": body["failed"][0]["error"]}]
    drive_card = cards["shipped.smart-failed"]
    pool_card = cards["shipped.zfs-pool-degraded"]

    # The replicas read carries each item's lab block, judged like any drive.
    status, read = proxy.get("/fleet/hosts/replicas/drives")
    assert status == 200 and read["kind"] == "replicas" and read["status"] == "ok"
    items = {i["id"]: i for i in read["items"]}
    drive = items[drive_card]
    assert drive["lab"]["replica"] == "shipped.smart-failed"
    assert drive["lab"]["kind"] == "ata"
    assert drive["lab"]["scenario"]["current"] == "smart_failed"
    assert {"id": "healthy", "label": "Healthy SATA HDD"} in drive["lab"]["scenario"]["options"]
    assert drive["verdict"]["state"] == "YES" and drive["view"]["health_failed"] is True
    assert "lab" not in drive["drive"]
    pool = items[pool_card]
    assert pool["type"] == "pool" and pool["view"]["layout"] == "two-disk mirror"
    assert pool["verdict"]["state"] == "YES"
    assert [d["mark"] for d in pool["view"]["disks"]] == [None, "crit"]

    # A Raw edit through /mock, then the export carries it.
    status, _ = proxy.request("PATCH", f"/mock/drives/{quote(drive_card, safe='')}/smart",
                              {"Reallocated_Sector_Ct": 99})
    assert status == 200
    status, headers, raw = _raw(proxy, "GET", f"/replicas/{quote(drive_card, safe='')}/file")
    assert status == 200
    assert headers["content-disposition"] == 'attachment; filename="smart-failed.json"'
    exported = json.loads(raw)
    table = {a["name"]: a for a in exported["smart"]["ata_smart_attributes"]["table"]}
    assert table["Reallocated_Sector_Ct"]["raw"]["value"] == 99
    # The download is a file the upload takes back.
    exported["name"] = "Round trip"
    assert proxy.post("/replicas", exported)[0] == 201
    status, _, raw = _raw(proxy, "GET", f"/replicas/{quote(pool_card, safe='')}/file")
    assert status == 200 and json.loads(raw)["kind"] == "zfs_pool"

    for card in (drive_card, pool_card):
        status, body = proxy.request("DELETE", f"/replicas/{quote(card, safe='')}")
        assert status == 200 and body == {"removed": card}
        assert proxy.request("DELETE", f"/replicas/{quote(card, safe='')}")[0] == 404
    assert proxy.get("/replicas/nope/file")[0] == 404


def _wwn_drive(drive_id: str, serial: str) -> dict[str, Any]:
    drive = simple_drive(drive_id, serial, {"Reallocated_Sector_Ct": 3})
    drive["smart_data"].update({
        "serial_number": serial, "model_name": drive["model"],
        "wwn": {"naa": 5, "oui": 3152, "id": 77777777},
    })
    return drive


def _no_trace(user: Path, entry_id: str, *secrets: str) -> dict[str, Any]:
    text = (user / f"{entry_id[len('user.'):]}.json").read_text(encoding="utf-8")
    for secret in secrets:
        assert secret not in text
    return json.loads(text)


@needs_mock
@needs_rules
def test_save_as_from_self(proxy, mock_agent):
    SELF_AGENT.drives = [_wwn_drive("sda", "SELF-SERIAL-77")]
    status, body = proxy.post("/replicas/save-as", {"host": "self", "drive_id": "sda"})
    assert status == 201
    entry = body["file"]
    assert entry["source"] == "user" and entry["kind"] == "ata"
    doc = _no_trace(mock_agent["user"], entry["id"], "SELF-SERIAL-77", "77777777")
    assert doc["origin"]["type"] == "saved"
    assert doc["verdict"]["state"] == "YES"
    assert doc["verdict"]["reasons"] == ["Reallocated Sector Count: 3 (expected 0)"]
    assert ("GET", "/api/drives/sda", f"Bearer {APP_TOKEN}") in SELF_AGENT.requests
    # The new file is on the page at once, as a replica card.
    _, read = proxy.get("/fleet/hosts/replicas/drives")
    assert body["card_id"] in {i["id"] for i in read["items"]}
    proxy.request("DELETE", f"/replicas/{quote(body['card_id'], safe='')}")


@needs_mock
@needs_rules
def test_save_as_from_field_host(proxy, field, mock_agent):
    FIELD["127.0.0.3"].drives = [_wwn_drive("sdb", "FIELD-SERIAL-55")]
    _store({"address": "127.0.0.3", "port": field, "password": FIELD_PASSWORD})
    _refresh_hosts(proxy)
    status, body = proxy.post("/replicas/save-as",
                              {"host": f"127.0.0.3:{field}", "drive_id": "sdb"})
    assert status == 201
    _no_trace(mock_agent["user"], body["file"]["id"], "FIELD-SERIAL-55", "77777777")
    assert ("GET", "/api/drives/sdb", f"Bearer {FIELD_PASSWORD}") in FIELD["127.0.0.3"].requests
    proxy.request("DELETE", f"/replicas/{quote(body['card_id'], safe='')}")


@needs_mock
@needs_rules
def test_save_as_refusals(proxy, field):
    SELF_AGENT.drives = [simple_drive("sdu", "USB-1", smart=False)]
    status, body = proxy.post("/replicas/save-as", {"host": "self", "drive_id": "sdu"})
    assert status == 409 and body["error"] == "unsupported_drive"
    assert proxy.post("/replicas/save-as", {"host": "self", "drive_id": "nope"})[0] == 404
    assert proxy.post("/replicas/save-as", {"host": "10.9.9.9:1", "drive_id": "sda"})[0] == 404
    assert proxy.post("/replicas/save-as", {"host": "self"})[0] == 400
    _store({"address": "127.0.0.6", "port": field})
    _refresh_hosts(proxy)
    status, body = proxy.post("/replicas/save-as", {"host": f"127.0.0.6:{field}", "drive_id": "sda"})
    assert status == 504 and body["error"] == "not_answering"


def test_reverse_name_ignores_an_address_answer(mod, monkeypatch):
    # musl answers gethostbyaddr with the address itself when there is no PTR
    monkeypatch.setattr(mod.socket, "gethostbyaddr", lambda a: (a, [a], [a]))
    assert mod.reverse_name("10.0.10.12") is None
    monkeypatch.setattr(mod.socket, "gethostbyaddr", lambda a: ("nas.lan", [], [a]))
    assert mod.reverse_name("10.0.10.12") == "nas"


def test_stats_from_the_ata_table_when_top_level_is_missing(mod):
    attr = lambda i, raw: {"id": i, "raw": {"value": 0, "string": raw}}  # noqa: E731
    smart = {"ata_smart_attributes": {"table": [
        attr(194, "36 (Min/Max 20/52)"), attr(9, "18240"), attr(12, "212")]}}
    assert mod._stats(smart) == {"temp_c": 36, "power_on_hours": 18240, "power_cycles": 212}


# ── Cards: ids, filesystems, order, hide, remove, download ──────────────────

def _fs(fs_id: str, mount: str, device: str, fstype: str = "ext4", pct: float = 41.0) -> dict[str, Any]:
    """One filesystem as the agent's /api/filesystems lists it."""
    total = 1_000_000_000_000
    used = int(total * pct / 100)
    return {"id": fs_id, "uuid": "", "mountpoint": mount, "device": device, "fstype": fstype,
            "total_bytes": total, "used_bytes": used, "available_bytes": total - used,
            "use_percent": pct, "status": "ok"}


def test_attach_filesystems(mod):
    items = [
        {"type": "drive", "id": "sda", "drive": {"device_path": "/dev/sda"}},
        {"type": "drive", "id": "nvme0", "drive": {"device_path": "/dev/nvme0n1"}},
        {"type": "drive", "id": "nvme1", "drive": {"device_path": "/dev/nvme1"}},
        {"type": "pool", "id": "pool.tank", "pool": {"name": "tank"}},
    ]
    found = [
        _fs("fs-1", "/", "/dev/sda1"),
        _fs("fs-2", "/boot", "/dev/nvme0n1p2", "vfat"),
        _fs("fs-3", "/srv", "/dev/nvme1n1p1"),
        _fs("fs-4", "/tank", "tank", "zfs"),
        _fs("fs-5", "/tank/media", "tank/media", "zfs"),
        _fs("fs-6", "/data", "/dev/mapper/vg-data", "xfs"),
        _fs("fs-7", "/mnt/x", "/dev/sdaa1"),
        _fs("fs-8", "/other", "other/set", "zfs"),
        _fs("fs-9", "/whole", "/dev/sda"),
    ]
    left = mod.attach_filesystems("10.0.0.9:9099", items, found)
    on = {i["id"]: [f["id"] for f in i["filesystems"]] for i in items}
    assert on == {"sda": ["fs-1", "fs-9"], "nvme0": ["fs-2"], "nvme1": ["fs-3"],
                  "pool.tank": ["fs-4", "fs-5"]}
    assert [c["id"] for c in left] == ["fs-6", "fs-7", "fs-8"]
    assert left[0] == {"type": "filesystem", "id": "fs-6", "card_id": "10.0.0.9:9099|fs:fs-6",
                       "filesystem": found[5]}


def test_card_ids(mod):
    assert mod.drive_card_key({"serial": " S5GX "}, "nvme0") == "S5GX"
    assert mod.drive_card_key({"serial": ""}, "sdb") == "id:sdb"
    assert mod.card_id("10.0.10.20:9099", "pool:tank") == "10.0.10.20:9099|pool:tank"
    assert mod.split_card_id("10.0.10.20:9099|pool:tank") == ("10.0.10.20:9099", "pool:tank")
    assert mod.split_card_id("10.0.10.20:9099") == ("10.0.10.20:9099", None)


@needs_rules
def test_drives_card_ids_and_filesystems(proxy, field):
    key = f"127.0.0.2:{field}"
    FIELD["127.0.0.2"].drives[1]["serial"] = ""
    FIELD["127.0.0.2"].pools = [{"name": "tank", "state": "ONLINE", "problem_vdevs": []}]
    FIELD["127.0.0.2"].filesystems = [
        _fs("fs-a", "/", "/dev/sda1", pct=82.0), _fs("fs-t", "/tank/media", "tank/media", "zfs"),
        _fs("fs-l", "/data", "/dev/md0", "xfs")]
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    status, body = proxy.get(f"/fleet/hosts/{key}/drives")
    assert status == 200 and body["filesystems_status"] == "ok"
    items = {i["id"]: i for i in body["items"]}
    assert items["sda"]["card_id"] == f"{key}|NUC-1"
    assert items["sdb"]["card_id"] == f"{key}|id:sdb"
    assert items["pool.tank"]["card_id"] == f"{key}|pool:tank"
    assert [f["mountpoint"] for f in items["sda"]["filesystems"]] == ["/"]
    assert items["sdb"]["filesystems"] == []
    assert [f["id"] for f in items["pool.tank"]["filesystems"]] == ["fs-t"]
    assert [(c["type"], c["card_id"]) for c in body["filesystem_cards"]] == [
        ("filesystem", f"{key}|fs:fs-l")]
    # The fields the page already reads are unchanged.
    assert {"type", "id", "drive", "verdict", "view", "lab"} <= set(items["sda"])
    # Without the endpoint in the health answer, nothing is asked.
    FIELD["127.0.0.2"].filesystems = None
    FIELD["127.0.0.2"].requests.clear()
    _, body = proxy.get(f"/fleet/hosts/{key}/drives")
    assert body["filesystems_status"] == "none" and body["filesystem_cards"] == []
    assert not any(p == "/api/filesystems" for _, p, _ in FIELD["127.0.0.2"].requests)
    status, body = proxy.get("/fleet/hosts/self/drives")
    assert body["items"][0]["card_id"] == "self|SER-A"


def test_cards_order(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    proxy.get(f"/fleet/hosts/{key}/drives")
    proxy.get("/fleet/hosts/self/drives")
    STATE.calls.clear()
    order = [f"{key}|NUC-2", "self|SER-A", "nope|x", f"{key}|NUC-2", f"{key}|NUC-1", 7, key]
    status, body = proxy.request("PUT", "/cards/order", {"order": order})
    assert status == 200
    assert body["order"] == [f"{key}|NUC-2", "self|SER-A", f"{key}|NUC-1", key]
    assert len(_option_writes()) == 1
    assert STATE.options["card_order"] == body["order"]
    assert STATE.options["hosts"] == [{"address": "127.0.0.2", "port": field}]
    assert proxy.get("/cards") == (200, {"order": body["order"], "hidden": [],
                                         "names": {}, "host_names": {}})
    # A later order keeps the stored cards the page did not send, after it.
    status, body = proxy.request("PUT", "/cards/order", {"order": ["self|SER-A"]})
    assert body["order"] == ["self|SER-A", f"{key}|NUC-2", f"{key}|NUC-1", key]
    assert proxy.request("PUT", "/cards/order", {"order": "x"})[0] == 400


def test_cards_without_order_options(proxy):
    # Options written by 0.3.0 before cards have neither key.
    assert proxy.get("/cards") == (200, {"order": [], "hidden": [], "names": {}, "host_names": {}})


def test_card_hide_and_show(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    STATE.calls.clear()
    cid = quote(f"{key}|NUC-1", safe="")
    status, body = proxy.request("PATCH", f"/cards/{cid}", {"hidden": True})
    assert status == 200 and body["hidden"] is True
    assert STATE.options["hidden_cards"] == [f"{key}|NUC-1"]
    assert proxy.request("PATCH", "/cards/self%7CSER-A", {"hidden": True})[0] == 200
    assert STATE.options["hidden_cards"] == [f"{key}|NUC-1", "self|SER-A"]
    assert len(_option_writes()) == 2
    # Hiding what is hidden writes nothing.
    assert proxy.request("PATCH", f"/cards/{cid}", {"hidden": True})[0] == 200
    assert len(_option_writes()) == 2
    status, body = proxy.request("PATCH", f"/cards/{cid}", {"hidden": False})
    assert body["hidden_cards"] == ["self|SER-A"] and len(_option_writes()) == 3
    assert proxy.get("/cards")[1]["hidden"] == ["self|SER-A"]
    assert proxy.request("PATCH", "/cards/10.9.9.9%3A1%7Csda", {"hidden": True})[0] == 404
    assert proxy.request("PATCH", f"/cards/{cid}", {"hidden": "yes"})[0] == 400


@needs_rules
def test_hidden_machine_becomes_hidden_cards(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field, "hidden": True})
    _refresh_hosts(proxy)
    STATE.calls.clear()
    assert proxy.get(f"/fleet/hosts/{key}/drives")[1]["status"] == "ok"
    assert len(_option_writes()) == 1
    assert STATE.options["hosts"] == [{"address": "127.0.0.2", "port": field}]
    assert STATE.options["hidden_cards"] == [f"{key}|NUC-1", f"{key}|NUC-2"]
    proxy.get(f"/fleet/hosts/{key}/drives")
    assert len(_option_writes()) == 1  # once


@needs_rules
def test_card_remove_field_drives_then_machine(proxy, field, data_dir):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    STATE.options["card_order"] = [f"{key}|NUC-1", "self|SER-A", f"{key}|NUC-2"]
    STATE.options["hidden_cards"] = [f"{key}|NUC-2"]
    _refresh_hosts(proxy)
    proxy.get(f"/fleet/hosts/{key}/drives")
    STATE.calls.clear()
    status, body = proxy.request("DELETE", f"/cards/{quote(f'{key}|NUC-1', safe='')}")
    assert status == 200 and body == {"removed": f"{key}|NUC-1", "host_removed": False}
    assert len(_option_writes()) == 1
    assert STATE.options["hosts"] == [{"address": "127.0.0.2", "port": field, "removed": ["NUC-1"]}]
    assert STATE.options["card_order"] == ["self|SER-A", f"{key}|NUC-2"]
    # The removed drive stays off the page on the next read.
    _, read = proxy.get(f"/fleet/hosts/{key}/drives")
    assert [i["id"] for i in read["items"]] == ["sdb"] and read["summary"]["drives"] == 1
    # Its last card takes the machine with it: no longer stored or contacted.
    status, body = proxy.request("DELETE", f"/cards/{quote(f'{key}|NUC-2', safe='')}")
    assert body == {"removed": f"{key}|NUC-2", "host_removed": True}
    assert STATE.options["hosts"] == [] and STATE.options["hidden_cards"] == []
    assert STATE.options["card_order"] == ["self|SER-A"]
    cache = json.loads((data_dir / "fleet-cache.json").read_text(encoding="utf-8"))
    assert key not in cache["hosts"]
    assert proxy.get(f"/fleet/hosts/{key}/drives")[0] == 404
    # Adding the machine again brings every drive back.
    proxy.post("/fleet/hosts", {"hosts": [{"address": "127.0.0.2", "port": field}]})
    _, read = proxy.get(f"/fleet/hosts/{key}/drives")
    assert [i["id"] for i in read["items"]] == ["sda", "sdb"]


def test_card_remove_placeholder_and_refusals(proxy, field):
    key = f"127.0.0.3:{field}"
    _store({"address": "127.0.0.3", "port": field})
    _refresh_hosts(proxy)
    assert proxy.get(f"/fleet/hosts/{key}/drives")[1]["status"] == "password_needed"
    status, body = proxy.request("DELETE", f"/cards/{quote(key, safe='')}")
    assert status == 200 and body["host_removed"] is True and STATE.options["hosts"] == []
    status, body = proxy.request("DELETE", "/cards/self%7CSER-A")
    assert status == 409 and body["error"] == "cannot_remove"
    assert proxy.request("DELETE", "/cards/10.9.9.9%3A1%7Csda")[0] == 404
    assert proxy.request("DELETE", "/cards/self")[0] == 404


def _probe_added(proxy, field, address: str = "127.0.0.2") -> bool:
    status, row = proxy.post("/fleet/probe", {"host": address, "port": field})
    assert status == 200, row
    return row["added"]


@needs_rules
def test_card_remove_counts_filesystem_cards(proxy, field, data_dir):
    # A drive and a FILESYSTEM card: the drive going leaves the machine
    # stored, as the filesystem is still on the page; hidden, it still counts.
    key = f"127.0.0.2:{field}"
    FIELD["127.0.0.2"].drives = [simple_drive("sda", "NUC-1")]
    FIELD["127.0.0.2"].filesystems = [_fs("fs-l", "/data", "/dev/md0", "xfs")]
    _store({"address": "127.0.0.2", "port": field})
    STATE.options["hidden_cards"] = [f"{key}|fs:fs-l"]
    _refresh_hosts(proxy)
    _, read = proxy.get(f"/fleet/hosts/{key}/drives")
    assert [c["card_id"] for c in read["filesystem_cards"]] == [f"{key}|fs:fs-l"]
    status, body = proxy.request("DELETE", f"/cards/{quote(f'{key}|NUC-1', safe='')}")
    assert status == 200 and body == {"removed": f"{key}|NUC-1", "host_removed": False}
    assert STATE.options["hosts"] == [{"address": "127.0.0.2", "port": field, "removed": ["NUC-1"]}]
    _, read = proxy.get(f"/fleet/hosts/{key}/drives")
    assert read["items"] == [] and [c["card_id"] for c in read["filesystem_cards"]] == [f"{key}|fs:fs-l"]
    _refresh_hosts(proxy)
    assert _probe_added(proxy, field) is True
    # The FILESYSTEM card was the last one: the machine goes, and a check
    # finds it not added.
    status, body = proxy.request("DELETE", f"/cards/{quote(f'{key}|fs:fs-l', safe='')}")
    assert body == {"removed": f"{key}|fs:fs-l", "host_removed": True}
    assert STATE.options["hosts"] == [] and STATE.options["hidden_cards"] == []
    cache = json.loads((data_dir / "fleet-cache.json").read_text(encoding="utf-8"))
    assert key not in cache["hosts"]
    _refresh_hosts(proxy)
    assert _probe_added(proxy, field) is False


@needs_rules
def test_card_remove_the_only_card(proxy, field):
    key = f"127.0.0.2:{field}"
    FIELD["127.0.0.2"].drives = [simple_drive("sda", "NUC-1")]
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    proxy.get(f"/fleet/hosts/{key}/drives")
    status, body = proxy.request("DELETE", f"/cards/{quote(f'{key}|NUC-1', safe='')}")
    assert status == 200 and body == {"removed": f"{key}|NUC-1", "host_removed": True}
    assert STATE.options["hosts"] == []
    _refresh_hosts(proxy)
    assert _probe_added(proxy, field) is False


@needs_rules
def test_card_remove_reads_again_when_the_last_read_lacks_the_card(proxy, field):
    # The machine changed since its last read: the card is not in it, so the
    # machine is read before its cards are counted.
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    proxy.get(f"/fleet/hosts/{key}/drives")
    FIELD["127.0.0.2"].drives = [simple_drive("sdc", "NUC-3")]
    FIELD["127.0.0.2"].filesystems = [_fs("fs-l", "/data", "/dev/md0", "xfs")]
    status, body = proxy.request("DELETE", f"/cards/{quote(f'{key}|NUC-3', safe='')}")
    assert status == 200 and body["host_removed"] is False
    assert STATE.options["hosts"] == [{"address": "127.0.0.2", "port": field, "removed": ["NUC-3"]}]
    status, body = proxy.request("DELETE", f"/cards/{quote(f'{key}|fs:fs-l', safe='')}")
    assert status == 200 and body["host_removed"] is True
    assert STATE.options["hosts"] == []


@needs_rules
def test_scan_and_check_count_only_machines_with_cards(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field, "removed": ["NUC-1"]},
           {"address": "127.0.0.3", "port": field, "password": FIELD_PASSWORD})
    _refresh_hosts(proxy)
    proxy.get(f"/fleet/hosts/{key}/drives")
    # One drive left: added, by hand and in a scan.
    assert _probe_added(proxy, field) is True
    status, started = proxy.post("/fleet/scan", {"block": "127.0.0.0/29", "port": field})
    rows = {r["address"]: r for r in _wait_scan(proxy, started["scan_id"])["found"]}
    assert rows["127.0.0.2"]["added"] is True and rows["127.0.0.3"]["added"] is True
    # Every card removed: not added, so it can be ticked again.
    STATE.options["hosts"][0]["removed"] = ["NUC-1", "NUC-2"]
    _refresh_hosts(proxy)
    assert _probe_added(proxy, field) is False
    status, started = proxy.post("/fleet/scan", {"block": "127.0.0.0/29", "port": field})
    rows = {r["address"]: r for r in _wait_scan(proxy, started["scan_id"])["found"]}
    assert rows["127.0.0.2"]["added"] is False and rows["127.0.0.3"]["added"] is True
    # A machine never read shows its own card: added.
    assert _probe_added(proxy, field, "127.0.0.3") is True


@needs_rules
def test_adding_a_stored_machine_brings_its_cards_back(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field, "removed": ["NUC-1"]})
    _refresh_hosts(proxy)
    _, read = proxy.get(f"/fleet/hosts/{key}/drives")
    assert [i["id"] for i in read["items"]] == ["sdb"]
    STATE.calls.clear()
    status, body = proxy.post("/fleet/hosts", {"hosts": [{"address": "127.0.0.2", "port": field}]})
    assert status == 201 and body["skipped"] == []
    assert [h["id"] for h in body["added"]] == [key]
    assert len(_option_writes()) == 1
    assert STATE.options["hosts"] == [{"address": "127.0.0.2", "port": field}]
    _, read = proxy.get(f"/fleet/hosts/{key}/drives")
    assert [i["id"] for i in read["items"]] == ["sda", "sdb"]
    # Stored with nothing removed: skipped as before, no write.
    status, body = proxy.post("/fleet/hosts", {"hosts": [{"address": "127.0.0.2", "port": field}]})
    assert body["added"] == [] and body["skipped"] == [
        {"address": "127.0.0.2", "port": field, "reason": "already_added"}]
    assert len(_option_writes()) == 1


@needs_rules
def test_a_machine_with_every_card_removed_is_dropped_on_read(proxy, field, data_dir):
    # Left by an older version: stored, every card of its last read removed.
    key = f"127.0.0.2:{field}"
    FIELD["127.0.0.2"].drives = [simple_drive("sda", "NUC-1")]
    FIELD["127.0.0.2"].filesystems = [_fs("fs-l", "/data", "/dev/md0", "xfs")]
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    proxy.get(f"/fleet/hosts/{key}/drives")
    _store({"address": "127.0.0.2", "port": field, "removed": ["NUC-1", "fs:fs-l"]},
           {"address": "127.0.0.3", "port": field, "password": FIELD_PASSWORD})
    STATE.options["card_order"] = [f"{key}|NUC-1", "self|SER-A", f"{key}|fs:fs-l"]
    STATE.options["hidden_cards"] = [f"{key}|fs:fs-l"]
    STATE.calls.clear()
    hosts = _refresh_hosts(proxy)
    assert [h["id"] for h in hosts] == [f"127.0.0.3:{field}"]
    assert len(_option_writes()) == 1
    assert STATE.options["hosts"] == [{"address": "127.0.0.3", "port": field,
                                       "password": FIELD_PASSWORD}]
    assert STATE.options["card_order"] == ["self|SER-A"] and STATE.options["hidden_cards"] == []
    cache = json.loads((data_dir / "fleet-cache.json").read_text(encoding="utf-8"))
    assert key not in cache["hosts"]
    _refresh_hosts(proxy)
    assert proxy.get("/cards")[0] == 200
    assert len(_option_writes()) == 1  # once
    assert _probe_added(proxy, field) is False
    # One FILESYSTEM card not removed keeps it.
    proxy.post("/fleet/hosts", {"hosts": [{"address": "127.0.0.2", "port": field}]})
    proxy.get(f"/fleet/hosts/{key}/drives")
    STATE.options["hosts"][-1]["removed"] = ["NUC-1"]
    assert key in [h["id"] for h in _refresh_hosts(proxy)]


@needs_mock
@needs_rules
def test_card_replica_download_and_remove(proxy):
    status, body = proxy.post("/replicas/add", {"files": ["shipped.smart-failed", "shipped.zfs-pool-degraded"]})
    assert status == 201
    _, read = proxy.get("/fleet/hosts/replicas/drives")
    by_mock = {i["id"]: i["card_id"] for i in read["items"]}
    drive_card = by_mock[body["cards"][0]["card_id"]]
    pool_card = by_mock[body["cards"][1]["card_id"]]
    assert drive_card.startswith("replicas|") and pool_card.startswith("replicas|pool:tank")
    status, headers, raw = _raw(proxy, "GET", f"/cards/{quote(drive_card, safe='')}/file")
    assert status == 200 and headers["content-disposition"].startswith("attachment;")
    assert json.loads(raw)["format"] == "smart-sniffer-replica"
    STATE.options["card_order"] = [pool_card]
    for card in (drive_card, pool_card):
        status, body = proxy.request("DELETE", f"/cards/{quote(card, safe='')}")
        assert status == 200 and body == {"removed": card, "host_removed": False}
    assert STATE.options["card_order"] == []
    _, read = proxy.get("/fleet/hosts/replicas/drives")
    assert {drive_card, pool_card}.isdisjoint({i["card_id"] for i in read["items"]})


@needs_mock
@needs_rules
def test_card_download_real_drive_is_anonymised(proxy, mock_agent):
    SELF_AGENT.drives = [_wwn_drive("sda", "SELF-SERIAL-88")]
    proxy.get("/fleet/hosts/self/drives")
    status, headers, raw = _raw(proxy, "GET", "/cards/self%7CSELF-SERIAL-88/file")
    assert status == 200 and headers["content-disposition"].startswith("attachment;")
    text = raw.decode()
    assert "SELF-SERIAL-88" not in text and "77777777" not in text
    doc = json.loads(text)
    assert doc["format"] == "smart-sniffer-replica" and doc["origin"]["type"] == "saved"
    assert proxy.get("/cards/self%7CNOPE/file")[0] == 404


@needs_mock
@needs_rules
def test_replica_filesystems_from_the_mock(proxy):
    """The replica agent serves /api/filesystems from its files; each lands
    on its drive or pool card, and none is left over."""
    status, added = proxy.post("/replicas/add", {"files": ["shipped.smart-failed", "shipped.zfs-pool-degraded"]})
    assert status == 201
    _, read = proxy.get("/fleet/hosts/replicas/drives")
    if read.get("filesystems_status") == "none":
        pytest.skip("this replica agent does not serve /api/filesystems")
    assert read["filesystems_status"] == "ok"
    items = {i["id"]: i for i in read["items"]}
    drive = items[added["cards"][0]["card_id"]]
    pool = items[added["cards"][1]["card_id"]]
    assert drive["filesystems"] and all(
        f["device"].startswith(drive["drive"]["device_path"]) for f in drive["filesystems"])
    assert pool["filesystems"] and all(f["fstype"] == "zfs" for f in pool["filesystems"])
    assert {f["device"].split("/")[0] for f in pool["filesystems"]} == {pool["pool"]["name"]}
    assert read["filesystem_cards"] == []
    for card in added["cards"]:
        proxy.request("DELETE", f"/replicas/{quote(card['card_id'], safe='')}")


# ── Thresholds ──────────────────────────────────────────────────────────────

def _thr_drive(serial: str, **attrs: int) -> dict[str, Any]:
    names = {"realloc": "Reallocated_Sector_Ct", "pending": "Current_Pending_Sector",
             "timeout": "Command_Timeout"}
    return simple_drive("sda", serial, {names[k]: v for k, v in attrs.items()})


def _in_ha(proxy, entry_id: str, serial: str, overrides: dict[str, int] | None = None) -> str:
    """This app's agent in Home Assistant as entry_id, the drive's device
    in it, the integration storing these overrides. Returns the card id."""
    STATE.entries = [_entry(entry_id, f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})")]
    STATE.devices = [_device(f"dev-{serial}", entry_id, serial=serial)]
    STATE.int_labels = {entry_id: {"sda": list(ATA_LABELS)}}
    STATE.int_thresholds = {entry_id: {"sda": dict(overrides or {})}}
    return f"self|{serial}"


def _options_flow_calls() -> list[tuple[str, str, Any]]:
    return [c for c in STATE.calls if c[1].startswith("/core/api/config/config_entries/options/flow")]


def _labels(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {lim["label"]: lim for lim in body["labels"]}


def _app_thresholds() -> list[dict[str, Any]]:
    return STATE.options.get("thresholds") or []


@needs_rules
def test_thresholds_app_source_put_and_clear(proxy):
    SELF_AGENT.drives = [_thr_drive("THR-APP", realloc=3)]
    cid = "self|THR-APP"
    status, body = proxy.get(f"/cards/{quote(cid, safe='')}/thresholds")
    assert status == 200 and body["source"] == "app"
    labels = _labels(body)
    # Only the labels the drive's protocol has: the ATA ones, no NVMe spare.
    assert "NVMe Spare Warn Below" not in labels and "Reallocated Sector Count" in labels
    realloc = labels["Reallocated Sector Count"]
    assert {k: realloc[k] for k in ("slug", "reading", "default", "value", "overridden")} == {
        "slug": "reallocated_sector_count", "reading": 3, "default": 0, "value": 0,
        "overridden": False}
    assert realloc["flagged"] is True and labels["Command Timeout"]["default"] == 100
    assert _options_flow_calls() == []  # not in Home Assistant: nothing asked there

    status, body = proxy.request("PUT", f"/cards/{quote(cid, safe='')}/thresholds",
                                 {"values": {"Reallocated Sector Count": 5, "Command Timeout": 100}})
    assert status == 200 and body["source"] == "app"
    assert _labels(body)["Reallocated Sector Count"]["value"] == 5
    assert _labels(body)["Reallocated Sector Count"]["accepted"] is True
    # The default for Command Timeout is not stored.
    assert _app_thresholds() == [{"card": cid, "label": "Reallocated Sector Count", "value": 5}]
    assert STATE.options["hosts"] == []  # every other option as it was

    # The card is judged with the override: accepted, not flagged.
    _, drives = proxy.get("/fleet/hosts/self/drives")
    item = drives["items"][0]
    assert item["thresholds_source"] == "app" and item["verdict"]["state"] == "NO"
    row = {r["key"]: r for r in item["view"]["rows"]}["reallocated"]
    assert row["mark"] is None and row["accepted"] is True
    assert row["limit"]["text"] == "alert above 5, default 0"

    # A value equal to the default clears the override, and the option empties.
    status, body = proxy.request("PUT", f"/cards/{quote(cid, safe='')}/thresholds",
                                 {"values": {"Reallocated Sector Count": 0}})
    assert status == 200 and _labels(body)["Reallocated Sector Count"]["overridden"] is False
    assert _app_thresholds() == []
    _, drives = proxy.get("/fleet/hosts/self/drives")
    assert drives["items"][0]["verdict"]["state"] == "YES"


@needs_rules
def test_thresholds_put_refusals(proxy):
    SELF_AGENT.drives = [_thr_drive("THR-BAD")]
    path = f"/cards/{quote('self|THR-BAD', safe='')}/thresholds"
    for body in ({}, {"values": {"Reallocated Sector Count": -1}},
                 {"values": {"Reallocated Sector Count": 1.5}},
                 {"values": {"Reallocated Sector Count": True}},
                 {"values": {"NVMe Spare Warn Below": 10}}):
        status, answer = proxy.request("PUT", path, body)
        assert status == 400 and answer["error"] == "bad_request", body
    assert _option_writes() == []


@needs_rules
def test_thresholds_integration_source_from_diagnostics(proxy):
    SELF_AGENT.drives = [_thr_drive("THR-HA", realloc=24)]
    cid = _in_ha(proxy, "e-thr-diag", "THR-HA", {"Reallocated Sector Count": 30})
    # The app has an override of its own for the card: Home Assistant wins.
    STATE.options["thresholds"] = [{"card": cid, "label": "Reallocated Sector Count", "value": 2}]
    status, body = proxy.get(f"/cards/{quote(cid, safe='')}/thresholds")
    assert status == 200 and body["source"] == "integration"
    realloc = _labels(body)["Reallocated Sector Count"]
    assert realloc["value"] == 30 and realloc["overridden"] and realloc["accepted"]
    # Read from the diagnostics; no options flow was started.
    assert ("GET", "/core/api/diagnostics/config_entry/e-thr-diag", None) in STATE.calls
    assert _options_flow_calls() == []
    # The card's verdict uses the integration's 30: the same as Home Assistant.
    _, drives = proxy.get("/fleet/hosts/self/drives")
    item = drives["items"][0]
    assert item["thresholds_source"] == "integration"
    assert item["verdict"]["state"] == "NO"
    assert item["verdict"]["accepted"] == ["Reallocated Sector Count: 24 (accepted 30)"]


@needs_rules
def test_thresholds_integration_read_through_the_flow(proxy):
    """Diagnostics without thresholds: the form's prefill, the flow aborted."""
    SELF_AGENT.drives = [_thr_drive("THR-FLOW", realloc=24)]
    cid = _in_ha(proxy, "e-thr-flow", "THR-FLOW", {"Reallocated Sector Count": 30})
    STATE.diag_thresholds = False
    status, body = proxy.get(f"/cards/{quote(cid, safe='')}/thresholds")
    assert status == 200 and body["source"] == "integration"
    assert _labels(body)["Reallocated Sector Count"]["value"] == 30
    calls = [(m, p.rsplit("/", 1)[-1], b) for m, p, b in _options_flow_calls()]
    assert calls == [
        ("POST", "flow", {"handler": "e-thr-flow", "show_advanced_options": False}),
        ("POST", "opt1", {"next_step_id": "thresholds"}),
        ("DELETE", "opt1", None),
    ]
    assert STATE.int_thresholds["e-thr-flow"]["sda"] == {"Reallocated Sector Count": 30}


@needs_rules
def test_thresholds_integration_write_through_the_flow(proxy):
    SELF_AGENT.drives = [_thr_drive("THR-W", realloc=24, timeout=150)]
    cid = _in_ha(proxy, "e-thr-write", "THR-W", {"Command Timeout": 200})
    # Two drives in the entry: the flow asks which one.
    STATE.int_labels["e-thr-write"]["sdb"] = list(ATA_LABELS)
    status, body = proxy.request("PUT", f"/cards/{quote(cid, safe='')}/thresholds",
                                 {"values": {"Reallocated Sector Count": 30}})
    assert status == 200 and body["source"] == "integration"
    posts = [(p.rsplit("/", 1)[-1], b) for m, p, b in _options_flow_calls() if m == "POST"]
    assert posts[:3] == [
        ("flow", {"handler": "e-thr-write", "show_advanced_options": False}),
        ("opt1", {"next_step_id": "thresholds"}),
        ("opt1", {"drive_id": "sda"}),
    ]
    # Every label of the form: the overrides, and the default for the rest.
    submitted = posts[3][1]
    assert set(submitted) == set(ATA_LABELS)
    assert submitted["Reallocated Sector Count"] == 30 and submitted["Command Timeout"] == 200
    assert submitted["Current Pending Sector Count"] == 0 and submitted["SSD Wear Percent Used"] == 90
    assert STATE.int_thresholds["e-thr-write"]["sda"] == {
        "Reallocated Sector Count": 30, "Command Timeout": 200}
    assert _app_thresholds() == []  # nothing in the app options
    labels = _labels(body)
    assert labels["Reallocated Sector Count"]["value"] == 30 and labels["Command Timeout"]["value"] == 200


@needs_rules
def test_thresholds_integration_errors_are_502(proxy):
    SELF_AGENT.drives = [_thr_drive("THR-ERR", realloc=2)]
    cid = _in_ha(proxy, "e-thr-err", "THR-ERR")
    STATE.options_flow_abort = "no_drives"
    status, body = proxy.request("PUT", f"/cards/{quote(cid, safe='')}/thresholds",
                                 {"values": {"Reallocated Sector Count": 3}})
    assert status == 502 and body == {"error": "integration_failed", "message": "no_drives"}
    assert STATE.int_thresholds["e-thr-err"]["sda"] == {}
    assert _app_thresholds() == []


@needs_rules
def test_thresholds_accept_and_reset(proxy):
    SELF_AGENT.drives = [_thr_drive("THR-ACC", realloc=24, pending=3, timeout=150)]
    cid = "self|THR-ACC"
    path = f"/cards/{quote(cid, safe='')}/thresholds"
    status, body = proxy.post(path + "/accept")
    assert status == 200 and body["source"] == "app"
    labels = _labels(body)
    # Each flagged label takes its current reading.
    assert labels["Reallocated Sector Count"]["value"] == 24
    assert labels["Current Pending Sector Count"]["value"] == 3
    assert labels["Command Timeout"]["value"] == 150
    assert not any(lim["flagged"] for lim in body["labels"])
    assert {(t["label"], t["value"]) for t in _app_thresholds()} == {
        ("Reallocated Sector Count", 24), ("Current Pending Sector Count", 3), ("Command Timeout", 150)}
    _, drives = proxy.get("/fleet/hosts/self/drives")
    assert drives["items"][0]["verdict"]["state"] == "NO"
    # Nothing flagged: nothing to accept.
    status, body = proxy.post(path + "/accept")
    assert status == 409 and body["error"] == "nothing_flagged"
    status, body = proxy.post(path + "/reset")
    assert status == 200 and not any(lim["overridden"] for lim in body["labels"])
    assert _app_thresholds() == []
    _, drives = proxy.get("/fleet/hosts/self/drives")
    assert drives["items"][0]["verdict"]["state"] == "YES"


@needs_rules
def test_thresholds_accept_and_reset_in_home_assistant(proxy):
    SELF_AGENT.drives = [_thr_drive("THR-ACC-HA", realloc=24, pending=3)]
    cid = _in_ha(proxy, "e-thr-acc", "THR-ACC-HA", {"Spin Retry Count": 4})
    path = f"/cards/{quote(cid, safe='')}/thresholds"
    status, body = proxy.post(path + "/accept")
    assert status == 200 and body["source"] == "integration"
    assert STATE.int_thresholds["e-thr-acc"]["sda"] == {
        "Reallocated Sector Count": 24, "Current Pending Sector Count": 3, "Spin Retry Count": 4}
    status, body = proxy.post(path + "/reset")
    assert status == 200
    assert STATE.int_thresholds["e-thr-acc"]["sda"] == {}
    assert _app_thresholds() == []


@needs_rules
def test_thresholds_only_for_drive_cards(proxy, field):
    key = f"127.0.0.2:{field}"
    pools = json.loads((FIXTURES / "pools" / "degraded-faulted.json").read_text(encoding="utf-8"))
    FIELD["127.0.0.2"].pools = pools[:1]
    FIELD["127.0.0.2"].filesystems = [_fs("fs-x", "/srv", "/dev/zz9", "ext4")]
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    _, body = proxy.get(f"/fleet/hosts/{key}/drives")
    pool_id = next(i["card_id"] for i in body["items"] if i["type"] == "pool")
    fs_id = body["filesystem_cards"][0]["card_id"]
    for cid in (pool_id, fs_id, key, "self|NOPE", "10.9.9.9:9099|X", "self"):
        status, answer = proxy.get(f"/cards/{quote(cid, safe='')}/thresholds")
        assert status == 404, cid
    # A field drive has them, kept in the app.
    status, answer = proxy.get(f"/cards/{quote(key + '|NUC-1', safe='')}/thresholds")
    assert status == 200 and answer["source"] == "app"


@needs_rules
def test_thresholds_copied_to_home_assistant_on_add(proxy):
    SELF_AGENT.drives = [_thr_drive("THR-COPY", realloc=24)]
    cid = "self|THR-COPY"
    STATE.options["thresholds"] = [
        {"card": cid, "label": "Reallocated Sector Count", "value": 30},
        {"card": "replicas|OTHER", "label": "Spin Retry Count", "value": 2},
    ]
    _refresh_hosts(proxy)
    proxy.get("/fleet/hosts/self/drives")  # the card as the page has it
    STATE.flow_step = {"type": "create_entry", "result": {"entry_id": "e-thr-new"}}
    STATE.int_labels = {"e-thr-new": {"sda": list(ATA_LABELS)}}
    STATE.on_create = (_entry("e-thr-new", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})"),
                       [_device("dev-copy", "e-thr-new", serial="THR-COPY")])
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 200 and body == {"result": "created", "entry_id": "e-thr-new",
                                      "thresholds_copied": [cid]}
    assert STATE.int_thresholds["e-thr-new"]["sda"] == {"Reallocated Sector Count": 30}
    # Gone from the app; another machine's stay.
    assert _app_thresholds() == [{"card": "replicas|OTHER", "label": "Spin Retry Count", "value": 2}]
    status, body = proxy.get(f"/cards/{quote(cid, safe='')}/thresholds")
    assert body["source"] == "integration"
    assert _labels(body)["Reallocated Sector Count"]["value"] == 30


@needs_rules
def test_thresholds_stay_in_the_app_when_the_copy_fails(proxy):
    SELF_AGENT.drives = [_thr_drive("THR-KEEP", realloc=24)]
    cid = "self|THR-KEEP"
    STATE.options["thresholds"] = [{"card": cid, "label": "Reallocated Sector Count", "value": 30}]
    _refresh_hosts(proxy)
    proxy.get("/fleet/hosts/self/drives")
    STATE.flow_step = {"type": "create_entry", "result": {"entry_id": "e-thr-keep"}}
    STATE.int_labels = {"e-thr-keep": {"sda": list(ATA_LABELS)}}
    STATE.options_flow_abort = "no_drives"
    STATE.on_create = (_entry("e-thr-keep", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})"),
                       [_device("dev-keep", "e-thr-keep", serial="THR-KEEP")])
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 200 and body == {"result": "created", "entry_id": "e-thr-keep"}
    assert _app_thresholds() == [{"card": cid, "label": "Reallocated Sector Count", "value": 30}]


@needs_rules
def test_thresholds_leave_with_a_removed_card(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    STATE.options["thresholds"] = [
        {"card": f"{key}|NUC-1", "label": "Reallocated Sector Count", "value": 3},
        {"card": "self|SER-A", "label": "Spin Retry Count", "value": 2},
    ]
    _refresh_hosts(proxy)
    proxy.get(f"/fleet/hosts/{key}/drives")
    status, _ = proxy.request("DELETE", f"/cards/{quote(key + '|NUC-1', safe='')}")
    assert status == 200
    assert _app_thresholds() == [{"card": "self|SER-A", "label": "Spin Retry Count", "value": 2}]


def test_thresholds_options_shape(mod):
    raw = [{"card": "self|A", "label": "Spin Retry Count", "value": 2},
           {"card": "self|A", "label": "Command Timeout", "value": 150},
           {"card": "", "label": "x", "value": 1}, {"card": "self|B", "label": "y", "value": -1},
           {"card": "self|B", "label": "z", "value": True}, "junk"]
    parsed = mod._normalize_thresholds(raw)
    assert parsed == {"self|A": {"Spin Retry Count": 2, "Command Timeout": 150}}
    assert mod._serialize_thresholds(parsed) == [
        {"card": "self|A", "label": "Command Timeout", "value": 150},
        {"card": "self|A", "label": "Spin Retry Count", "value": 2}]


@needs_rules
def test_view_override_notes(mod):
    drive = _ata_drive({"Reallocated_Sector_Ct": 24, "Current_Pending_Sector": 3})
    work = copy.deepcopy(drive)
    verdict = mod.loaded_rules.evaluate(work, {"Reallocated Sector Count": 5, "Current Pending Sector Count": 3})
    rows = _rows(mod.drive_view(work, verdict))
    # Over its override: flagged, and the note names the override.
    assert rows["reallocated"]["mark"] == "crit"
    assert rows["reallocated"]["limit"]["text"] == "alert above 5, default 0"
    # Within its override: accepted, not flagged.
    assert rows["pending"]["mark"] is None and rows["pending"]["accepted"] is True
    limits = {lim["label"]: lim for lim in verdict["limits"]}
    assert limits["Reallocated Sector Count"]["flagged"] and not limits["Current Pending Sector Count"]["flagged"]
    assert mod.accept_value(91, "ge") == 92 and mod.accept_value(15, "lt") == 15
    assert mod.threshold_text(15, 20, "lt", "%") == "alert below 15%, default 20%"


# ── The websocket client, against the fake Core websocket ──────────────────

def _ws_url(backends) -> str:
    return f"ws://127.0.0.1:{backends[0].server_address[1]}/core/websocket"


def test_ws_client_renames_a_device(mod, backends):
    STATE.devices = [_device("dev-1", "e-1", serial="SER-1")]
    reply = mod.ha_ws_command(_ws_url(backends), TOKEN, {
        "type": "config/device_registry/update", "device_id": "dev-1", "name_by_user": "Boot disk"})
    assert reply["success"] is True and reply["result"]["name_by_user"] == "Boot disk"
    assert STATE.ws_commands == [{"type": "config/device_registry/update", "device_id": "dev-1",
                                  "name_by_user": "Boot disk", "id": 1}]
    assert STATE.devices[0]["name_by_user"] == "Boot disk"
    # The ping before auth was answered with its payload; the result came in
    # two frames after another message; the client closed with a close frame.
    assert STATE.ws_pongs == [b"hello"]
    assert STATE.ws_closed_cleanly == [True]


@pytest.mark.parametrize("size", [10, 300, 70000])
def test_ws_client_frame_lengths(mod, backends, size):
    """Payloads under 126 bytes, up to 65535 (16-bit length) and above
    (64-bit length), both ways."""
    data = "x" * size
    reply = mod.ha_ws_command(_ws_url(backends), TOKEN, {"type": "test/echo", "data": data})
    assert reply["result"] == data and STATE.ws_commands[0]["data"] == data


@pytest.mark.parametrize("mode,words", [
    ("refuse", "Device not found"), ("auth", "refused the auth"),
    ("down", "upgrade was refused"), ("drop", ""),
])
def test_ws_client_failures(mod, backends, mode, words):
    STATE.ws_mode = mode
    with pytest.raises(mod.WebSocketError) as err:
        mod.ha_ws_command(_ws_url(backends), TOKEN, {
            "type": "config/device_registry/update", "device_id": "dev-1", "name_by_user": "x"})
    assert words in str(err.value)
    assert TOKEN not in str(err.value)


def test_ws_client_bad_accept_and_no_server(mod):
    """A server that answers 101 with the wrong accept key, and a closed port."""
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def answer() -> None:
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                             b"Connection: Upgrade\r\nSec-WebSocket-Accept: wrong\r\n\r\n")

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        with pytest.raises(mod.WebSocketError, match="Sec-WebSocket-Accept"):
            mod.WebSocket(f"ws://127.0.0.1:{port}/core/websocket").connect()
        thread.join(5)
    with pytest.raises(mod.WebSocketError, match="could not connect"):
        mod.WebSocket(f"ws://127.0.0.1:{_free_port()}/x", timeout=1).connect()
    with pytest.raises(mod.WebSocketError, match="not a ws://"):
        mod.WebSocket("wss://supervisor/core/websocket")


def test_ws_url_from_the_supervisor_address(mod):
    assert mod.Supervisor("http://supervisor", "t").ws_url() == "ws://supervisor/core/websocket"
    assert mod.Supervisor("http://127.0.0.1:8080/", "t").ws_url() == "ws://127.0.0.1:8080/core/websocket"


# ── Names: /cards/<id>/name and /hosts/<id>/name ────────────────────────────

def _put_name(proxy, path: str, name: Any) -> tuple[int, Any]:
    return proxy.request("PUT", path, {"name": name})


def test_card_name_kept_in_the_options(proxy):
    cid = "self|SER-A"
    path = f"/cards/{quote(cid, safe='')}/name"
    status, body = _put_name(proxy, path, "  Boot   disk ")
    assert status == 200 and body == {"id": cid, "name": "Boot disk", "home_assistant": None}
    assert STATE.options["card_names"] == [{"card": cid, "name": "Boot disk"}]
    assert proxy.get("/cards")[1]["names"] == {cid: "Boot disk"}
    assert not STATE.ws_commands  # not in Home Assistant: nothing sent there
    # The same name again writes nothing; empty restores the original.
    writes = len(_option_writes())
    _put_name(proxy, path, "Boot disk")
    assert len(_option_writes()) == writes
    status, body = _put_name(proxy, path, "")
    assert status == 200 and body["name"] == ""
    assert STATE.options["card_names"] == []
    assert proxy.get("/cards")[1]["names"] == {}


def test_card_name_refusals(proxy):
    assert _put_name(proxy, f"/cards/{quote('self|SER-A', safe='')}/name", 5)[0] == 400
    assert proxy.request("PUT", f"/cards/{quote('self|SER-A', safe='')}/name", ["x"])[0] == 400
    assert _put_name(proxy, f"/cards/{quote('10.9.9.9:9099|X', safe='')}/name", "x")[0] == 404
    assert _put_name(proxy, "/cards/self/name", "x")[0] == 404
    status, _ = _put_name(proxy, f"/cards/{quote('self|SER-A', safe='')}/name", "y" * 300)
    assert status == 200 and len(STATE.options["card_names"][0]["name"]) == 100


def _self_in_ha(proxy, serial: str) -> str:
    STATE.entries = [_entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})")]
    STATE.devices = [_device(f"dev-{serial}", "e-agent", serial=serial)]
    return f"self|{serial}"


def test_card_name_sets_the_home_assistant_device_name(proxy):
    cid = _self_in_ha(proxy, "SER-HA")
    path = f"/cards/{quote(cid, safe='')}/name"
    status, body = _put_name(proxy, path, "Boot disk")
    assert status == 200 and body["home_assistant"] == "saved"
    assert STATE.ws_commands == [{"type": "config/device_registry/update", "device_id": "dev-SER-HA",
                                  "name_by_user": "Boot disk", "id": 1}]
    assert STATE.options["card_names"] == [{"card": cid, "name": "Boot disk"}]
    # Read back from Home Assistant on the next /ha/devices.
    names = proxy.get("/ha/devices")[1]["targets"]["self"]["names"]
    assert names["SER-HA"] == {"name": "SER-HA", "name_by_user": "Boot disk"}
    # Empty clears the device's name too (null, Home Assistant's own name).
    _put_name(proxy, path, "")
    assert STATE.ws_commands[-1]["name_by_user"] is None
    assert proxy.get("/ha/devices")[1]["targets"]["self"]["names"]["SER-HA"]["name_by_user"] is None
    assert STATE.options["card_names"] == []


def test_card_name_home_assistant_refuses(proxy):
    cid = _self_in_ha(proxy, "SER-NO")
    STATE.ws_mode = "refuse"
    status, body = _put_name(proxy, f"/cards/{quote(cid, safe='')}/name", "Boot disk")
    assert status == 200 and body == {"id": cid, "name": "Boot disk", "home_assistant": "failed"}
    assert STATE.options["card_names"] == [{"card": cid, "name": "Boot disk"}]  # kept in the app
    STATE.ws_mode = "down"
    assert _put_name(proxy, f"/cards/{quote(cid, safe='')}/name", "Other")[1]["home_assistant"] == "failed"


def test_ha_devices_reads_names_given_in_home_assistant(proxy):
    _self_in_ha(proxy, "SER-RB")
    STATE.devices[0]["name_by_user"] = "Renamed in HA"
    names = proxy.get("/ha/devices")[1]["targets"]["self"]["names"]
    assert names == {"SER-RB": {"name": "SER-RB", "name_by_user": "Renamed in HA"}}
    template = [c[2] for c in STATE.calls if c[1] == "/core/api/template"][-1]["template"]
    assert "device_attr(d, 'name_by_user')" in template and "device_attr(d, 'name')" in template


def test_host_name(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    status, body = _put_name(proxy, f"/hosts/{quote(key, safe='')}/name", "Basement NAS")
    assert status == 200 and body == {"id": key, "name": "Basement NAS"}
    assert STATE.options["host_names"] == [{"host": key, "name": "Basement NAS"}]
    # Every card of that machine follows: the page reads one name per host.
    assert proxy.get("/cards")[1]["host_names"] == {key: "Basement NAS"}
    assert STATE.options["hosts"] == [{"address": "127.0.0.2", "port": field}]
    _put_name(proxy, f"/hosts/{quote(key, safe='')}/name", "")
    assert STATE.options["host_names"] == []
    assert _put_name(proxy, "/hosts/self/name", "x")[0] == 409
    assert _put_name(proxy, "/hosts/replicas/name", "x")[0] == 409
    assert _put_name(proxy, "/hosts/10.9.9.9:9099/name", "x")[0] == 404
    assert not STATE.ws_commands


def test_names_leave_with_their_cards(proxy, field):
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    STATE.options["card_names"] = [{"card": f"{key}|NUC-1", "name": "One"},
                                   {"card": f"{key}|NUC-2", "name": "Two"},
                                   {"card": "self|SER-A", "name": "Mine"}]
    STATE.options["host_names"] = [{"host": key, "name": "Basement"}]
    _refresh_hosts(proxy)
    proxy.get(f"/fleet/hosts/{key}/drives")
    proxy.request("DELETE", f"/cards/{quote(f'{key}|NUC-1', safe='')}")
    assert [n["card"] for n in STATE.options["card_names"]] == [f"{key}|NUC-2", "self|SER-A"]
    assert STATE.options["host_names"] == [{"host": key, "name": "Basement"}]
    proxy.request("DELETE", f"/cards/{quote(f'{key}|NUC-2', safe='')}")
    assert STATE.options["card_names"] == [{"card": "self|SER-A", "name": "Mine"}]
    assert STATE.options["host_names"] == []


# ── The trash in Home Assistant: /ha/devices ha_removal, DELETE ?ha= ────────

def _drive_device(device_id: str, entry_id: str, serial: str, drive_id: str) -> dict[str, Any]:
    """A drive's device as the integration registers it: its identifier is
    the agent's drive id."""
    return {"id": device_id, "name": serial, "serial_number": serial,
            "identifiers": [["smart_sniffer", drive_id]], "config_entries": [entry_id]}


def test_ha_removal_mode(mod):
    target = {"entry_id": "e1", "devices": {"S1": "d1", "S2": "d2", "pool:tank": "dp", "pool:old": "do"},
              "identifiers": {"S1": "sda", "S2": "ata-OLD"}}
    read = {"status": "ok", "stale": False, "pools_status": "ok", "items": [
        {"type": "drive", "id": "sda"}, {"type": "pool", "id": "pool.tank", "pool": {"name": "tank"}}]}
    mode = mod.ha_removal_mode
    assert mode("drive", "S1", target, read, False) == "disable"  # still reported
    assert mode("drive", "S2", target, read, False) == "delete"  # its id is no longer reported
    assert mode("drive", "S1", target, read, True) == "entry"  # the machine's last card
    assert mode("drive", "S9", target, read, False) is None  # not in Home Assistant
    assert mode("drive", "S9", target, read, True) is None
    assert mode("drive", "S1", None, read, True) is None  # the machine has no entry
    assert mode("filesystem", "fs:x", target, read, False) is None
    assert mode("pool", "pool:tank", target, read, False) == "disable"
    assert mode("pool", "pool:old", target, read, False) == "delete"
    assert mode("pool", "pool:old", target, {**read, "pools_status": "failed"}, False) == "disable"
    # An agent not answering: the integration deletes nothing, so disable.
    assert mode("drive", "S2", target, {**read, "status": "unreachable", "stale": True}, False) == "disable"
    # A device whose drive id is not known is never offered a delete.
    assert mode("drive", "S3", {**target, "devices": {"S3": "d3"}}, read, False) == "disable"
    # Entries only (Core's device list unreadable): the entry, on the last card.
    bare = {"entry_id": "e1", "devices": {}, "identifiers": {}}
    assert mode("drive", "S1", bare, read, True, True) == "entry"
    assert mode("drive", "S1", bare, read, False, True) is None
    # A replica with a device: always "replica", its last one too (the
    # replicas' entry stays); none without a device.
    assert mode("drive", "S1", target, read, False, replica=True) == "replica"
    assert mode("drive", "S1", target, read, True, replica=True) == "replica"
    assert mode("pool", "pool:tank", target, read, True, replica=True) == "replica"
    assert mode("drive", "S9", target, read, True, replica=True) is None
    assert mode("drive", "S1", bare, read, True, True, replica=True) is None


def _field_in_ha(proxy, field, devices: list[dict[str, Any]]) -> str:
    key = f"127.0.0.2:{field}"
    _store({"address": "127.0.0.2", "port": field})
    _refresh_hosts(proxy)
    STATE.entries = [_entry("e-field", f"SMART Sniffer (127.0.0.2:{field})"),
                     _entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})")]
    STATE.devices = devices
    assert proxy.get(f"/fleet/hosts/{key}/drives")[1]["status"] == "ok"
    return key


@needs_rules
def test_ha_devices_ha_removal(proxy, field):
    FIELD["127.0.0.2"].drives = [simple_drive("sda", "NUC-1"), simple_drive("sdb", "NUC-2"),
                                 simple_drive("sdc", "NUC-3")]
    FIELD["127.0.0.2"].pools = [{"name": "tank", "state": "ONLINE", "problem_vdevs": []}]
    key = _field_in_ha(proxy, field, [
        _drive_device("dev-1", "e-field", "NUC-1", "sda"),
        _drive_device("dev-2", "e-field", "NUC-2", "ata-OLD-NUC-2"),  # an id the agent no longer reports
        _device("dev-pool", "e-field", pool="tank"),
        _drive_device("dev-self", "e-agent", "SER-A", "nvme0"),
    ])
    _, body = proxy.get("/ha/devices")
    # NUC-3 has no device (a grey icon); HAOS cards have no trash.
    assert body["ha_removal"] == {f"{key}|NUC-1": "disable", f"{key}|NUC-2": "delete",
                                  f"{key}|pool:tank": "disable"}
    # Removed here only, all but one: the one left is the machine's last card.
    for gone in ("NUC-2", "NUC-3", "pool:tank"):
        assert proxy.request("DELETE", f"/cards/{quote(f'{key}|{gone}', safe='')}")[0] == 200
    _, body = proxy.get("/ha/devices")
    assert body["ha_removal"] == {f"{key}|NUC-1": "entry"}
    # Hidden still counts as shown.
    FIELD["127.0.0.2"].drives = [simple_drive("sda", "NUC-1"), simple_drive("sdd", "NUC-4")]
    STATE.options["hidden_cards"] = [f"{key}|NUC-4"]
    proxy.get(f"/fleet/hosts/{key}/drives")
    assert proxy.get("/ha/devices")[1]["ha_removal"] == {f"{key}|NUC-1": "disable"}


def _delete(proxy, cid: str, ha: str | None) -> tuple[int, Any]:
    return proxy.request("DELETE", f"/cards/{quote(cid, safe='')}" + (f"?ha={ha}" if ha else ""))


@needs_rules
def test_card_delete_ha_disable(proxy, field):
    key = _field_in_ha(proxy, field, [_drive_device("dev-1", "e-field", "NUC-1", "sda"),
                                      _drive_device("dev-2", "e-field", "NUC-2", "sdb")])
    status, body = _delete(proxy, f"{key}|NUC-1", "disable")
    assert status == 200 and body == {"removed": f"{key}|NUC-1", "host_removed": False,
                                      "home_assistant": "disabled", "ha_error": None}
    assert STATE.ws_commands == [{"type": "config/device_registry/update", "device_id": "dev-1",
                                  "disabled_by": "user", "id": 1}]
    assert STATE.options["hosts"][0]["removed"] == ["NUC-1"]
    # Home Assistant refusing keeps the page removal and says why.
    STATE.options["hosts"][0]["removed"] = []
    STATE.ws_mode = "refuse"
    STATE.ws_error = {"code": "home_assistant_error", "message": "Device is busy."}
    status, body = _delete(proxy, f"{key}|NUC-1", "disable")
    assert status == 200 and body["home_assistant"] == "failed" and body["ha_error"] == "Device is busy"
    assert STATE.options["hosts"][0]["removed"] == ["NUC-1"]
    STATE.ws_mode = "down"
    status, body = _delete(proxy, f"{key}|NUC-2", "disable")
    assert body["home_assistant"] == "failed" and body["ha_error"] and body["host_removed"] is True
    assert not any(c[0] == "DELETE" for c in STATE.calls if "config_entries" in c[1])


@needs_rules
def test_card_delete_ha_delete(proxy, field):
    key = _field_in_ha(proxy, field, [_drive_device("dev-1", "e-field", "NUC-1", "sda"),
                                      _drive_device("dev-2", "e-field", "NUC-2", "ata-OLD")])
    status, body = _delete(proxy, f"{key}|NUC-2", "delete")
    assert status == 200 and body["home_assistant"] == "deleted" and body["ha_error"] is None
    assert STATE.ws_commands == [{"type": "config/device_registry/remove_config_entry",
                                  "device_id": "dev-2", "config_entry_id": "e-field", "id": 1}]
    assert [d["id"] for d in STATE.devices] == ["dev-1"]
    # The integration's own check refuses: its reason reaches the page.
    STATE.ws_mode = "refuse"
    STATE.ws_error = {"code": "home_assistant_error",
                      "message": "office-nuc still reports this drive. Remove the drive first."}
    status, body = _delete(proxy, f"{key}|NUC-1", "delete")
    assert status == 200 and body["home_assistant"] == "failed"
    assert body["ha_error"] == "office-nuc still reports this drive. Remove the drive first"
    assert body["host_removed"] is True  # the page removal stands


@needs_rules
def test_card_delete_ha_entry(proxy, field):
    key = _field_in_ha(proxy, field, [_drive_device("dev-1", "e-field", "NUC-1", "sda"),
                                      _drive_device("dev-2", "e-field", "NUC-2", "sdb")])
    # Not the last card: the entry is left alone.
    status, body = _delete(proxy, f"{key}|NUC-1", "entry")
    assert status == 200 and body["home_assistant"] == "failed"
    assert body["ha_error"] == "other cards of this machine are still on the page"
    assert [e["entry_id"] for e in STATE.entries] == ["e-field", "e-agent"]
    # The last card: the machine leaves the page and its entry Home Assistant.
    status, body = _delete(proxy, f"{key}|NUC-2", "entry")
    assert body == {"removed": f"{key}|NUC-2", "host_removed": True,
                    "home_assistant": "deleted", "ha_error": None}
    assert ("DELETE", "/core/api/config/config_entries/entry/e-field", None) in STATE.calls
    assert [e["entry_id"] for e in STATE.entries] == ["e-agent"]
    assert not STATE.ws_commands


@needs_rules
def test_card_delete_ha_entry_refused_and_bad_requests(proxy, field):
    FIELD["127.0.0.2"].drives = [simple_drive("sda", "NUC-1")]
    key = _field_in_ha(proxy, field, [_drive_device("dev-1", "e-field", "NUC-1", "sda")])
    assert _delete(proxy, f"{key}|NUC-1", "everything")[0] == 400
    assert STATE.options["hosts"][0].get("removed", []) == []  # nothing removed
    assert _delete(proxy, "self|SER-A", "disable")[0] == 409
    STATE.entry_delete_status = 404
    status, body = _delete(proxy, f"{key}|NUC-1", "entry")
    assert status == 200 and body["host_removed"] is True
    assert body["home_assistant"] == "failed" and body["ha_error"] == "Config entry not found"


@needs_rules
def test_card_delete_ha_not_in_home_assistant(proxy, field):
    key = _field_in_ha(proxy, field, [])
    status, body = _delete(proxy, f"{key}|NUC-1", "disable")
    assert status == 200 and body["home_assistant"] == "failed"
    assert body["ha_error"] == "the card has no device in Home Assistant"
    STATE.entries = []
    status, body = _delete(proxy, f"{key}|NUC-2", "delete")
    assert body["ha_error"] == "the card is not in Home Assistant" and body["host_removed"] is True
    assert not STATE.ws_commands


def _replicas_in_ha(proxy) -> tuple[str, str]:
    """Two replicas on the page, each with a device under the replicas'
    entry. Their card ids."""
    status, body = proxy.post("/replicas/add", {"files": ["shipped.smart-failed", "shipped.nvme-wear"]})
    assert status == 201
    _, read = proxy.get("/fleet/hosts/replicas/drives")
    mine = {c["card_id"] for c in body["cards"]}
    items = [i for i in read["items"] if i["id"] in mine]
    STATE.entries = [_entry("e-lab", f"SMART Sniffer ({AGENT_HOST}:{proxy.mock_port})")]
    STATE.devices = [_drive_device(f"dev-r{n}", "e-lab", i["card_id"].split("|", 1)[1], i["id"])
                     for n, i in enumerate(items, 1)]
    STATE.device_entities = {f"dev-r{n}": [f"sensor.r{n}_temperature", f"sensor.r{n}_health"]
                             for n in range(1, len(items) + 1)}
    return items[0]["card_id"], items[1]["card_id"]


def _updates() -> list[Any]:
    return [c[2] for c in STATE.calls if c[1] == "/core/api/services/homeassistant/update_entity"]


@needs_mock
@needs_rules
def test_card_delete_ha_replica(proxy):
    first, second = _replicas_in_ha(proxy)
    # Every replica in Home Assistant is a "replica", the last one too.
    assert proxy.get("/ha/devices")[1]["ha_removal"] == {first: "replica", second: "replica"}
    # The integration refuses once (its last poll still had the drive): the
    # removal polls through an entity of the device and asks again.
    STATE.remove_refusals = 1
    STATE.ws_error = {"code": "home_assistant_error",
                      "message": "The replica agent still reports this drive."}
    status, body = _delete(proxy, first, "replica")
    assert status == 200 and body == {"removed": first, "host_removed": False,
                                      "home_assistant": "deleted", "ha_error": None}
    assert _updates() == [{"entity_id": "sensor.r1_temperature"}] * 2
    removes = [c for c in STATE.ws_commands if c["type"] == "config/device_registry/remove_config_entry"]
    assert removes == [{"type": "config/device_registry/remove_config_entry", "device_id": "dev-r1",
                        "config_entry_id": "e-lab", "id": 1}] * 2
    assert [d["id"] for d in STATE.devices] == ["dev-r2"]
    _, read = proxy.get("/fleet/hosts/replicas/drives")
    assert first not in {i["card_id"] for i in read["items"]}
    assert proxy.get("/ha/devices")[1]["ha_removal"] == {second: "replica"}
    # Refused past the wait: the replica is gone here, the reason passed on,
    # and the replicas' entry is never touched.
    STATE.calls.clear()
    STATE.remove_refusals = 99
    status, body = _delete(proxy, second, "replica")
    assert status == 200 and body["home_assistant"] == "failed"
    assert body["ha_error"] == "The replica agent still reports this drive"
    assert len(_updates()) >= 2
    _, read = proxy.get("/fleet/hosts/replicas/drives")
    assert second not in {i["card_id"] for i in read["items"]}
    assert [e["entry_id"] for e in STATE.entries] == ["e-lab"]
    assert not any(c[0] == "DELETE" and "config_entries" in c[1] for c in STATE.calls)


@needs_mock
@needs_rules
def test_card_delete_ha_replica_no_entity_and_bad_requests(proxy, field):
    first, second = _replicas_in_ha(proxy)
    # No entity with a state to poll through: the removal still asks.
    STATE.device_entities = {}
    status, body = _delete(proxy, first, "replica")
    assert status == 200 and body["home_assistant"] == "deleted"
    assert not _updates()
    # The replicas keep their entry; ha=replica is for replicas only.
    assert _delete(proxy, second, "entry")[0] == 400
    key = _field_in_ha(proxy, field, [_drive_device("dev-1", "e-field", "NUC-1", "sda")])
    assert _delete(proxy, f"{key}|NUC-1", "replica")[0] == 400
    assert STATE.options["hosts"][0].get("removed", []) == []
    _delete(proxy, second, None)
