"""Page checks against the real proxy.py, not devserver.py.

The dev harness answers like the proxy but serves its files its own way, so a
page that works there can still fail in the app: on the test box the proxy
let browsers keep cc.js for a day, and a browser that had seen the old page
ran yesterday's script against the new markup and drew nothing but the +
card. These tests run the page in Chromium against proxy.py with the fake
Supervisor and agents of test_proxy.py and the replica mock agent.

Needs what test_page.py needs (Playwright, VENDOR_DIR) and what the replica
tests of test_proxy.py need (SS_RULES_DIR or SS_MOCK).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from test_proxy import (  # noqa: E402 -- after the skip
    APP_TOKEN,
    FIELD_PASSWORD,
    MOCK_AGENT,
    RULES_DIR,
    STATE,
    TOKEN,
    AgentState,
    FakeSupervisor,
    _device,
    _entry,
    _free_port,
    _mock_has_replicas,
    _serve,
    _start_proxy,
    _stop_proxy,
    _wait_for_port,
    agent_handler,
    simple_drive,
)

WEB_DIR = Path(__file__).resolve().parent
VENDOR = os.environ.get("VENDOR_DIR", "")
VENDORED = ("alpine.min.js", "tailwind.min.js")

pytestmark = [
    pytest.mark.skipif(not (VENDOR and all((Path(VENDOR) / n).is_file() for n in VENDORED)),
                       reason="set VENDOR_DIR to a folder with alpine.min.js and tailwind.min.js"),
    pytest.mark.skipif(not _mock_has_replicas(), reason=f"replica mock agent not found ({MOCK_AGENT})"),
]

SELF = AgentState(token=APP_TOKEN, drives=[simple_drive("sda", "HAOS-DISK-1")])
NAS = AgentState(token=FIELD_PASSWORD, hostname="nas-01",
                 drives=[simple_drive("sda", "NAS-A"), simple_drive("sdb", "NAS-B")])


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
    nas = _serve(agent_handler(NAS))
    STATE.options["hosts"] = [{"address": "127.0.0.1", "port": nas.server_address[1],
                               "password": FIELD_PASSWORD}]
    work = Path(tempfile.mkdtemp(prefix="ss-mock-"))
    (work / "user").mkdir()
    (work / "data").mkdir()
    mock_port = _free_port()
    mock = subprocess.Popen(
        [sys.executable, str(MOCK_AGENT), f"--port={mock_port}", "--host=127.0.0.1", "--no-mdns",
         "--replicas", str(MOCK_AGENT.parent / "replicas"), "--user-replicas", str(work / "user")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _wait_for_port(mock_port, mock)
    proc, client = _start_proxy([
        f"--agent-port={self_agent.server_address[1]}",
        f"--mock-port={mock_port}",
        "--app-version=0.3.0",
        f"--rules-dir={RULES_DIR}",
        f"--supervisor-url=http://127.0.0.1:{supervisor.server_address[1]}",
        f"--token={APP_TOKEN}",
        f"--data-dir={work / 'data'}",
        f"--static-dir={static_dir}",
        "--ha-cache-seconds=0",
    ], TOKEN)
    client.nas = f"127.0.0.1:{nas.server_address[1]}"  # type: ignore[attr-defined]
    yield client
    output = _stop_proxy(proc)
    assert TOKEN not in output
    mock.terminate()
    mock.wait(5)
    for server in (supervisor, self_agent, nas):
        server.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    STATE.reset()


class Tab:
    """One browser profile on the proxy, with the page's errors and failed
    requests collected."""

    def __init__(self, browser, base: str):
        self.base = base
        self.ctx = browser.new_context(viewport={"width": 1280, "height": 900})
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
    t = Tab(browser, app.base)
    yield t
    t.close()


def test_cards_render_and_a_replica_adds(tab, app):
    tab.load()
    tab.page.wait_for_selector('article.card[data-card="self|HAOS-DISK-1"]', timeout=10000)
    for serial in ("NAS-A", "NAS-B"):
        tab.card(f"{app.nas}|{serial}").wait_for(timeout=10000)
    assert "nas-01" in tab.card(f"{app.nas}|NAS-A").inner_text().lower()

    tab.page.get_by_role("button", name="Add a drive, or a replica for testing").click()
    region = tab.page.get_by_role("region", name="Add", exact=True)
    region.locator(".replica-pick label").first.wait_for(timeout=10000)
    region.locator(".replica-pick label").first.click()
    region.locator(".add-foot .btn-primary").click()
    tab.page.wait_for_selector('article.card[data-card^="replicas|"]', timeout=10000)
    assert not tab.errors, tab.errors
    assert not tab.failed, tab.failed


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


def test_check_answers_against_the_proxy(tab, app):
    """Check by hand, through proxy.py's probe route: every outcome says so
    in a line under the fields."""
    other = _serve(agent_handler(AgentState(hostname="spare-01", drives=[simple_drive("sda", "SPARE-A")])))
    stranger = _serve(agent_handler(AgentState(mode="notagent")))
    try:
        tab.load()
        tab.page.get_by_role("button", name="Add a drive, or a replica for testing").click()
        region = tab.page.get_by_role("region", name="Add", exact=True)
        msg = region.locator(".check-msg")

        def check(port: int, text: str) -> None:
            region.get_by_label("Host or IP").fill("127.0.0.1")
            region.get_by_label("Port", exact=True).nth(1).fill(str(port))
            region.get_by_role("button", name="Check").click()
            msg.filter(has_text=text).wait_for(timeout=10000)

        spare = other.server_address[1]
        check(spare, "Added 127.0.0.1 to the list, ticked.")
        row = region.locator(".pick-row", has_text=f"127.0.0.1:{spare}")
        assert row.locator("input").is_checked()
        row.click()
        check(spare, "127.0.0.1 is in the list above, ticked.")
        assert row.locator("input").is_checked() and region.locator(".pick-row").count() >= 1
        check(int(app.nas.split(":")[1]), "127.0.0.1 is already on the page.")
        free = _free_port()
        check(free, f"Nothing answered at 127.0.0.1:{free}.")
        check(stranger.server_address[1], f"127.0.0.1:{stranger.server_address[1]} answered, but it is not a SMART Sniffer agent.")
        assert not tab.errors, tab.errors
    finally:
        other.shutdown()
        stranger.shutdown()


def test_rename_reaches_home_assistant_and_reads_back(tab, app):
    """A drive in Home Assistant: the page's rename goes through proxy.py's
    websocket client to the fake Core, and a name set in Home Assistant
    shows on the next refresh. The machine pill renames every card of it."""
    info = json.load(urllib.request.urlopen(app.base + "/app/info"))
    STATE.entries = [_entry("e-agent", f"SMART Sniffer ({info['hostname']}:{info['agent_port']})")]
    STATE.devices = [_device("dev-haos", "e-agent", serial="HAOS-DISK-1")]
    STATE.ws_mode, STATE.ws_commands = "ok", []
    try:
        tab.load()
        card = tab.card("self|HAOS-DISK-1")
        card.locator("a.exit").wait_for(timeout=10000)
        card.locator(".t-name").dblclick()
        card.locator("input.name-in").fill("Boot disk")
        tab.page.keyboard.press("Enter")
        tab.page.wait_for_function("() => document.querySelector('[data-card=\"self|HAOS-DISK-1\"] h3').textContent === 'Boot disk'")
        tab.page.wait_for_timeout(500)
        assert STATE.ws_commands and STATE.ws_commands[-1]["name_by_user"] == "Boot disk"
        assert STATE.devices[0]["name_by_user"] == "Boot disk"
        assert STATE.options["card_names"] == [{"card": "self|HAOS-DISK-1", "name": "Boot disk"}]
        assert card.locator(".act-note").is_hidden()
        # Renamed in Home Assistant: the page shows it after Refresh.
        STATE.devices[0]["name_by_user"] = "Named in HA"
        tab.page.get_by_role("button", name="Refresh").click()
        tab.page.wait_for_function("() => document.querySelector('[data-card=\"self|HAOS-DISK-1\"] h3').textContent === 'Named in HA'")
        card.locator(".chev").click()
        assert card.locator(".c-model").inner_text().startswith("Model: ")
        # Home Assistant refusing: the app keeps the name and says so.
        STATE.ws_mode = "refuse"
        STATE.devices[0]["name_by_user"] = None
        tab.page.get_by_role("button", name="Refresh").click()
        tab.page.wait_for_function("() => document.querySelector('[data-card=\"self|HAOS-DISK-1\"] h3').textContent === 'Boot disk'")
        card.locator(".t-name").dblclick()
        card.locator("input.name-in").fill("Second name")
        tab.page.keyboard.press("Enter")
        card.locator(".act-note").filter(has_text="Home Assistant did not take the name").wait_for(timeout=5000)
        assert card.locator("h3.c-title").inner_text() == "Second name"
        # The machine pill: every card of nas-01 follows.
        nas = [tab.card(f"{app.nas}|{s}") for s in ("NAS-A", "NAS-B")]
        nas[0].locator(".pill").dblclick()
        nas[0].locator("input.name-in").fill("Basement")
        tab.page.keyboard.press("Enter")
        tab.page.wait_for_timeout(300)
        assert [c.locator(".pill").inner_text() for c in nas] == ["BASEMENT", "BASEMENT"]
        assert STATE.options["host_names"] == [{"host": app.nas, "name": "Basement"}]
        tab.load()
        tab.card(f"{app.nas}|NAS-B").locator(".pill").filter(has_text="BASEMENT").wait_for(timeout=10000)
        assert not tab.errors, tab.errors
    finally:
        STATE.entries, STATE.devices, STATE.ws_mode = [], [], "ok"
        STATE.options.pop("card_names", None)
        STATE.options.pop("host_names", None)
