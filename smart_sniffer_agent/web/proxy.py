#!/usr/bin/env python3
"""SMART Sniffer App — Web UI Proxy Server.

Serves static files from /opt/web/ at the root path and proxies:
  /api/*  → localhost:$AGENT_PORT  (real Go agent)
  /mock/* → localhost:$MOCK_PORT   (Python mock agent)

Runs on the ingress port (8099) so HA can embed the UI in the sidebar.
All requests from the web UI go to the same origin — no CORS issues.
"""

from __future__ import annotations

import argparse
import http.client
import mimetypes
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# Ensure .js and .css MIME types are correct
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# Directory containing static web assets
STATIC_DIR = "/opt/web"


class ProxyHandler(BaseHTTPRequestHandler):
    """Serves static files and proxies API requests."""

    agent_port: int = 9099
    mock_port: int = 9100
    token: str = ""

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

    def _handle_request(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path

        # Proxy /api/* to the real Go agent
        if path.startswith("/api/"):
            self._proxy_request(method, "127.0.0.1", self.agent_port, self.path)
            return

        # Proxy /mock/* to the Python mock agent
        # Rewrite /mock/ → /api/ because mock agent serves the same /api/* routes
        if path.startswith("/mock/"):
            rewritten = self.path.replace("/mock/", "/api/", 1)
            self._proxy_request(method, "127.0.0.1", self.mock_port, rewritten)
            return

        # Serve static files (GET only for static)
        if method != "GET":
            self.send_error(405, "Method Not Allowed")
            return

        self._serve_static(path)

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
            # This is safe — only localhost can reach this proxy.
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
    args = parser.parse_args()

    ProxyHandler.agent_port = args.agent_port
    ProxyHandler.mock_port = args.mock_port
    ProxyHandler.token = args.token

    server = HTTPServer(("0.0.0.0", args.port), ProxyHandler)
    print(f"[webui] Serving on port {args.port}", flush=True)
    print(f"[webui] Proxying /api/* → 127.0.0.1:{args.agent_port}", flush=True)
    print(f"[webui] Proxying /mock/* → 127.0.0.1:{args.mock_port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[webui] Shutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
