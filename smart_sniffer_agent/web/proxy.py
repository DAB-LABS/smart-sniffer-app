#!/usr/bin/env python3
"""SMART Sniffer App -- Web UI Proxy Server.

Serves static files from /opt/web/ at the root path and proxies:
  /api/*  -> localhost:$AGENT_PORT  (real Go agent)
  /mock/* -> localhost:$MOCK_PORT   (Python mock agent, /mock/ rewritten to /api/)

It also answers the Control Center's own routes:
  GET  /app/info     app version, rules version, ports, Test Lab flag, the
                     app's identity (hostname, slug, mDNS name) and the Home
                     Assistant version, which picks the page's link paths
  POST /evaluate     the integration's Attention verdict for one drive payload
  POST /lab/on       turn the Test Lab on (sets mock_mode, restarts the app)
  POST /lab/off      turn the Test Lab off (same, mock_mode false)
  GET  /ha/status    is Home Assistant reachable, is the integration there,
                     which of this app's agents have a config entry
  POST /ha/connect   add the integration entry for the agent or the Test Lab
  POST /ha/disconnect  remove the Test Lab entry (never the real agent's)
  GET  /ha/verdict   the live Attention Needed state Home Assistant shows

/evaluate runs the integration's own rule modules (attention.py, devstat.py),
which the Dockerfile downloads from the smart-sniffer release this app is
built against. They import nothing from Home Assistant.

The /lab and /ha routes talk to the Supervisor (and, through it, to Home
Assistant Core) with the SUPERVISOR_TOKEN the Supervisor puts in the app's
environment. The token is never logged and never sent to the browser.

Runs on the ingress port (8099) so HA can embed the UI in the sidebar.
All requests from the web UI go to the same origin, so there is no CORS.
Python standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import importlib
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
from urllib.parse import parse_qs, quote, urlparse

# Ensure .js and .css MIME types are correct
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# Directory containing static web assets
STATIC_DIR = "/opt/web"

# Where the Dockerfile puts the integration's rule modules: this directory
# holds smart_sniffer/{attention,devstat,extract}.py, and its parent holds
# VERSION (the smart-sniffer release tag they came from).
DEFAULT_RULES_DIR = "/opt/ssrules/custom_components"

# The hostname Home Assistant uses to reach this app's agents from the
# integration. run.sh passes the container's real hostname with --agent-host
# (it follows the slug, so a local install is local-smart-sniffer-agent); this
# default, the published app's hostname, is only for running the proxy by hand.
DEFAULT_AGENT_HOST = "0449a086-smart-sniffer-agent"

INTEGRATION_DOMAIN = "smart_sniffer"
SUPERVISOR_TIMEOUT = 5  # seconds, every Supervisor and Core call
MAX_BODY = 4 * 1024 * 1024  # largest request body the app routes accept
RESTART_DELAY = 0.5  # seconds between answering /lab/* and asking for the restart
HA_API_CACHE_SECONDS = 30

# Matches the titles the integration's config flow gives its entries:
#   user step:     "SMART Sniffer (<host>:<port>)"
#   zeroconf step: "SMART Sniffer (<hostname>)"
_TITLE_RE = re.compile(r"^SMART Sniffer \((?P<inner>.+)\)$")


def log(message: str) -> None:
    print(f"[webui] {message}", flush=True)


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def slugify(text: str) -> str:
    """Home Assistant's slugify for ASCII text: lower case, every run of
    characters other than a-z and 0-9 becomes one underscore, no leading or
    trailing underscore."""
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


# ── Integration rules ───────────────────────────────────────────────────────

class Rules:
    """The integration's attention and devstat modules, loaded once.

    The modules use relative imports (attention imports devstat), so they are
    loaded as members of a synthetic package custom_components.smart_sniffer
    whose __path__ is the rules directory. The integration's own __init__.py
    (which imports Home Assistant) is never present and never executed.
    """

    def __init__(self, rules_dir: str) -> None:
        self.rules_dir = rules_dir
        self.attention: ModuleType | None = None
        self.devstat: ModuleType | None = None
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

    def load(self) -> bool:
        """Import the modules on first use. True when /evaluate can run."""
        with self._lock:
            if self._tried:
                return self.attention is not None
            self._tried = True
            package_dir = os.path.join(self.rules_dir, "smart_sniffer")
            if not os.path.isfile(os.path.join(package_dir, "attention.py")):
                log(f"Integration rules not found in {package_dir}; /evaluate is off")
                return False
            try:
                if "custom_components" not in sys.modules:
                    parent = ModuleType("custom_components")
                    parent.__path__ = [self.rules_dir]
                    sys.modules["custom_components"] = parent
                if "custom_components.smart_sniffer" not in sys.modules:
                    package = ModuleType("custom_components.smart_sniffer")
                    package.__path__ = [package_dir]
                    sys.modules["custom_components.smart_sniffer"] = package
                attention = importlib.import_module(
                    "custom_components.smart_sniffer.attention"
                )
                for name in ("evaluate_attention", "current_readings", "compose_reasons_text"):
                    if not callable(getattr(attention, name, None)):
                        raise ImportError(f"attention.{name} is missing")
                self.attention = attention
                # devstat.py arrived with integration v0.8.0. Without it the
                # verdict is computed with no Device Statistics merge.
                if os.path.isfile(os.path.join(package_dir, "devstat.py")):
                    devstat = importlib.import_module(
                        "custom_components.smart_sniffer.devstat"
                    )
                    if callable(getattr(devstat, "merge_devstat", None)) and hasattr(
                        devstat, "DEVSTAT_KEY"
                    ):
                        self.devstat = devstat
            except Exception as err:  # noqa: BLE001 -- any failure means no rules
                log(f"Could not load the integration rules: {type(err).__name__}: {err}")
                self.attention = None
                self.devstat = None
                return False
            log(f"Integration rules loaded ({self.version or 'unknown version'})")
            return True

    def evaluate(self, drive: dict[str, Any]) -> dict[str, Any]:
        """The verdict the integration would give this drive, at default
        thresholds and with no held Device Statistics readings."""
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
        self, method: str, path: str, body: Any = None
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
            log(f"Supervisor {method} {path}: HTTP {status}")
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            reason = getattr(err, "reason", err)
            log(f"Supervisor {method} {path}: {type(err).__name__}: {reason}")
            return None, None
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
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


# ── Request handler ─────────────────────────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    """Serves static files, proxies API requests, answers the app routes."""

    agent_port: int = 9099
    mock_port: int = 9100
    token: str = ""
    app_version: str | None = None
    mock_enabled: bool = False
    agent_host: str = DEFAULT_AGENT_HOST
    mdns_name: str | None = None
    rules: Rules = Rules(DEFAULT_RULES_DIR)
    supervisor: Supervisor = Supervisor("http://supervisor", "")

    def log_message(self, format, *args):
        """Send access logs to stdout for HA log viewer."""
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

    # Routes answered by the proxy itself: path -> (method, handler name)
    APP_ROUTES = {
        "/app/info": ("GET", "_route_app_info"),
        "/evaluate": ("POST", "_route_evaluate"),
        "/lab/on": ("POST", "_route_lab_on"),
        "/lab/off": ("POST", "_route_lab_off"),
        "/ha/status": ("GET", "_route_ha_status"),
        "/ha/connect": ("POST", "_route_ha_connect"),
        "/ha/disconnect": ("POST", "_route_ha_disconnect"),
        "/ha/verdict": ("GET", "_route_ha_verdict"),
    }

    def _handle_request(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path

        route = self.APP_ROUTES.get(path.rstrip("/") or "/")
        if route is not None:
            allowed, handler_name = route
            if method != allowed:
                self._send_json(405, {"error": f"use {allowed}"})
                return
            try:
                getattr(self, handler_name)(parsed)
            except Exception as err:  # noqa: BLE001 -- answer, never crash the server
                log(f"{path} failed: {type(err).__name__}: {err}")
                self._send_json(500, {"error": "internal error"})
            return

        # Proxy /api/* to the real Go agent
        if path.startswith("/api/"):
            self._proxy_request(method, "127.0.0.1", self.agent_port, self.path)
            return

        # Proxy /mock/* to the Python mock agent
        # Rewrite /mock/ -> /api/ because mock agent serves the same /api/* routes
        if path.startswith("/mock/"):
            rewritten = self.path.replace("/mock/", "/api/", 1)
            self._proxy_request(method, "127.0.0.1", self.mock_port, rewritten)
            return

        # Serve static files (GET only for static)
        if method != "GET":
            self.send_error(405, "Method Not Allowed")
            return

        self._serve_static(path)

    # ── helpers ──

    def _send_json(self, code: int, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> tuple[Any, str | None]:
        """Parse the request body. Returns (value, error message)."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return None, "bad Content-Length"
        if length > MAX_BODY:
            return None, "body too large"
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return None, "empty body"
        try:
            return json.loads(raw), None
        except ValueError:
            return None, "body is not JSON"

    def _target_port(self, target: str) -> int | None:
        if target == "agent":
            return self.agent_port
        if target == "testlab":
            return self.mock_port
        return None

    # ── /app/info ──

    def _route_app_info(self, _parsed) -> None:
        ha_api = self.supervisor.available and self.supervisor.core_reachable_cached()
        self._send_json(200, {
            "app_version": self.app_version,
            "rules_version": self.rules.version,
            "agent_port": self.agent_port,
            "mock_port": self.mock_port,
            "mock_enabled": self.mock_enabled,
            "ha_api": ha_api,
            "hostname": self.agent_host,
            "slug": self.supervisor.app_slug(),
            "mdns_name": self.mdns_name,
            "ha_version": self.supervisor.ha_version(),
        })

    # ── /evaluate ──

    def _route_evaluate(self, _parsed) -> None:
        drive, error = self._read_json()
        if error:
            self._send_json(400, {"error": error})
            return
        if not isinstance(drive, dict):
            self._send_json(400, {"error": "body must be a drive object"})
            return
        if not self.rules.load():
            self._send_json(503, {"error": "rules not available"})
            return
        self._send_json(200, self.rules.evaluate(drive))

    # ── /lab/on, /lab/off ──

    def _route_lab_on(self, _parsed) -> None:
        self._set_lab(True)

    def _route_lab_off(self, _parsed) -> None:
        self._set_lab(False)

    def _set_lab(self, enabled: bool) -> None:
        sv = self.supervisor
        if not sv.available:
            self._send_json(503, {"error": "supervisor not available"})
            return

        # The options call replaces the whole options object, so start from
        # the current one and change only mock_mode.
        status, info = sv.call("GET", "/addons/self/info")
        options = None
        if status == 200 and isinstance(info, dict):
            options = (info.get("data") or {}).get("options")
        if not isinstance(options, dict):
            self._send_json(502, {"error": "could not read the app options"})
            return
        options = {**options, "mock_mode": enabled}

        status, reply = sv.call("POST", "/addons/self/options", {"options": options})
        if status != 200:
            message = reply.get("message") if isinstance(reply, dict) else None
            self._send_json(502, {"error": message or "could not save the app options"})
            return

        # Answer first: the restart stops this process. The response is written
        # and flushed before the restart is requested, from a separate thread
        # after a short pause so ingress can deliver it. If the Supervisor were
        # faster than that pause the page would see a dropped connection
        # instead of the answer; it polls /api/health either way.
        self._send_json(200, {"result": "restarting"})
        try:
            self.wfile.flush()
        except OSError:
            pass
        log(f"Test Lab {'on' if enabled else 'off'}: restarting the app")

        def restart() -> None:
            time.sleep(RESTART_DELAY)
            sv.call("POST", "/addons/self/restart")

        threading.Thread(target=restart, daemon=True).start()

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
        body, error = self._read_json()
        target = body.get("target") if isinstance(body, dict) else None
        port = self._target_port(target) if isinstance(target, str) else None
        if error or port is None:
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
        # The Test Lab's mock agent is started without a token, so its entry
        # carries none.
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
        body, error = self._read_json()
        target = body.get("target") if isinstance(body, dict) else None
        if error or target != "testlab":
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
        serial = (parse_qs(parsed.query).get("serial") or [""])[0]
        slug = slugify(serial)
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
        # Prefer an entity that currently has a value over one that is
        # unavailable (for example a drive the agent no longer reports).
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

    def _proxy_request(self, method: str, host: str, port: int, path: str):
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
            # This is safe -- only localhost can reach this proxy.
            if self.token and "Authorization" not in headers:
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
        except Exception as e:
            self.send_error(502, f"Proxy error: {e}")

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
    parser.add_argument("--agent-port", type=int, default=9099, help="Real agent port")
    parser.add_argument("--mock-port", type=int, default=9100, help="Mock agent port")
    parser.add_argument("--token", type=str, default="", help="Bearer token for agent auth")
    parser.add_argument("--app-version", type=str, default=os.environ.get("APP_VERSION") or None,
                        help="App version shown on the page (default: $APP_VERSION)")
    parser.add_argument("--mock-enabled", type=str, default="false",
                        help="Whether the Test Lab (mock agent) is on: true or false")
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
    ProxyHandler.mock_enabled = parse_bool(args.mock_enabled)
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
        log("No Supervisor token: Test Lab button and Home Assistant routes are off")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
