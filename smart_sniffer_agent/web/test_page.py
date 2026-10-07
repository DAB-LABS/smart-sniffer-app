"""Page checks for the Control Center, run against devserver.py in Chromium.

Development only, not part of the image. Needs pytest and Playwright, and the
real Alpine and Tailwind files (the repo holds placeholders): put them in a
folder and set VENDOR_DIR.

    VENDOR_DIR=/path/to/vendor pytest web/test_page.py
    PW_CHROMIUM=/path/to/chrome   use this Chromium instead of Playwright's own
    PAGE_SHOTS=/some/dir          where test_screenshots writes (default
                                  page-shots in the system's temp folder)
    SHOT_WIDTHS=1100,390          widths for the screenshots (default 1280,390)

test_screenshots saves one picture per board state, named by the board's
number: 3.2-1280.png is board 3.2 at 1280 px. They are for comparing with the
board by eye; the other tests assert.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

WEB_DIR = Path(__file__).resolve().parent
VENDOR = os.environ.get("VENDOR_DIR", "")
SHOTS = Path(os.environ.get("PAGE_SHOTS") or Path(tempfile.gettempdir()) / "page-shots")
WIDTHS = [int(w) for w in os.environ.get("SHOT_WIDTHS", "1280,390").split(",") if w.strip()]


def _vendored() -> bool:
    for name in ("alpine.min.js", "tailwind.min.js"):
        if (WEB_DIR / name).read_bytes().startswith(b"// PLACEHOLDER"):
            if not (VENDOR and (Path(VENDOR) / name).is_file()):
                return False
    return True


pytestmark = pytest.mark.skipif(not _vendored(), reason="set VENDOR_DIR to a folder with alpine.min.js and tailwind.min.js")

# Colours from the board's tokens, as computed styles report them.
RED = "rgb(255, 75, 75)"
GOLD = "rgb(245, 165, 36)"
GREEN = "rgb(63, 185, 80)"
BLUE = "rgb(3, 169, 244)"


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, QUIET="1", LATENCY="0.01")
    for key in ("AGENT", "DRIVES", "VDISK", "RULES", "HA", "HA_LOOKUP", "HA_SELF", "ADD", "NETWORK", "SCAN",
                "HOSTS", "POOL", "REPLICAS", "REPLICA_FOLDER", "HOSTNAME", "SLUG", "HA_VERSION"):
        env.pop(key, None)
    proc = subprocess.Popen([sys.executable, str(WEB_DIR / "devserver.py"), "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/app/info", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield base
    proc.terminate()
    proc.wait(5)


@pytest.fixture(scope="session")
def browser():
    with playwright_api.sync_playwright() as p:
        kwargs = {}
        if os.environ.get("PW_CHROMIUM"):
            kwargs["executable_path"] = os.environ["PW_CHROMIUM"]
        b = p.chromium.launch(**kwargs)
        yield b
        b.close()


class Page:
    """A page on the dev harness with a log of every request it made."""

    def __init__(self, browser, server, flags: str = "", width: int = 1280):
        self.base = server
        self.flags = flags
        urllib.request.urlopen(urllib.request.Request(f"{server}/dev/reset?{flags}", method="POST"), timeout=5)
        self.ctx = browser.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
        self.page = self.ctx.new_page()
        self.requests: list[tuple[str, str, str | None]] = []
        self.errors: list[str] = []
        self.page.on("request", lambda r: self.requests.append((r.method, r.url, r.post_data)))
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(f"{server}/?{flags}")
        self.page.wait_for_function("() => window.Alpine && document.body._x_dataStack")
        self.page.wait_for_timeout(700)

    def api(self, method: str, path: str) -> list:
        return [r for r in self.requests if r[0] == method and re.search(path, r[1].split("?")[0])]

    def close(self):
        assert not self.errors, self.errors
        self.ctx.close()


@pytest.fixture
def open_page(browser, server):
    pages = []

    def make(flags: str = "", width: int = 1280) -> Page:
        p = Page(browser, server, flags, width)
        pages.append(p)
        return p

    yield make
    for p in pages:
        p.close()


def card(p: Page, text: str):
    return p.page.locator("article.card", has_text=text).first


def open_add(p: Page):
    p.page.get_by_role("button", name="Add field agents and replicas").click()
    p.page.wait_for_selector("[role=region][aria-label=Add]")


# ── Colour and text checks, run in the page ──────────────────────────────────

COLOUR_AUDIT = """
() => {
  const want = { red: 'rgb(255, 75, 75)', gold: 'rgb(245, 165, 36)', green: 'rgb(63, 185, 80)', blue: 'rgb(3, 169, 244)' };
  const allowed = {
    red: (el, pseudo) => pseudo ? el.matches('.kv dt.crit, .drive-row.crit > summary') : el.matches('.hd-att, .hd-fail b, .mk.crit'),
    gold: (el, pseudo) => pseudo ? el.matches('.kv dt.warn, .drive-row.warn > summary') : el.matches('.hd-maybe, .mk.warn'),
    green: (el, pseudo) => !pseudo && el.matches('.hd-ok'),
    blue: (el, pseudo) => !pseudo && !!el.closest('.exit:not(.add)'),
  };
  const bad = [];
  const visible = (el) => el.getClientRects().length > 0;
  const check = (el, cs, pseudo) => {
    const used = [cs.color, cs.backgroundColor];
    for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
      if (cs['border' + side + 'Style'] !== 'none' && parseFloat(cs['border' + side + 'Width']) > 0) used.push(cs['border' + side + 'Color']);
    }
    if (el instanceof SVGElement) used.push(cs.stroke, cs.fill);
    const text = pseudo ? cs.content !== 'none' && cs.content !== 'normal' : true;
    for (const [name, rgb] of Object.entries(want)) {
      const hit = used.some((u) => u && u.startsWith(rgb));
      if (!hit) continue;
      // An element that shows no text and no box in that colour does not count:
      // a colour inherited by an empty wrapper is not drawn.
      const drawn = cs.backgroundColor.startsWith(rgb) || used.slice(2).some((u) => u && u.startsWith(rgb))
        || (cs.color.startsWith(rgb) && (pseudo ? text : [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())))
        || (el instanceof SVGElement);
      if (drawn && !allowed[name](el, pseudo)) bad.push(name + ': ' + (pseudo ? '::before of ' : '') + el.outerHTML.slice(0, 140));
    }
  };
  for (const el of document.body.querySelectorAll('*')) {
    if (!visible(el)) continue;
    check(el, getComputedStyle(el), false);
    const before = getComputedStyle(el, '::before');
    if (before.content && before.content !== 'none' && before.content !== 'normal') check(el, before, true);
  }
  return bad;
}
"""


def assert_colours(p: Page):
    bad = p.page.evaluate(COLOUR_AUDIT)
    assert not bad, "\n".join(bad)


def assert_no_dashes(p: Page):
    text = p.page.evaluate("document.body.innerText")
    assert "\u2014" not in text and "\u2013" not in text, "an em-dash or en-dash in the page"


def assert_no_scroll(p: Page):
    sw, vw = p.page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert sw == vw, f"page is {sw} px wide at {vw} px"


# Every visible element ends inside the viewport, unless an ancestor clips
# or scrolls it (a wide table inside its wrapper, an ellipsis cell).
OVERFLOW_AUDIT = """
() => {
  const vw = document.documentElement.clientWidth;
  const clipped = (el) => {
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const o = getComputedStyle(a).overflowX;
      if (o !== 'visible') return true;
    }
    return false;
  };
  const out = [];
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if ((r.right > vw + 0.5 || r.left < -0.5) && !clipped(el)) out.push(el.outerHTML.slice(0, 120));
  }
  return out;
}
"""


def assert_no_overflow(p: Page):
    bad = p.page.evaluate(OVERFLOW_AUDIT)
    assert not bad, "\n".join(bad)


# Each header word, its text and its colour.
HEADER_AUDIT = """
() => {
  const words = { 'hd-att': ['Attention', 'rgb(255, 75, 75)'], 'hd-maybe': ['Maybe', 'rgb(245, 165, 36)'],
                  'hd-ok': ['Health OK', 'rgb(63, 185, 80)'], 'hd-unk': ['Health not known', 'rgb(189, 189, 189)'] };
  const bad = [];
  const seen = [];
  document.querySelectorAll('.hd > span').forEach((el) => {
    if (!el.getClientRects().length) return;
    const cls = Object.keys(words).find((c) => el.classList.contains(c));
    if (el.classList.contains('hd-fail')) {
      if (el.innerText.trim() !== 'Health FAILED' || getComputedStyle(el.querySelector('b')).color !== 'rgb(255, 75, 75)'
          || getComputedStyle(el).color !== 'rgb(241, 241, 241)') bad.push(el.outerHTML);
      seen.push('Health FAILED');
      return;
    }
    if (!cls) { bad.push(el.outerHTML); return; }
    const [text, colour] = words[cls];
    if (el.innerText.trim() !== text || getComputedStyle(el).color !== colour) bad.push(el.outerHTML);
    seen.push(text);
  });
  return { bad, seen };
}
"""


def header_words(p: Page) -> set:
    out = p.page.evaluate(HEADER_AUDIT)
    assert not out["bad"], "\n".join(out["bad"])
    return set(out["seen"])


# ── The states the page checks walk through ──────────────────────────────────

# Switch set, the header words it shows, and the exit glyphs: (blue, grey)
# present, or None for no glyph at all.
STATES = [
    ("", {"Attention", "Maybe", "Health OK", "Health not known", "Health FAILED"}, (True, True)),
    ("AGENT=down", {"Health not known", "Attention", "Maybe", "Health OK"}, (True, True)),
    ("VDISK=1&HA_SELF=0", {"Health not known", "Health OK", "Attention"}, (True, True)),
    ("HA=not_installed", {"Attention", "Maybe", "Health OK"}, None),
    ("HA=unreachable", {"Attention", "Maybe", "Health OK"}, None),
    ("HA_LOOKUP=entries_only", {"Attention", "Health OK"}, (True, True)),
    ("DRIVES=0", {"Attention", "Maybe"}, (True, True)),
    ("RULES=missing", {"Health not known"}, (True, True)),
    ("POOL=degraded", {"Attention", "Maybe"}, (True, True)),
    ("HOSTS=none&REPLICAS=0&HA=not_installed&DRIVES=1", {"Health OK"}, None),
    ("REPLICA_FOLDER=missing", {"Attention", "Health OK"}, (True, True)),
]


@pytest.mark.parametrize("flags,words,glyphs", STATES, ids=[s[0] or "board" for s in STATES])
def test_state_rules(open_page, flags, words, glyphs):
    """Header words and colours, glyphs, dashes and phone width on each switch set."""
    for width in (1280, 390):
        p = open_page(flags, width)
        p.page.wait_for_timeout(300)
        assert words <= header_words(p), (words, header_words(p))
        blue = p.page.locator("a.exit:visible").count()
        grey = p.page.locator("button.exit.add:visible").count()
        if glyphs is None:
            assert blue == 0 and grey == 0
        else:
            assert (blue > 0, grey > 0) == glyphs, (blue, grey)
            if blue:
                assert p.page.evaluate("getComputedStyle(document.querySelector('a.exit')).color") == BLUE
            if grey:
                assert p.page.evaluate("getComputedStyle(document.querySelector('button.exit.add')).color") != BLUE
        assert_colours(p)
        assert_no_dashes(p)
        assert_no_scroll(p)
        if width < 760:
            assert_no_overflow(p)
        # Rows opened and the + card open must hold too.
        for summary in p.page.locator("details.drive-row > summary").all()[:6]:
            summary.click()
        p.page.locator("details.report > summary").first.click() if p.page.locator("details.report").count() else None
        open_add(p)
        p.page.wait_for_timeout(600)
        assert_colours(p)
        assert_no_dashes(p)
        assert_no_scroll(p)
        if width < 760:
            assert_no_overflow(p)


def test_header_words(open_page):
    p = open_page()
    colours = p.page.evaluate("""() => Object.fromEntries(['.hd-ok', '.hd-att', '.hd-maybe', '.hd-fail b']
        .map((s) => [s, getComputedStyle(document.querySelector(s)).color]))""")
    assert colours == {".hd-ok": GREEN, ".hd-att": RED, ".hd-maybe": GOLD, ".hd-fail b": RED}
    texts = set(p.page.locator(".hd > span").all_inner_texts())
    assert {"Health OK", "Attention", "Maybe", "Health not known"} <= {t.strip() for t in texts}
    assert p.page.evaluate("getComputedStyle(document.querySelector('.exit:not(.add)')).color") == BLUE


def test_header_versions_and_agent_down(open_page):
    p = open_page()
    assert "app 0.3.0" in p.page.locator(".versions").inner_text()
    assert "agent 0.8.1" in p.page.locator(".versions").inner_text()
    p = open_page("AGENT=down")
    assert "agent not answering" in p.page.locator(".versions").inner_text()
    down = card(p, "This app's agent is not answering")
    text = down.inner_text()
    assert "Drives last seen" in text and "Samsung SSD 980 PRO 1TB" in text
    assert "local-smart-sniffer-agent:9099" in text
    assert re.search(r"asking again in \d s", text)
    assert "last answer 4 min ago" in text


def test_requests_stay_home_and_nothing_scans_at_load(open_page):
    p = open_page()
    p.page.wait_for_timeout(1500)
    assert all(url.startswith(p.base) for _, url, _ in p.requests)
    assert not p.api("POST", r"/fleet/scan$") and not p.api("POST", r"/fleet/probe$")
    assert not p.api("GET", r"/fleet/scan/")
    open_add(p)
    p.page.wait_for_timeout(600)
    assert len(p.api("POST", r"/fleet/scan$")) == 1
    assert p.api("GET", r"/fleet/scan/")
    assert all(url.startswith(p.base) for _, url, _ in p.requests)


def test_ingress_prefix(open_page, browser, server):
    """Every request stays relative to the page, as it must behind ingress."""
    ctx = browser.new_context()
    page = ctx.new_page()
    seen = []
    page.on("request", lambda r: seen.append(r.url))
    page.goto(f"{server}/api/hassio_ingress/tok123/")
    page.wait_for_selector("article.card")
    ctx.close()
    api = [u for u in seen if not u.endswith((".js", ".css", ".png"))][1:]
    assert api and all("/api/hassio_ingress/tok123/" in u for u in api), api


def test_add_card_flow(open_page):
    p = open_page("HOSTS=one")
    open_add(p)
    region = p.page.get_by_role("region", name="Add")
    block = region.get_by_label("Network block")
    assert block.input_value() == "10.0.10.0/24"
    assert region.get_by_role("button", name="Rescan").is_visible()
    p.page.wait_for_function("() => document.querySelectorAll('.pick-row').length === 3", timeout=6000)
    p.page.wait_for_timeout(500)
    # The two new agents arrive ticked; the stored one is listed muted and not counted.
    assert region.locator(".found b").inner_text() == "2"
    done = region.locator(".pick-row.done")
    assert "proxmox-01, already a field agent" in done.inner_text()
    assert done.locator("input").is_disabled()
    assert region.locator(".add-foot .btn-primary").inner_text() == "Add 2"
    assert "wants a password" in region.locator(".add-foot").inner_text()
    # Ticks and the count agree, across both lists.
    region.locator(".replica-pick label", has_text="NVMe, wear").click()
    assert region.locator(".add-foot .btn-primary").inner_text() == "Add 3"
    region.locator(".replica-pick label", has_text="NVMe, wear").click()
    region.locator(".pick-row", has_text="office-nuc").click()
    assert region.locator(".add-foot .btn-primary").inner_text() == "Add 1"
    region.locator(".pick-row", has_text="office-nuc").click()
    # Editing the block: Scan, and the bar, count and results go.
    block.fill("10.0.20.0/24")
    assert region.get_by_role("button", name="Scan", exact=True).is_visible()
    assert region.locator(".scan-prog").count() == 0 and region.locator(".pick-row").count() == 0
    assert region.locator(".add-foot .btn-primary").is_disabled()
    assert region.locator(".add-foot .btn-primary").inner_text() == "Add"
    block.fill("10.0.10.0/24")
    assert region.get_by_role("button", name="Rescan").is_visible()
    # Too wide: a warning, and nothing sent.
    before = len(p.api("POST", r"/fleet/scan$"))
    block.fill("10.0.0.0/16")
    region.get_by_role("button", name="Scan", exact=True).click()
    alert = region.get_by_role("alert")
    assert "That is 65,534 addresses." in alert.inner_text()
    assert alert.get_by_role("button").inner_text() == "Use 10.0.10.0/24"
    p.page.wait_for_timeout(300)
    assert len(p.api("POST", r"/fleet/scan$")) == before
    alert.get_by_role("button").click()
    p.page.wait_for_function("() => document.querySelectorAll('.pick-row').length === 3", timeout=6000)
    # Add by hand.
    region.get_by_label("Host or IP").fill("nowhere.lan")
    region.get_by_role("button", name="Check").click()
    p.page.wait_for_selector("text=Nothing answered on nowhere.lan, port 9099.")
    region.get_by_label("Host or IP").fill("nas.lan")
    region.get_by_role("button", name="Check").click()
    p.page.wait_for_selector(".pick-row:has-text('nas.lan:9099')")
    assert region.locator(".add-foot .btn-primary").inner_text() == "Add 3"
    region.locator(".pick-row", has_text="nas.lan").click()
    # Add with two hosts is one request; the new cards show at once.
    region.locator(".add-foot .btn-primary").click()
    p.page.wait_for_selector("[role=region][aria-label=Add]", state="detached")
    posts = p.api("POST", r"/fleet/hosts$")
    assert len(posts) == 1
    body = json.loads(posts[0][2])
    assert sorted(h["address"] for h in body["hosts"]) == ["10.0.10.52", "10.0.10.61"]
    p.page.wait_for_selector("article.card:has-text('office-nuc') .drive-row")
    pending = card(p, "backup-pi")
    assert "This agent wants a password." in pending.inner_text()
    assert "drives show after the password" in pending.inner_text()
    assert "added just now" in pending.inner_text()
    assert p.page.get_by_role("button", name="Add field agents and replicas").get_attribute("aria-expanded") == "false"


def test_closing_the_add_card_cancels_the_scan(open_page):
    p = open_page("SCAN=slow")
    open_add(p)
    p.page.wait_for_timeout(600)
    p.page.get_by_role("button", name="Close").click()
    p.page.wait_for_timeout(300)
    assert p.api("DELETE", r"/fleet/scan/")


def _set_hidden(p: Page, hidden: bool) -> None:
    p.page.evaluate("""(hidden) => {
        Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
        document.dispatchEvent(new Event('visibilitychange'));
    }""", hidden)


def test_scan_polling_pauses_while_hidden_and_resumes_at_once(open_page):
    p = open_page("SCAN=slow")
    open_add(p)
    p.page.wait_for_timeout(900)
    polls = lambda: len(p.api("GET", r"/fleet/scan/"))  # noqa: E731
    assert polls() >= 1
    _set_hidden(p, True)
    p.page.wait_for_timeout(500)
    before = polls()
    probed = p.page.evaluate("() => Alpine.$data(document.body).add.scan.probed")
    p.page.wait_for_timeout(1600)
    assert polls() == before  # nothing while hidden
    # Shown again: a poll at once, well inside the 400 ms rhythm, and the
    # bar catches up with the time spent hidden.
    _set_hidden(p, False)
    p.page.wait_for_timeout(60)
    assert polls() == before + 1
    p.page.wait_for_function(f"() => Alpine.$data(document.body).add.scan.probed > {probed}", timeout=2000)
    p.page.wait_for_timeout(900)
    assert before + 2 <= polls() <= before + 5  # one poll loop, not two


def test_nothing_answered(open_page):
    p = open_page("SCAN=none&HOSTS=one")
    open_add(p)
    p.page.wait_for_selector("text=None answered on 10.0.10.0/24, port 9099.", timeout=6000)
    assert p.page.locator(".found b").inner_text() == "0"


def test_wide_prefill_sends_nothing(open_page):
    p = open_page("NETWORK=wide")
    open_add(p)
    p.page.wait_for_timeout(500)
    assert "That is 65,534 addresses." in p.page.get_by_role("alert").inner_text()
    assert not p.api("POST", r"/fleet/scan$")


def test_hide_and_unhide(open_page):
    p = open_page()
    eye = p.page.locator(".eye-btn")
    assert eye.inner_text().strip() == "1"
    assert card(p, "printer-box").count() == 0
    card(p, "office-nuc").get_by_role("button", name="Hide this card").click()
    assert eye.inner_text().strip() == "2"
    assert card(p, "office-nuc").count() == 0
    p.page.wait_for_timeout(300)
    patches = p.api("PATCH", r"/fleet/hosts/")
    assert len(patches) == 1 and json.loads(patches[0][2]) == {"hidden": True}
    eye.click()
    assert eye.get_attribute("aria-pressed") == "true"
    shown = card(p, "printer-box")
    assert "is-hidden" in shown.get_attribute("class")
    assert "hidden 12 days ago" in shown.inner_text()
    shown.get_by_role("button", name="Hidden. Show this card again").click()
    assert eye.inner_text().strip() == "1"
    assert "is-hidden" not in card(p, "printer-box").get_attribute("class")


def test_hide_rolls_back_on_error(open_page):
    p = open_page()
    p.page.route("**/fleet/hosts/10.0.10.61*", lambda route: route.fulfill(status=502, body='{"error": "supervisor_failed"}'))
    card(p, "office-nuc").get_by_role("button", name="Hide this card").click()
    p.page.wait_for_selector("text=That change was not saved. Try again.")
    assert card(p, "office-nuc").count() == 1
    assert p.page.locator(".eye-btn").inner_text().strip() == "1"


def test_authenticate(open_page):
    p = open_page()
    pending = card(p, "backup-pi")
    pending.get_by_label("Agent password").fill("wrong")
    pending.get_by_role("button", name="Authenticate").click()
    p.page.wait_for_selector("text=That password was refused.")
    assert "bad" in pending.get_by_label("Agent password").get_attribute("class")
    pending.get_by_label("Agent password").fill("fieldpass")
    pending.get_by_role("button", name="Authenticate").click()
    p.page.wait_for_selector("article.card:has-text('backup-pi') .drive-row")
    done = card(p, "backup-pi")
    assert "pending" not in done.get_attribute("class")
    assert done.get_by_role("button", name="Password saved. Change it").is_visible()
    assert "fieldpass" not in p.page.content()


def test_grey_glyph_confirm_and_add(open_page):
    p = open_page("HOSTS=one")
    host = card(p, "proxmox-01")
    host.get_by_role("button", name="Not in Home Assistant yet. Add proxmox-01").click()
    ask = host.get_by_role("group", name="Confirm")
    assert ask.inner_text().startswith("Add proxmox-01 to Home Assistant?")
    p.page.keyboard.press("Escape")
    assert host.get_by_role("group", name="Confirm").count() == 0
    host.get_by_role("button", name="Not in Home Assistant yet. Add proxmox-01").click()
    host.get_by_role("group", name="Confirm").get_by_role("button", name="Add").click()
    p.page.wait_for_selector("article.card:has-text('proxmox-01') a.exit", timeout=6000)
    assert json.loads(p.api("POST", r"/ha/add$")[0][2]) == {"target": "10.0.10.21:9099"}
    assert host.locator(".drive-row a.exit").count() >= 3


def test_add_error_shows_on_the_confirm(open_page):
    p = open_page("HA_SELF=0&ADD=error")
    c = card(p, "Samsung SSD 980 PRO 1TB")
    c.get_by_role("button", name="Not in Home Assistant yet. Add this machine's drives").click()
    ask = c.get_by_role("group", name="Confirm")
    assert "Add this machine's drives to Home Assistant?" in ask.inner_text()
    ask.get_by_role("button", name="Add").click()
    p.page.wait_for_selector("text=Home Assistant did not add it.")


def test_save_as_replica_and_download_upload(open_page, tmp_path):
    p = open_page("REPLICAS=0")
    assert p.page.locator(".replica-empty").inner_text().strip() == "No replicas yet. Add one with +."
    card(p, "Seagate BarraCuda 2TB").get_by_role("button", name="Save as replica").click()
    p.page.wait_for_selector("article.card.replica")
    assert p.page.locator(".replica-count").inner_text() == "1 replica"
    rep = p.page.locator("article.card.replica").first
    assert "Seagate BarraCuda 2TB ST2000DM008" in rep.inner_text()
    assert re.search(r"REPLICA-[0-9A-F]{8}", rep.inner_text())
    with p.page.expect_download() as dl:
        rep.get_by_role("button", name="Download").click()
    path = tmp_path / "replica.json"
    dl.value.save_as(path)
    assert dl.value.suggested_filename.endswith(".json")
    doc = json.loads(path.read_text())
    assert doc["format"] == "smart-sniffer-replica"
    # The downloaded file goes back in through Upload a replica.
    open_add(p)
    p.page.wait_for_selector(".replica-pick label")
    p.page.locator("input[type=file][aria-label='Upload a replica']").set_input_files(str(path))
    p.page.wait_for_function("() => document.querySelectorAll('.replica-pick label.on').length === 1")
    assert len(p.api("POST", r"/replicas$")) == 1
    p.page.locator(".add-foot .btn-primary").click()
    p.page.wait_for_function("() => document.querySelectorAll('article.card.replica').length === 2")


def test_upload_refused(open_page, tmp_path):
    p = open_page()
    bad = tmp_path / "notes.json"
    bad.write_text('{"hello": 1}')
    open_add(p)
    p.page.wait_for_selector(".replica-pick label")
    p.page.locator("input[type=file][aria-label='Upload a replica']").set_input_files(str(bad))
    p.page.wait_for_selector(".up-msg:has-text('This is not a replica file.')")


def test_replica_folder_missing(open_page):
    p = open_page("REPLICA_FOLDER=missing")
    open_add(p)
    p.page.wait_for_selector("text=Your replica folder is not available.")
    assert p.page.get_by_role("button", name="Choose file").is_disabled()
    assert p.page.get_by_role("button", name="Save as replica").count() == 0


def test_replica_scenario_and_remove(open_page):
    p = open_page()
    rep = card(p, "MOCK-NVME-0001")
    assert "Maybe" in rep.locator(".hd").inner_text()
    rep.get_by_label("Scenario").select_option(label="Media errors")
    rep.get_by_role("button", name="Apply").click()
    p.page.wait_for_function("""() => [...document.querySelectorAll('article.card.replica')]
        .some((c) => c.innerText.includes('MOCK-NVME-0001') && c.querySelector('.hd-att'))""")
    assert p.page.locator(".replica-count").inner_text() == "4 replicas"
    rep.get_by_role("button", name="Remove").click()
    p.page.wait_for_function("() => document.querySelector('.replica-count').innerText === '3 replicas'")


def test_replica_grey_glyph_confirm(open_page):
    p = open_page()
    rep = card(p, "MOCK-NVME-0001")
    rep.get_by_role("button", name="Not in Home Assistant yet. Add this replica").click()
    assert rep.get_by_role("group", name="Confirm").inner_text().startswith("Add this replica to Home Assistant?")
    rep.get_by_role("group", name="Confirm").get_by_role("button", name="Add").click()
    p.page.wait_for_selector("article.card:has-text('MOCK-NVME-0001') a.exit", timeout=6000)


def test_first_run(open_page):
    p = open_page("HOSTS=none&REPLICAS=0&HA=not_installed&DRIVES=1")
    assert p.page.locator("article.card").count() == 1
    eye = p.page.locator(".eye-btn")
    assert eye.is_disabled() and eye.inner_text().strip() == "0"
    assert p.page.get_by_role("button", name="Refresh").count() == 0
    assert p.page.get_by_role("button", name="Add field agents and replicas").is_visible()
    links = p.page.locator(".app-foot a:visible").all_inner_texts()
    assert links == ["Mission Log", "App configuration", "GitHub"]
    assert p.page.locator(".exit").count() == 0


def test_field_report_marks(open_page):
    p = open_page()
    c = card(p, "Seagate BarraCuda 2TB")
    c.locator("details.report > summary").click()
    c.locator("table.attrs").wait_for()
    hits = c.locator("table.attrs tr.hit")
    assert hits.count() == 2
    assert sorted(t.split()[0] for t in hits.all_inner_texts()) == ["197", "5"]


def test_many_drives_fold(open_page):
    p = open_page()
    c = card(p, "nas-backup")
    assert c.locator("details.drive-row").count() == 8
    assert c.locator(".row-cause").count() == 2
    first = c.locator("details.drive-row").first
    assert "crit" in first.get_attribute("class")
    c.get_by_role("button", name="and 21 more").click()
    assert c.locator("details.drive-row").count() == 29


def test_page_polls_only_its_own_reads(open_page):
    p = open_page()
    p.page.wait_for_timeout(9000)
    paths = {unquote(re.sub(r"\?.*", "", u[len(p.base):])) for _, u, _ in p.requests
             if not u.endswith((".js", ".css", ".png"))}
    assert paths <= {"/", "/app/info", "/fleet/hosts", "/fleet/hosts/self/drives", "/fleet/hosts/replicas/drives",
                     "/ha/devices"} | {f"/fleet/hosts/{h}:9099/drives" for h in
                                       ("10.0.10.21", "10.0.10.61", "10.0.10.52", "10.0.10.30", "10.0.10.44",
                                        "10.0.10.20", "10.0.10.90")}, paths
    assert len(p.api("GET", r"/fleet/hosts/self/drives$")) == 2
    assert len(p.api("GET", r"/fleet/hosts$")) == 1


# ── Screenshots of every board state ─────────────────────────────────────────

def _shot(p: Page, name: str, target=None):
    SHOTS.mkdir(parents=True, exist_ok=True)
    width = p.page.viewport_size["width"]
    path = SHOTS / f"{name}-{width}.png"
    p.page.wait_for_timeout(250)
    if target is None:
        p.page.screenshot(path=str(path), full_page=True)
    else:
        target.scroll_into_view_if_needed()
        target.screenshot(path=str(path))
    assert_no_scroll(p)
    if width < 760:
        assert_no_overflow(p)


def _section(p: Page, heading: str):
    return p.page.locator("section.sec", has=p.page.locator("h2", has_text=heading))


def test_screenshots(open_page):
    for width in WIDTHS:
        p = open_page("", width)
        _shot(p, "1.1", p.page.locator("header.app-head"))
        _shot(p, "2.0", _section(p, "Case file").locator(".sec-head"))
        _shot(p, "2.2", card(p, "Seagate BarraCuda 2TB"))
        c = card(p, "Seagate BarraCuda 2TB")
        c.locator("details.report > summary").click()
        _shot(p, "2.5", c.locator("details.report"))
        _shot(p, "3.0", _section(p, "Field agents").locator(".sec-head"))
        _shot(p, "3.9", card(p, "studio-mac"))
        _shot(p, "3.10", card(p, "nas-backup"))
        _shot(p, "3.12", card(p, "nas-01"))
        _shot(p, "4.2", _section(p, "Replicas").locator(".toolbar"))
        _shot(p, "4.3", card(p, "MOCK-SATA-0001"))
        _shot(p, "4.4", card(p, "MOCK-NVME-0001"))
        _shot(p, "4.5", card(p, "MOCK-DS-9JG0X1"))
        _shot(p, "4.6", card(p, "Replica pool"))
        _shot(p, "6.1", p.page.locator("footer.app-foot"))
        p.page.locator(".eye-btn").click()
        _shot(p, "3.13", _section(p, "Field agents"))
        _shot(p, "page", None)

        p = open_page("HA=not_installed", width)
        _shot(p, "2.1", card(p, "Samsung SSD 980 PRO 1TB"))

        p = open_page("VDISK=1&HA=not_installed", width)
        _shot(p, "2.3", card(p, "QEMU HARDDISK"))

        p = open_page("AGENT=down", width)
        _shot(p, "1.2", p.page.locator("header.app-head"))
        _shot(p, "2.4", card(p, "This app's agent is not answering"))

        p = open_page("HA_SELF=0", width)
        c = card(p, "Samsung SSD 980 PRO 1TB")
        c.get_by_role("button", name="Not in Home Assistant yet. Add this machine's drives").click()
        _shot(p, "2.grey-confirm", c)

        p = open_page("DRIVES=0", width)
        _shot(p, "2.empty", _section(p, "Case file"))

        p = open_page("HOSTS=one&HA=not_installed", width)
        c = card(p, "proxmox-01")
        c.locator("details.drive-row").first.locator("summary").click()
        _shot(p, "3.1", _section(p, "Field agents").locator(".grid-2"))
        c.locator("details.drive-row").first.locator("summary").click()
        _shot(p, "3.11-not-installed", c)

        p = open_page("HOSTS=one&SCAN=slow&HA=not_installed", width)
        open_add(p)
        p.page.wait_for_function("() => document.querySelectorAll('.pick-row').length === 2", timeout=15000)
        _shot(p, "3.2", p.page.get_by_role("region", name="Add"))

        p = open_page("HOSTS=one&SCAN=none&HA=not_installed", width)
        open_add(p)
        p.page.wait_for_selector("text=None answered", timeout=6000)
        p.page.locator(".replica-pick label", has_text="SATA HDD, reallocated sectors growing").click()
        _shot(p, "3.3", p.page.get_by_role("region", name="Add"))
        p.page.get_by_label("Network block").fill("10.0.20.0/24")
        p.page.locator(".replica-pick label", has_text="SATA HDD, reallocated sectors growing").click()
        _shot(p, "3.4", p.page.get_by_role("region", name="Add"))

        p = open_page("NETWORK=wide&HOSTS=one&HA=not_installed", width)
        open_add(p)
        p.page.wait_for_selector(".scan-wide")
        _shot(p, "3.5", p.page.get_by_role("region", name="Add"))

        p = open_page("HOSTS=one&HA=not_installed", width)
        open_add(p)
        p.page.wait_for_function("() => document.querySelectorAll('.pick-row').length === 3", timeout=6000)
        p.page.wait_for_timeout(1000)
        p.page.locator(".add-foot .btn-primary").click()
        p.page.wait_for_selector("article.card:has-text('office-nuc') .drive-row")
        _shot(p, "3.6", _section(p, "Field agents").locator(".grid-2"))
        pending = card(p, "backup-pi")
        pending.get_by_label("Agent password").fill("wrong")
        pending.get_by_role("button", name="Authenticate").click()
        p.page.wait_for_selector("text=That password was refused.")
        _shot(p, "3.7", pending)
        pending.get_by_label("Agent password").fill("fieldpass")
        pending.get_by_role("button", name="Authenticate").click()
        p.page.wait_for_selector("article.card:has-text('backup-pi') .drive-row")
        _shot(p, "3.8", card(p, "backup-pi"))

        p = open_page("HOSTS=one", width)
        c = card(p, "proxmox-01")
        c.get_by_role("button", name="Not in Home Assistant yet. Add proxmox-01").click()
        _shot(p, "3.11", c)

        p = open_page("HOSTS=one&POOL=degraded&HA=not_installed", width)
        c = card(p, "proxmox-01")
        c.locator("details.drive-row.pool > summary").click()
        _shot(p, "3.14", c)

        p = open_page("REPLICAS=0", width)
        _shot(p, "4.1", _section(p, "Replicas"))

        p = open_page("", width)
        open_add(p)
        p.page.wait_for_selector(".replica-pick label")
        p.page.get_by_role("button", name="Close").click()
        open_add(p)
        p.page.wait_for_selector(".replica-pick label")
        p.page.locator(".pick-row.on").evaluate_all("rows => rows.forEach(r => r.click())")
        p.page.locator(".replica-pick label", has_text="SATA HDD, healthy").click()
        p.page.locator(".add-foot .btn-primary").click()
        p.page.wait_for_selector("article.card:has-text('MOCK-SATA-0002')")
        c = card(p, "MOCK-SATA-0002")
        c.get_by_role("button", name="Not in Home Assistant yet. Add this replica").click()
        _shot(p, "4.7", c)

        p = open_page("HOSTS=none&REPLICAS=0&HA=not_installed&DRIVES=1", width)
        _shot(p, "5", None)
