"""Tests for the web UI proxy's own routes (proxy.py), App 0.4.

The proxy runs as a real subprocess on a free port. Two small HTTP servers in
this process stand in for its backends: one for the Go agent, one for the
Supervisor (including Home Assistant Core behind /core/api and its websocket
behind /core/websocket). The proxy reaches the second through
--supervisor-url.

Drive reads need the integration's rule modules. Point SS_RULES_DIR at a
smart-sniffer checkout's custom_components directory (default: a
smart-sniffer checkout beside this repository); the drive fixtures come from
the same checkout's tests/fixtures, or from SS_FIXTURES. Tests that need them
are skipped when the rules are absent.

drive_view(), pool_verdict(), Rules and the integration lookup are also
tested directly, imported from proxy.py without starting it.

Run with:
  SS_RULES_DIR=../smart-sniffer/custom_components \\
    python3 -m pytest smart_sniffer_agent/web/test_proxy.py
"""

from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
# Home Assistant 2026 moved the app pages to /config/app/<slug>/...; the page
# picks its link paths from this version.
HA_VERSION = "2026.7.2"

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
        # The options as 0.3.1 left them: field machines and all.
        self.options: dict[str, Any] = {
            "port": 9099, "token": "", "scan_interval": 60, "hosts": [],
        }
        self.options_status = 200  # 400 makes the Supervisor refuse a write
        # The schema as the Supervisor's app info reports it (its UI form);
        # None when it reports none.
        self.schema: list[dict[str, Any]] | None = [
            {"name": "port", "type": "integer"}, {"name": "token", "type": "string"},
            {"name": "scan_interval", "type": "integer"},
            {"name": "hosts", "type": "schema", "multiple": True},
            {"name": "card_order", "type": "string", "multiple": True},
            {"name": "hidden_cards", "type": "string", "multiple": True},
            {"name": "thresholds", "type": "schema", "multiple": True},
            {"name": "card_names", "type": "schema", "multiple": True},
            {"name": "host_names", "type": "schema", "multiple": True},
        ]
        self.calls: list[tuple[str, str, Any]] = []
        # Core's websocket behind the Supervisor: "ok", "refuse" (every
        # command fails), "auth" (the token is refused), "down" (no
        # upgrade), "drop" (the connection closes after auth). The fake
        # pings before auth_required and splits its result into two frames,
        # so the client's pong and continuation handling are used every time.
        self.ws_mode = "ok"
        self.ws_commands: list[dict[str, Any]] = []
        self.ws_pongs: list[bytes] = []
        self.ws_closed_cleanly: list[bool] = []
        self.ws_error: dict[str, Any] = {"code": "not_found", "message": "Device not found"}
        # The integration as the websocket tells it: smart_sniffer/info's
        # answer (None: unknown command, as before v0.9 or not loaded;
        # "refused": the command is there and fails), and manifest/get's
        # version (None: not installed).
        self.info: dict[str, Any] | str | None = None
        self.manifest_version: str | None = None
        # The Supervisor's discovery store: uuid -> message.
        self.discovery: dict[str, dict[str, Any]] = {}
        self.discovery_status = 200  # else the Supervisor refuses a post
        self.discovery_seq = 0
        # The integration's handover (smart_sniffer/app/import), as v0.9
        # answers it: a job per call, a ledger by item id. import_skip: field
        # addresses it cannot take; import_lost / poll_lost: answers lost on
        # the way back (the work is done); import_restart: Home Assistant
        # restarted, the job is gone.
        self.import_ledger: dict[str, dict[str, Any]] = {}
        self.import_jobs: dict[str, dict[str, Any]] = {}
        self.import_skip: set[str] = set()
        self.import_lost = 0
        self.poll_lost = 0
        self.import_restart = False
        self.import_created: list[str] = []


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
        if path == "/discovery" and method == "POST":
            if STATE.discovery_status != 200:
                _json_reply(self, STATE.discovery_status, {"result": "error", "message": "nope"})
                return
            STATE.discovery_seq += 1
            uuid = f"uuid-{STATE.discovery_seq}"
            STATE.discovery[uuid] = body
            _json_reply(self, 200, {"result": "ok", "data": {"uuid": uuid}})
            return
        if path.startswith("/discovery/") and method == "DELETE":
            uuid = path.rsplit("/", 1)[1]
            if STATE.discovery.pop(uuid, None) is None:
                _json_reply(self, 404, {"result": "error", "message": "Discovery message not found"})
                return
            _json_reply(self, 200, {"result": "ok", "data": {}})
            return

        if not path.startswith("/core/api/"):
            _json_reply(self, 404, {"message": "not found"})
            return
        if STATE.core_status != 200:
            _json_reply(self, STATE.core_status, {"message": "unauthorized"})
            return
        core = path[len("/core/api"):]
        parsed = urllib.parse.urlparse(core)

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
        elif core == "/config/config_entries/flow" and method == "POST":
            if body.get("handler") != "smart_sniffer" or not STATE.handler_known:
                _json_reply(self, 404, {"message": "Invalid handler specified"})
                return
            _json_reply(self, 200, {
                "type": "form", "flow_id": "flow123", "handler": "smart_sniffer",
                "step_id": "user", "errors": {}, "data_schema": [],
            })
        elif core == "/config/config_entries/flow/flow123" and method == "POST":
            if STATE.flow_step.get("type") == "create_entry" and isinstance(STATE.info, dict):
                STATE.info["haos_entry"] = True
            _json_reply(self, 200, {"flow_id": "flow123", **STATE.flow_step})
        elif core == "/config/config_entries/flow/flow123" and method == "DELETE":
            _json_reply(self, 200, {"message": "Flow aborted"})
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


def _ws_result(command: dict[str, Any]) -> dict[str, Any]:
    """Core's answer to one command."""
    def ok(result: Any) -> dict[str, Any]:
        return {"id": command["id"], "type": "result", "success": True, "result": result}

    def error(code: str, message: str) -> dict[str, Any]:
        return {"id": command["id"], "type": "result", "success": False,
                "error": {"code": code, "message": message}}

    kind = command.get("type")
    if kind == "test/echo":
        return ok(command.get("data"))
    if STATE.ws_mode == "refuse":
        return {"id": command["id"], "type": "result", "success": False, "error": STATE.ws_error}
    if kind == "smart_sniffer/info":
        if STATE.info is None:
            return error("unknown_command", "Unknown command.")
        if STATE.info == "refused":
            return error("unauthorized", "Unauthorized")
        return ok(STATE.info)
    if kind == "smart_sniffer/app/import":
        return _fake_import(command)
    if kind == "smart_sniffer/app/import/result":
        if STATE.import_restart or command.get("job_id") not in STATE.import_jobs:
            return error("not_found", "no such job")
        if STATE.poll_lost > 0:
            STATE.poll_lost -= 1
            return None
        return ok({"job_id": command["job_id"], "state": "done",
                   "report": STATE.import_jobs[command["job_id"]]})
    if kind == "manifest/get":
        if command.get("integration") != "smart_sniffer" or STATE.manifest_version is None:
            return error("not_found", "Integration not found")
        return ok({"domain": "smart_sniffer", "name": "SMART Sniffer",
                   "version": STATE.manifest_version, "is_built_in": False})
    return error("unknown_command", "Unknown command.")


def _fake_import(command: dict[str, Any]) -> dict[str, Any] | None:
    """The integration's job, done at once; None when its answer is lost."""
    out: dict[str, Any] = {}
    for item in command.get("items") or []:
        row = STATE.import_ledger.get(item["item_id"])
        if row is not None:
            out[item["item_id"]] = {**row, "already": True}
            continue
        if item["kind"] == "field":
            if item["address"] in STATE.import_skip:
                out[item["item_id"]] = {"outcome": "skipped", "reason": "error"}
                continue
            STATE.import_created.append(item["address"])
            row = {"outcome": "created_offline", "entry_id": f"E{len(STATE.import_created)}"}
        elif item["kind"] == "replicas":
            row = {"outcome": "replicas_dropped"}
        else:
            row = {"outcome": "applied"}
        STATE.import_ledger[item["item_id"]] = row
        out[item["item_id"]] = row
    job_id = f"job{len(STATE.import_jobs) + 1}"
    STATE.import_jobs[job_id] = {"items": out}
    if STATE.import_lost > 0:
        STATE.import_lost -= 1
        return None
    return {"id": command["id"], "type": "result", "success": True, "result": {"job_id": job_id}}


def _fake_core_websocket(handler: BaseHTTPRequestHandler) -> None:
    try:
        _fake_core_websocket_run(handler)
    except (BrokenPipeError, ConnectionResetError):
        pass  # the proxy stopped mid-command


def _fake_core_websocket_run(handler: BaseHTTPRequestHandler) -> None:
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
    if STATE.ws_mode == "drop" or STATE.core_status != 200:
        _ws_send(handler, 0x8, struct.pack("!H", 1011))
        return
    command = _ws_message(handler)
    STATE.ws_commands.append(command)
    reply = _ws_result(command)
    if reply is None:  # the answer is lost: the connection just closes
        _ws_send(handler, 0x8, struct.pack("!H", 1011))
        return
    # An event for another subscription first, then the result in two frames.
    send({"id": 99, "type": "event", "event": {}})
    text = json.dumps(reply).encode()
    _ws_send(handler, 0x1, text[:5], fin=False)
    _ws_send(handler, 0x0, text[5:])
    try:
        opcode, _payload = _ws_read(handler)
    except ValueError:
        opcode = None  # the client went away without a close frame
    STATE.ws_closed_cleanly.append(opcode == 0x8)
    if opcode == 0x8:
        _ws_send(handler, 0x8, struct.pack("!H", 1000))


APP_TOKEN = "app-token-123"  # this app's agent token (the token option)
MAX_BODY_FOR_TESTS = 64 * 1024  # proxy.py's MAX_BODY
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


def _start_proxy(args: list[str], token: str, agent_token: str = "") -> tuple[subprocess.Popen, Proxy]:
    """token is the Supervisor's; agent_token this app's agent token, which
    run.sh hands over in the environment as well."""
    port = _free_port()
    env = {**os.environ, "SUPERVISOR_TOKEN": token, "AGENT_TOKEN": agent_token}
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
def data_dir():
    path = Path(tempfile.mkdtemp(prefix="ss-data-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _proxy_args(supervisor, agent_port: int, data_dir: Any, *extra: str) -> list[str]:
    return [
        f"--agent-port={agent_port}",
        "--app-version=0.4.0-dev",
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{supervisor.server_address[1]}",
        f"--agent-host={AGENT_HOST}",
        f"--mdns-name={MDNS_NAME}",
        f"--data-dir={data_dir}",
        f"--static-dir={PROXY.parent}",
        *extra,
    ]


@pytest.fixture(scope="session")
def proxy(backends, data_dir):
    supervisor, agent = backends
    agent_port = agent.server_address[1]
    proc, client = _start_proxy(_proxy_args(supervisor, agent_port, data_dir,
                                            "--ha-cache-seconds=0"), TOKEN, APP_TOKEN)
    client.agent_port = agent_port  # type: ignore[attr-defined]
    yield client
    # The Supervisor token and the agent token must never reach the log.
    output = _stop_proxy(proc)
    assert TOKEN not in output
    assert APP_TOKEN not in output
    assert FIELD_PASSWORD not in output


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
    assert view["rows"][i + 1] == {"key": "reported_unc_devstat", "name": "Reported uncorrectable",
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

def test_app_info(proxy):
    status, info = proxy.get("/app/info")
    assert status == 200
    assert info == {
        "app_version": "0.4.0-dev",
        "rules_version": info["rules_version"],  # from VERSION beside the rules, if any
        "agent_port": proxy.agent_port,
        "hostname": AGENT_HOST,
        "slug": SLUG,
        "mdns_name": MDNS_NAME,
        "ha_version": HA_VERSION,
        "ha_api": True,
        "supervisor": True,
    }


def test_app_info_slug_is_cached(proxy):
    proxy.get("/app/info")
    STATE.calls.clear()
    _, info = proxy.get("/app/info")
    assert info["slug"] == SLUG
    assert info["ha_version"] == HA_VERSION
    assert not any(c[1] == "/addons/self/info" for c in STATE.calls)
    assert not any(c[1] == "/core/api/config" for c in STATE.calls)


def _fresh(supervisor, *extra: str, token: str = TOKEN, data_dir: str = "",
           agent_token: str = "") -> tuple[subprocess.Popen, Proxy]:
    """A proxy of its own, so nothing is cached yet and its start is seen."""
    return _start_proxy([
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{supervisor.server_address[1]}",
        f"--agent-host={AGENT_HOST}",
        f"--data-dir={data_dir}",
        *extra,
    ], token, agent_token)


def _app_info_from_fresh_proxy(supervisor, *extra: str) -> dict[str, Any]:
    proc, client = _fresh(supervisor, *extra)
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
    STATE.core_status = 401
    info = _app_info_from_fresh_proxy(backends[0])
    assert info["ha_api"] is False
    assert info["slug"] == SLUG
    assert info["ha_version"] is None


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
        "--mdns-name=", "--data-dir=",
    ], "not-the-token")
    try:
        status, info = client.get("/app/info")
        assert client.get("/ha/integration")[1]["reachable"] is False
    finally:
        _stop_proxy(proc)
    assert status == 200
    assert info["ha_api"] is False
    assert info["supervisor"] is False
    assert info["slug"] is None
    assert info["mdns_name"] is None
    assert info["ha_version"] is None
    assert info["hostname"] == module.DEFAULT_AGENT_HOST


def test_wrong_method_is_405(proxy):
    status, body = proxy.request("POST", "/app/info", {})
    assert status == 405 and body["error"] == "method_not_allowed"


def test_agent_proxy_still_works(proxy):
    status, health = proxy.get("/api/health")
    assert status == 200 and health["status"] == "ok"


# ── Who is answered, and how much is read ───────────────────────────────────

def raw_request(base: str, method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None,
                source: str = "127.0.0.1") -> tuple[int, bytes]:
    """One request to the proxy from the given local address. Every
    127.x.x.x address is this machine, so 127.0.0.2 stands in for a client
    on the network: the proxy sees it as the client address."""
    port = int(base.rsplit(":", 1)[1])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10, source_address=(source, 0))
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if body is not None and "Content-Length" not in (headers or {}):
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


@pytest.mark.parametrize("address,allowed", [
    ("172.30.32.2", True), ("127.0.0.1", True), ("::1", True),
    ("::ffff:127.0.0.1", True), ("::ffff:172.30.32.2", True),
    ("172.30.32.1", False), ("172.30.33.2", False), ("127.0.0.2", False),
    ("192.168.1.20", False), ("10.0.0.5", False), ("0.0.0.0", False),
    ("::ffff:192.168.1.20", False), ("fe80::1", False), ("::", False),
    ("", False), ("not-an-address", False),
])
def test_client_allowed(mod, address, allowed):
    assert mod.client_allowed(address) is allowed


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/", None), ("GET", "/index.html", None), ("GET", "/cc.js", None),
    ("GET", "/api/health", None), ("GET", "/api/drives", None),
    ("DELETE", "/api/drives/sda", None), ("GET", "/app/info", None),
    ("GET", "/fleet/hosts/self/drives", None), ("GET", "/ha/integration", None),
    ("GET", "/cards/self%7CSER-A/file", None),
    ("POST", "/ha/add", b'{"target": "self"}'), ("POST", "/app/handover", b"{}"),
    ("POST", "/app/notice/dismiss", b'{"notice": "install"}'),
    ("PUT", "/nothing", b"{}"), ("PATCH", "/api/drives", b"{}"),
    ("HEAD", "/", None), ("OPTIONS", "/", None), ("BREW", "/pot", None),
])
def test_other_clients_are_refused(proxy, method, path, body):
    agent_asked = len(SELF_AGENT.requests)
    status, raw = raw_request(proxy.base, method, path, body, source="127.0.0.2")
    assert status == 403
    if method != "HEAD":
        assert json.loads(raw) == {"error": "forbidden", "message": None}
    assert APP_TOKEN.encode() not in raw
    assert len(SELF_AGENT.requests) == agent_asked
    assert _flow_posts() == []
    assert not any(c[1] in ("/addons/self/options", "/addons/self/info") for c in STATE.calls)
    # This machine itself is still answered.
    assert raw_request(proxy.base, "GET", "/api/health")[0] == 200


@pytest.mark.parametrize("method,path", [
    ("POST", "/ha/add"), ("POST", "/app/handover"), ("POST", "/app/notice/dismiss"),
    ("POST", "/api/drives"), ("GET", "/"), ("PUT", "/nothing"),
])
def test_body_over_the_limit_is_413_unread(proxy, method, path):
    # Only the headers are sent: a proxy that waited for the body would time
    # out instead of answering.
    agent_asked = len(SELF_AGENT.requests)
    status, raw = raw_request(proxy.base, method, path,
                              headers={"Content-Length": str(MAX_BODY_FOR_TESTS + 1)})
    assert status == 413 and json.loads(raw)["error"] == "too_large"
    assert len(SELF_AGENT.requests) == agent_asked
    assert _flow_posts() == []


@pytest.mark.parametrize("length", ["abc", "-1", "1e3"])
def test_bad_content_length_is_400(proxy, length):
    status, raw = raw_request(proxy.base, "POST", "/ha/add", headers={"Content-Length": length})
    assert status == 400 and json.loads(raw)["error"] == "bad_request"


def test_body_at_the_limit_is_read(proxy, mod):
    assert mod.MAX_BODY == MAX_BODY_FOR_TESTS
    head = b'{"target": "other", "pad": "'
    body = head + b"x" * (MAX_BODY_FOR_TESTS - len(head) - 2) + b'"}'
    assert len(body) == MAX_BODY_FOR_TESTS
    status, raw = raw_request(proxy.base, "POST", "/ha/add", body,
                              headers={"Content-Type": "application/json"})
    assert status == 400 and json.loads(raw)["error"] == "bad_target"  # read and parsed
    assert _flow_posts() == []


def test_token_never_on_the_command_line():
    # run.sh hands the agent token over in AGENT_TOKEN; there is no flag for it.
    out = subprocess.run([sys.executable, str(PROXY), "--help"], capture_output=True, text=True,
                         timeout=10).stdout
    assert "--token" not in out


# ── What 0.4 no longer answers ──────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("POST", "/lab/on"), ("POST", "/evaluate"),
    ("POST", "/fleet/scan"), ("GET", "/fleet/scan/abc"), ("POST", "/fleet/probe"),
    ("GET", "/fleet/hosts"), ("POST", "/fleet/hosts"), ("PATCH", "/fleet/hosts/nas.lan:9099"),
    ("DELETE", "/fleet/hosts/nas.lan:9099"), ("GET", "/fleet/hosts/nas.lan:9099/drives"),
    ("GET", "/fleet/hosts/replicas/drives"),
    ("GET", "/ha/devices"), ("GET", "/ha/status"),
    ("GET", "/replicas"), ("POST", "/replicas"), ("POST", "/replicas/add"),
    ("POST", "/replicas/save-as"), ("GET", "/replicas/replicas%7Cx/file"),
    ("DELETE", "/replicas/replicas%7Cx"),
    ("GET", "/cards"), ("PUT", "/cards/order"), ("PATCH", "/cards/self%7CSER-A"),
    ("DELETE", "/cards/self%7CSER-A"), ("PUT", "/cards/self%7CSER-A/name"),
    ("PUT", "/hosts/nas.lan:9099/name"),
    ("GET", "/cards/self%7CSER-A/thresholds"), ("PUT", "/cards/self%7CSER-A/thresholds"),
    ("POST", "/cards/self%7CSER-A/thresholds/accept"), ("POST", "/cards/self%7CSER-A/thresholds/reset"),
    ("GET", "/agentic-access"), ("GET", "/agentic-access.json"),
    ("GET", "/mock/drives"), ("POST", "/mock/drives/sda/scenario"),
])
def test_removed_routes_are_404(proxy, method, path):
    status, body = proxy.request(method, path, {} if method in ("POST", "PUT", "PATCH") else None)
    assert status == 404 and body["error"] == "not_found"
    assert not any(c[1] == "/addons/self/options" for c in STATE.calls)


def test_no_replica_port(proxy):
    # 0.3.x ran a replica agent without auth on 9100; nothing listens for it
    # now, and the proxy has no option for it.
    out = subprocess.run([sys.executable, str(PROXY), "--help"], capture_output=True, text=True,
                         timeout=10).stdout
    assert "--mock-port" not in out and "mock" not in out.lower()


# ── Start: old options and old data ─────────────────────────────────────────

def _option_writes() -> list[Any]:
    return [c[2] for c in STATE.calls if c[0] == "POST" and c[1] == "/addons/self/options"]


def _wait_for(check, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


FIELD_OPTIONS = {
    "hosts": [{"address": "nas.lan", "port": 9099, "password": "fieldpass", "removed": ["X"]}],
    "card_order": ["nas.lan:9099|S1", "self|SER-A"],
    "hidden_cards": ["nas.lan:9099|S2"],
    "thresholds": [{"card": "nas.lan:9099|S1", "label": "Command Timeout", "value": 5}],
    "card_names": [{"card": "nas.lan:9099|S1", "name": "Big disk"}],
    "host_names": [{"host": "nas.lan:9099", "name": "NAS"}],
}


@pytest.mark.parametrize("schema", ["reported", "not_reported"])
def test_start_removes_options_the_schema_no_longer_has(backends, schema):
    # mock_port (0.3.x) and mock_mode (0.2.x) stay in the options after the
    # update until they are written; the proxy writes them once at start
    # without them, and every field machine, name and card as they were.
    STATE.options = {**STATE.options, "mock_mode": True, "mock_port": 9100, "future": 1,
                     **copy.deepcopy(FIELD_OPTIONS)}
    expected = {"port": 9099, "token": "", "scan_interval": 60, **FIELD_OPTIONS}
    if schema == "not_reported":
        # Without the schema only the known old options go.
        STATE.schema = None
        expected["future"] = 1
    proc, _ = _fresh(backends[0])
    try:
        _wait_for(lambda: _option_writes())
        time.sleep(0.3)
    finally:
        output = _stop_proxy(proc)
    assert _option_writes() == [{"options": expected}]
    assert STATE.options == expected
    removed = [line for line in output.splitlines() if "Removed the old option" in line]
    assert len(removed) == 1 and "mock_mode" in removed[0] and "mock_port" in removed[0]
    assert "fieldpass" not in output


def test_start_writes_nothing_when_the_options_are_current(backends):
    STATE.options = {**STATE.options, **copy.deepcopy(FIELD_OPTIONS)}
    proc, client = _fresh(backends[0])
    try:
        _wait_for(lambda: any(c[1] == "/addons/self/info" for c in STATE.calls))
        client.get("/fleet/hosts/self/drives")
        client.get("/ha/integration")
        time.sleep(0.3)
    finally:
        output = _stop_proxy(proc)
    assert _option_writes() == []
    assert "Removed the old option" not in output


def test_old_fleet_cache_is_read_for_this_box_and_never_written(backends, tmp_path):
    # 0.3.x kept the last read of every machine in /data/fleet-cache.json.
    # 0.4 shows this box's last read from it while the agent is down, and
    # leaves the file (with the field machines in it) exactly as it was.
    _, agent = backends
    old = {"version": 1, "hosts": {
        "self": {"last_answer": "2026-10-01T10:00:00Z", "response": {
            "items": [{"type": "drive", "id": "sda", "card_id": "self|OLD-1",
                       "drive": {"id": "sda", "model": "Old", "serial": "OLD-1"},
                       "verdict": None, "view": None}],
            "filesystem_cards": []}},
        "nas.lan:9099": {"last_answer": "2026-10-01T10:00:00Z", "response": {"items": []}},
    }}
    cache = tmp_path / "fleet-cache.json"
    cache.write_text(json.dumps(old))
    before = cache.read_bytes()
    proc, client = _fresh(backends[0], f"--agent-port={_free_port()}", data_dir=str(tmp_path))
    try:
        status, body = client.get("/fleet/hosts/self/drives")
        assert status == 200 and body["status"] == "unreachable" and body["stale"] is True
        assert [i["card_id"] for i in body["items"]] == ["self|OLD-1"]
        assert body["last_answer"] == "2026-10-01T10:00:00Z"
    finally:
        _stop_proxy(proc)
    # A good read later is kept in memory only.
    proc2, client2 = _fresh(backends[0], f"--agent-port={agent.server_address[1]}",
                            data_dir=str(tmp_path), agent_token=APP_TOKEN)
    try:
        assert client2.get("/fleet/hosts/self/drives")[1]["status"] == "ok"
    finally:
        _stop_proxy(proc2)
    assert cache.read_bytes() == before
    # No other file appears beside it but the page's own small state.
    assert {p.name for p in tmp_path.iterdir()} <= {"app-state.json", "fleet-cache.json"}


# ── /fleet/hosts/self/drives ────────────────────────────────────────────────

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
    assert item["type"] == "drive" and item["id"] == "sda" and item["card_id"] == "self|SELF-1"
    assert item["verdict"]["state"] == "YES"
    assert item["view"]["rows"][0]["mark"] == "crit"
    assert body["pools_status"] == "none"
    assert ("GET", "/api/drives", f"Bearer {APP_TOKEN}") in SELF_AGENT.requests
    assert ("GET", "/api/drives/sda", f"Bearer {APP_TOKEN}") in SELF_AGENT.requests


@needs_rules
def test_drives_self_never_asks_home_assistant(proxy):
    # The page's 30 s read of this box reads the agent and, at most once a
    # minute, the 0.3.x thresholds from the options; Core is never asked.
    proxy.get("/fleet/hosts/self/drives")
    STATE.calls.clear()
    for _ in range(3):
        assert proxy.get("/fleet/hosts/self/drives")[1]["status"] == "ok"
    assert not any(c[1].startswith("/core/") for c in STATE.calls)
    assert not any(c[0] == "WS" for c in STATE.calls)


@needs_rules
def test_drives_self_with_saved_thresholds(backends, data_dir):
    # A limit 0.3.x saved for this box's drive still judges it: 4 reallocated
    # sectors under a limit of 10 are accepted, not Attention. Limits of
    # field machines' cards are not read for anything.
    supervisor, agent = backends
    SELF_AGENT.drives = [simple_drive("sda", "SELF-1", {"Reallocated_Sector_Ct": 4})]
    STATE.options = {**STATE.options, "thresholds": [
        {"card": "self|SELF-1", "label": "Reallocated Sector Count", "value": 10},
        {"card": "nas.lan:9099|S1", "label": "Command Timeout", "value": 5}]}
    proc, client = _start_proxy(_proxy_args(supervisor, agent.server_address[1], ""), TOKEN, APP_TOKEN)
    try:
        _, body = client.get("/fleet/hosts/self/drives")
    finally:
        _stop_proxy(proc)
    item = body["items"][0]
    assert item["verdict"]["state"] == "NO"
    assert item["thresholds_source"] == "app"
    limits = {lim["label"]: lim for lim in item["verdict"]["limits"]}
    assert limits["Reallocated Sector Count"]["value"] == 10
    assert _option_writes() == []


def test_drives_agent_down_and_password(backends):
    supervisor, _ = backends
    proc, client = _start_proxy(_proxy_args(supervisor, _free_port(), ""), TOKEN, APP_TOKEN)
    try:
        _, body = client.get("/fleet/hosts/self/drives")
        assert body["status"] == "unreachable" and body["stale"] is True and body["items"] == []
    finally:
        _stop_proxy(proc)
    wrong = AgentState(token="other")
    server = _serve(agent_handler(wrong))
    proc, client = _start_proxy(_proxy_args(supervisor, server.server_address[1], ""), TOKEN, APP_TOKEN)
    try:
        _, body = client.get("/fleet/hosts/self/drives")
        assert body["status"] == "password_refused"
    finally:
        _stop_proxy(proc)
        server.shutdown()


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


# ── Cards: ids, filesystems, the card file ──────────────────────────────────

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


def test_attach_filesystems_by_drive_id(mod):
    # Agent 0.8.3 on macOS: an APFS volume's device is a synthesized
    # container disk, the drive an IOService path; drive_id names the drive.
    io_path = "IOService:/FakeRoot/fake-io@0/FakeNVMeController/NS_01@1"
    items = [
        {"type": "drive", "id": "fake-sn-0001", "drive": {"device_path": io_path}},
        {"type": "drive", "id": "disk4", "drive": {"device_path": "/dev/disk4"}},
    ]
    data = {**_fs("fs-d", "/System/Volumes/Data", "/dev/disk3s5", "apfs"),
            "physical_devices": ["/dev/disk0"], "drive_id": "fake-sn-0001"}
    gone = {**_fs("fs-g", "/Volumes/Gone", "/dev/disk7s1", "apfs"),
            "physical_devices": ["/dev/disk7"], "drive_id": "not-listed"}
    usb = {**_fs("fs-u", "/Volumes/FakeUSB", "/dev/disk4s2", "hfs"),
           "physical_devices": ["/dev/disk4"], "drive_id": "disk4"}
    fusion = {**_fs("fs-f", "/", "/dev/disk5s1", "apfs"),
              "physical_devices": ["/dev/disk0", "/dev/disk1"]}
    bad = {**_fs("fs-b", "/Volumes/Odd", "/dev/disk4s3", "apfs"), "drive_id": ["disk4"]}
    whole = {**_fs("fs-w", "/Volumes/Whole", "/dev/disk4"), "drive_id": "not-listed"}
    left = mod.attach_filesystems("10.0.0.9:9099", items, [data, gone, usb, fusion, bad, whole])
    on = {i["id"]: [f["id"] for f in i["filesystems"]] for i in items}
    # A drive_id the drive list lacks, or one that is not text, falls back
    # to the device path, as for any other filesystem.
    assert on == {"fake-sn-0001": ["fs-d"], "disk4": ["fs-u", "fs-w"]}
    assert [c["id"] for c in left] == ["fs-g", "fs-f", "fs-b"]
    assert items[0]["filesystems"][0] is data


def test_card_ids(mod):
    assert mod.drive_card_key({"serial": " S5GX "}, "nvme0") == "S5GX"
    assert mod.drive_card_key({"serial": ""}, "sdb") == "id:sdb"
    assert mod.card_id("10.0.10.20:9099", "pool:tank") == "10.0.10.20:9099|pool:tank"
    assert mod.split_card_id("10.0.10.20:9099|pool:tank") == ("10.0.10.20:9099", "pool:tank")
    assert mod.split_card_id("10.0.10.20:9099") == ("10.0.10.20:9099", None)



@needs_rules
def test_self_card_ids_and_filesystems(proxy):
    SELF_AGENT.drives = [simple_drive("sda", "NUC-1"), simple_drive("sdb", "")]
    SELF_AGENT.pools = [{"name": "tank", "state": "ONLINE", "problem_vdevs": []}]
    SELF_AGENT.filesystems = [
        _fs("fs-a", "/", "/dev/sda1", pct=82.0), _fs("fs-t", "/tank/media", "tank/media", "zfs"),
        _fs("fs-l", "/data", "/dev/md0", "xfs")]
    status, body = proxy.get("/fleet/hosts/self/drives")
    assert status == 200 and body["filesystems_status"] == "ok" and body["pools_status"] == "ok"
    items = {i["id"]: i for i in body["items"]}
    assert items["sda"]["card_id"] == "self|NUC-1"
    assert items["sdb"]["card_id"] == "self|id:sdb"
    assert items["pool.tank"]["card_id"] == "self|pool:tank"
    assert [f["mountpoint"] for f in items["sda"]["filesystems"]] == ["/"]
    assert [f["id"] for f in items["pool.tank"]["filesystems"]] == ["fs-t"]
    assert [(c["type"], c["card_id"]) for c in body["filesystem_cards"]] == [
        ("filesystem", "self|fs:fs-l")]
    assert {"type", "id", "drive", "verdict", "view", "lab"} <= set(items["sda"])


def _download(proxy, cid: str) -> tuple[int, dict[str, str], Any]:
    req = urllib.request.Request(proxy.base + "/cards/" + urllib.parse.quote(cid, safe="") + "/file")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), json.loads(err.read())


@needs_rules
def test_card_file_for_this_box(proxy):
    SELF_AGENT.drives = [simple_drive("sda", "NUC-1")]
    SELF_AGENT.pools = [{"name": "tank", "state": "ONLINE", "problem_vdevs": []}]
    SELF_AGENT.filesystems = [_fs("fs-a", "/", "/dev/sda1")]
    proxy.get("/fleet/hosts/self/drives")
    status, headers, doc = _download(proxy, "self|NUC-1")
    assert status == 200
    assert headers["Content-Disposition"] == 'attachment; filename="drive-Model-sda.json"'
    assert doc["drive"]["serial"] == "NUC-1" and doc["drive"]["smart_data"]
    assert [f["id"] for f in doc["filesystems"]] == ["fs-a"]
    status, headers, doc = _download(proxy, "self|pool:tank")
    assert status == 200 and doc["pool"]["name"] == "tank"
    assert 'filename="pool-tank.json"' in headers["Content-Disposition"]
    # Only this box's drive and pool cards; never another machine's.
    for cid in ("self|fs:fs-x", "self|NOPE", "self", "nas.lan:9099|S1", "replicas|x"):
        assert _download(proxy, cid)[0] == 404, cid


# ── The integration: what the page shows, and the discovery ─────────────────
#
# smart_sniffer/info answers only on v0.9 and later, loaded. Without it the
# version on disk (manifest/get) and the entries tell the rest.

def _loaded(**info: Any) -> None:
    STATE.info = {"version": "0.9.2", "panel": True, "panel_path": "/smart-sniffer",
                  "haos_entry": True, **info}
    STATE.manifest_version = "0.9.2"


INTEGRATION_CASES = {
    "loaded": (lambda: _loaded(), {"state": "loaded", "panel": True, "haos_entry": True, "create": False}),
    "loaded_no_haos": (lambda: _loaded(haos_entry=False),
                       {"state": "loaded", "panel": True, "haos_entry": False, "create": True}),
    "panel_failed": (lambda: _loaded(panel=False), {"state": "loaded", "panel": False}),
    "refused": (lambda: setattr(STATE, "info", "refused"),
                {"state": "loaded", "panel": True, "haos_entry": None, "create": False}),
    "no_entries": (lambda: setattr(STATE, "manifest_version", "0.9.0-beta.2"),
                   {"state": "no_entries", "version": "0.9.0-beta.2", "haos_entry": False, "create": True}),
    "restart": (lambda: (setattr(STATE, "manifest_version", "0.10.1"),
                         STATE.entries.append(_entry("e1", "SMART Sniffer (nas.lan:9099)"))),
                {"state": "restart", "version": "0.10.1", "create": False}),
    "update": (lambda: (setattr(STATE, "manifest_version", "0.8.5"),
                        STATE.entries.append(_entry("e1", "SMART Sniffer (nas.lan:9099)"))),
               {"state": "update", "version": "0.8.5", "create": False}),
    "update_no_entries": (lambda: setattr(STATE, "manifest_version", "v0.8.4"),
                          {"state": "update", "create": False}),
    "missing": (lambda: None, {"state": "missing", "version": None, "create": False, "hacs": False}),
}


@pytest.mark.parametrize("case", list(INTEGRATION_CASES))
def test_integration_states(proxy, case):
    setup, expected = INTEGRATION_CASES[case]
    setup()
    status, body = proxy.get("/ha/integration")
    assert status == 200 and body["reachable"] is True
    assert {k: body[k] for k in expected} == expected
    assert body["panel_path"] == "/smart-sniffer"
    assert body["integration_url"] == "/config/integrations/integration/smart_sniffer"
    assert set(body["dismissed"]) == {"install", "update"}


def test_integration_panel_path_from_info(proxy):
    _loaded(panel_path="/smart-sniffer-test")
    assert proxy.get("/ha/integration")[1]["panel_path"] == "/smart-sniffer-test"
    # Only a plain sidebar path is taken from it.
    for bad in ("javascript:alert(1)", "//evil.example", "/a/b", "", 7):
        _loaded(panel_path=bad)
        assert proxy.get("/ha/integration")[1]["panel_path"] == "/smart-sniffer", bad


def test_integration_hacs_only_while_missing(proxy):
    STATE.components = ["sensor", "hacs"]
    assert proxy.get("/ha/integration")[1]["hacs"] is True
    STATE.manifest_version = "0.8.5"
    assert proxy.get("/ha/integration")[1]["hacs"] is None


def test_integration_core_unreachable(backends):
    STATE.core_status = 401
    proc, client = _fresh(backends[0], "--ha-cache-seconds=0")
    try:
        _, body = client.get("/ha/integration")
    finally:
        _stop_proxy(proc)
    assert body["reachable"] is False and body["state"] is None and body["create"] is False


def test_integration_keeps_its_last_state_while_core_restarts(backends):
    # Core answers its REST API but not the websocket for a moment: the
    # page keeps the last state it had, and the discovery stays.
    _loaded()
    proc, client = _fresh(backends[0], "--ha-cache-seconds=0")
    try:
        assert client.get("/ha/integration")[1]["state"] == "loaded"
        STATE.ws_mode = "drop"
        _, body = client.get("/ha/integration")
        assert body["state"] == "loaded" and body["reachable"] is True
        assert len(STATE.discovery) == 1
    finally:
        _stop_proxy(proc)


def test_integration_manifest_fallback(mod):
    # manifest/get refused for another reason: the older installed check
    # decides between update and missing.
    class Sv(mod.Supervisor):
        def __init__(self, entries, components):
            super().__init__("http://supervisor", "token")
            self._e, self._c = entries, components

        def ws(self, command):
            if command["type"] == "smart_sniffer/info":
                raise mod.WebSocketError("x", "Unknown command.", "unknown_command")
            raise mod.WebSocketError("x", "boom", "home_assistant_error")

        def entries(self):
            return self._e

        def core(self, method, path, body=None):
            if path == "/config":
                return 200, {"components": self._c}
            if path == "/config/config_entries/flow_handlers":
                return 200, []
            return 500, None

    assert mod.detect_integration(Sv([], ["smart_sniffer"]))["state"] == "update"
    assert mod.detect_integration(Sv([], ["sensor"]))["state"] == "missing"
    assert mod.detect_integration(Sv(None, []))["state"] is None  # entries unknown


def test_integration_unknown_when_websocket_fails(mod):
    class Sv(mod.Supervisor):
        def ws(self, command):
            raise mod.WebSocketError("could not connect")  # no code: no answer at all

    assert mod.detect_integration(Sv("http://supervisor", "token"))["state"] is None
    assert mod.detect_integration(mod.Supervisor("http://supervisor", ""))["state"] is None


@pytest.mark.parametrize("text,expected", [
    ("0.9.0", True), ("0.9.2", True), ("v0.9.0-beta.1", True), ("0.10", True), ("1.0.0", True),
    ("0.8.5", False), ("0.8", False), ("v0.8.4", False), ("", False), (None, False), ("dev", False),
])
def test_panel_capable(mod, text, expected):
    assert mod.panel_capable(text) is expected


# The discovery: posted only while integration v0.9 or later is installed,
# with the agent's host and port only, and replaced at every start.

def _discovery() -> list[dict[str, Any]]:
    return list(STATE.discovery.values())


def _posts() -> list[Any]:
    return [c[2] for c in STATE.calls if c[0] == "POST" and c[1] == "/discovery"]


def _deletes() -> list[str]:
    return [c[1] for c in STATE.calls if c[0] == "DELETE" and c[1].startswith("/discovery/")]


@pytest.mark.parametrize("case,posted", [
    ("loaded", True), ("loaded_no_haos", True), ("panel_failed", True), ("refused", True),
    ("no_entries", True), ("restart", True),
    ("update", False), ("update_no_entries", False), ("missing", False),
])
def test_discovery_only_for_v09(backends, tmp_path, case, posted):
    INTEGRATION_CASES[case][0]()
    proc, client = _fresh(backends[0], "--agent-port=9099", "--ha-cache-seconds=0",
                          data_dir=str(tmp_path))
    try:
        _wait_for(lambda: any(c[0] == "WS" for c in STATE.calls))
        client.get("/ha/integration")
        time.sleep(0.2)
    finally:
        output = _stop_proxy(proc)
    if posted:
        assert _discovery() == [{"service": "smart_sniffer",
                                 "config": {"host": AGENT_HOST, "port": 9099}}]
        assert len(_posts()) == 1  # once, not on every lookup
        assert json.loads((tmp_path / "app-state.json").read_text())["discovery_uuid"] == "uuid-1"
    else:
        assert _discovery() == [] and _posts() == []
    assert TOKEN not in output


def test_discovery_never_carries_the_token(backends, tmp_path):
    _loaded()
    proc, client = _fresh(backends[0], "--agent-port=9099", "--ha-cache-seconds=0",
                          data_dir=str(tmp_path), agent_token=APP_TOKEN)
    try:
        _wait_for(lambda: _posts())
    finally:
        output = _stop_proxy(proc)
    assert _posts() == [{"service": "smart_sniffer", "config": {"host": AGENT_HOST, "port": 9099}}]
    assert APP_TOKEN not in json.dumps(STATE.calls) and APP_TOKEN not in output


def test_discovery_replaced_at_every_start(backends, tmp_path):
    # The previous run's message is deleted first and a new one posted, so
    # a new uuid always reaches Core.
    _loaded()
    for run in (1, 2, 3):
        proc, _ = _fresh(backends[0], "--agent-port=9099", "--ha-cache-seconds=0",
                         data_dir=str(tmp_path))
        try:
            _wait_for(lambda: len(_posts()) == run)
        finally:
            _stop_proxy(proc)
        assert list(STATE.discovery) == [f"uuid-{run}"]
    assert _deletes() == ["/discovery/uuid-1", "/discovery/uuid-2"]
    # Each delete came before that run's post.
    order = [c[0] + c[1] for c in STATE.calls if c[1].startswith("/discovery")]
    assert order == ["POST/discovery", "DELETE/discovery/uuid-1", "POST/discovery",
                     "DELETE/discovery/uuid-2", "POST/discovery"]


def test_discovery_old_one_gone_already(backends, tmp_path):
    # The Supervisor no longer has the stored message (removed on its own):
    # that counts as deleted, and a new one is posted.
    (tmp_path / "app-state.json").write_text(json.dumps({"discovery_uuid": "uuid-old"}))
    _loaded()
    proc, _ = _fresh(backends[0], "--agent-port=9099", data_dir=str(tmp_path))
    try:
        _wait_for(lambda: _posts())
    finally:
        _stop_proxy(proc)
    assert _deletes() == ["/discovery/uuid-old"] and list(STATE.discovery) == ["uuid-1"]


def test_discovery_withdrawn_when_the_integration_goes(backends, tmp_path):
    # v0.9 installed, then removed (or rolled back to 0.8.x): the message
    # goes; installed again: a new one.
    _loaded()
    proc, client = _fresh(backends[0], "--agent-port=9099", "--ha-cache-seconds=0",
                          data_dir=str(tmp_path))
    try:
        _wait_for(lambda: _posts())
        STATE.info, STATE.manifest_version = None, "0.8.5"
        assert client.get("/ha/integration")[1]["state"] == "update"
        assert STATE.discovery == {}
        assert json.loads((tmp_path / "app-state.json").read_text())["discovery_uuid"] is None
        client.get("/ha/integration")
        assert len(_deletes()) == 1  # not asked again
        _loaded()
        assert client.get("/ha/integration")[1]["state"] == "loaded"
        assert list(STATE.discovery) == ["uuid-2"]
    finally:
        _stop_proxy(proc)


def test_discovery_waits_while_home_assistant_is_down(backends, tmp_path):
    # Core not answering at start: the old message is still replaced, but
    # nothing new is posted until the integration's state is known; an
    # unknown state never withdraws one either.
    (tmp_path / "app-state.json").write_text(json.dumps({"discovery_uuid": "uuid-old"}))
    STATE.discovery["uuid-old"] = {"service": "smart_sniffer", "config": {}}
    STATE.core_status = 401
    _loaded()
    proc, client = _fresh(backends[0], "--agent-port=9099", "--ha-cache-seconds=0",
                          data_dir=str(tmp_path))
    try:
        _wait_for(lambda: _deletes())
        time.sleep(0.3)
        assert STATE.discovery == {} and _posts() == []
        STATE.core_status = 200
        assert client.get("/ha/integration")[1]["state"] == "loaded"
        assert list(STATE.discovery) == ["uuid-1"]
        STATE.core_status = 401
        client.get("/ha/integration")
        assert list(STATE.discovery) == ["uuid-1"]
    finally:
        _stop_proxy(proc)


def test_discovery_refused_is_tried_again(backends, tmp_path):
    _loaded()
    STATE.discovery_status = 400
    proc, client = _fresh(backends[0], "--agent-port=9099", "--ha-cache-seconds=0",
                          data_dir=str(tmp_path))
    try:
        _wait_for(lambda: any(c[1] == "/discovery" for c in STATE.calls))
        assert STATE.discovery == {}
        STATE.discovery_status = 200
        client.get("/ha/integration")
        assert list(STATE.discovery) == ["uuid-1"]
    finally:
        output = _stop_proxy(proc)
    assert "did not take the discovery" in output


def test_discovery_unit(mod, tmp_path):
    """Discovery.sync without a proxy: the start, the states, unknown."""
    calls: list[tuple[str, Any]] = []

    class Sv(mod.Supervisor):
        def discovery_post(self, config):
            calls.append(("post", config))
            return f"u{len(calls)}"

        def discovery_delete(self, uuid):
            calls.append(("delete", uuid))
            return True

    state = mod.AppState(str(tmp_path / "s.json"))
    state.set("discovery_uuid", "u-prev")
    d = mod.Discovery(Sv("http://supervisor", "token"), state, "local-smart-sniffer-agent", 9099)
    d.sync(None)
    assert calls == [("delete", "u-prev")] and state.get("discovery_uuid") is None
    d.sync("missing")
    d.sync("update")
    assert len(calls) == 1
    d.sync("restart")
    d.sync("loaded")
    assert calls[1:] == [("post", {"host": "local-smart-sniffer-agent", "port": 9099})]
    d.sync(None)
    d.sync("missing")
    assert calls[2] == ("delete", "u2") and state.get("discovery_uuid") is None
    # No Supervisor token: nothing at all.
    quiet = mod.Discovery(mod.Supervisor("http://supervisor", ""), mod.AppState(None), "h", 1)
    quiet.sync("loaded")


# ── /ha/add ─────────────────────────────────────────────────────────────────

def _flow_posts() -> list[tuple[str, str, Any]]:
    return [c for c in STATE.calls
            if c[1].startswith("/core/api/config/config_entries/flow") and c[0] == "POST"]


def test_ha_add_self(proxy):
    _loaded(haos_entry=False)
    assert proxy.get("/ha/integration")[1]["create"] is True
    STATE.flow_step = {"type": "create_entry", "result": {"entry_id": "new-self"}}
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 200 and body == {"result": "created", "entry_id": "new-self"}
    assert _flow_posts() == [
        ("POST", "/core/api/config/config_entries/flow",
         {"handler": "smart_sniffer", "show_advanced_options": False}),
        ("POST", "/core/api/config/config_entries/flow/flow123",
         {"host": AGENT_HOST, "port": proxy.agent_port, "token": APP_TOKEN, "scan_interval": 60}),
    ]
    # The lookup is read again: HAOS is a machine now, the line goes.
    _, after = proxy.get("/ha/integration")
    assert after["haos_entry"] is True and after["create"] is False


def test_ha_add_already_added(proxy):
    _loaded(haos_entry=True)
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 200 and body == {"result": "already_added", "entry_id": None}
    assert _flow_posts() == []
    STATE.info = None
    STATE.flow_step = {"type": "abort", "reason": "already_configured"}
    status, body = proxy.post("/ha/add", {"target": "self"})
    assert status == 200 and body["result"] == "already_added"


def test_ha_add_cannot_connect(proxy):
    STATE.flow_step = {"type": "form", "step_id": "user", "errors": {"base": "cannot_connect"}}
    status, body = proxy.post("/ha/add", {"target": "self"})
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


def test_ha_add_only_this_box(proxy):
    for target in ("replicas", "nas.lan:9099", "nas", None):
        status, body = proxy.post("/ha/add", {"target": target})
        assert status == 400 and body["error"] == "bad_target"
    assert _flow_posts() == []


# ── The install and update cards: closed for every browser ──────────────────

def _app_state(data_dir) -> dict[str, Any]:
    path = data_dir / "app-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def test_install_card_dismiss_persists_and_resets(proxy, data_dir):
    _, body = proxy.get("/ha/integration")
    assert body["state"] == "missing" and body["dismissed"] == {"install": False, "update": False}
    status, answer = proxy.post("/app/notice/dismiss", {"notice": "install"})
    assert status == 200 and answer["notice"] == "install" and answer["dismissed"] is True
    assert _app_state(data_dir)["integration_notice_dismissed"] is True  # in /data, for every browser
    assert proxy.get("/ha/integration")[1]["dismissed"]["install"] is True
    assert proxy.request("GET", "/app/notice/dismiss")[0] == 405
    # Seen installed: the flag goes, so a later removal shows the card again.
    STATE.manifest_version = "0.8.5"
    assert proxy.get("/ha/integration")[1]["dismissed"]["install"] is False
    STATE.manifest_version = None
    assert proxy.get("/ha/integration")[1]["dismissed"]["install"] is False


def test_update_card_has_its_own_key(proxy, data_dir):
    # Closing the install card never hides the update card, and the other
    # way round.
    proxy.post("/app/notice/dismiss", {"notice": "install"})
    STATE.manifest_version = "0.8.5"
    _, body = proxy.get("/ha/integration")
    assert body["state"] == "update" and body["dismissed"] == {"install": False, "update": False}
    proxy.post("/app/notice/dismiss", {"notice": "update"})
    assert proxy.get("/ha/integration")[1]["dismissed"] == {"install": False, "update": True}
    # An empty body is the install card, as 0.3.x sent it.
    status, answer = proxy.request("POST", "/app/notice/dismiss")
    assert status == 200 and answer["notice"] == "install"
    assert proxy.post("/app/notice/dismiss", {"notice": "other"})[0] == 400
    # Seen at 0.9: the update flag goes.
    _loaded()
    assert proxy.get("/ha/integration")[1]["dismissed"]["update"] is False
    assert _app_state(data_dir)["integration_update_dismissed"] is False


def test_app_state_file(mod, tmp_path):
    path = tmp_path / "app-state.json"
    state = mod.AppState(str(path))
    assert state.get("x") is None
    state.set("x", True)
    assert json.loads(path.read_text()) == {"x": True}
    assert mod.AppState(str(path)).get("x") is True
    path.write_text("not json")
    assert mod.AppState(str(path)).get("x") is None  # unreadable: start empty
    memory = mod.AppState(None)
    memory.set("x", 1)
    assert memory.get("x") == 1


# ── The websocket client, against the fake Core websocket ──────────────────

def _ws_url(backends) -> str:
    return f"ws://127.0.0.1:{backends[0].server_address[1]}/core/websocket"


def test_ws_client_info_command(mod, backends):
    _loaded()
    reply = mod.ha_ws_command(_ws_url(backends), TOKEN, {"type": "smart_sniffer/info"})
    assert reply["success"] is True and reply["result"]["version"] == "0.9.2"
    assert STATE.ws_commands == [{"type": "smart_sniffer/info", "id": 1}]
    # The ping before auth was answered with its payload; the result came in
    # two frames after another message; the client closed with a close frame.
    assert STATE.ws_pongs == [b"hello"]
    assert STATE.ws_closed_cleanly == [True]


def test_ws_client_error_code(mod, backends):
    with pytest.raises(mod.WebSocketError) as err:
        mod.ha_ws_command(_ws_url(backends), TOKEN, {"type": "smart_sniffer/info"})
    assert err.value.code == "unknown_command" and err.value.reason == "Unknown command."


@pytest.mark.parametrize("size", [10, 300, 70000])
def test_ws_client_frame_lengths(mod, backends, size):
    """Payloads under 126 bytes, up to 65535 (16-bit length) and above
    (64-bit length), both ways."""
    data = "x" * size
    reply = mod.ha_ws_command(_ws_url(backends), TOKEN, {"type": "test/echo", "data": data})
    assert reply["result"] == data and STATE.ws_commands[0]["data"] == data


@pytest.mark.parametrize("mode,words,code", [
    ("refuse", "Device not found", "not_found"), ("auth", "refused the auth", None),
    ("down", "upgrade was refused", None), ("drop", "", None),
])
def test_ws_client_failures(mod, backends, mode, words, code):
    STATE.ws_mode = mode
    with pytest.raises(mod.WebSocketError) as err:
        mod.ha_ws_command(_ws_url(backends), TOKEN, {"type": "smart_sniffer/info"})
    assert words in str(err.value)
    assert err.value.code == code
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


# ── Home Assistant slow: the page's reads do not pile up on it ──────────────

CORE_DELAY = 0.4


class SlowCoreSupervisor(FakeSupervisor):
    def _dispatch(self, method: str) -> None:
        if self.path.startswith("/core/"):
            time.sleep(CORE_DELAY)
        super()._dispatch(method)


@pytest.fixture
def slow_core(backends):
    _supervisor, agent = backends
    slow = _serve(SlowCoreSupervisor)
    proc, client = _start_proxy(_proxy_args(slow, agent.server_address[1], "",
                                            "--ha-cache-seconds=30"), TOKEN, APP_TOKEN)
    yield client
    _stop_proxy(proc)
    slow.shutdown()


@needs_rules
def test_drive_read_does_not_wait_for_home_assistant(slow_core):
    start = time.monotonic()
    status, body = slow_core.get("/fleet/hosts/self/drives")
    assert status == 200 and body["status"] == "ok"
    assert time.monotonic() - start < CORE_DELAY


def test_integration_lookup_single_flight(slow_core):
    _loaded()
    time.sleep(3 * CORE_DELAY + 0.5)  # the start's own lookup
    STATE.calls.clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(slow_core.get, ["/ha/integration"] * 4))
    assert all(status == 200 and body["state"] == "loaded" for status, body in answers)
    # Four callers within the cache time: no lookup at all after the first.
    assert sum(1 for c in STATE.calls if c[0] == "WS") <= 1


# ── The handover of the field machines to the integration ───────────────────

HANDOVER_OPTIONS = {
    "hosts": [
        {"address": "NAS.lan", "port": 9099, "password": FIELD_PASSWORD, "removed": ["X"]},
        {"address": "10.0.10.250", "port": 9099, "hidden": True},
    ],
    "card_order": ["nas.lan:9099|S1", "self|SER-A", "replicas|R1", "10.0.10.250:9099|Q1"],
    "hidden_cards": ["nas.lan:9099|S2", "self|SER-B", "replicas|R2"],
    "thresholds": [{"card": "nas.lan:9099|S1", "label": "Command Timeout", "value": 5},
                   {"card": "self|SER-A", "label": "Temperature", "value": 60}],
    "card_names": [{"card": "nas.lan:9099|S1", "name": "Big disk"},
                   {"card": "self|SER-A", "name": "Boot"},
                   {"card": "gone.lan:9099|Z1", "name": "Orphan"}],
    "host_names": [{"host": "nas.lan:9099", "name": "NAS"}, {"host": "replicas", "name": "Lab"}],
}
INFO_V09 = {"version": "0.9.0", "panel": True, "panel_path": "/smart-sniffer",
            "haos_entry": True, "app_import": 1}


def _handover(mod, backends, data_dir, monkeypatch):
    monkeypatch.setattr(mod, "HANDOVER_POLL_SECONDS", 0.01)
    sv = mod.Supervisor(f"http://127.0.0.1:{backends[0].server_address[1]}", TOKEN)
    state = mod.AppState(str(Path(data_dir) / "app-state.json"))
    watch = mod.IntegrationWatch(sv, None, 0)
    return mod.Handover(sv, state, watch, str(data_dir))


def _imports() -> list[dict[str, Any]]:
    return [c for c in STATE.ws_commands if c.get("type") == "smart_sniffer/app/import"]


def test_handover_items_from_the_options(mod):
    cache = {"nas.lan:9099": {"response": {"agent": {"hostname": "nas-01"}}}}
    items = mod.build_handover(copy.deepcopy(HANDOVER_OPTIONS), cache)
    by_id = {i["item_id"]: i for i in items}
    nas = by_id[hashlib.sha256(b"nas.lan:9099").hexdigest()]
    assert nas == {"item_id": nas["item_id"], "kind": "field", "address": "NAS.lan", "port": 9099,
                   "password": FIELD_PASSWORD, "hidden": False, "removed": ["X"],
                   "name": "NAS", "hostname": "nas-01",
                   "cards": {"S1": {"name": "Big disk",
                                    "thresholds": {"Command Timeout": 5}},
                             "S2": {"hidden": True}}}
    old = by_id[hashlib.sha256(b"10.0.10.250:9099").hexdigest()]
    assert old["hidden"] is True and old["password"] == "" and old["cards"] == {}
    assert by_id["self"] == {"item_id": "self", "kind": "self", "cards": {
        "SER-A": {"name": "Boot", "thresholds": {"Temperature": 60}}, "SER-B": {"hidden": True}}}
    assert by_id["card_order"]["order"] == ["nas.lan:9099|S1", "self|SER-A", "10.0.10.250:9099|Q1"]
    assert by_id["replicas"] == {"item_id": "replicas", "kind": "replicas"}
    assert "replicas|" not in json.dumps(items)
    # Sent and kept here: this box's settings and the order go once.
    again = mod.build_handover(copy.deepcopy(HANDOVER_OPTIONS), cache, ["self", "card_order"])
    assert {i["item_id"] for i in again} == set(by_id) - {"self", "card_order"}
    assert mod.build_handover({"hosts": [], "card_order": []}) == []


def test_clear_only_what_the_integration_took(mod):
    options = {"port": 9099, "token": "t", **copy.deepcopy(HANDOVER_OPTIONS)}
    one = mod.clear_handed_over(options, {"nas.lan:9099"}, drop_replicas=False)
    assert one["hosts"] == [HANDOVER_OPTIONS["hosts"][1]]
    assert one["card_order"] == ["self|SER-A", "replicas|R1", "10.0.10.250:9099|Q1"]
    assert one["hidden_cards"] == ["self|SER-B", "replicas|R2"]
    assert one["thresholds"] == [HANDOVER_OPTIONS["thresholds"][1]]
    # A machine still here keeps the orphans for now.
    assert [n["card"] for n in one["card_names"]] == ["self|SER-A", "gone.lan:9099|Z1"]
    assert one["host_names"] == [{"host": "replicas", "name": "Lab"}]
    assert one["port"] == 9099 and one["token"] == "t"
    every = mod.clear_handed_over(one, {"10.0.10.250:9099"}, drop_replicas=True)
    assert every["hosts"] == [] and every["host_names"] == []
    assert every["card_order"] == ["self|SER-A"] and every["hidden_cards"] == ["self|SER-B"]
    assert every["card_names"] == [{"card": "self|SER-A", "name": "Boot"}]
    assert every["thresholds"] == [HANDOVER_OPTIONS["thresholds"][1]]
    assert FIELD_PASSWORD not in json.dumps(every)


@pytest.mark.parametrize("info,ready", [
    (None, False), ({"version": "0.9.0", "panel": True}, False),
    ({"version": "0.8.5", "app_import": 1}, False), (INFO_V09, True),
    ({**INFO_V09, "version": "0.10.1"}, True), ({**INFO_V09, "app_import": True}, False)])
def test_handover_only_to_an_integration_that_takes_it(mod, backends, tmp_path, monkeypatch,
                                                       info, ready):
    STATE.info = copy.deepcopy(info)
    STATE.manifest_version = "0.8.5"
    STATE.options = {**STATE.options, **copy.deepcopy(HANDOVER_OPTIONS)}
    handover = _handover(mod, backends, tmp_path, monkeypatch)
    assert handover.ready(handover.watch.get()) is ready
    handover.step()
    assert bool(_imports()) is ready
    if not ready:
        assert STATE.options["hosts"] == HANDOVER_OPTIONS["hosts"] and _option_writes() == []


def test_handover_clears_what_was_taken_and_keeps_this_box(mod, backends, tmp_path, monkeypatch):
    STATE.info = copy.deepcopy(INFO_V09)
    STATE.options = {**STATE.options, **copy.deepcopy(HANDOVER_OPTIONS)}
    (tmp_path / "fleet-cache.json").write_text(json.dumps({"version": 1, "hosts": {}}))
    handover = _handover(mod, backends, tmp_path, monkeypatch)
    assert handover.run_once() == "done"
    assert STATE.import_created == ["NAS.lan", "10.0.10.250"]
    assert STATE.options["hosts"] == [] and STATE.options["host_names"] == []
    assert STATE.options["card_order"] == ["self|SER-A"]
    assert STATE.options["hidden_cards"] == ["self|SER-B"]
    assert STATE.options["card_names"] == [{"card": "self|SER-A", "name": "Boot"}]
    assert STATE.options["thresholds"] == [HANDOVER_OPTIONS["thresholds"][1]]
    assert STATE.options["port"] == 9099
    assert not (tmp_path / "fleet-cache.json").exists()
    note = json.loads((tmp_path / "migration.json").read_text())
    assert {v["outcome"] for v in note["items"].values()} == {"created_offline", "applied",
                                                               "replicas_dropped"}
    for path in tmp_path.iterdir():
        assert FIELD_PASSWORD not in path.read_text()
    # The password went to the integration, once, in the import command only.
    assert FIELD_PASSWORD in json.dumps(_imports()[0])
    # Nothing is left: nothing is sent again.
    assert handover.run_once() == "nothing" and len(_imports()) == 1


def test_handover_partial_then_retried(mod, backends, tmp_path, monkeypatch):
    STATE.info = copy.deepcopy(INFO_V09)
    STATE.options = {**STATE.options, **copy.deepcopy(HANDOVER_OPTIONS)}
    STATE.import_skip = {"10.0.10.250"}
    handover = _handover(mod, backends, tmp_path, monkeypatch)
    assert handover.run_once() == "partial"
    assert STATE.options["hosts"] == [HANDOVER_OPTIONS["hosts"][1]]
    assert "10.0.10.250:9099|Q1" in STATE.options["card_order"]
    handover.watch.get()
    assert handover.step() == mod.HANDOVER_RETRY_SECONDS  # still skipped: later
    STATE.import_skip = set()
    assert handover.run_once() == "done"
    assert STATE.import_created == ["NAS.lan", "10.0.10.250"]  # each made once
    assert STATE.options["hosts"] == []
    last = _imports()[-1]
    assert [i["kind"] for i in last["items"]] == ["field"]  # only what was left


def test_handover_lost_answer_and_lost_polls(mod, backends, tmp_path, monkeypatch):
    STATE.info = copy.deepcopy(INFO_V09)
    STATE.options = {**STATE.options, **copy.deepcopy(HANDOVER_OPTIONS)}
    handover = _handover(mod, backends, tmp_path, monkeypatch)
    handover.watch.get()  # the watch's lookup, as at start
    STATE.import_lost = 1  # the integration did it, the answer never came
    assert handover.run_once() == "lost"
    assert STATE.options["hosts"] == HANDOVER_OPTIONS["hosts"] and _option_writes() == []
    assert handover.step() == mod.HANDOVER_RETRY_SECONDS
    # step() sent it again: the ledger answered "already"; nothing made twice.
    assert STATE.import_created == ["NAS.lan", "10.0.10.250"]
    assert STATE.options["hosts"] == []
    # Lost polls on a later handover: the job goes on and is read again.
    STATE.options["hosts"] = [{"address": "10.0.10.9", "port": 9099}]
    STATE.poll_lost = 2
    assert handover.run_once() == "done" and STATE.options["hosts"] == []
    # Home Assistant restarted mid-job: tried again later, nothing cleared.
    STATE.options["hosts"] = [{"address": "10.0.10.8", "port": 9099}]
    STATE.import_restart = True
    assert handover.run_once() == "lost" and STATE.options["hosts"] != []
    STATE.import_restart = False
    assert handover.run_once() == "done" and STATE.import_created.count("10.0.10.8") == 1


def test_handover_runs_at_start_without_logging_a_password(backends, tmp_path):
    STATE.info = copy.deepcopy(INFO_V09)
    STATE.options = {**STATE.options, **copy.deepcopy(HANDOVER_OPTIONS)}
    proc, client = _fresh(backends[0], data_dir=str(tmp_path))
    try:
        _wait_for(lambda: STATE.options.get("hosts") == [], timeout=15)
        # A manual retry is taken; nothing is left to send.
        status, body = client.post("/app/handover")
        assert status == 202 and body == {"queued": True}
        time.sleep(0.5)
    finally:
        output = _stop_proxy(proc)
    assert len(_imports()) == 1
    assert FIELD_PASSWORD not in output and "Handover to the integration: 2 field machines" in output
    assert STATE.options["card_names"] == [{"card": "self|SER-A", "name": "Boot"}]


def test_no_handover_without_the_integration(backends, tmp_path):
    STATE.options = {**STATE.options, **copy.deepcopy(HANDOVER_OPTIONS)}
    STATE.manifest_version = "0.8.5"  # an older integration
    proc, client = _fresh(backends[0], data_dir=str(tmp_path))
    try:
        _wait_for(lambda: any(c.get("type") == "manifest/get" for c in STATE.ws_commands))
        client.post("/app/handover")
        time.sleep(0.5)
    finally:
        _stop_proxy(proc)
    assert _imports() == [] and STATE.options["hosts"] == HANDOVER_OPTIONS["hosts"]
