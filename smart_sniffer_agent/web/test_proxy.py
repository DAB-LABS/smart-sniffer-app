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

Run with:
  SS_RULES_DIR=../smart-sniffer/custom_components \
    python3 -m pytest smart_sniffer_agent/web/test_proxy.py
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import socket
import subprocess
import sys
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
TOKEN = "test-supervisor-token"
# The app installed as a local app: run.sh passes the container hostname and
# the Supervisor reports the slug.
AGENT_HOST = "local-smart-sniffer-agent"
SLUG = "local_smart_sniffer_agent"
MDNS_NAME = "smartha-homeassistant"

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
        self.entries: list[dict[str, Any]] = []
        self.components: list[str] = ["sensor", "zeroconf"]
        self.flow_handlers: list[str] = ["met", "zha"]
        self.flow_step: dict[str, Any] = {"type": "create_entry"}
        self.options: dict[str, Any] = {
            "port": 9099, "token": "", "scan_interval": 60,
            "mock_mode": False, "mock_port": 9100,
        }
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
            STATE.options = body["options"]
            _json_reply(self, 200, {"result": "ok", "data": {}})
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
            _json_reply(self, 200, {"components": STATE.components})
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
def proxy(backends):
    supervisor, agent = backends
    agent_port = agent.server_address[1]
    proc, client = _start_proxy([
        f"--agent-port={agent_port}",
        "--mock-port=9100",
        "--app-version=0.3.0",
        "--mock-enabled=true",
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{supervisor.server_address[1]}",
        f"--agent-host={AGENT_HOST}",
        f"--mdns-name={MDNS_NAME}",
    ], TOKEN)
    client.agent_port = agent_port  # type: ignore[attr-defined]
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


# ── /app/info ───────────────────────────────────────────────────────────────

def test_app_info(proxy):
    status, info = proxy.get("/app/info")
    assert status == 200
    assert info == {
        "app_version": "0.3.0",
        "rules_version": info["rules_version"],  # from VERSION beside the rules, if any
        "agent_port": proxy.agent_port,
        "mock_port": 9100,
        "mock_enabled": True,
        "ha_api": True,
        "hostname": AGENT_HOST,
        "slug": SLUG,
        "mdns_name": MDNS_NAME,
    }


def test_app_info_slug_is_cached(proxy):
    proxy.get("/app/info")
    STATE.calls.clear()
    _, info = proxy.get("/app/info")
    assert info["slug"] == SLUG
    assert not any(c[1] == "/addons/self/info" for c in STATE.calls)


def test_app_info_without_supervisor(backends):
    # The Supervisor refuses the token, run.sh found no mDNS name, and no
    # --agent-host was given: slug and mdns_name are null, the host is the
    # proxy's default.
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
    assert info["slug"] is None
    assert info["mdns_name"] is None
    assert info["hostname"] == module.DEFAULT_AGENT_HOST


def test_wrong_method_is_405(proxy):
    status, body = proxy.get("/evaluate")
    assert status == 405 and "error" in body


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


def test_evaluate_rejects_non_object(proxy):
    status, body = proxy.post("/evaluate", [1, 2])
    assert status == 400 and "error" in body


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
    assert body["targets"]["testlab"]["port"] == 9100


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
        _entry("e-lab", f"SMART Sniffer ({AGENT_HOST}:9100)", state="setup_retry"),
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
    STATE.diagnostics = {"z-lab": 9100}
    _, body = proxy.get("/ha/status")
    assert body["targets"]["agent"]["entry_id"] == "z-agent"
    assert body["targets"]["testlab"]["entry_id"] == "z-lab"


# ── /ha/connect ─────────────────────────────────────────────────────────────

def test_ha_connect_creates_entry(proxy):
    STATE.flow_handlers.append("smart_sniffer")
    STATE.flow_step = {
        "type": "create_entry", "title": f"SMART Sniffer ({AGENT_HOST}:9100)",
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
        {"host": AGENT_HOST, "port": 9100, "token": "", "scan_interval": 60},
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
        _entry("e-lab", f"SMART Sniffer ({AGENT_HOST}:9100)"),
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


# ── /lab/on, /lab/off ───────────────────────────────────────────────────────

def test_lab_on(proxy):
    STATE.options["token"] = "agent-secret"
    status, body = proxy.post("/lab/on")
    assert status == 200
    assert body == {"result": "restarting"}
    # The full options object goes back, with only mock_mode changed.
    assert STATE.options == {
        "port": 9099, "token": "agent-secret", "scan_interval": 60,
        "mock_mode": True, "mock_port": 9100,
    }
    assert STATE.restarted.wait(5), "restart was not requested"
    order = [c[1] for c in STATE.calls if c[1].startswith("/addons/")]
    assert order == ["/addons/self/info", "/addons/self/options", "/addons/self/restart"]


def test_lab_off(proxy):
    STATE.options["mock_mode"] = True
    status, body = proxy.post("/lab/off")
    assert status == 200 and body == {"result": "restarting"}
    assert STATE.options["mock_mode"] is False
    assert STATE.restarted.wait(5)
