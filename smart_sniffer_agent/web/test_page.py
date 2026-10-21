"""Page checks for the SMART Sniffer App page, run against devserver.py in Chromium.

Development only, not part of the image. Needs pytest and Playwright, and the
real Alpine and Tailwind files (the repo holds placeholders): put them in a
folder and set VENDOR_DIR.

    VENDOR_DIR=/path/to/vendor pytest web/test_page.py
    PW_CHROMIUM=/path/to/chrome   use this Chromium instead of Playwright's own
    PAGE_SHOTS=/some/dir          where test_screenshots writes (default
                                  page-shots in the system's temp folder)
    SHOT_WIDTHS=1100,390          widths for the screenshots (default 1280,390)

test_screenshots saves one picture per state of the page, named by the
state and the width: app-2-install-390.png is the page without the
integration at 390 px. They are for looking at by eye; the other tests
assert.
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

# The muted status colours of the card design, as computed styles report them.
ATT = "rgb(204, 107, 99)"
MAYBE = "rgb(204, 159, 82)"
OK = "rgb(93, 163, 114)"
BLUE = "rgb(3, 169, 244)"


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, QUIET="1", LATENCY="0.01")
    for key in ("AGENT", "DRIVES", "VDISK", "POOL", "RULES", "INTEGRATION", "HAOS_ENTRY", "PANEL_PATH",
                "HACS", "ADD", "HOSTNAME", "SLUG", "HA_VERSION", "MDNS", "SELF_DELAY"):
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


class Page:
    """A page on the dev harness with a log of every request it made."""

    def __init__(self, browser, server, flags: str = "", width: int = 1280, touch: bool = False):
        self.base = server
        self.flags = flags
        urllib.request.urlopen(urllib.request.Request(f"{server}/dev/reset?{flags}", method="POST"), timeout=5)
        # touch: a phone (a touch screen that cannot hover).
        self.ctx = browser.new_context(viewport={"width": width, "height": 900}, accept_downloads=True,
                                       has_touch=touch, is_mobile=touch)
        self.page = self.ctx.new_page()
        self.requests: list[tuple[str, str, str | None]] = []
        self.errors: list[str] = []
        self.page.on("request", lambda r: self.requests.append((r.method, r.url, r.post_data)))
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(f"{server}/?{flags}")
        self.page.wait_for_function("() => window.Alpine && document.body._x_dataStack")
        self.page.wait_for_timeout(700)

    def wait(self, fn, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if fn():
                return True
            self.page.wait_for_timeout(50)
        return False

    def api(self, method: str, path: str) -> list:
        return [r for r in self.requests if r[0] == method and re.search(path, r[1].split("?")[0])]

    def close(self):
        assert not self.errors, self.errors
        self.ctx.close()


@pytest.fixture
def open_page(browser, server):
    pages = []

    def make(flags: str = "", width: int = 1280, touch: bool = False) -> Page:
        p = Page(browser, server, flags, width, touch)
        pages.append(p)
        return p

    yield make
    for p in pages:
        p.close()


def card(p: Page, text: str):
    return p.page.locator("article.card", has_text=text).first


def by_id(p: Page, card_id: str):
    return p.page.locator(f'article.card[data-card="{card_id}"]')


def card_ids(p: Page) -> list:
    return p.page.eval_on_selector_all(".grid > article.card", "els => els.map((e) => e.dataset.card)")


def link_card(p: Page):
    return p.page.locator("section.link-card")


# Card ids of the harness's drives.
NVME = "self|S5GXNF0R412233A"
BARRACUDA = "self|ZFL0K7Q2"
RED = "self|WD-WCC7K1234567"
POOL = "self|pool:tank"
DIM = "rgb(143, 143, 143)"
HACS_REPO = ("https://my.home-assistant.io/redirect/hacs_repository/"
             "?owner=DAB-LABS&repository=smart-sniffer&category=integration")
HACS_DOCS = "https://hacs.xyz/docs/use/"
SEE_ALL = "See all your machines in the SMART Sniffer Control Center"
GET_MORE = "Get more out of SMART Sniffer"


# ── Colour and text checks, run in the page ──────────────────────────────────

COLOUR_AUDIT = """
() => {
  const want = { att: 'rgb(204, 107, 99)', maybe: 'rgb(204, 159, 82)', ok: 'rgb(93, 163, 114)', blue: 'rgb(3, 169, 244)' };
  const allowed = {
    att: (el) => !!el.closest('.h-att, .h-fail b, .chev.attention, .hbox.attention, .agent-dot.down, .pw-row.crit .lb, .pw-row.crit .pw-val, '
      + '.kv > div.crit .lb, .kv > div.crit .v, table.attrs tr.crit'),
    maybe: (el) => !!el.closest('.h-maybe, .chev.maybe, .hbox.maybe, .pw-row.warn .lb, .pw-row.warn .pw-val, '
      + '.kv > div.warn .lb, .kv > div.warn .v, table.attrs tr.warn'),
    ok: (el) => !!el.closest('.h-ok, .chev.ok, .hbox.ok, .agent-dot.up'),
    blue: () => false,  // no exit icons on this page: nothing is blue
  };
  const bad = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (!el.getClientRects().length) continue;
    const cs = getComputedStyle(el);
    const used = [cs.color, cs.backgroundColor];
    for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
      if (cs['border' + side + 'Style'] !== 'none' && parseFloat(cs['border' + side + 'Width']) > 0) used.push(cs['border' + side + 'Color']);
    }
    if (el instanceof SVGElement) used.push(cs.stroke, cs.fill);
    for (const [name, rgb] of Object.entries(want)) {
      const hit = used.some((u) => u && u.startsWith(rgb));
      if (!hit) continue;
      const drawn = cs.backgroundColor.startsWith(rgb) || used.slice(2).some((u) => u && u.startsWith(rgb))
        || (cs.color.startsWith(rgb) && [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()))
        || (el instanceof SVGElement);
      if (drawn && !allowed[name](el)) bad.push(name + ': ' + el.outerHTML.slice(0, 140));
    }
  }
  return bad;
}
"""


def assert_colours(p: Page):
    bad = p.page.evaluate(COLOUR_AUDIT)
    assert not bad, "\n".join(bad)


def assert_no_dashes(p: Page):
    text = p.page.evaluate("document.body.innerText")
    assert "—" not in text and "–" not in text, "an em-dash or en-dash in the page"
    assert "✓" not in text and "✔" not in text, "a check mark in the page"


def assert_no_scroll(p: Page):
    sw, vw = p.page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert sw == vw, f"page is {sw} px wide at {vw} px"


OVERFLOW_AUDIT = """
() => {
  const vw = document.documentElement.clientWidth;
  const clipped = (el) => {
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      if (getComputedStyle(a).overflowX !== 'visible') return true;
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


def assert_inside_cards(p: Page):
    """Nothing in a card reaches past its edge, unless a wrapper scrolls it."""
    bad = p.page.evaluate("""() => {
      const out = [];
      for (const c of document.querySelectorAll('.grid > .card')) {
        const box = c.getBoundingClientRect();
        for (const el of c.querySelectorAll('*')) {
          const r = el.getBoundingClientRect();
          if (!r.width || !r.height || el.closest('.table-wrap')) continue;
          if (r.right > box.right + 0.5 || r.left < box.left - 0.5) out.push(el.outerHTML.slice(0, 100));
        }
      }
      return out;
    }""")
    assert not bad, "\n".join(bad)


def health_words(p: Page) -> set:
    return {t.strip() for t in p.page.locator(".c-health > span").all_inner_texts()}


# ── Every switch set: colours, copy, width ───────────────────────────────────

STATES = [
    ("", {"Attention", "Health OK"}),
    ("DRIVES=mixed&POOL=degraded&VDISK=1", {"Attention", "Maybe", "Health OK", "Health not known"}),
    ("AGENT=down", {"Attention", "Health OK"}),
    ("INTEGRATION=missing", {"Attention", "Health OK"}),
    ("INTEGRATION=missing&HACS=missing", {"Attention", "Health OK"}),
    ("INTEGRATION=update", {"Attention", "Health OK"}),
    ("INTEGRATION=no_entries", {"Attention", "Health OK"}),
    ("INTEGRATION=restart", {"Attention", "Health OK"}),
    ("INTEGRATION=panel_failed", {"Attention", "Health OK"}),
    ("INTEGRATION=loaded&HAOS_ENTRY=0", {"Attention", "Health OK"}),
    ("INTEGRATION=unreachable", {"Attention", "Health OK"}),
    ("RULES=missing", {"Health not known"}),
    ("DRIVES=0", set()),
]


@pytest.mark.parametrize("flags,words", STATES, ids=[s[0] or "board" for s in STATES])
def test_state_rules(open_page, flags, words):
    """Health words, colours, copy and phone width on each switch set,
    Details closed and open."""
    for width in (1280, 390):
        p = open_page(flags, width)
        p.page.wait_for_timeout(300)
        assert words <= health_words(p), (words, health_words(p))
        assert p.page.locator("h2").count() == 0  # no section headings
        for check in (assert_colours, assert_no_dashes, assert_no_scroll, assert_inside_cards):
            check(p)
        if width < 760:
            assert_no_overflow(p)
        for chev in p.page.locator(".chev").all():
            chev.click()
        p.page.wait_for_timeout(300)
        for check in (assert_colours, assert_no_dashes, assert_no_scroll, assert_inside_cards):
            check(p)
        if width < 760:
            assert_no_overflow(p)


# ── This box only, read only ─────────────────────────────────────────────────

def test_cards_are_this_box_read_only(open_page):
    p = open_page("DRIVES=mixed&POOL=online&VDISK=1")
    ids = card_ids(p)
    assert ids[:3] == [NVME, BARRACUDA, RED] and POOL in ids
    assert all(i.startswith("self|") for i in ids), ids
    # Every pill is HAOS, fixed: no rename on it, never a ghost.
    pills = p.page.locator("article.card .pill")
    assert set(pills.all_inner_texts()) == {"HAOS"}
    for pill in pills.all():
        assert pill.get_attribute("tabindex") is None and pill.get_attribute("role") is None
    assert p.page.locator(".pill-ghost, .pill-ed").count() == 0
    # Omitted on each card: grip, rename, exit icon, eye, trash, tune and
    # the thresholds view, Save as replica.
    for gone in (".grip", ".t-name", "input.name-in", ".exit", ".exit-slot", ".icon.trash", ".rm-set",
                 ".tune", ".thr", "input.raw-in", "select.scen"):
        assert p.page.locator(gone).count() == 0, gone
    assert p.page.get_by_role("button", name=re.compile("Hide|Remove|Save as replica|Edit thresholds")).count() == 0
    # Omitted on the page: the + card, the hidden-cards eye, other machines.
    assert p.page.locator(".card.ghost, .card.ghost-open, .eye-btn, #drag-layer").count() == 0
    assert p.page.get_by_text("Add a drive, or a replica for testing").count() == 0
    # Kept: the title is the model, Details opens, the download icon.
    assert by_id(p, NVME).locator("h3.c-title").inner_text() == "Samsung SSD 980 PRO 1TB"
    for cid in (NVME, BARRACUDA, POOL):
        icon = by_id(p, cid).get_by_role("button", name="Download this card as a file")
        assert icon.count() == 1 and icon.get_attribute("title") == "Download as a file"
    # A filesystem card has no health line and no download.
    fs = p.page.locator("article.card", has=p.page.locator(".c-where", has_text="Filesystem"))
    assert fs.count() == 0 or fs.first.locator(".c-health, .icons button").count() == 0
    # Editing anything is never asked of the proxy.
    for chev in p.page.locator(".chev").all():
        chev.click()
    p.page.wait_for_timeout(300)
    writes = [r for r in p.requests if r[0] in ("PUT", "PATCH", "DELETE")]
    assert writes == []
    assert not p.api("GET", r"/(cards|fleet/hosts|ha/devices|replicas)$")
    assert not p.api("GET", r"/thresholds$")


def test_title_and_where_line_where_the_grip_left_them(open_page):
    # The read-only head keeps the title at the grip's old offset, in line
    # with the where line and the stat row's labels under it.
    p = open_page()
    c = by_id(p, BARRACUDA)
    title = c.locator("h3.c-title").bounding_box()["x"]
    where = c.locator(".c-where").bounding_box()["x"]
    vitals = c.locator(".c-stats .st-k").first.bounding_box()["x"]
    edge = c.bounding_box()["x"]
    assert abs(title - where) < 1 and abs(where - vitals) < 1, (title, where, vitals)
    assert 30 < title - edge < 36, title - edge  # 1 px border, 14 px padding, 18 px


def test_health_box_and_details(open_page):
    p = open_page("DRIVES=mixed")
    att = by_id(p, BARRACUDA)
    box = att.locator(".hbox")
    assert box.count() == 1 and box.locator(".c-health .h-att").count() == 1
    assert box.locator(".pw-row").count() == 3 and box.locator(".pw-more").inner_text() == "and 1 more"
    assert box.locator(".pw-row").first.inner_text().replace("\n", " ").startswith("Reallocated sectors (expected 0)")
    maybe = by_id(p, RED)
    assert maybe.locator(".h-maybe").count() == 1
    assert maybe.locator(".pw-row.warn .lb").evaluate("e => getComputedStyle(e).color") == MAYBE
    assert by_id(p, NVME).locator(".hbox .h-ok").evaluate("e => getComputedStyle(e).color") == OK
    att.locator(".pw-more").click()
    p.page.wait_for_timeout(200)
    det = att.locator(".hbox .details")
    assert det.locator(".kv dt .lb").all_inner_texts() == ["Model", "Serial", "Firmware", "Capacity", "Interface"]
    assert "Data Read" not in det.inner_text() and "not applicable" not in det.inner_text()
    assert det.locator("table.attrs").count() == 1
    assert det.locator("thead th").all_inner_texts() == ["ID", "Name", "Value", "Worst", "Thresh", "Raw"]
    flagged = sorted(t.split()[0] for t in det.locator("tbody tr.crit").all_inner_texts())
    assert flagged == ["187", "197", "198", "5"], flagged
    # Raw values are text, never inputs.
    assert det.locator("input").count() == 0
    heads = dict(det.locator("thead th").evaluate_all("els => els.map((e) => [e.textContent, e.title])"))
    assert heads["Raw"] == "The real count or reading. Alerts use this"
    # Connect by hand stays in Details, with this box's address.
    rows = det.locator(".conn .cp").evaluate_all(
        "els => els.map((e) => [e.querySelector('.cp-k').textContent, e.querySelector('.cp-v').textContent])")
    assert rows == [["Host", "local-smart-sniffer-agent"], ["Port", "9099"], ["mDNS", "smartha-homeassistant"]]
    # The NVMe health log is its table.
    nv = by_id(p, NVME)
    nv.locator(".chev").click()
    assert nv.locator(".details thead th").all_inner_texts() == ["Name", "Value"]
    # Closed again, the problem rows come back.
    att.locator(".chev").click()
    assert att.locator(".hbox .pw-row").count() == 3



def test_big_numbers_read_cleanly_with_the_exact_one_behind(open_page):
    """LBAs and NVMe data units as a size, power-on hours as days, other
    large counts compact; the exact number in the tooltip and in a popover
    on click or tap, with Copy. Only what is shown changes."""
    p = open_page("DRIVES=mixed")
    att = by_id(p, BARRACUDA)
    assert re.search(r"\b\d[\d,]* d\b", att.locator(".c-stats").inner_text())
    att.locator(".chev").click()
    det = att.locator(".details")
    det.locator("table.attrs").wait_for()
    poh = det.locator("tr", has_text="Power_On_Hours").locator("td.c-raw .bign")
    assert poh.inner_text().endswith(" d") and poh.get_attribute("title").endswith(" hours")
    lba = det.locator("tr", has_text="LBAs").locator("td.c-raw .bign")
    if lba.count():
        assert lba.first.inner_text().endswith(("TB", "GB", "MB")) and "LBAs" in lba.first.get_attribute("title")
    poh.click()
    pop = p.page.locator(".numpop")
    pop.wait_for()
    assert "hours" in pop.inner_text() and pop.get_by_role("button", name="Copy").count() == 1
    # The header's copy glyph, not a word.
    assert pop.locator("button.copy svg use").get_attribute("href") == "#copy"
    assert pop.locator("button.copy").inner_text().strip() == ""
    p.page.keyboard.press("Escape")
    p.page.wait_for_timeout(100)
    assert pop.count() == 0
    nv = by_id(p, NVME)
    nv.locator(".chev").click()
    units = nv.locator("tr", has_text="data_units_written").locator("td.c-raw .bign")
    if units.count():
        assert units.inner_text().endswith(("TB", "GB", "MB")) and "data units" in units.get_attribute("title")

def test_pool_card(open_page):
    p = open_page("POOL=degraded")
    pool = by_id(p, POOL)
    assert pool.locator(".c-where").inner_text().split("\n")[0].strip() == "ZFS pool"
    assert pool.locator(".h-att").count() == 1
    assert "FAULTED" in pool.locator(".pw").inner_text()
    pool.locator(".chev").click()
    assert pool.locator(".details thead th").all_inner_texts()[:2] == ["Disk", "State"]


def test_space_bars(open_page):
    p = open_page()
    rows = by_id(p, NVME).locator(".sp-row")
    assert rows.count() == 2
    assert rows.first.locator(".sp-text").inner_text() == "41% of 980 GB"


@pytest.mark.parametrize("width", [1280, 800, 390])
def test_open_card_keeps_row_heights(open_page, width):
    """An open card grows down; no other card or the link card changes height."""
    p = open_page("DRIVES=mixed", width)
    heights = "() => [...document.querySelector('.grid').children].filter((e) => e.tagName !== 'TEMPLATE')" \
        ".map((e) => Math.round(e.getBoundingClientRect().height))"
    closed = p.page.evaluate(heights)
    i = card_ids(p).index(BARRACUDA)
    by_id(p, BARRACUDA).locator(".chev").click()
    p.page.wait_for_timeout(300)
    now = p.page.evaluate(heights)
    assert now[i] > closed[i] + 40
    assert [h for j, h in enumerate(now) if j != i] == [h for j, h in enumerate(closed) if j != i], (now, closed)
    by_id(p, BARRACUDA).locator(".chev").click()
    p.page.wait_for_timeout(300)
    assert p.page.evaluate(heights) == closed


def test_download(open_page, tmp_path):
    p = open_page()
    with p.page.expect_download() as dl:
        by_id(p, BARRACUDA).get_by_role("button", name="Download this card as a file").click()
    assert dl.value.suggested_filename == "drive-Seagate-BarraCuda-2TB-ST2000DM008.json"
    path = tmp_path / "card.json"
    dl.value.save_as(path)
    doc = json.loads(path.read_text())
    assert doc["drive"]["serial"] == "ZFL0K7Q2" and doc["drive"]["smart_data"]
    assert p.api("GET", r"/cards/self%7CZFL0K7Q2/file$")


# ── Agent states ─────────────────────────────────────────────────────────────

def test_agent_down(open_page):
    p = open_page("AGENT=down")
    assert "agent not answering" in p.page.locator(".versions").inner_text()
    seen = by_id(p, NVME).locator(".seen").inner_text()
    assert seen.startswith("Last seen: ") and seen.endswith(", not answering")
    # Asked again every 8 seconds while down; every 30 when it answers.
    assert 6000 < p.page.evaluate("Alpine.$data(document.body).selfNext - Date.now()") <= 8000
    assert link_card(p).count() == 1  # the card under the grid stays


def test_page_polls_every_30_seconds(open_page):
    p = open_page()
    wait = p.page.evaluate("Alpine.$data(document.body).selfNext - Date.now()")
    assert 27000 < wait <= 30000, wait
    ha = p.page.evaluate("Alpine.$data(document.body).haNext - Date.now()")
    assert 27000 < ha <= 30000, ha
    p.page.wait_for_timeout(2000)
    paths = {unquote(re.sub(r"\?.*", "", u[len(p.base):])) for _, u, _ in p.requests
             if not u.endswith((".js", ".css", ".png"))}
    assert paths <= {"/", "/app/info", "/fleet/hosts/self/drives", "/ha/integration"}, paths
    assert len(p.api("GET", r"/fleet/hosts/self/drives$")) == 1


def test_self_reading_card(open_page):
    p = open_page("SELF_DELAY=3")
    reading = by_id(p, "self")
    assert reading.locator(".m-msg").inner_text() == "Reading this machine's drives."
    assert reading.locator(".pill").inner_text() == "HAOS"
    assert reading.locator(".hbox, .c-health").count() == 0
    by_id(p, NVME).wait_for(timeout=6000)
    assert by_id(p, "self").count() == 0


def test_no_drives(open_page):
    p = open_page("DRIVES=0")
    assert "reports no readable drives" in p.page.locator(".empty-line").inner_text()
    assert card_ids(p) == []
    assert link_card(p).count() == 1


def test_requests_stay_home(open_page):
    p = open_page()
    p.page.wait_for_timeout(500)
    assert all(url.startswith(p.base) for _, url, _ in p.requests)


def test_ingress_prefix(browser, server):
    """Every request stays relative to the page, as it must behind ingress."""
    ctx = browser.new_context()
    page = ctx.new_page()
    seen = []
    page.on("request", lambda r: seen.append(r.url))
    page.goto(f"{server}/api/hassio_ingress/tok123/")
    page.wait_for_selector("article.card")
    page.wait_for_selector("section.link-card")
    ctx.close()
    api = [u for u in seen if not u.endswith((".js", ".css", ".png"))][1:]
    assert api and all("/api/hassio_ingress/tok123/" in u for u in api), api


# ── Header, agent line, footer ───────────────────────────────────────────────

def test_top_of_page(open_page):
    p = open_page()
    assert p.page.title() == "SMART Sniffer App"
    line = p.page.locator(".agent-line")
    assert line.inner_text().replace("\n", " ").split() == [
        "HAOS", "SMART", "AGENT:", "local-smart-sniffer-agent:9099", "mDNS", "smartha-homeassistant"]
    assert line.locator("button.copy").count() == 2
    assert [h.inner_text().strip() for h in p.page.locator(".sec-label").all()] == ["HAOS SMART AGENT:", "DRIVES:"]
    words = p.page.locator(".versions").inner_text().replace("\n", " ").split()
    assert words[:5] == ["app", "0.4.0-dev", "agent", "0.8.5", "rules"] and len(words) == 6, words
    # The header: SMART SNIFFER | App · HAOS, "· HAOS" muted. No buttons at
    # all: the hidden-cards eye and Refresh are gone (the page polls).
    head = p.page.locator("header.app-head")
    name = head.locator(".page-name")
    assert name.inner_text() == "App · HAOS"
    assert name.locator(".pn-sub").inner_text() == "· HAOS"
    assert name.evaluate("e => getComputedStyle(e).color") == "rgb(189, 189, 189)"
    assert name.locator(".pn-sub").evaluate("e => getComputedStyle(e).color") == DIM
    assert head.locator("button, .head-tools").count() == 0
    assert p.page.get_by_role("button", name="Refresh").count() == 0
    assert p.page.evaluate("typeof Alpine.$data(document.body).refresh") == "undefined"
    # The retired notice line is not there in any state.
    for flags in ("INTEGRATION=missing", ""):
        q = open_page(flags)
        assert q.page.locator(".ha-notice").count() == 0
        assert "Want sensors and alerts?" not in q.page.locator("main").inner_text()


def test_copy_buttons(open_page):
    p = open_page()
    p.ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    p.page.locator(".agent-line button.copy").first.click()
    assert p.page.evaluate("navigator.clipboard.readText()") == "local-smart-sniffer-agent:9099"
    assert p.page.locator(".agent-line .copied:visible").inner_text() == "copied"


@pytest.mark.parametrize("flags,links", [
    ("", ["App log", "App configuration", "Integration", "GitHub", "HAIR, infrared remotes in Home Assistant"]),
    ("INTEGRATION=update", ["App log", "App configuration", "Integration", "GitHub",
                            "HAIR, infrared remotes in Home Assistant"]),
    ("INTEGRATION=missing", ["App log", "App configuration", "GitHub", "HAIR, infrared remotes in Home Assistant"]),
])
def test_footer_links(open_page, flags, links):
    p = open_page(flags)
    foot = p.page.locator("footer.app-foot")
    assert foot.locator("a:visible").all_inner_texts() == links
    assert foot.get_by_text("Agentic access").count() == 0
    assert foot.get_by_role("link", name="App log").get_attribute("href") == \
        "/config/app/local_smart_sniffer_agent/logs"
    if "Integration" in links:
        assert foot.get_by_role("link", name="Integration").get_attribute("href") == \
            "/config/integrations/integration/smart_sniffer"
    assert "DAB-LABS" in foot.inner_text()


# ── The card under the grid: Control Center, install, update ────────────────

LINK_CASES = {
    "loaded": ("INTEGRATION=loaded", SEE_ALL, None, "Open", False),
    "no_entries": ("INTEGRATION=no_entries", SEE_ALL,
                   "It opens once SMART Sniffer has its first machine. Create sensors and alerts for HAOS "
                   "above, or add a machine in Settings, Devices & services.", None, False),
    "restart": ("INTEGRATION=restart", SEE_ALL, "Restart Home Assistant to finish the update.", None, False),
    "panel_failed": ("INTEGRATION=panel_failed", SEE_ALL,
                     "The Control Center did not start. Check the Home Assistant log.", None, False),
    "missing": ("INTEGRATION=missing", GET_MORE,
                "Install the SMART Sniffer integration to get these drives with sensors and alerts, "
                "and to also add other machines.", "How to install", True),
    "missing_one_drive": ("INTEGRATION=missing&DRIVES=1", GET_MORE,
                          "Install the SMART Sniffer integration to get this drive with sensors and alerts, "
                          "and to also add other machines.", "How to install", True),
    "update": ("INTEGRATION=update", GET_MORE,
               "Update the SMART Sniffer integration to 0.9 to add your other machines and see them all "
               "in the Control Center.", "Open in HACS", True),
}


@pytest.mark.parametrize("case", list(LINK_CASES))
@pytest.mark.parametrize("width", [1280, 390])
def test_link_card_states(open_page, case, width):
    flags, title, body, action, close = LINK_CASES[case]
    p = open_page(flags, width)
    lc = link_card(p)
    lc.wait_for(timeout=5000)
    assert lc.count() == 1
    assert lc.locator(".lc-title").inner_text() == title
    if body:
        assert lc.locator(".lc-body").inner_text() == body
    else:
        assert lc.locator(".lc-body").count() == 0
    acts = lc.locator(".lc-act a").all_inner_texts()
    assert acts == ([action] if action else []), acts
    assert (lc.get_by_role("button", name="Close").count() == 1) is close
    # Last in the grid, across every column, in the built card style.
    last = p.page.evaluate("""() => [...document.querySelector('.grid').children]
        .filter((e) => e.tagName !== 'TEMPLATE').pop().classList.contains('link-card')""")
    assert last
    grid = p.page.locator(".grid").bounding_box()
    box = lc.bounding_box()
    assert abs(box["width"] - grid["width"]) < 1 and abs(box["x"] - grid["x"]) < 1, (box, grid)
    style = lc.evaluate("""e => { const c = getComputedStyle(e); return [c.backgroundColor, c.borderTopColor,
        c.borderTopWidth, c.borderTopLeftRadius !== '0px']; }""")
    assert style == ["rgb(7, 7, 7)", "rgb(43, 43, 43)", "1px", True], style
    assert lc.locator(".lc-title").evaluate("e => getComputedStyle(e).color") == "rgb(241, 241, 241)"
    if body:
        # #bdbdbd at 0.86 rem.
        got = lc.locator(".lc-body").evaluate(
            "e => [getComputedStyle(e).color, parseFloat(getComputedStyle(e).fontSize)"
            " / parseFloat(getComputedStyle(document.documentElement).fontSize)]")
        assert got[0] == "rgb(189, 189, 189)" and abs(got[1] - 0.86) < 0.001, got
    if width == 390:
        # The action drops under the text.
        text = lc.locator(".lc-text").bounding_box()
        act = lc.locator(".lc-act").bounding_box()
        assert act["y"] >= text["y"] + text["height"] - 1
    assert_no_scroll(p)
    assert_no_dashes(p)
    if width < 760:
        assert_no_overflow(p)


def test_link_card_waits_for_the_integration(open_page):
    # Home Assistant not answering: no card, no Create line, no Integration
    # link; this box's drives as always.
    p = open_page("INTEGRATION=unreachable")
    assert link_card(p).count() == 0 and p.page.locator(".create").count() == 0
    assert by_id(p, NVME).count() == 1


def test_link_card_links(open_page):
    p = open_page("INTEGRATION=missing")
    link = link_card(p).get_by_role("link", name="How to install")
    assert link.get_attribute("href") == HACS_REPO
    assert link.get_attribute("target") == "_blank" and link.get_attribute("rel") == "noopener"
    p = open_page("INTEGRATION=missing&HACS=missing")
    assert link_card(p).get_by_role("link", name="How to install").get_attribute("href") == HACS_DOCS
    p = open_page("INTEGRATION=update")
    link = link_card(p).get_by_role("link", name="Open in HACS")
    assert link.get_attribute("href") == HACS_REPO and link.get_attribute("target") == "_blank"
    p = open_page("INTEGRATION=loaded")
    open_ = link_card(p).get_by_role("link", name="Open")
    assert open_.get_attribute("href") == "/smart-sniffer" and open_.get_attribute("target") == "_top"
    assert "btn" in open_.get_attribute("class")
    p = open_page("INTEGRATION=loaded&PANEL_PATH=/smart-sniffer-dev")
    assert link_card(p).get_by_role("link", name="Open").get_attribute("href") == "/smart-sniffer-dev"


@pytest.mark.parametrize("which", ["install", "update"])
def test_link_card_close_persists(open_page, browser, server, which):
    flags = "INTEGRATION=missing" if which == "install" else "INTEGRATION=update"
    p = open_page(flags)
    link_card(p).get_by_role("button", name="Close").click()
    assert link_card(p).count() == 0
    posts = p.api("POST", r"/app/notice/dismiss$")
    assert len(posts) == 1 and json.loads(posts[0][2]) == {"notice": which}
    # Another browser: closed there too (the app keeps it, not the browser).
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(f"{server}/?{flags}")
        page.wait_for_selector("article.card")
        page.wait_for_timeout(800)
        assert page.locator("section.link-card").count() == 0
    finally:
        ctx.close()


def test_link_card_never_closes_while_it_leads_somewhere(open_page):
    for flags in ("INTEGRATION=loaded", "INTEGRATION=no_entries", "INTEGRATION=restart",
                  "INTEGRATION=panel_failed"):
        p = open_page(flags)
        assert link_card(p).get_by_role("button", name="Close").count() == 0, flags


# Open: in its own tab a plain link; framed in Home Assistant, the page
# asks its parent with home-assistant/navigate, and goes there itself when
# the parent does not.

PARENT = """<!doctype html><html><body style="margin:0">
<iframe id="f" src="{src}" style="width:1200px;height:800px;border:0"></iframe>
<script>
  window.got = [];
  window.marker = 'parent';
  window.addEventListener('message', (e) => {{
    window.got.push(e.data);
    if ({handle} && e.data && e.data.type === 'home-assistant/navigate') history.pushState(null, '', e.data.path);
  }});
</script></body></html>"""


def _framed(browser, server, handle: bool):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    shell = f"{server}/ha-shell"
    page.route(shell, lambda route: route.fulfill(
        status=200, content_type="text/html",
        body=PARENT.format(src="/?INTEGRATION=loaded", handle="true" if handle else "false")))
    page.route(f"{server}/smart-sniffer", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<!doctype html><title>panel</title><p>panel</p>"))
    page.goto(shell)
    frame = page.frame_locator("#f")
    frame.locator("section.link-card").get_by_role("link", name="Open").wait_for(timeout=10000)
    return ctx, page, frame


def test_open_asks_home_assistant_to_navigate(browser, server):
    ctx, page, frame = _framed(browser, server, handle=True)
    try:
        frame.locator("section.link-card").get_by_role("link", name="Open").click()
        page.wait_for_timeout(1200)
        assert page.evaluate("window.got") == [{"type": "home-assistant/navigate", "path": "/smart-sniffer"}]
        # Home Assistant went there itself: no second, full navigation.
        assert page.evaluate("window.marker") == "parent"
        assert page.evaluate("location.pathname") == "/smart-sniffer"
    finally:
        ctx.close()


def test_open_falls_back_to_the_top_window(browser, server):
    ctx, page, frame = _framed(browser, server, handle=False)
    try:
        with page.expect_navigation(timeout=5000):
            frame.locator("section.link-card").get_by_role("link", name="Open").click()
        assert page.url == f"{server}/smart-sniffer"
        assert page.title() == "panel"
    finally:
        ctx.close()


def test_open_in_its_own_tab_is_a_plain_link(open_page, server):
    p = open_page("INTEGRATION=loaded")
    p.page.route(f"{server}/smart-sniffer", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<!doctype html><title>panel</title>"))
    with p.page.expect_navigation(timeout=5000):
        link_card(p).get_by_role("link", name="Open").click()
    assert p.page.url == f"{server}/smart-sniffer"
    p.errors.clear()


# ── Create sensors and alerts for HAOS ───────────────────────────────────────

def test_create_line_and_confirm(open_page):
    p = open_page("INTEGRATION=loaded&HAOS_ENTRY=0")
    line = p.page.get_by_role("button", name="Create sensors and alerts for HAOS")
    assert line.count() == 1
    # Directly under DRIVES:, above the cards.
    label = p.page.locator("p.sec-drives").bounding_box()
    at = line.bounding_box()
    grid = p.page.locator(".grid").bounding_box()
    assert label["y"] < at["y"] < grid["y"]
    # Built link style: underlined, paper-white, no box.
    assert line.evaluate("e => getComputedStyle(e).textDecorationLine") == "underline"
    assert line.evaluate("e => getComputedStyle(e).borderTopStyle") == "none"
    line.click()
    ask = p.page.get_by_role("group", name="Confirm")
    assert ask.locator(".ask-title").inner_text() == "Create sensors and alerts for HAOS?"
    assert ask.locator(".ask-line").inner_text() == (
        "Each of its 2 drives gets health sensors for dashboards and automations, and an alert when it needs attention.")
    assert ask.get_by_role("button").all_inner_texts() == ["Create", "Cancel"]
    ask.get_by_role("button", name="Cancel").click()
    assert p.page.locator(".create .ask").count() == 0
    line.click()
    p.page.keyboard.press("Escape")
    assert p.page.locator(".create .ask").count() == 0
    # The card under the grid says Open meanwhile.
    assert link_card(p).get_by_role("link", name="Open").count() == 1
    line.click()
    p.page.get_by_role("group", name="Confirm").get_by_role("button", name="Create").click()
    line.wait_for(state="detached", timeout=5000)
    posts = p.api("POST", r"/ha/add$")
    assert len(posts) == 1 and json.loads(posts[0][2]) == {"target": "self"}
    assert link_card(p).locator(".lc-title").inner_text() == SEE_ALL
    for text in p.page.locator("main").all_inner_texts():
        assert "to Home Assistant" not in text


def test_create_line_one_drive_and_error(open_page):
    p = open_page("INTEGRATION=loaded&HAOS_ENTRY=0&DRIVES=1&ADD=error")
    p.page.get_by_role("button", name="Create sensors and alerts for HAOS").click()
    ask = p.page.get_by_role("group", name="Confirm")
    assert ask.locator(".ask-line").inner_text() == (
        "Its drive gets health sensors for dashboards and automations, and an alert when it needs attention.")
    ask.get_by_role("button", name="Create").click()
    ask.locator(".ask-err").wait_for(timeout=5000)
    assert ask.locator(".ask-err").inner_text() == "Home Assistant did not create them. The App log may say why."
    assert p.page.get_by_role("button", name="Create sensors and alerts for HAOS").count() == 1


def test_create_line_with_no_machines_yet(open_page):
    # Integration 0.9 installed with no machine: the line, and the card says
    # how it opens. After Create, Open.
    p = open_page("INTEGRATION=no_entries")
    assert p.page.get_by_role("button", name="Create sensors and alerts for HAOS").count() == 1
    assert link_card(p).get_by_role("link", name="Open").count() == 0
    p.page.get_by_role("button", name="Create sensors and alerts for HAOS").click()
    p.page.get_by_role("group", name="Confirm").get_by_role("button", name="Create").click()
    link_card(p).get_by_role("link", name="Open").wait_for(timeout=5000)


@pytest.mark.parametrize("flags", ["INTEGRATION=loaded", "INTEGRATION=update", "INTEGRATION=missing",
                                   "INTEGRATION=restart", "INTEGRATION=panel_failed"])
def test_no_create_line(open_page, flags):
    p = open_page(flags)
    assert p.page.locator(".create").count() == 0


# ── Cards as the integration's Control Center draws them ─────────────────────
# The stat row, the Details header, Device Statistics, one number rule,
# hover-only underlines and the popovers with Copy, as in integration 0.9.

WUH = "self|9JG0X1AB"
QEMU = "self|QM00005"


def _open(c):
    c.locator("button.chev").first.click()
    c.locator(".details").first.wait_for()


def _stats(c):
    """A card's stat row as (label, value) pairs, labels as written in the
    page (the uppercase is CSS)."""
    return c.locator(".c-stats .st-t").evaluate_all(
        "els => els.map(e => [e.querySelector('dt').textContent, e.querySelector('dd').textContent])")


def test_stat_row_temperature_days_written_read_worn(open_page):
    p = open_page("DRIVES=2&VDISK=1")
    nvme = by_id(p, NVME)
    tiles = _stats(nvme)
    assert tiles == [["Temp", "41 C"], ["Powered", "92 d"], ["Written", "4.67 TB"], ["Read", "9.34 TB"],
                     ["Worn", "2%"]], tiles
    # Shown in capitals, a hairline before every tile (the first one clipped).
    assert nvme.locator(".st-k").first.inner_text() == "TEMP"
    lines = nvme.locator(".st-t").evaluate_all("els => els.map(e => getComputedStyle(e).borderLeftStyle)")
    assert set(lines) == {"solid"}, lines
    clip = nvme.locator(".c-stats").evaluate(
        "e => [e.getBoundingClientRect().left, e.querySelector('.st-t').getBoundingClientRect().left,"
        " getComputedStyle(e).overflow]")
    assert clip[1] < clip[0] and clip[2] == "hidden", clip
    # No status colours: every value in one colour, the units lighter grey.
    colors = nvme.locator(".st-v > span").evaluate_all("els => [...new Set(els.map(e => getComputedStyle(e).color))]")
    assert len(colors) == 1, colors
    # A hard disk with Device Statistics but no page 7: written and read
    # from them, never "worn".
    hdd = dict(_stats(by_id(p, WUH)))
    assert hdd["Written"] == "108 TB" and hdd["Read"] == "792 TB" and "Worn" not in hdd, hdd
    # A virtual disk that reports nothing: no row at all, no stray hairline.
    assert by_id(p, QEMU).locator(".c-stats, .st-t, .c-vitals").count() == 0
    # A SATA drive with attribute 241 only: written, no read, no worn.
    q = open_page()
    bc = dict(_stats(by_id(q, BARRACUDA)))
    assert list(bc) == ["Temp", "Powered", "Written"] and bc["Written"] == "61.2 TB", bc


def test_stat_row_leaves_out_what_is_not_reported_and_zero_degrees(open_page):
    p = open_page()
    got = p.page.evaluate("""() => {
      const x = Alpine.$data(document.body);
      const card = (stats) => ({ kind: 'drive', it: { drive: { smart_data: {} }, view: { stats } } });
      const keys = (c) => x.stats(c).map((t) => t.k + '=' + t.n + t.u);
      return {
        zero: keys(card({ temp_c: 0, power_on_hours: 12000 })),
        missing: keys(card({ power_on_hours: 30 })),
        none: x.stats(card({ temp_c: 0 })).length,
        warm: keys(card({ temp_c: 41 })),
        pool: x.stats({ kind: 'pool', it: {} }).length,
      };
    }""")
    assert got == {"zero": ["Powered=500 d"], "missing": ["Powered=30 h"], "none": 0, "warm": ["Temp=41 C"],
                   "pool": 0}, got


@pytest.mark.parametrize("width", [1100, 390])
def test_stat_row_is_one_row_with_all_five(open_page, width):
    p = open_page("", width)
    nvme = by_id(p, NVME)
    tops = nvme.locator(".st-t").evaluate_all("els => els.map(e => Math.round(e.getBoundingClientRect().top))")
    assert len(tops) == 5 and len(set(tops)) == 1, tops
    # The pool card keeps its plain line: no stat row.
    q = open_page("POOL=online", width)
    assert by_id(q, POOL).locator(".c-stats").count() == 0 and by_id(q, POOL).locator(".c-vitals").count() == 1


@pytest.mark.parametrize("width", [1280, 390])
def test_details_top_names_the_drive(open_page, width):
    p = open_page("DRIVES=mixed", width)
    bc = by_id(p, BARRACUDA)
    _open(bc)
    ident = bc.locator("dl.kv.ident")
    assert ident.locator("dt").all_inner_texts() == ["Model", "Serial", "Firmware", "Capacity", "Interface"]
    values = ident.locator("dd").all_inner_texts()
    assert values == ["Seagate BarraCuda 2TB ST2000DM008", "ZFL0K7Q2", "0001", "2.00 TB", "SATA 6.0 Gb/s"], values
    # No copy glyph at rest: model, serial and firmware open the popover;
    # capacity keeps its exact bytes behind it; every value lines up right.
    assert bc.locator(".details button.copy, .details .copied").count() == 0
    assert ident.locator(".bign.txt").all_inner_texts() == values[:3]
    assert ident.locator(".bign:not(.txt)").get_attribute("title") == "2,000,398,934,016 bytes"
    rights = ident.locator("dd").evaluate_all(
        "els => els.map(e => Math.round((e.querySelector('.v') || e).getBoundingClientRect().right))")
    assert len(set(rights)) == 1, rights
    # The drive's own lines, then Connect by hand, then the table.
    order = bc.locator(".details > *").evaluate_all("els => els.map(e => e.className)")
    assert order.index("kv ident") < order.index("conn") < order.index("table-wrap"), order
    # A link slower than the drive says both speeds; NVMe says NVMe and has
    # no Device Statistics.
    red = by_id(p, RED)
    _open(red)
    assert red.locator("dl.kv.ident dd").all_inner_texts()[-1] == "SATA 3.0 Gb/s (max 6.0 Gb/s)"
    nv = by_id(p, NVME)
    _open(nv)
    assert nv.locator("dl.kv.ident dd").all_inner_texts()[2:] == ["5B2QGXA7", "1.00 TB", "NVMe"]
    assert nv.locator(".devstat").count() == 0
    assert_no_scroll(p)
    assert_inside_cards(p)


def test_device_statistics_table_takes_the_stand_in_rows(open_page):
    p = open_page("DRIVES=2")
    c = by_id(p, WUH)
    _open(c)
    attrs = c.locator("table.attrs").first
    attrs.wait_for()
    # The page 4 count is not in the attribute table but in Device
    # Statistics, under its own small heading, inside the same Details.
    assert attrs.locator("tr", has_text="Reported uncorrectable").count() == 0
    assert c.locator(".details .devstat h4").inner_text().lower() == "device statistics"
    ds = c.locator("table.devstat-t")
    names = ds.locator("td.nm .lb").all_inner_texts()
    assert {"Power cycles", "Reported uncorrectable", "Command resets", "Temperature, lifetime max",
            "Temperature, lifetime min", "Time above the operating maximum", "Hardware resets",
            "Interface CRC errors"} <= set(names), names
    # What the stat row shows is left out here.
    assert not {"Temperature", "Power-on hours", "Data written", "Data read", "Endurance used"} & set(names), names
    vals = ds.locator("td.c-raw").all_inner_texts()
    assert vals[names.index("Temperature, lifetime max")] == "47 C"
    assert vals[names.index("Time above the operating maximum")] == "0 min"
    assert ds.locator("tr", has_text="Reported uncorrectable").locator("td.c-raw").inner_text() == "0"
    # Read only: no inputs, no editor.
    assert c.locator(".details input, .details .tune, .details .thr").count() == 0


def test_one_number_rule_everywhere(open_page):
    p = open_page()
    bc = by_id(p, BARRACUDA)
    written = dict(_stats(bc))["Written"]
    _open(bc)
    attr = bc.locator("table.attrs tr", has_text="Total_LBAs_Written").locator(".bign").inner_text()
    assert attr == written, (attr, written)
    fmt = p.page.evaluate("""() => {
      const x = Alpine.$data(document.body);
      return [x.bytes(5104198542848), x.bytes(19834000000000), x.bytes(898000000000), x.bytes(999.6e9),
              x.bytes(512), x.compact(344046933), x.compact(12345), x.bytes(2000398934016)];
    }""")
    assert fmt == ["5.10 TB", "19.8 TB", "898 GB", "1.00 TB", "512 B", "344 M", "12,345", "2.00 TB"], fmt
    js = (WEB_DIR / "cc.js").read_text()
    assert "tb(" not in js and "toFixed" not in js


def test_formatted_numbers_underline_only_on_hover(open_page):
    p = open_page()
    big = by_id(p, NVME).locator(".c-stats .bign").first
    deco = lambda: big.evaluate("e => getComputedStyle(e).textDecorationLine")  # noqa: E731
    assert p.page.evaluate("matchMedia('(hover: hover)').matches")
    p.page.mouse.move(0, 0)
    assert deco() == "none"
    big.hover()
    assert p.wait(lambda: deco() == "underline")
    assert big.evaluate("e => getComputedStyle(e).cursor") == "pointer" and big.get_attribute("title")
    big.click()
    p.page.locator(".numpop").wait_for()


def test_a_phone_gets_no_underline_and_a_tap_opens_the_number(open_page):
    p = open_page("", 390, touch=True)
    assert not p.page.evaluate("matchMedia('(hover: hover)').matches")
    big = by_id(p, NVME).locator(".c-stats .bign").first
    big.tap()
    pop = p.page.locator(".numpop")
    pop.wait_for()
    assert "hours" in pop.inner_text() and pop.locator("button.copy").count() == 1
    assert big.evaluate("e => getComputedStyle(e).textDecorationLine") == "none"


def _text_fields(c):
    """(label, value, trigger) for Model, Serial and Firmware in Details and
    Host, Port and mDNS under Connect by hand."""
    out = []
    for row in c.locator("dl.kv.ident > div").all():
        label = row.locator("dt").inner_text()
        if label in ("Model", "Serial", "Firmware"):
            out.append((label, row.locator("dd").text_content().strip(), row.locator(".bign.txt")))
    for cp in c.locator(".conn .cp").all():
        out.append((cp.locator(".cp-k").inner_text(), cp.locator(".cp-v").text_content(), cp.locator(".bign.txt")))
    return out


def _text_card(open_page, width=1280, touch=False):
    p = open_page("DRIVES=mixed", width, touch=touch)
    c = by_id(p, BARRACUDA)
    _open(c)
    fields = _text_fields(c)
    assert [f[0] for f in fields] == ["Model", "Serial", "Firmware", "Host", "Port", "mDNS"], fields
    return p, c, fields


@pytest.mark.parametrize("width", [1280, 390])
def test_details_text_values_open_the_popover_and_copy_the_value(open_page, width):
    p, c, fields = _text_card(open_page, width)
    p.ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    pop = p.page.locator(".numpop")
    for label, value, trigger in fields:
        trigger.click()
        pop.wait_for()
        # The exact value, whole: letters, dots and spaces kept.
        assert pop.locator(".cp-v").text_content() == value, (label, value)
        assert pop.locator("button.copy svg use").get_attribute("href") == "#copy"
        box = pop.evaluate("e => e.getBoundingClientRect().toJSON()")
        assert box["left"] >= 0 and box["right"] <= width, (label, box)
        # Under the value where it fits, else pulled in from the right edge.
        left = trigger.evaluate("e => e.getBoundingClientRect().left")
        if left + box["width"] <= width - 8:
            assert abs(box["left"] - left) <= 1, (label, left, box)
        else:
            assert abs(box["right"] - (width - 8)) <= 1, (label, box)
        pop.locator("button.copy").click()
        assert p.wait(lambda: pop.locator(".copied").is_visible() and pop.locator(".copied").inner_text() == "copied")
        assert p.page.evaluate("navigator.clipboard.readText()") == value, label
        # A second click on the value closes it.
        trigger.click()
        assert p.wait(lambda: pop.count() == 0), label
    assert_no_scroll(p)


def test_copy_leaves_nothing_selected(open_page):
    """A phone that selected the value (iOS on a tap) keeps no selection
    handles over it once Copy has copied; Copied still shows."""
    p, c, fields = _text_card(open_page, 390, touch=True)
    p.ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    pop = p.page.locator(".numpop")
    label, value, trigger = fields[1]  # the serial
    trigger.tap()
    pop.wait_for()
    p.page.evaluate("""() => { const r = document.createRange();
      r.selectNodeContents(document.querySelector('.numpop .cp-v'));
      const s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }""")
    assert p.page.evaluate("window.getSelection().toString()") == value
    pop.locator("button.copy").tap()
    assert p.wait(lambda: pop.locator(".copied").is_visible() and pop.locator(".copied").inner_text() == "copied")
    assert p.page.evaluate("[window.getSelection().rangeCount, window.getSelection().toString()]") == [0, ""]
    assert p.page.evaluate("navigator.clipboard.readText()") == value
    # The header's copy buttons clear it the same way.
    p.page.evaluate("""() => { const r = document.createRange();
      r.selectNodeContents(document.querySelector('.agent-line .cp-v'));
      window.getSelection().addRange(r); }""")
    p.page.locator(".agent-line button.copy").first.tap()
    assert p.wait(lambda: p.page.evaluate("window.getSelection().rangeCount") == 0)


def test_a_big_number_still_copies_its_digits(open_page):
    p = open_page()
    p.ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    bc = by_id(p, BARRACUDA)
    _open(bc)
    lba = bc.locator("table.attrs tr", has_text="Total_LBAs_Written").locator(".bign")
    exact = lba.get_attribute("title")
    lba.click()
    pop = p.page.locator(".numpop")
    pop.wait_for()
    assert pop.locator(".cp-v").inner_text() == exact
    pop.locator("button.copy").click()
    assert p.wait(lambda: pop.locator(".copied").is_visible())
    assert p.page.evaluate("navigator.clipboard.readText()") == re.sub(r"[^0-9]", "", exact)


@pytest.mark.parametrize("touch", [False, True])
def test_no_copy_glyph_at_rest_in_details(open_page, touch):
    p, c, fields = _text_card(open_page, 390 if touch else 1280, touch)
    assert c.locator(".details button.copy, .details .copied").count() == 0
    assert p.page.locator(".numpop").count() == 0
    for label, _value, trigger in fields:
        assert trigger.evaluate("e => e.parentElement.querySelectorAll('button, svg').length") == 0, label


def test_text_values_underline_only_on_hover(open_page):
    p, c, fields = _text_card(open_page)
    for label, _value, trigger in fields:
        style = lambda t=trigger: t.evaluate(  # noqa: E731
            "e => { const s = getComputedStyle(e); return [s.textDecorationLine, s.textDecorationStyle, s.cursor]; }")
        p.page.mouse.move(0, 0)
        assert p.wait(lambda s=style: s()[0] == "none"), label
        assert trigger.get_attribute("title") is None, label
        trigger.hover()
        assert p.wait(lambda s=style: s()[0] == "underline"), label
        assert style()[1:] == ["dotted", "pointer"], (label, style())


def test_a_phone_gets_no_underline_on_text_values_and_a_tap_opens_them(open_page):
    p, c, fields = _text_card(open_page, 390, touch=True)
    pop = p.page.locator(".numpop")
    for label, value, trigger in fields:
        trigger.tap()
        pop.wait_for()
        assert pop.locator(".cp-v").text_content() == value, label
        assert trigger.evaluate("e => getComputedStyle(e).textDecorationLine") == "none", label
        trigger.tap()
        assert p.wait(lambda: pop.count() == 0), label


def test_text_values_open_and_close_from_the_keyboard(open_page):
    p, c, fields = _text_card(open_page)
    pop = p.page.locator(".numpop")
    for label, value, trigger in fields:
        assert trigger.get_attribute("role") == "button" and trigger.get_attribute("tabindex") == "0", label
        assert trigger.get_attribute("aria-label") == f"{label} {value}"
        trigger.focus()
        for key in ("Enter", " "):
            p.page.keyboard.press(key)
            assert p.wait(lambda: pop.count() == 1), (label, key)
            assert pop.locator(".cp-v").text_content() == value
            p.page.keyboard.press("Escape")
            assert p.wait(lambda: pop.count() == 0), (label, key)
    big = c.locator(".c-stats .bign").first
    big.focus()
    p.page.keyboard.press(" ")
    assert p.wait(lambda: pop.count() == 1)
    p.page.keyboard.press("Escape")
    assert p.wait(lambda: pop.count() == 0)


@pytest.mark.parametrize("width", [1100, 390])
def test_attribute_names_never_break_inside_a_word(open_page, width):
    p = open_page("", width)
    nv = by_id(p, NVME)
    _open(nv)
    table = nv.locator("table.attrs").first
    names = table.locator("td.nm .lb").all_inner_texts()
    assert "available_spare_threshold" in names, names
    split = table.locator("td.nm .lb > span > span").evaluate_all(
        "els => els.filter(e => e.getClientRects().length > 1).map(e => e.textContent)")
    assert split == [], split
    # A very long raw value wraps in its own box: the table stays inside
    # the card and the names keep their width.
    fit = table.evaluate("""t => {
      const cell = [...t.querySelectorAll('tbody tr')][1].querySelector('td.c-raw');
      const s = document.createElement('span');
      s.className = 'long';
      s.textContent = '1449400005035951791936293027446784';
      cell.replaceChildren(s);
      return [t.getBoundingClientRect().width <= t.parentElement.getBoundingClientRect().width + 1,
              t.querySelector('tbody tr td.nm').getBoundingClientRect().width];
    }""")
    assert fit[0] and fit[1] > 150, fit
    assert_no_scroll(p)


@pytest.mark.parametrize("width", [1270, 1100, 390])
def test_short_raw_values_stay_on_one_line(open_page, width):
    """A raw value of 20 characters or fewer never wraps, also in a narrow
    card (three columns or a phone); the table stays inside the card."""
    p = open_page("", width)
    c = by_id(p, BARRACUDA)
    _open(c)
    table = c.locator("table.attrs").first
    got = table.evaluate("""t => {
      const rows = [...t.querySelectorAll('tbody tr')];
      return ['38 (0 18 0 0 0)', '38 (Min/Max 19/48) C'].map((text, i) => {
        const span = rows[i].querySelector('td.c-raw span');
        span.textContent = text;
        const names = [...t.querySelectorAll('td.nm .lb > span > span')]
          .filter(e => e.getClientRects().length > 1).length;
        return [span.getClientRects().length, names,
                t.getBoundingClientRect().width <= t.parentElement.getBoundingClientRect().width + 1];
      });
    }""")
    assert got == [[1, 0, True], [1, 0, True]], got
    assert_no_scroll(p)


def test_name_parts_and_long_values_in_the_page(open_page):
    p = open_page()
    got = p.page.evaluate("""() => {
      const x = Alpine.$data(document.body);
      return [x.nameParts('available_spare_threshold'), x.nameParts('Power cycles'), x.nameParts(null)];
    }""")
    assert got == [["available_", "spare_", "threshold"], ["Power cycles"], [""]], got
    html = (WEB_DIR / "index.html").read_text()
    assert ':class="{ long: String(a.disp).length > 20 }"' in html


def test_device_statistics_trim_is_a_fixed_page_offset_list():
    js = (WEB_DIR / "cc.js").read_text()
    m = re.search(r"const DEVSTAT_IN_STATS = new Set\(\[([^\]]*)\]\)", js)
    assert m, "DEVSTAT_IN_STATS not found"
    assert sorted(re.findall(r"'(\d+:\d+)'", m.group(1))) == sorted(["5:8", "1:16", "1:24", "1:40", "7:8"])


@pytest.mark.parametrize("width", [1280, 390])
def test_header_names_the_page_at_every_width(open_page, width):
    p = open_page("", width)
    name = p.page.locator("header.app-head .page-name")
    assert name.is_visible() and name.inner_text() == "App · HAOS"
    word = p.page.locator("header.app-head .wordmark").bounding_box()
    box = name.bounding_box()
    head = p.page.locator("header.app-head").bounding_box()
    if width < 760:
        # Its own line under the wordmark, starting where the wordmark does,
        # with no rule before it, smaller.
        assert box["y"] >= word["y"] + word["height"] - 1 and abs(box["x"] - word["x"]) < 1, (box, word)
        assert name.evaluate("e => getComputedStyle(e).borderLeftStyle") == "none"
        assert name.evaluate("e => parseFloat(getComputedStyle(e).fontSize)") < 14
        assert_no_overflow(p)
    else:
        assert box["x"] > word["x"] + word["width"] and name.evaluate(
            "e => getComputedStyle(e).borderLeftStyle") == "solid"
    assert box["x"] + box["width"] <= head["x"] + head["width"]
    assert_no_scroll(p)


# ── Screenshots, named by state ──────────────────────────────────────────────

SHOT_STATES = [
    ("app-1-open", "INTEGRATION=loaded"),
    ("app-1-create", "INTEGRATION=loaded&HAOS_ENTRY=0"),
    ("app-1a-no-machines", "INTEGRATION=no_entries"),
    ("app-1b-restart", "INTEGRATION=restart"),
    ("app-1c-panel-failed", "INTEGRATION=panel_failed"),
    ("app-2-install", "INTEGRATION=missing"),
    ("app-3-update", "INTEGRATION=update"),
    ("app-agent-down", "AGENT=down"),
    ("app-no-drives", "DRIVES=0&INTEGRATION=missing"),
]


def test_screenshots(open_page):
    SHOTS.mkdir(parents=True, exist_ok=True)
    for width in WIDTHS:
        for name, flags in SHOT_STATES:
            p = open_page(flags, width)
            p.page.wait_for_timeout(250)
            p.page.screenshot(path=str(SHOTS / f"{name}-{width}.png"), full_page=True)
            assert_no_scroll(p)
            if width < 760:
                assert_no_overflow(p)
        # Details open, and the Create confirm open.
        p = open_page("INTEGRATION=loaded&HAOS_ENTRY=0", width)
        p.page.get_by_role("button", name="Create sensors and alerts for HAOS").click()
        by_id(p, BARRACUDA).locator(".chev").click()
        p.page.wait_for_timeout(300)
        p.page.screenshot(path=str(SHOTS / f"app-1-create-confirm-details-{width}.png"), full_page=True)
