"""Page checks against the real proxy.py, not devserver.py.

The dev harness answers like the proxy but serves its files its own way, so a
page that works there can still fail in the app: on the test box the proxy
let browsers keep cc.js for a day, and a browser that had seen the old page
ran yesterday's script against the new markup and drew nothing. These tests
run the page in Chromium against proxy.py with the fake Supervisor (and its
Core websocket) and agent of test_proxy.py.

Needs what test_page.py needs (Playwright, VENDOR_DIR) and the integration's
rules (SS_RULES_DIR), as test_proxy.py does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from test_proxy import (  # noqa: E402 -- after the skip
    APP_TOKEN,
    RULES_DIR,
    STATE,
    TOKEN,
    AgentState,
    FakeSupervisor,
    _entry,
    _serve,
    _start_proxy,
    _stop_proxy,
    agent_handler,
    raw_request,
    simple_drive,
)

WEB_DIR = Path(__file__).resolve().parent
VENDOR = os.environ.get("VENDOR_DIR", "")
VENDORED = ("alpine.min.js", "tailwind.min.js")

pytestmark = [
    pytest.mark.skipif(not (VENDOR and all((Path(VENDOR) / n).is_file() for n in VENDORED)),
                       reason="set VENDOR_DIR to a folder with alpine.min.js and tailwind.min.js"),
    pytest.mark.skipif(not (RULES_DIR / "smart_sniffer" / "attention.py").is_file(),
                       reason=f"integration rules not found ({RULES_DIR})"),
]

SELF = AgentState(token=APP_TOKEN, drives=[simple_drive("sda", "HAOS-DISK-1")])


@pytest.fixture(scope="module")
def static_dir():
    """The page as the image has it: the web folder with the real Alpine and
    Tailwind in place of the placeholders."""
    work = Path(tempfile.mkdtemp(prefix="ss-web-"))
    root = work / "web"
    shutil.copytree(WEB_DIR, root, ignore=shutil.ignore_patterns("__pycache__", ".*", "test_*", "devserver.py"))
    for name in VENDORED:
        shutil.copy(Path(VENDOR) / name, root / name)
    yield root
    shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module")
def app(static_dir):
    STATE.reset()
    supervisor = _serve(FakeSupervisor)
    self_agent = _serve(agent_handler(SELF))
    work = Path(tempfile.mkdtemp(prefix="ss-data-"))
    proc, client = _start_proxy([
        f"--agent-port={self_agent.server_address[1]}",
        "--app-version=0.4.0-dev",
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{supervisor.server_address[1]}",
        f"--data-dir={work}",
        f"--static-dir={static_dir}",
        "--ha-cache-seconds=0",
    ], TOKEN, APP_TOKEN)
    yield client
    output = _stop_proxy(proc)
    assert TOKEN not in output and APP_TOKEN not in output
    for server in (supervisor, self_agent):
        server.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    STATE.reset()


class Tab:
    """One browser profile on the proxy, with the page's errors and failed
    requests collected."""

    def __init__(self, browser, base: str, width: int = 1280):
        self.base = base
        self.ctx = browser.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.failed: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("response", lambda r: r.status >= 400 and self.failed.append(f"{r.status} {r.url}"))

    def load(self):
        self.page.goto(self.base + "/")
        self.page.wait_for_function("() => window.Alpine && document.body._x_dataStack")

    def card(self, card_id: str):
        return self.page.locator(f'article.card[data-card="{card_id}"]')

    def close(self):
        self.ctx.close()


@pytest.fixture
def tab(browser, app):
    STATE.reset()
    t = Tab(browser, app.base)
    yield t
    t.close()


def _loaded(**info) -> None:
    STATE.info = {"version": "0.9.2", "panel": True, "panel_path": "/smart-sniffer",
                  "haos_entry": True, **info}
    STATE.manifest_version = "0.9.2"


def test_this_box_read_only_with_the_control_center_card(tab, app):
    _loaded()
    tab.load()
    card = tab.card("self|HAOS-DISK-1")
    card.wait_for(timeout=10000)
    assert card.locator(".pill").inner_text() == "HAOS"
    # Read only: nothing to drag, rename, hide, remove or tune.
    for gone in (".grip", ".t-name", ".exit", ".trash", ".tune", ".pill-ed"):
        assert card.locator(gone).count() == 0, gone
    assert tab.page.locator(".card.ghost, .card.ghost-open").count() == 0
    link = tab.page.locator("section.link-card")
    link.wait_for(timeout=10000)
    assert link.locator(".lc-title").inner_text() == "See all your machines in the SMART Sniffer Control Center"
    assert link.get_by_role("link", name="Open").get_attribute("href") == "/smart-sniffer"
    assert link.get_by_role("link", name="Open").get_attribute("target") == "_top"
    assert not tab.errors, tab.errors
    assert not tab.failed, tab.failed


def test_create_line_through_the_proxy(tab, app):
    _loaded(haos_entry=False)
    STATE.flow_step = {"type": "create_entry", "result": {"entry_id": "new-self"}}
    tab.load()
    line = tab.page.get_by_role("button", name="Create sensors and alerts for HAOS")
    line.wait_for(timeout=10000)
    line.click()
    ask = tab.page.locator(".create .ask")
    assert ask.locator(".ask-title").inner_text() == "Create sensors and alerts for HAOS?"
    assert ask.locator(".ask-line").inner_text().startswith("Its drive gets health sensors")
    ask.get_by_role("button", name="Create").click()
    line.wait_for(state="detached", timeout=10000)
    posts = [c for c in STATE.calls if c[1] == "/core/api/config/config_entries/flow/flow123"]
    assert posts and posts[0][2]["token"] == APP_TOKEN
    assert not tab.errors, tab.errors


def test_card_file_downloads(tab, app):
    _loaded()
    tab.load()
    card = tab.card("self|HAOS-DISK-1")
    card.wait_for(timeout=10000)
    with tab.page.expect_download() as dl:
        card.get_by_role("button", name="Download this card as a file").click()
    assert dl.value.suggested_filename == "drive-Model-sda.json"
    doc = json.loads(Path(dl.value.path()).read_text())
    assert doc["drive"]["serial"] == "HAOS-DISK-1"
    assert not tab.errors, tab.errors


def test_install_card_closes_for_every_browser(browser, app):
    STATE.reset()
    first = Tab(browser, app.base)
    try:
        first.load()
        card = first.page.locator("section.link-card")
        card.wait_for(timeout=10000)
        assert card.locator(".lc-title").inner_text() == "Get more out of SMART Sniffer"
        card.get_by_role("button", name="Close").click()
        card.wait_for(state="detached", timeout=5000)
        assert not first.errors, first.errors
    finally:
        first.close()
    other = Tab(browser, app.base)
    try:
        other.load()
        other.page.wait_for_timeout(1200)
        assert other.page.locator("section.link-card").count() == 0
    finally:
        other.close()


def test_a_new_script_reaches_a_browser_that_saw_the_old_one(tab, static_dir):
    # The same browser profile opens the page, the app is updated, the page
    # is opened again: it must run the new cc.js, not a cached one.
    tab.load()
    script = static_dir / "cc.js"
    before = script.read_text(encoding="utf-8")
    try:
        script.write_text(before + "\nwindow.ccBuild = 'next';\n", encoding="utf-8")
        tab.load()
        assert tab.page.evaluate("() => window.ccBuild") == "next"
    finally:
        script.write_text(before, encoding="utf-8")
    assert not tab.errors, tab.errors


def test_assets_are_versioned_and_revalidated(app, static_dir):
    with urllib.request.urlopen(app.base + "/") as resp:
        page = resp.read().decode()
        assert resp.headers["Cache-Control"] == "no-cache"
    for name in ("cc.js", "cc.css"):
        found = re.search(rf'"{re.escape(name)}\?v=([0-9a-f]{{12}})"', page)
        assert found, name
        with urllib.request.urlopen(f"{app.base}/{name}?v={found.group(1)}") as resp:
            assert "immutable" in resp.headers["Cache-Control"]
    with urllib.request.urlopen(app.base + "/img/mark.png") as resp:
        assert resp.headers["Cache-Control"] == "no-cache"
        etag = resp.headers["ETag"]
    req = urllib.request.Request(app.base + "/img/mark.png", headers={"If-None-Match": etag})
    try:
        urllib.request.urlopen(req)
        pytest.fail("expected 304")
    except urllib.error.HTTPError as err:
        assert err.code == 304
    # The pages of 0.3.x are gone with their routes.
    for path in ("/agentic-access", "/agentic.html", "/agentic.js"):
        try:
            with urllib.request.urlopen(app.base + path) as resp:
                assert b"agentic" not in resp.read().lower(), path  # the SPA fallback only
        except urllib.error.HTTPError as err:
            assert err.code == 404, path


def test_page_answers_ingress_and_loopback_only(tab, app):
    """The browser here is on loopback, as Home Assistant's ingress gateway is
    in the app: the page loads. A client on the network (127.0.0.2 stands in
    for one) gets 403 for the page, its scripts and every route."""
    _loaded()
    tab.load()
    tab.card("self|HAOS-DISK-1").wait_for(timeout=10000)
    assert not tab.errors, tab.errors
    for path in ("/", "/cc.js", "/cc.css", "/app/info", "/api/health",
                 "/fleet/hosts/self/drives", "/ha/integration"):
        status, raw = raw_request(app.base, "GET", path, source="127.0.0.2")
        assert status == 403, path
        assert json.loads(raw)["error"] == "forbidden"
    status, _ = raw_request(app.base, "POST", "/ha/add", b'{"target": "self"}', source="127.0.0.2")
    assert status == 403
    assert raw_request(app.base, "GET", "/")[0] == 200


# Home Assistant slow, as when it is busy or just restarted: every /core/*
# call answers late.
CORE_DELAY = 0.5


class SlowCoreSupervisor(FakeSupervisor):
    def _dispatch(self, method: str) -> None:
        if self.path.startswith("/core/"):
            time.sleep(CORE_DELAY)
        super()._dispatch(method)


def test_drives_show_without_waiting_on_home_assistant(browser, static_dir):
    """This box's drives show at once; the integration's card follows when
    Home Assistant answers."""
    STATE.reset()
    STATE.entries = [_entry("e1", "SMART Sniffer (nas.lan:9099)")]
    slow = _serve(SlowCoreSupervisor)
    agent = _serve(agent_handler(SELF))
    work = Path(tempfile.mkdtemp(prefix="ss-slow-"))
    proc, client = _start_proxy([
        f"--agent-port={agent.server_address[1]}",
        "--app-version=0.4.0-dev",
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{slow.server_address[1]}",
        f"--data-dir={work}",
        f"--static-dir={static_dir}",
        "--ha-cache-seconds=1",
    ], TOKEN, APP_TOKEN)
    try:
        t = Tab(browser, client.base)
        try:
            start = time.monotonic()
            t.load()
            t.page.wait_for_selector('article.card[data-card="self|HAOS-DISK-1"]', timeout=10000)
            assert time.monotonic() - start < 1.5
            assert not t.errors, t.errors
        finally:
            t.close()
    finally:
        _stop_proxy(proc)
        slow.shutdown()
        agent.shutdown()
        shutil.rmtree(work, ignore_errors=True)
        STATE.reset()
