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

import copy
import importlib.util
import json
import os
import shutil
import socket
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
from urllib.parse import urlparse

import pytest

PROXY = Path(__file__).resolve().parent / "proxy.py"
# Default: a smart-sniffer checkout next to this repository's checkout.
_SIBLING = Path(__file__).resolve().parents[3] / "smart-sniffer" / "custom_components"
RULES_DIR = Path(os.environ.get("SS_RULES_DIR", _SIBLING))
FIXTURES = Path(os.environ.get("SS_FIXTURES", RULES_DIR.parent / "tests" / "fixtures"))
# The replica agent: SS_MOCK, else the rules checkout's tools/mock-agent.py.
# The replica tests are skipped when that mock has no replica files.
MOCK_AGENT = Path(os.environ.get("SS_MOCK", RULES_DIR.parent / "tools" / "mock-agent.py"))
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
    try:
        text = MOCK_AGENT.read_text(encoding="utf-8")
    except OSError:
        return False
    return "--user-replicas" in text and (MOCK_AGENT.parent / "replicas").is_dir()


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
        self.options: dict[str, Any] = {
            "port": 9099, "token": "", "scan_interval": 60,
            "mock_port": 9100, "hosts": [],
        }
        self.options_status = 200  # 400 makes the Supervisor refuse a write
        self.network: dict[str, Any] = {"interfaces": [
            {"interface": "lo0", "primary": False, "connected": True,
             "ipv4": {"address": ["192.168.99.2/24"]}},
            {"interface": "enp0s18", "primary": True, "connected": True,
             "ipv4": {"address": ["127.0.0.1/24"]}},
        ]}
        self.states: list[dict[str, Any]] = []
        self.diagnostics: dict[str, int] = {}  # entry_id -> port
        self.calls: list[tuple[str, str, Any]] = []
        self.restarted = threading.Event()


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
        body = self._body()
        path = self.path
        STATE.calls.append((method, path, body))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            _json_reply(self, 401, {"message": "unauthorized"})
            return

        if path == "/addons/self/info" and method == "GET":
            _json_reply(self, 200, {"result": "ok", "data": {
                "slug": SLUG, "hostname": AGENT_HOST, "options": STATE.options,
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
            STATE.entries = [e for e in STATE.entries if e["entry_id"] != entry_id]
            _json_reply(self, 200, {"require_restart": False})
        elif core == "/config/config_entries/flow" and method == "POST":
            if body.get("handler") != "smart_sniffer":
                _json_reply(self, 404, {"message": "Invalid handler specified"})
                return
            _json_reply(self, 200, {
                "type": "form", "flow_id": "flow123", "handler": "smart_sniffer",
                "step_id": "user", "errors": {}, "data_schema": [],
            })
        elif core == "/config/config_entries/flow/flow123" and method == "POST":
            _json_reply(self, 200, {"flow_id": "flow123", **STATE.flow_step})
        elif core == "/config/config_entries/flow/flow123" and method == "DELETE":
            _json_reply(self, 200, {"message": "Flow aborted"})
        elif core.startswith("/diagnostics/config_entry/") and method == "GET":
            entry_id = core.rsplit("/", 1)[1]
            if entry_id not in STATE.diagnostics:
                _json_reply(self, 404, {"message": "not found"})
                return
            _json_reply(self, 200, {"data": {"config_entry": {
                "host": "172.30.33.5", "port": STATE.diagnostics[entry_id],
            }}})
        elif core == "/states" and method == "GET":
            _json_reply(self, 200, STATE.states)
        else:
            _json_reply(self, 404, {"message": f"no fake for {method} {core}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


class FakeAgent(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/api/health":
            _json_reply(self, 200, {"status": "ok", "version": "0.8.0"})
        elif self.path == "/api/drives":
            _json_reply(self, 200, [{"id": "nvme0", "model": "Fixture NVMe"}])
        else:
            _json_reply(self, 404, {"error": "not found"})


def _serve(handler) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
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
    agent = _serve(FakeAgent)
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
def proxy(backends, mock_agent):
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
    ], TOKEN)
    client.agent_port = agent_port  # type: ignore[attr-defined]
    client.mock_port = mock_port  # type: ignore[attr-defined]
    yield client
    # The Supervisor token must never reach the log.
    assert TOKEN not in _stop_proxy(proc)


@pytest.fixture(autouse=True)
def fresh_state():
    STATE.reset()
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
        "NVMe drive wear at 92% of rated life — consider scheduling replacement",
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


# ── /ha/status ──────────────────────────────────────────────────────────────

def test_ha_status_unreachable(proxy):
    STATE.core_status = 401
    status, body = proxy.get("/ha/status")
    assert status == 200
    assert body["reachable"] is False
    assert body["integration_loaded"] is False
    assert body["targets"]["agent"] == {
        "host": AGENT_HOST, "port": proxy.agent_port, "entry_id": None, "state": None,
    }
    assert body["targets"]["testlab"]["port"] == proxy.mock_port


def test_ha_status_reachable_without_integration(proxy):
    status, body = proxy.get("/ha/status")
    assert status == 200
    assert body["reachable"] is True
    assert body["integration_loaded"] is False
    assert body["targets"]["agent"]["entry_id"] is None


def test_ha_status_installed_but_no_entries(proxy):
    STATE.flow_handlers.append("smart_sniffer")
    _, body = proxy.get("/ha/status")
    assert body["reachable"] is True and body["integration_loaded"] is True


def test_ha_status_with_entries(proxy):
    STATE.entries = [
        _entry("e-nas", "SMART Sniffer (nas.local:9099)"),
        _entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})"),
        _entry("e-lab", f"SMART Sniffer ({AGENT_HOST}:{proxy.mock_port})", state="setup_retry"),
    ]
    status, body = proxy.get("/ha/status")
    assert status == 200
    assert body["reachable"] is True
    assert body["integration_loaded"] is True
    assert body["targets"]["agent"]["entry_id"] == "e-agent"
    assert body["targets"]["agent"]["state"] == "loaded"
    assert body["targets"]["testlab"]["entry_id"] == "e-lab"
    assert body["targets"]["testlab"]["state"] == "setup_retry"


def test_ha_status_zeroconf_titles(proxy):
    # Zeroconf entries carry the hostname only; the port comes from the
    # entry's diagnostics, and without them the entry is the agent's.
    STATE.entries = [
        _entry("z-agent", f"SMART Sniffer ({AGENT_HOST})"),
        _entry("z-lab", f"SMART Sniffer ({AGENT_HOST})"),
    ]
    STATE.diagnostics = {"z-lab": proxy.mock_port}
    _, body = proxy.get("/ha/status")
    assert body["targets"]["agent"]["entry_id"] == "z-agent"
    assert body["targets"]["testlab"]["entry_id"] == "z-lab"


# ── /ha/connect ─────────────────────────────────────────────────────────────

def test_ha_connect_creates_entry(proxy):
    STATE.flow_handlers.append("smart_sniffer")
    STATE.flow_step = {
        "type": "create_entry", "title": f"SMART Sniffer ({AGENT_HOST}:{proxy.mock_port})",
        "result": {"entry_id": "new-lab", "domain": "smart_sniffer"},
    }
    status, body = proxy.post("/ha/connect", {"target": "testlab"})
    assert status == 200
    assert body == {"result": "created", "entry_id": "new-lab"}
    flow_calls = [
        c for c in STATE.calls
        if c[1].startswith("/core/api/config/config_entries/flow") and c[0] == "POST"
    ]
    assert flow_calls[0] == (
        "POST", "/core/api/config/config_entries/flow",
        {"handler": "smart_sniffer", "show_advanced_options": False},
    )
    assert flow_calls[1] == (
        "POST", "/core/api/config/config_entries/flow/flow123",
        {"host": AGENT_HOST, "port": proxy.mock_port, "token": "", "scan_interval": 60},
    )


def test_ha_connect_already_configured(proxy):
    STATE.flow_step = {"type": "abort", "reason": "already_configured"}
    status, body = proxy.post("/ha/connect", {"target": "agent"})
    assert status == 200
    assert body == {"result": "already_connected"}


def test_ha_connect_existing_entry_short_circuits(proxy):
    STATE.entries = [_entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})")]
    status, body = proxy.post("/ha/connect", {"target": "agent"})
    assert status == 200
    assert body == {"result": "already_connected", "entry_id": "e-agent"}
    assert not any("/flow" in c[1] for c in STATE.calls)


def test_ha_connect_cannot_connect(proxy):
    STATE.flow_step = {
        "type": "form", "step_id": "user", "errors": {"base": "cannot_connect"},
    }
    status, body = proxy.post("/ha/connect", {"target": "testlab"})
    assert status == 200
    assert body == {"result": "error", "message": "cannot_connect"}
    # The half-finished flow is cleaned up.
    assert ("DELETE", "/core/api/config/config_entries/flow/flow123", None) in STATE.calls


def test_ha_connect_bad_target(proxy):
    status, _ = proxy.post("/ha/connect", {"target": "nas"})
    assert status == 400


# ── /ha/disconnect ──────────────────────────────────────────────────────────

def test_ha_disconnect_refuses_agent(proxy):
    STATE.entries = [_entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})")]
    status, body = proxy.post("/ha/disconnect", {"target": "agent"})
    assert status == 400 and "error" in body
    assert not any(c[0] == "DELETE" for c in STATE.calls)
    assert STATE.entries


def test_ha_disconnect_testlab(proxy):
    STATE.entries = [
        _entry("e-agent", f"SMART Sniffer ({AGENT_HOST}:{proxy.agent_port})"),
        _entry("e-lab", f"SMART Sniffer ({AGENT_HOST}:{proxy.mock_port})"),
    ]
    status, body = proxy.post("/ha/disconnect", {"target": "testlab"})
    assert status == 200
    assert body == {"result": "removed", "entry_id": "e-lab"}
    assert [e["entry_id"] for e in STATE.entries] == ["e-agent"]


# ── /ha/verdict ─────────────────────────────────────────────────────────────

def test_ha_verdict(proxy):
    STATE.states = [
        {"entity_id": "sensor.samsung_ssd_980_pro_1tb_s5gxnf0r000001_temperature",
         "state": "35", "attributes": {}},
        {"entity_id": "sensor.samsung_ssd_980_pro_1tb_s5gxnf0r000001_attention_needed",
         "state": "YES",
         "attributes": {"severity": "critical",
                        "reasons": ["NVMe media errors: 3 (expected 0)"]}},
        {"entity_id": "sensor.other_drive_zz_attention_needed", "state": "NO",
         "attributes": {"severity": "none", "reasons": ["No issues detected"]}},
    ]
    status, body = proxy.get("/ha/verdict?serial=S5GXNF0R000001")
    assert status == 200
    assert body == {
        "found": True,
        "entity_id": "sensor.samsung_ssd_980_pro_1tb_s5gxnf0r000001_attention_needed",
        "state": "YES",
        "severity": "critical",
        "reasons": ["NVMe media errors: 3 (expected 0)"],
    }


def test_ha_verdict_slugifies_serial(proxy):
    STATE.states = [
        {"entity_id": "sensor.wdc_wd40_wd_wx12a3_attention_needed", "state": "MAYBE",
         "attributes": {"severity": "warning", "reasons": ["x"]}},
    ]
    _, body = proxy.get("/ha/verdict?serial=WD-WX12A3")
    assert body["found"] is True and body["state"] == "MAYBE"


def test_ha_verdict_not_found(proxy):
    status, body = proxy.get("/ha/verdict?serial=NOPE123")
    assert status == 200
    assert body["found"] is False and body["state"] is None


def test_ha_verdict_needs_serial(proxy):
    status, _ = proxy.get("/ha/verdict")
    assert status == 400


