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
            if body.get("handler") != "smart_sniffer" or not STATE.handler_known:
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
            if entry_id in STATE.diagnostics_broken:
                _json_reply(self, 500, {"message": "500 Internal Server Error"})
                return
            if entry_id not in STATE.diagnostics:
                _json_reply(self, 404, {"message": "not found"})
                return
            config = STATE.diagnostics[entry_id]
            if not isinstance(config, dict):
                config = {"host": "172.30.33.5", "port": config}
            _json_reply(self, 200, {"data": {"config_entry": config}})
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
                 pools: list[dict[str, Any]] | None = None, version: str = "0.8.1") -> None:
        self.mode = mode
        self.token = token
        self.hostname = hostname
        self.drives = drives if drives is not None else [simple_drive("sda", "SER-A")]
        self.pools = pools  # None: /api/pools not advertised
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
                health = {"status": "ok", "version": state.version, "os": "linux",
                          "uptime_seconds": 3542400, "endpoints": endpoints,
                          "drives": len(state.drives), "filesystems": 0}
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
                    "targets": {}}


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
