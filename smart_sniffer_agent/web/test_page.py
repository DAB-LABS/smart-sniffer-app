"""Page checks for the Control Center, run against devserver.py in Chromium.

Development only, not part of the image. Needs pytest and Playwright, and the
real Alpine and Tailwind files (the repo holds placeholders): put them in a
folder and set VENDOR_DIR.

    VENDOR_DIR=/path/to/vendor pytest web/test_page.py
    PW_CHROMIUM=/path/to/chrome   use this Chromium instead of Playwright's own
    PAGE_SHOTS=/some/dir          where test_screenshots writes (default
                                  page-shots in the system's temp folder)
    SHOT_WIDTHS=1100,390          widths for the screenshots (default 1280,390)

test_screenshots saves one picture per state of the card design, named by
the state and the width: grid-1280.png is the whole grid at 1280 px. They are
for looking at by eye; the other tests assert.
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
    for key in ("AGENT", "DRIVES", "VDISK", "RULES", "HA", "HA_LOOKUP", "HA_SELF", "ADD", "NETWORK", "SCAN",
                "HOSTS", "POOL", "REPLICAS", "REPLICA_FOLDER", "HOSTNAME", "SLUG", "HA_VERSION", "MDNS"):
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


def by_id(p: Page, card_id: str):
    return p.page.locator(f'article.card[data-card="{card_id}"]')


def card_ids(p: Page) -> list:
    return p.page.eval_on_selector_all(".grid > article.card", "els => els.map((e) => e.dataset.card)")


# Card ids of the harness's board world.
NVME = "self|S5GXNF0R412233A"
BARRACUDA = "self|ZFL0K7Q2"
NAS = "10.0.10.20:9099"
NAS_ATT = f"{NAS}|VK0ABCDE"
NAS_MAYBE = f"{NAS}|VK0FGHIJ"
NAS_USB = f"{NAS}|DD56419883961"
NAS_POOL = f"{NAS}|pool:tank"
NAS_FS = f"{NAS}|fs:fs-9daebfc0"
STUDIO = "10.0.10.44:9099|0ba01c2d4e5f6a7b"
BACKUP_PI = "10.0.10.52:9099"
REP_FAILED = "replicas|MOCK-SATA-0001"
REP_NVME = "replicas|MOCK-NVME-0001"
REP_POOL = "replicas|pool:tank"
NAS_OVER = f"{NAS}|ZA1B2C3D"  # in Home Assistant, 8 reallocated over its threshold of 5
NAS_ACCEPTED = f"{NAS}|ZA1B2C3E"  # in Home Assistant, 2 reallocated within its threshold of 4
DIM = "rgb(143, 143, 143)"


def last_is_plus(p: Page) -> bool:
    return p.page.evaluate("""() => [...document.querySelector('.grid').children]
        .filter((e) => e.tagName !== 'TEMPLATE').pop().classList.contains('ghost')""")


def open_add(p: Page):
    p.page.get_by_role("button", name="Add a drive, or a replica for testing").click()
    p.page.wait_for_selector("[role=region][aria-label=Add]")


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
    blue: (el) => !!el.closest('.exit:not(.add)'),
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
      // A colour inherited by a wrapper that draws no text or box is not drawn.
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
    assert "\u2014" not in text and "\u2013" not in text, "an em-dash or en-dash in the page"
    assert "\u2713" not in text and "\u2714" not in text, "a check mark in the page"


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
      if (getComputedStyle(a).overflowX !== 'visible') return true;
    }
    return false;
  };
  const out = [];
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || el.closest('#drag-layer')) continue;
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
    ("", {"Attention", "Maybe", "Health OK", "Health not known", "Health FAILED"}, (True, True)),
    ("AGENT=down", {"Health not known", "Attention", "Maybe", "Health OK"}, (True, True)),
    ("HA=not_installed", {"Attention", "Maybe", "Health OK"}, None),
    ("HA=unreachable", {"Attention", "Maybe", "Health OK"}, None),
    ("HA_LOOKUP=entries_only", {"Attention", "Health OK"}, (True, True)),
    ("HA_SELF=0&VDISK=1", {"Health not known", "Health OK", "Attention"}, (True, True)),
    ("RULES=missing", {"Health not known"}, (True, True)),
    ("POOL=degraded", {"Attention", "Maybe"}, (True, True)),
    ("DRIVES=0", {"Attention", "Maybe"}, (True, True)),
    ("HOSTS=none&REPLICAS=0&HA=not_installed&DRIVES=1", {"Health OK"}, None),
]


@pytest.mark.parametrize("flags,words,glyphs", STATES, ids=[s[0] or "board" for s in STATES])
def test_state_rules(open_page, flags, words, glyphs):
    """Health words, colours, exit icons, copy and phone width on each switch set."""
    for width in (1280, 390):
        p = open_page(flags, width)
        p.page.wait_for_timeout(300)
        assert words <= health_words(p), (words, health_words(p))
        blue = p.page.locator("a.exit:visible").count()
        grey = p.page.locator("button.exit.add:visible").count()
        if glyphs is None:
            assert blue == 0 and grey == 0
        else:
            assert (blue > 0, grey > 0) == glyphs, (blue, grey)
        assert p.page.locator("h2").count() == 0  # no section headings
        assert_colours(p)
        assert_no_dashes(p)
        assert_no_scroll(p)
        assert_inside_cards(p)
        if width < 760:
            assert_no_overflow(p)
        # Details open and the + card open must hold too.
        for chev in p.page.locator(".chev").all()[:5]:
            chev.click()
        open_add(p)
        p.page.wait_for_timeout(600)
        assert_colours(p)
        assert_no_dashes(p)
        assert_no_scroll(p)
        assert_inside_cards(p)
        if width < 760:
            assert_no_overflow(p)


def test_colours(open_page):
    p = open_page()
    colours = p.page.evaluate("""() => Object.fromEntries(['.h-ok', '.h-att', '.h-maybe', '.h-fail b',
        '.chev.attention', '.chev.maybe', '.chev.ok', 'a.exit']
        .map((s) => [s, getComputedStyle(document.querySelector(s)).color]))""")
    assert colours == {".h-ok": OK, ".h-att": ATT, ".h-maybe": MAYBE, ".h-fail b": ATT,
                       ".chev.attention": ATT, ".chev.maybe": MAYBE, ".chev.ok": OK, "a.exit": BLUE}
    assert p.page.evaluate("getComputedStyle(document.querySelector('button.exit.add')).color") != BLUE
    # The health box: a 1 px frame in the state colour at 25%, all the way
    # round, and no fill; Health not known is grey (#8f8f8f) at 25%, dashed.
    for cid, style, frame in ((NAS_ATT, "solid", "rgba(204, 107, 99, 0.25)"),
                              (NAS_MAYBE, "solid", "rgba(204, 159, 82, 0.25)"),
                              (NVME, "solid", "rgba(93, 163, 114, 0.25)"),
                              (NAS_USB, "dashed", "rgba(143, 143, 143, 0.25)")):
        assert by_id(p, cid).locator(".hbox").evaluate("e => getComputedStyle(e).backgroundColor") == "rgba(0, 0, 0, 0)", cid
        box = by_id(p, cid).locator(".hbox").evaluate(BOX_STROKE)
        assert box == [[frame, "1px", style]] * 4, (cid, box)
    # The words in the box keep their full colour.
    assert by_id(p, NAS_ATT).locator(".hbox .h-att").evaluate("e => getComputedStyle(e).color") == ATT
    assert by_id(p, NAS_MAYBE).locator(".hbox .h-maybe").evaluate("e => getComputedStyle(e).color") == MAYBE
    assert by_id(p, NVME).locator(".hbox .h-ok").evaluate("e => getComputedStyle(e).color") == OK
    # A flagged row's label and value are in its state colour; its note is not.
    for mark, rgb, cid in (("crit", ATT, NAS_ATT), ("warn", MAYBE, NAS_MAYBE)):
        row = by_id(p, cid).locator(f".pw-row.{mark}").first
        assert row.locator(".lb").evaluate("e => getComputedStyle(e).color") == rgb
        assert row.locator(".pw-val").evaluate("e => getComputedStyle(e).color") == rgb
        assert row.locator("small").evaluate("e => getComputedStyle(e).color") not in (ATT, MAYBE)
    assert p.page.locator(".mk, .pw-row .mark").count() == 0
    # Pills and the health box have sharp corners; cards round ones.
    radii = p.page.evaluate("""() => ['.pill', '.hbox', '.card'].map((s) =>
        getComputedStyle(document.querySelector(s)).borderTopLeftRadius)""")
    assert radii[0] == "0px" and radii[1] == "0px" and radii[2] != "0px"


BOX_STROKE = """e => ['Top', 'Right', 'Bottom', 'Left'].map((s) => {
  const cs = getComputedStyle(e);
  return [cs['border' + s + 'Color'], cs['border' + s + 'Width'], cs['border' + s + 'Style']];
})"""

# Elements inside a card's health box that scroll, or would.
BOX_SCROLLERS = """els => els.flatMap((b) => [b, ...b.querySelectorAll('*')])
  .filter((e) => { const cs = getComputedStyle(e);
    return ['auto', 'scroll'].includes(cs.overflowX) || ['auto', 'scroll'].includes(cs.overflowY)
      || e.scrollHeight > e.clientHeight + 1 && cs.overflowY !== 'visible'; })
  .map((e) => e.outerHTML.slice(0, 80))"""


def test_health_box(open_page):
    """One framed box: health line and chevron, three rows and "and N more" closed, Details open."""
    for width in (1280, 390):
        p = open_page("", width)
        att = by_id(p, NAS_ATT)
        box = att.locator(".hbox")
        assert box.count() == 1
        # The health line and the chevron are in the box, the chevron at its top right.
        assert box.locator(".c-health .h-att").count() == 1 and box.locator(".c-health .chev").count() == 1
        b, ch = box.bounding_box(), box.locator(".chev").bounding_box()
        assert b["x"] + b["width"] - (ch["x"] + ch["width"]) < 16 and ch["y"] - b["y"] < 16, (b, ch)
        # Closed: the first three of five problem rows, then "and 2 more", muted.
        assert box.locator(".pw-row").count() == 3
        assert box.locator(".pw-row").first.inner_text().replace("\n", " ").startswith("Reallocated sectors (expected 0)")
        more = box.locator(".pw-more")
        assert more.inner_text() == "and 2 more"
        assert more.evaluate("e => getComputedStyle(e).color") not in (ATT, MAYBE, OK)
        # A card with three problem rows or fewer has no "and N more".
        maybe = by_id(p, NAS_MAYBE)
        assert 1 <= maybe.locator(".hbox .pw-row").count() <= 3 and maybe.locator(".pw-more").count() == 0
        # A healthy card's box holds just Health OK and the chevron.
        ok = by_id(p, NVME).locator(".hbox")
        assert ok.locator(".pw, .pw-row, .details").count() == 0
        assert ok.locator(".c-health").inner_text().strip() == "Health OK" and ok.locator(".chev").count() == 1
        # Nothing in any box scrolls, closed.
        assert p.page.eval_on_selector_all(".hbox", BOX_SCROLLERS) == []
        # "and N more" opens Details, inside the same bordered element.
        handle = box.element_handle()
        more.click()
        p.page.wait_for_timeout(200)
        assert att.locator(".chev").get_attribute("aria-expanded") == "true"
        assert att.locator(".hbox").count() == 1
        assert att.locator(".hbox").evaluate("(e, h) => e === h", handle)
        assert att.locator(".hbox .details table.attrs tr.crit").count() >= 3
        assert att.locator(".hbox .details table.attrs").count() == 1
        assert att.locator(".details").evaluate("e => !!e.closest('.hbox')")
        assert att.locator(".hbox .pw").count() == 0
        # Open, the frame goes all the way round everything in Details.
        b = att.locator(".hbox").bounding_box()
        for el in att.locator(".hbox .details > :not(template)").all():
            r = el.bounding_box()
            assert r["x"] >= b["x"] and r["x"] + r["width"] <= b["x"] + b["width"] + 0.5, (r, b)
            assert r["y"] >= b["y"] and r["y"] + r["height"] <= b["y"] + b["height"] + 0.5, (r, b)
        assert att.locator(".hbox").evaluate(BOX_STROKE)[0][0] == "rgba(204, 107, 99, 0.25)"
        # Open, there is still no fill.
        assert att.locator(".hbox").evaluate("e => getComputedStyle(e).backgroundColor") == "rgba(0, 0, 0, 0)"
        # Open every box: still nothing scrolls.
        closed = p.page.locator(".chev[aria-expanded=false]")
        while closed.count():
            closed.first.click()
        p.page.wait_for_timeout(200)
        assert p.page.eval_on_selector_all(".hbox", BOX_SCROLLERS) == []
        # And closed again, back to three rows and the line.
        att.locator(".chev").click()
        assert att.locator(".hbox .pw-row").count() == 3 and att.locator(".hbox .pw-more").count() == 1
        # No box on a FILESYSTEM card, the placeholder or this app's agent down.
        assert by_id(p, NAS_FS).locator(".hbox").count() == 0
        assert by_id(p, BACKUP_PI).locator(".hbox").count() == 0
        # Health FAILED stays next to Attention, in the box.
        assert by_id(p, REP_FAILED).locator(".hbox .c-health .h-fail").count() == 1


BOX_BOTTOMS = """els => els.map((c) => {
  const box = c.querySelector('.hbox'), rule = c.querySelector('.c-foot');
  if (!box) return null;
  const b = box.getBoundingClientRect(), r = rule.getBoundingClientRect(), top = c.querySelector('.c-head').getBoundingClientRect();
  return { state: box.className, bottom: b.bottom, rule: r.top, head: top.top - c.getBoundingClientRect().top };
})"""


@pytest.mark.parametrize("width", [1280, 390])
def test_health_box_sits_on_footer(open_page, width):
    """The health box ends on the footer rule; spare height goes above it, the top rows stay at the top."""
    p = open_page("", width)
    cards = p.page.eval_on_selector_all(".grid > article.card", BOX_BOTTOMS)
    first = by_id(p, NVME).evaluate("e => e.getBoundingClientRect().top")
    row = [c for c, top in zip(cards, p.page.eval_on_selector_all(
        ".grid > article.card", "els => els.map((e) => e.getBoundingClientRect().top)")) if abs(top - first) < 1]
    boxes = [c for c in row if c]
    for c in boxes:
        assert 0 <= c["rule"] - c["bottom"] <= 8, c
        assert c["head"] < 16, c
    if width == 1280:
        # Mixed states in one grid row: Health OK and a five-line Attention share one bottom edge.
        states = {c["state"].split()[-1] for c in boxes}
        assert len(boxes) == 3 and {"ok", "attention"} <= states, boxes
        assert max(c["bottom"] for c in boxes) - min(c["bottom"] for c in boxes) <= 1, boxes
    else:
        # One column: every card's box sits on its own footer rule.
        assert all(0 <= c["rule"] - c["bottom"] <= 8 for c in cards if c), cards
    # Open, the box still ends on the footer rule.
    att = by_id(p, NAS_ATT)
    att.locator(".chev").click()
    p.page.wait_for_timeout(200)
    b, r = att.locator(".hbox").bounding_box(), att.locator(".c-foot").bounding_box()
    assert 0 <= r["y"] - (b["y"] + b["height"]) <= 8, (b, r)
    # Cards with no box keep their content at the top and the footer at the bottom.
    for cid in (NAS_FS, BACKUP_PI):
        card, foot = by_id(p, cid).bounding_box(), by_id(p, cid).locator(".c-foot").bounding_box()
        assert card["y"] + card["height"] - (foot["y"] + foot["height"]) < 16, (cid, card, foot)
        assert by_id(p, cid).locator(".c-head").bounding_box()["y"] - card["y"] < 16, cid


TABLE_ROWS = """t => [...t.querySelectorAll('tbody tr')].map((r) => [r.className,
  ...[...r.cells].filter((c) => getComputedStyle(c).display !== 'none').map((c) => c.innerText.trim())])"""


def test_details_one_table_with_flags(open_page):
    """Details: a short summary, then one table; flagged rows in the state colour, notes muted after the name."""
    p = open_page()
    att = by_id(p, NAS_ATT)
    att.locator(".chev").click()
    det = att.locator(".details")
    # The summary keeps Data Read and Device Statistics; no watched list.
    assert det.locator(".kv dt .lb").all_inner_texts() == ["Data Read", "Device Statistics"]
    assert det.locator("table").count() == 1 and det.locator(".table-wrap").count() == 1
    heads = det.locator("thead th").all_inner_texts()
    assert heads == ["ID", "Name", "Value", "Worst", "Thresh", "Raw"], heads
    rows = det.locator("table.attrs").evaluate(TABLE_ROWS)
    flagged = {r[1]: r for r in rows if r[0] in ("crit", "warn")}
    assert sorted(flagged) == ["187", "188", "197", "198", "5"], flagged
    assert flagged["5"][0] == "crit" and flagged["188"][0] == "warn"
    assert flagged["5"][2] == "Reallocated_Sector_Ct (expected 0)" and flagged["5"][-1] == "24"
    assert flagged["188"][2] == "Command_Timeout (warns above 100)"
    # Each reading appears once: no name twice in the table.
    names = [r[2].split(" (")[0] for r in rows]
    assert len(names) == len(set(names)), names
    for mark, rgb in (("crit", ATT), ("warn", MAYBE)):
        for row in det.locator(f"tbody tr.{mark}").all():
            colours = row.evaluate("""r => [...r.cells].map((c) => getComputedStyle(c).color)""")
            assert set(colours) == {rgb}, (mark, colours)
            assert row.locator("td.nm .lb").evaluate("e => getComputedStyle(e).color") == rgb
            assert row.locator("td.nm small").evaluate("e => getComputedStyle(e).color") not in (ATT, MAYBE)
            # The note sits right after the name, never in a number cell.
            assert row.locator("td:not(.nm) small").count() == 0
    plain = det.locator("tbody tr:not(.crit):not(.warn)").first
    assert plain.locator("td.c-raw").evaluate("e => getComputedStyle(e).color") not in (ATT, MAYBE)
    assert_colours(p)


def test_details_gap_rows_and_nvme(open_page):
    p = open_page("REPLICAS=carried")
    ds = by_id(p, "replicas|MOCK-DS-9JG0X1")
    ds.locator(".chev").click()
    rows = ds.locator("table.attrs").evaluate(TABLE_ROWS)
    # A watched attribute the drive does not report: its own row, "not reported" muted in Raw.
    gap = next(r for r in rows if r[1] == "187")
    assert gap[2] == "Reported uncorrectable (no attribute 187)" and gap[-1] == "not reported", gap
    na = ds.locator("tbody tr", has_text="Reported uncorrectable").locator("td.c-raw .na")
    assert na.evaluate("e => getComputedStyle(e).color") not in (ATT, MAYBE, OK)
    # The Device Statistics reading that stands in for it follows, flagged.
    after = rows[rows.index(gap) + 1]
    assert after[0] == "crit" and after[2] == "Uncorrectable, page 4 (expected 0, from device statistics)", after
    assert ds.locator("tbody tr.crit input.raw-in").input_value() == "18"
    assert ds.locator("table").count() == 1
    # An NVMe drive: its health log is the table, Name and Value, with the same flags.
    nv = by_id(p, REP_NVME)
    nv.locator(".chev").click()
    assert nv.locator(".details table").count() == 1
    assert nv.locator(".details thead th").all_inner_texts() == ["Name", "Value"]
    hit = nv.locator("tbody tr.warn")
    assert hit.count() == 1 and hit.locator("td.nm").inner_text() == "available_spare (warns below 20%)"
    assert hit.locator("td.nm .lb").evaluate("e => getComputedStyle(e).color") == MAYBE
    # Replica inputs stay narrow.
    widths = p.page.eval_on_selector_all("input.raw-in", "els => els.map((e) => e.getBoundingClientRect().width)")
    assert widths and max(widths) <= 96.5, widths
    # Pools keep their per-disk table.
    pool = by_id(p, REP_POOL)
    pool.locator(".chev").click()
    assert pool.locator(".details thead th").all_inner_texts()[:2] == ["Disk", "State"]


def test_notes_follow_labels(open_page):
    p = open_page()
    nas = by_id(p, NAS_ATT)
    assert nas.locator(".pw-row").first.locator(".pw-label").inner_text() == "Reallocated sectors (expected 0)"
    assert nas.locator(".pw-row").first.locator(".pw-val").inner_text().replace(",", "").isdigit()
    pw_vals = p.page.locator(".pw-val").all_inner_texts()
    assert pw_vals and not [v for v in pw_vals if "(" in v or "expected" in v], pw_vals
    for chev in p.page.locator(".chev").all():
        chev.click()
    p.page.wait_for_timeout(200)
    # No note, bracket or small text ever sits in a value cell.
    assert p.page.locator(".kv dd small").count() == 0
    values = p.page.locator(".kv dd").all_inner_texts()
    assert values and not [v for v in values if "(" in v or "from " in v or "expected" in v], values
    # Every note is in the label cell, right after the label.
    notes = p.page.evaluate("""() => [...document.querySelectorAll('.kv dt small')]
        .map((s) => {
          let prev = s.previousElementSibling;
          while (prev && prev.tagName === 'TEMPLATE') prev = prev.previousElementSibling;
          return [!!prev && prev.classList.contains('lb'), s.textContent];
        })""")
    assert notes and all(ok and t.startswith(" (") and t.endswith(")") for ok, t in notes), notes
    read = by_id(p, NVME).locator(".kv > div", has_text="Data Read")
    assert read.locator("dt").inner_text() == "Data Read (from the NVMe log)"
    assert re.fullmatch(r"[\d.]+ [KMGTP]?B", read.locator("dd").inner_text()), read.locator("dd").inner_text()
    stats = by_id(p, NVME).locator(".kv > div", has_text="Device Statistics")
    assert stats.locator("dt").inner_text() == "Device Statistics (NVMe)"
    assert stats.locator("dd").inner_text() == "not applicable"


def test_chevron_size_and_stroke(open_page):
    p = open_page()
    box = p.page.locator(".chev svg").first.bounding_box()
    # 80% of the 17.6 by 10.4 it was.
    assert abs(box["width"] - 17.6 * 0.8) < 0.05 and abs(box["height"] - 10.4 * 0.8) < 0.05, box
    # The 20 by 12 viewBox scales by the smaller factor; the drawn line is one
    # pixel heavier than the 2.75 units at 10.4 / 12 it was.
    sw = p.page.locator(".chev svg").first.evaluate("e => parseFloat(getComputedStyle(e).strokeWidth)")
    scale = min(box["width"] / 20, box["height"] / 12)
    assert abs(sw * scale - (2.75 * 10.4 / 12 + 1)) < 0.01, (sw, scale)


# ── The grid ─────────────────────────────────────────────────────────────────

def test_one_grid_of_cards(open_page):
    p = open_page()
    assert p.page.locator(".grid").count() == 1
    ids = card_ids(p)
    assert ids[:2] == [NVME, BARRACUDA]
    # 2 HAOS, 6 drives, a pool and a filesystem from nas-01, 1 unreachable,
    # 1 placeholder, 3 replicas.
    assert len(ids) == 15
    assert {NAS_ATT, NAS_POOL, NAS_FS, STUDIO, BACKUP_PI, REP_FAILED, REP_NVME, REP_POOL} <= set(ids)
    pills = {cid: by_id(p, cid).locator(".pill").inner_text() for cid in
             (NVME, NAS_ATT, NAS_POOL, NAS_FS, BACKUP_PI, REP_FAILED, REP_POOL)}
    assert pills == {NVME: "HAOS", NAS_ATT: "NAS-01", NAS_POOL: "ZFS POOL", NAS_FS: "FILESYSTEM",
                     BACKUP_PI: "BACKUP-PI", REP_FAILED: "REPLICA", REP_POOL: "ZFS POOL"}
    assert "on NAS-01" in by_id(p, NAS_POOL).locator(".c-where").inner_text()
    assert "on REPLICA" in by_id(p, REP_POOL).locator(".c-where").inner_text()
    # The + card is last.
    assert last_is_plus(p)
    # A FILESYSTEM card has no health line and no Download.
    fs_card = by_id(p, NAS_FS)
    assert fs_card.locator(".c-health").count() == 0
    assert fs_card.get_by_role("button", name="Save as replica").count() == 0


def test_rows_stretch_and_three_problem_rows_show(open_page):
    p = open_page()
    # Cards in a grid row stretch to the tallest one.
    heights = p.page.eval_on_selector_all(".grid > article.card", "els => els.slice(0, 3).map((e) => e.offsetHeight)")
    assert len(set(heights)) == 1, heights
    att = by_id(p, NAS_ATT)
    rows = att.locator(".pw-row")
    assert rows.count() == 3
    assert all(r.is_visible() for r in rows.all())
    assert att.locator(".pw-more").inner_text() == "and 2 more"
    # Details replaces the problem rows, in place.
    att.locator(".chev").click()
    assert att.locator(".pw").count() == 0 and att.locator(".details table.attrs").count() == 1
    assert att.locator(".chev").get_attribute("aria-expanded") == "true"
    att.locator(".chev").click()
    assert att.locator(".pw-row").count() == 3
    # A pool with a failing disk shows its row.
    p = open_page("POOL=degraded")
    pool = by_id(p, NAS_POOL)
    assert "sdf" in pool.locator(".pw").inner_text() and "FAULTED" in pool.locator(".pw").inner_text()


def test_space_bar_thresholds(open_page):
    p = open_page()
    rows = p.page.evaluate("""() => [...document.querySelectorAll('.sp-row')].map((r) => {
        const bar = r.querySelector('.sp-bar'), fill = r.querySelector('.sp-bar i'), num = r.querySelector('.sp-bar b');
        const lum = (c) => { const m = c.match(/\\d+/g).map(Number); return (m[0] + m[1] + m[2]) / 3; };
        return { mount: r.querySelector('.sp-mount').innerText, step: r.className.replace('sp-row ', ''),
                 inside: num.innerText, outside: r.querySelector('.sp-text').innerText,
                 weight: Number(getComputedStyle(num).fontWeight), ink: lum(getComputedStyle(num).color),
                 fill: lum(getComputedStyle(fill).backgroundColor), outline: getComputedStyle(bar).borderTopStyle };
      })""")
    by = {r["mount"]: r for r in rows}
    low = by["/"]
    assert low["step"] == "lo" and low["inside"] == "" and low["outside"] == "41% of 980 GB"
    assert by["/boot"]["outside"].startswith("12% of")
    s80, s90, s95 = by["/mnt/disk1"], by["/mnt/disk2"], by["/mnt/disk3"]
    assert (s80["step"], s90["step"], s95["step"]) == ("s80", "s90", "s95")
    assert (s80["inside"], s90["inside"], s95["inside"]) == ("82%", "91%", "97%")
    assert s80["outside"].endswith(" free") and s95["outside"].endswith(" free")
    # Inside the fill at 80% and up: dark type on a light fill, reading as knocked out.
    for r in (s80, s90, s95):
        assert r["ink"] < 60 and r["fill"] > 150, r
    assert s80["fill"] < s90["fill"] < s95["fill"]
    # Weight steps at 90 and 95; the dashed outline at 95 only.
    assert s80["weight"] < s90["weight"] < s95["weight"]
    assert s95["weight"] >= 700
    assert [r["outline"] for r in (low, s80, s90, s95)] == ["solid", "solid", "solid", "dashed"]
    assert by["/mnt/usb"]["outside"] == "not mounted" and by["/mnt/usb"]["outline"] == "dashed"
    # The pool carries its three datasets.
    assert by_id(p, NAS_POOL).locator(".sp-row").count() == 3


def test_drag_reorder_persists(open_page, browser, server):
    p = open_page()
    first, third = by_id(p, NVME), by_id(p, NAS_ATT)
    grip = first.locator(".grip").bounding_box()
    target = third.bounding_box()
    p.page.mouse.move(grip["x"] + grip["width"] / 2, grip["y"] + grip["height"] / 2)
    p.page.mouse.down()
    steps = 12
    for i in range(1, steps + 1):
        x = grip["x"] + (target["x"] + target["width"] * 0.75 - grip["x"]) * i / steps
        y = grip["y"] + (target["y"] + target["height"] / 2 - grip["y"]) * i / steps
        p.page.mouse.move(x, y)
        p.page.wait_for_timeout(20)
    # In hand: the lifted copy follows the pointer and the card's place is the gap.
    assert p.page.locator("#drag-layer .lifted").count() == 1
    assert "drop-gap" in by_id(p, NVME).get_attribute("class")
    p.page.mouse.up()
    p.page.wait_for_timeout(300)
    assert p.page.locator("#drag-layer .lifted").count() == 0
    ids = card_ids(p)
    assert ids.index(NVME) == 2 and ids[:2] == [BARRACUDA, NAS_ATT]
    puts = p.api("PUT", r"/cards/order$")
    assert len(puts) == 1
    sent = json.loads(puts[0][2])["order"]
    assert sent[:3] == [BARRACUDA, NAS_ATT, NVME] and len(sent) == 15
    # A fresh page has the same order (no reset: the harness keeps it).
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(f"{server}/")
    page.wait_for_selector("article.card")
    page.wait_for_timeout(700)
    assert page.eval_on_selector_all(".grid > article.card", "els => els.map((e) => e.dataset.card)")[:3] \
        == [BARRACUDA, NAS_ATT, NVME]
    ctx.close()
    # The + card has no grip and stays last.
    assert p.page.locator(".card.ghost .grip").count() == 0


def test_grip_keyboard_and_touch_needs_a_long_press(open_page):
    p = open_page()
    by_id(p, NVME).locator(".grip").focus()
    p.page.keyboard.press("ArrowRight")
    p.page.wait_for_timeout(200)
    assert card_ids(p)[:2] == [BARRACUDA, NVME]
    assert len(p.api("PUT", r"/cards/order$")) == 1
    # The grip alone takes touch; a quick touch moves nothing.
    assert by_id(p, NVME).locator(".grip").evaluate("e => getComputedStyle(e).touchAction") == "none"
    assert p.page.locator(".grid").evaluate("e => getComputedStyle(e).touchAction") == "auto"


def test_hide_and_reveal(open_page):
    p = open_page()
    eye = p.page.locator(".eye-btn")
    assert eye.is_visible() and eye.inner_text().strip() == "0" and eye.is_disabled()
    # HAOS: Download and hide, no trash. Replicas: no eye.
    nvme = by_id(p, NVME)
    assert nvme.get_by_role("button", name="Remove this card").count() == 0
    assert by_id(p, REP_NVME).get_by_role("button", name="Hide this card").count() == 0
    nvme.get_by_role("button", name="Hide this card").click()
    assert eye.inner_text().strip() == "1"
    assert by_id(p, NVME).count() == 0
    p.page.wait_for_timeout(300)
    patches = p.api("PATCH", r"/cards/")
    assert len(patches) == 1 and json.loads(patches[0][2]) == {"hidden": True}
    assert unquote(patches[0][1]).endswith(f"/cards/{NVME}")
    eye.click()
    assert eye.get_attribute("aria-pressed") == "true"
    shown = by_id(p, NVME)
    assert "is-hidden" in shown.get_attribute("class")
    assert float(shown.locator(".c-title").evaluate("e => getComputedStyle(e.closest('.c-head')).opacity")) < 0.5
    shown.get_by_role("button", name="Hidden. Show this card again").click()
    assert eye.inner_text().strip() == "0"
    assert "is-hidden" not in by_id(p, NVME).get_attribute("class")


def test_hide_rolls_back_on_error(open_page):
    p = open_page()
    p.page.route("**/cards/self*", lambda route: route.fulfill(status=502, body='{"error": "supervisor_failed"}'))
    by_id(p, NVME).get_by_role("button", name="Hide this card").click()
    p.page.wait_for_selector("text=That change was not saved. Try again.")
    assert by_id(p, NVME).count() == 1
    assert p.page.locator(".eye-btn").inner_text().strip() == "0"


def test_trash_confirm(open_page):
    p = open_page()
    usb = by_id(p, NAS_USB)
    usb.get_by_role("button", name="Remove this card").click()
    ask = usb.get_by_role("group", name="Confirm removal")
    assert ask.inner_text().startswith("Remove this drive?")
    ask.get_by_role("button", name="Cancel").click()
    assert usb.get_by_role("group", name="Confirm removal").count() == 0
    assert not p.api("DELETE", r"/cards/")
    usb.get_by_role("button", name="Remove this card").click()
    usb.get_by_role("group", name="Confirm removal").get_by_role("button", name="Remove").click()
    p.page.wait_for_function(f"() => !document.querySelector('[data-card=\"{NAS_USB}\"]')")
    assert len(p.api("DELETE", r"/cards/")) == 1
    assert NAS_ATT in card_ids(p)  # only the card clicked
    # Replica and pool texts.
    by_id(p, REP_NVME).get_by_role("button", name="Remove this card").click()
    assert by_id(p, REP_NVME).get_by_role("group", name="Confirm removal").inner_text().startswith("Remove this replica?")
    by_id(p, NAS_POOL).get_by_role("button", name="Remove this card").click()
    assert by_id(p, NAS_POOL).get_by_role("group", name="Confirm removal").inner_text().startswith("Remove this pool?")
    assert by_id(p, REP_NVME).get_by_role("group", name="Confirm removal").count() == 0  # one at a time
    # The placeholder's trash removes the machine.
    pi = by_id(p, BACKUP_PI)
    pi.get_by_role("button", name="Remove this card").click()
    assert pi.get_by_role("group", name="Confirm removal").inner_text().startswith("Remove backup-pi?")
    pi.get_by_role("group", name="Confirm removal").get_by_role("button", name="Remove").click()
    p.page.wait_for_function(f"() => !document.querySelector('[data-card=\"{BACKUP_PI}\"]')")


def _scenario(card):
    pick = card.locator(".c-where select.scen")
    return pick, pick.evaluate("e => e.options[e.selectedIndex].text"), pick.locator("option").all_inner_texts()


def test_scenario_picker_applies_on_change(open_page):
    p = open_page()
    rep = by_id(p, REP_NVME)
    assert rep.locator(".preset, .presets").count() == 0
    pick, current, options = _scenario(rep)
    assert pick.get_attribute("aria-label") == "Scenario"
    assert current == "NVMe wear"
    assert options == ["Healthy NVMe", "NVMe wear", "Media errors", "Critical warning"]
    pick.select_option("media_errors")
    p.page.wait_for_function(f"() => document.querySelector('[data-card=\"{REP_NVME}\"] .h-att')")
    posts = p.api("POST", r"/mock/drives/[^/]+/scenario$")
    assert len(posts) == 1 and json.loads(posts[0][2]) == {"id": "media_errors"}
    assert _scenario(rep)[1] == "Media errors"
    assert _scenario(by_id(p, REP_FAILED))[2] == [
        "Healthy SATA HDD", "Reallocated sectors growing", "Pending sectors", "Command timeouts", "SMART failed"]
    pool_pick, _, pool_options = _scenario(by_id(p, REP_POOL))
    assert pool_options == ["Healthy pool", "ZFS pool degraded", "Pool faulted"]
    assert by_id(p, REP_POOL).locator(".c-where").inner_text().split("\n")[-1] == "on REPLICA"
    # Quiet at rest: no border until hovered or focused.
    assert pick.evaluate("e => getComputedStyle(e).borderTopColor") == "rgba(0, 0, 0, 0)"
    assert pick.evaluate("e => getComputedStyle(e).fontFamily") == rep.locator(".c-where").evaluate(
        "e => getComputedStyle(e).fontFamily")
    pool_pick.select_option("faulted")
    p.page.wait_for_timeout(400)
    assert len(p.api("POST", r"/mock/pools/[^/]+/scenario$")) == 1
    assert json.loads(p.api("POST", r"/mock/pools/[^/]+/scenario$")[0][2]) == {"id": "faulted"}


def test_scenario_picker_on_touch(browser, server):
    urllib.request.urlopen(urllib.request.Request(f"{server}/dev/reset?", method="POST"), timeout=5)
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    try:
        page = ctx.new_page()
        page.goto(f"{server}/")
        page.wait_for_function("() => window.Alpine && document.body._x_dataStack")
        page.wait_for_timeout(700)
        pick = page.locator(f'article.card[data-card="{REP_NVME}"] select.scen')
        pick.scroll_into_view_if_needed()
        box = pick.bounding_box()
        card = page.locator(f'article.card[data-card="{REP_NVME}"]').bounding_box()
        assert box["height"] >= 24 and box["x"] >= card["x"] and box["x"] + box["width"] <= card["x"] + card["width"], box
        pick.tap()
        pick.select_option("critical_warning")
        page.wait_for_function(f"() => document.querySelector('[data-card=\"{REP_NVME}\"] .h-att')")
    finally:
        ctx.close()


def test_replica_with_no_scenario(open_page):
    """A replica carried over from the old Test Lab has no current scenario: the picker says so."""
    p = open_page("REPLICAS=carried")
    rep = by_id(p, REP_NVME)
    pick, current, options = _scenario(rep)
    assert current == "Choose scenario"
    assert options[0] == "Choose scenario" and options[1:] == ["Healthy NVMe", "NVMe wear", "Media errors",
                                                                  "Critical warning"]
    assert rep.locator(".scen-size").text_content() == "Choose scenario"
    assert pick.evaluate("e => e.getBoundingClientRect().width") > 80
    colour = pick.evaluate("e => getComputedStyle(e).color")
    assert colour != by_id(p, REP_FAILED).locator("select.scen").evaluate("e => getComputedStyle(e).color")
    pick.select_option("media_errors")
    p.page.wait_for_function(f"() => document.querySelector('[data-card=\"{REP_NVME}\"] .h-att')")
    posts = p.api("POST", r"/mock/drives/[^/]+/scenario$")
    assert len(posts) == 1 and json.loads(posts[0][2]) == {"id": "media_errors"}
    pick, current, options = _scenario(rep)
    assert current == "Media errors" and "Choose scenario" not in options


def test_download(open_page, tmp_path):
    p = open_page()
    rep = by_id(p, REP_FAILED)
    button = rep.get_by_role("button", name="Save as replica")
    assert button.get_attribute("title") == "Save as replica"
    with p.page.expect_download() as dl:
        button.click()
    path = tmp_path / "replica.json"
    dl.value.save_as(path)
    doc = json.loads(path.read_text())
    assert doc["format"] == "smart-sniffer-replica" and dl.value.suggested_filename.endswith(".json")
    assert p.api("GET", r"/cards/replicas%7CMOCK-SATA-0001/file$")
    # A real drive's file has its serial replaced.
    with p.page.expect_download() as dl:
        by_id(p, BARRACUDA).get_by_role("button", name="Save as replica").click()
    path = tmp_path / "real.json"
    dl.value.save_as(path)
    text = path.read_text()
    assert "ZFL0K7Q2" not in text and re.search(r"REPLICA-[0-9A-F]{8}", text)
    # It goes back in through Upload a replica.
    open_add(p)
    p.page.wait_for_selector(".replica-pick label")
    p.page.locator("input[type=file][aria-label='Upload a replica']").set_input_files(str(path))
    p.page.wait_for_function("() => document.querySelectorAll('.replica-pick label.on').length === 1")
    assert len(p.api("POST", r"/replicas$")) == 1


def test_exit_icon_confirms(open_page):
    p = open_page()
    usb = by_id(p, NAS_USB)
    usb.get_by_role("button", name="Not in Home Assistant yet. Add it").click()
    ask = usb.get_by_role("group", name="Confirm")
    assert ask.inner_text().startswith("Add NAS-01 to Home Assistant?")
    # The confirm sits under the title.
    assert usb.evaluate("""e => { let s = e.querySelector('.ask').previousElementSibling;
        while (s && s.tagName === 'TEMPLATE') s = s.previousElementSibling;
        return s.classList.contains('c-head'); }""")
    p.page.keyboard.press("Escape")
    assert usb.get_by_role("group", name="Confirm").count() == 0
    p = open_page("HA_SELF=0")
    nvme = by_id(p, NVME)
    nvme.get_by_role("button", name="Not in Home Assistant yet. Add it").click()
    assert nvme.get_by_role("group", name="Confirm").inner_text().startswith("Add this drive to Home Assistant?")
    nvme.get_by_role("group", name="Confirm").get_by_role("button", name="Add").click()
    p.page.wait_for_selector(f'[data-card="{NVME}"] a.exit', timeout=6000)
    assert json.loads(p.api("POST", r"/ha/add$")[0][2]) == {"target": "self"}
    # The exit icon's place is the same on every card, left of the pill.
    p = open_page()
    gaps = p.page.eval_on_selector_all(".grid > article.card .c-tags", """els => els.map((t) => {
        const s = t.querySelector('.exit-slot').getBoundingClientRect(), p = t.querySelector('.pill').getBoundingClientRect();
        return Math.round(p.left - s.right); })""")
    assert len(set(gaps)) == 1


def test_add_error_shows_on_the_confirm(open_page):
    p = open_page("HA_SELF=0&ADD=error")
    c = by_id(p, NVME)
    c.get_by_role("button", name="Not in Home Assistant yet. Add it").click()
    c.get_by_role("group", name="Confirm").get_by_role("button", name="Add").click()
    p.page.wait_for_selector("text=Home Assistant did not add it.")


def test_placeholder_authenticate(open_page):
    p = open_page()
    pending = by_id(p, BACKUP_PI)
    assert "machine" in pending.get_attribute("class")
    assert "This agent wants a password." in pending.inner_text()
    assert pending.locator(".c-vitals, .c-health").count() == 0
    pending.get_by_label("Agent password").fill("wrong")
    pending.get_by_role("button", name="Authenticate").click()
    p.page.wait_for_selector("text=That password was refused.")
    assert "bad" in pending.get_by_label("Agent password").get_attribute("class")
    pending.get_by_label("Agent password").fill("fieldpass")
    pending.get_by_role("button", name="Authenticate").click()
    p.page.wait_for_selector(f'[data-card="{BACKUP_PI}|32315D480012"]')
    assert by_id(p, BACKUP_PI).count() == 0
    assert "fieldpass" not in p.page.content()


def test_unreachable_keeps_its_readings(open_page):
    p = open_page()
    studio = by_id(p, STUDIO)
    assert studio.locator(".seen").inner_text() == "Last seen: 3 h ago, not answering"
    assert "33 C" in studio.locator(".c-vitals").inner_text()


def test_agent_down(open_page):
    p = open_page("AGENT=down")
    assert "agent not answering" in p.page.locator(".versions").inner_text()
    # The HAOS cards keep their last readings, marked not answering.
    seen = by_id(p, NVME).locator(".seen").inner_text()
    assert seen.startswith("Last seen: ") and seen.endswith(", not answering")


def test_requests_stay_home_and_nothing_scans_at_load(open_page):
    p = open_page()
    p.page.wait_for_timeout(1500)
    assert all(url.startswith(p.base) for _, url, _ in p.requests)
    assert not p.api("POST", r"/fleet/scan$") and not p.api("POST", r"/fleet/probe$")
    open_add(p)
    p.page.wait_for_timeout(600)
    assert len(p.api("POST", r"/fleet/scan$")) == 1
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


def test_add_card_areas_and_flow(open_page):
    p = open_page("HOSTS=one")
    open_add(p)
    region = p.page.get_by_role("region", name="Add", exact=True)
    # Three areas, each its own panel.
    areas = region.locator("section.area")
    assert [a.get_attribute("aria-label") for a in areas.all()] == ["Network", "Add by hand", "Replicas"]
    assert all(a.evaluate("e => getComputedStyle(e).borderTopStyle") == "solid" for a in areas.all())
    # Network on the left; Add by hand and Replicas stacked on the right.
    net, hand, reps = (a.bounding_box() for a in areas.all())
    assert hand["x"] > net["x"] + net["width"] - 1 and abs(hand["y"] - net["y"]) < 1
    assert abs(reps["x"] - hand["x"]) < 1 and reps["y"] > hand["y"] + hand["height"] - 1
    block = region.get_by_label("Network block")
    assert block.input_value() == "10.0.10.0/24"
    assert region.get_by_role("button", name="Rescan").is_visible()
    p.page.wait_for_function("() => document.querySelectorAll('.pick-row').length === 3", timeout=6000)
    p.page.wait_for_timeout(500)
    assert region.locator(".found b").inner_text() == "2"
    done = region.locator(".pick-row.done")
    assert "proxmox-01, already added" in done.inner_text() and done.locator("input").is_disabled()
    add = region.locator(".add-foot .btn-primary")
    assert add.inner_text() == "Add 2"
    # One Add counts ticks across all three areas.
    region.locator(".replica-pick label", has_text="NVMe, wear").click()
    assert add.inner_text() == "Add 3"
    region.locator(".replica-pick label", has_text="NVMe, wear").click()
    block.fill("10.0.20.0/24")
    assert region.get_by_role("button", name="Scan", exact=True).is_visible()
    assert add.is_disabled()
    block.fill("10.0.0.0/16")
    region.get_by_role("button", name="Scan", exact=True).click()
    alert = region.get_by_role("alert")
    assert "That is 65,534 addresses." in alert.inner_text()
    alert.get_by_role("button").click()
    p.page.wait_for_function("() => document.querySelectorAll('.pick-row').length === 3", timeout=6000)
    # Add by hand: a machine found drops into the Network list, ticked.
    region.get_by_label("Host or IP").fill("nowhere.lan")
    region.get_by_role("button", name="Check").click()
    p.page.wait_for_selector("text=Nothing answered at nowhere.lan:9099.")
    region.get_by_label("Host or IP").fill("nas.lan")
    region.get_by_role("button", name="Check").click()
    p.page.wait_for_selector("section.area[aria-label=Network] .pick-row.on:has-text('nas.lan:9099')")
    assert add.inner_text() == "Add 3"
    region.locator(".pick-row", has_text="nas.lan").click()
    add.click()
    p.page.wait_for_selector("[role=region][aria-label=Add]", state="detached")
    posts = p.api("POST", r"/fleet/hosts$")
    assert len(posts) == 1
    assert sorted(h["address"] for h in json.loads(posts[0][2])["hosts"]) == ["10.0.10.52", "10.0.10.61"]
    p.page.wait_for_selector('[data-card="10.0.10.61:9099|2309E6A1B2C3"]')
    assert "This agent wants a password." in by_id(p, BACKUP_PI).inner_text()
    # New cards land just before the + card.
    assert last_is_plus(p)


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
    assert polls() == before
    _set_hidden(p, False)
    p.page.wait_for_timeout(60)
    assert polls() == before + 1
    p.page.wait_for_function(f"() => Alpine.$data(document.body).add.scan.probed > {probed}", timeout=2000)
    p.page.wait_for_timeout(900)
    assert before + 2 <= polls() <= before + 5


def test_nothing_answered_and_wide_prefill(open_page):
    p = open_page("SCAN=none&HOSTS=one")
    open_add(p)
    p.page.wait_for_selector("text=None answered on 10.0.10.0/24, port 9099.", timeout=6000)
    assert p.page.locator(".found b").inner_text() == "0"
    p = open_page("NETWORK=wide")
    open_add(p)
    p.page.wait_for_timeout(500)
    assert "That is 65,534 addresses." in p.page.get_by_role("alert").inner_text()
    assert not p.api("POST", r"/fleet/scan$")


def test_upload_refused_and_folder_missing(open_page, tmp_path):
    p = open_page()
    bad = tmp_path / "notes.json"
    bad.write_text('{"hello": 1}')
    open_add(p)
    p.page.wait_for_selector(".replica-pick label")
    p.page.locator("input[type=file][aria-label='Upload a replica']").set_input_files(str(bad))
    p.page.wait_for_selector(".up-msg:has-text('This is not a replica file.')")
    p = open_page("REPLICA_FOLDER=missing")
    open_add(p)
    p.page.wait_for_selector("text=Your replica folder is not available.")
    assert p.page.get_by_role("button", name="Choose file").is_disabled()


def test_first_run(open_page):
    p = open_page("HOSTS=none&REPLICAS=0&HA=not_installed&DRIVES=1")
    assert p.page.locator("article.card").count() == 1
    eye = p.page.locator(".eye-btn")
    assert eye.is_visible() and eye.is_disabled() and eye.inner_text().strip() == "0"
    assert p.page.get_by_role("button", name="Refresh").is_visible()
    assert p.page.get_by_role("button", name="Add a drive, or a replica for testing").is_visible()
    assert p.page.locator(".app-foot a:visible").all_inner_texts() == [
        "Mission Log", "App configuration", "GitHub", "HAIR, infrared remotes in Home Assistant"]
    assert p.page.locator(".exit").count() == 0


def test_raw_table_marks(open_page):
    p = open_page()
    c = by_id(p, BARRACUDA)
    c.locator(".chev").click()
    hits = c.locator("table.attrs tr.crit")
    assert sorted(t.split()[0] for t in hits.all_inner_texts()) == ["197", "5"]


# The grid columns a card spans, and the wrappers that scroll sideways.
SPAN = """(e) => {
  const cols = getComputedStyle(e.parentElement).gridTemplateColumns.split(' ').length;
  const gap = parseFloat(getComputedStyle(e.parentElement).columnGap);
  const col = (e.parentElement.getBoundingClientRect().width - gap * (cols - 1)) / cols;
  return [cols, Math.round((e.getBoundingClientRect().width + gap) / (col + gap))];
}"""
WIDE_WRAPS = "els => els.filter((w) => w.scrollWidth > w.clientWidth + 0.5).map((w) => w.outerHTML.slice(0, 80))"
HEIGHTS = "els => els.map((e) => Math.round(e.getBoundingClientRect().height))"


@pytest.mark.parametrize("width,cols", [(1280, 3), (740, 2), (390, 1)])
def test_open_card_stays_one_column(open_page, width, cols):
    p = open_page("", width)
    c = by_id(p, NAS_ATT)
    assert c.evaluate(SPAN) == [cols, 1]
    # Closed, every grid row is as tall as its tallest card.
    if cols > 1:
        heights = p.page.eval_on_selector_all(".grid > article.card", HEIGHTS)[:cols * 2]
        assert len(set(heights[:cols])) == 1 and len(set(heights[cols:])) == 1, heights
    before = p.page.eval_on_selector_all(".grid > article.card", HEIGHTS)
    c.locator(".chev").click()
    p.page.wait_for_timeout(200)
    assert c.evaluate(SPAN) == [cols, 1]
    assert card_ids(p)[:3] == [NVME, BARRACUDA, NAS_ATT]
    after = p.page.eval_on_selector_all(".grid > article.card", HEIGHTS)
    i = card_ids(p).index(NAS_ATT)
    row = range(i - i % cols, i - i % cols + cols)
    # The open card grows down; the cards beside it keep their own height.
    assert after[i] > before[i] + 100, (before[i], after[i])
    if cols > 1:
        own = p.page.eval_on_selector_all(".grid > article.card", """els => els.map((e) => {
          const kids = [...e.children].filter((k) => getComputedStyle(k).display !== 'none');
          const last = kids[kids.length - 1].getBoundingClientRect();
          const cs = getComputedStyle(e);
          return Math.round(last.bottom + parseFloat(cs.paddingBottom) + parseFloat(cs.borderBottomWidth)
            - e.getBoundingClientRect().top);
        })""")
        for j in row:
            if j != i:
                assert after[j] < after[i] and abs(after[j] - own[j]) <= 1, (j, after[j], own[j], after[i])
        # Rows with no card open still stretch to their tallest card.
        nxt = after[(row[-1] + 1):(row[-1] + 1 + cols)]
        assert len(set(nxt)) == 1, nxt
    # The table fits without scrolling sideways, on every open card.
    for cid in (NAS_POOL, REP_FAILED, REP_NVME, BARRACUDA):
        by_id(p, cid).locator(".chev").click()
    p.page.wait_for_timeout(200)
    assert p.page.locator(".table-wrap").count() >= 5
    assert p.page.eval_on_selector_all(".table-wrap", WIDE_WRAPS) == []
    assert p.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert_no_scroll(p)
    assert_inside_cards(p)
    assert_no_overflow(p)
    heads = c.locator("table.attrs thead th:visible").all_inner_texts()
    # One column wide: ID, Name, Value and Raw; Worst and Thresh hide.
    assert heads == ["ID", "Name", "Value", "Raw"], heads
    assert c.locator("td.c-worst:visible, td.c-thresh:visible").count() == 0
    name = c.locator("table.attrs td.nm").first
    assert name.get_attribute("title") == name.locator(".lb").inner_text()
    # Raw sits right next to Value: no spare space between the number columns.
    gap = c.locator("tbody tr").first.evaluate("""r => {
      const cells = [...r.cells].filter((x) => getComputedStyle(x).display !== 'none');
      const v = cells[cells.length - 2].getBoundingClientRect(), raw = cells[cells.length - 1];
      const text = document.createRange(); text.selectNodeContents(raw);
      return text.getBoundingClientRect().left - v.right; }""")
    assert gap < 40, gap
    # Closed again, the row stretches as before.
    for cid in (NAS_ATT, NAS_POOL, REP_FAILED, REP_NVME, BARRACUDA):
        by_id(p, cid).locator(".chev").click()
    p.page.wait_for_timeout(200)
    assert p.page.eval_on_selector_all(".grid > article.card", HEIGHTS) == before


def test_wide_card_has_no_spacer_column(open_page):
    p = open_page("", 1280)
    p.page.add_style_tag(content=".grid { grid-template-columns: 1fr !important; }")
    c = by_id(p, NAS_ATT)
    c.locator(".chev").click()
    p.page.wait_for_timeout(200)
    assert c.locator("table.attrs thead th:visible").all_inner_texts() == ["ID", "Name", "Value", "Worst", "Thresh", "Raw"]
    # Name takes the spare width; Thresh and Raw sit together at the right.
    gap = c.locator("tbody tr").first.evaluate("""r => {
      const t = r.cells[4].getBoundingClientRect(), raw = r.cells[5];
      const text = document.createRange(); text.selectNodeContents(raw);
      return text.getBoundingClientRect().left - t.right; }""")
    assert gap < 120, gap
    assert c.locator("td.nm").first.bounding_box()["width"] > 600


def test_drag_with_an_open_card(open_page):
    p = open_page()
    by_id(p, BARRACUDA).locator(".chev").click()
    p.page.wait_for_timeout(200)
    grip = by_id(p, BARRACUDA).locator(".grip").bounding_box()
    target = by_id(p, NAS_MAYBE).bounding_box()
    p.page.mouse.move(grip["x"] + grip["width"] / 2, grip["y"] + grip["height"] / 2)
    p.page.mouse.down()
    for i in range(1, 13):
        p.page.mouse.move(grip["x"] + (target["x"] + target["width"] * 0.75 - grip["x"]) * i / 12,
                          grip["y"] + (target["y"] + target["height"] / 2 - grip["y"]) * i / 12)
        p.page.wait_for_timeout(20)
    p.page.mouse.up()
    p.page.wait_for_timeout(300)
    ids = card_ids(p)
    assert ids.index(BARRACUDA) == ids.index(NAS_MAYBE) + 1, ids[:6]
    assert by_id(p, BARRACUDA).evaluate(SPAN) == [3, 1]
    # The new row of the open card is the one that keeps its own heights.
    i = ids.index(BARRACUDA)
    marked = p.page.eval_on_selector_all(".grid > article.card", "els => els.map((e) => e.classList.contains('row-open'))")
    assert [j for j, m in enumerate(marked) if m] == list(range(i - i % 3, i - i % 3 + 3)), marked
    assert len(p.api("PUT", r"/cards/order$")) == 1


def test_chevron_outline_only_for_the_keyboard(open_page):
    p = open_page()
    chev = by_id(p, BARRACUDA).locator(".chev")
    chev.click()
    assert chev.get_attribute("aria-expanded") == "true"
    assert chev.evaluate("e => getComputedStyle(e).outlineStyle") == "none"
    assert not chev.evaluate("e => e.matches(':focus-visible')")
    # By Tab from the card's grip, the outline shows, and Enter still works.
    by_id(p, BARRACUDA).locator(".grip").focus()
    for _ in range(12):
        p.page.keyboard.press("Tab")
        if chev.evaluate("e => document.activeElement === e"):
            break
    assert chev.evaluate("e => document.activeElement === e")
    outline = chev.evaluate("e => [getComputedStyle(e).outlineStyle, getComputedStyle(e).outlineWidth]")
    assert outline == ["solid", "2px"], outline
    p.page.keyboard.press("Enter")
    assert chev.get_attribute("aria-expanded") == "false"
    assert chev.evaluate("e => e.matches(':focus-visible')")


def test_page_polls_only_its_own_reads(open_page):
    p = open_page()
    p.page.wait_for_timeout(9000)
    paths = {unquote(re.sub(r"\?.*", "", u[len(p.base):])) for _, u, _ in p.requests
             if not u.endswith((".js", ".css", ".png"))}
    assert paths <= {"/", "/app/info", "/cards", "/fleet/hosts", "/fleet/hosts/self/drives",
                     "/fleet/hosts/replicas/drives", "/ha/devices"} | {
        f"/fleet/hosts/{h}:9099/drives" for h in ("10.0.10.20", "10.0.10.44", "10.0.10.52")}, paths
    assert len(p.api("GET", r"/fleet/hosts/self/drives$")) == 2
    assert len(p.api("GET", r"/fleet/hosts$")) == 1


# ── Screenshots, named by state ──────────────────────────────────────────────

# ── Thresholds ───────────────────────────────────────────────────────────────

def _edit(p: Page, cid: str):
    c = by_id(p, cid)
    if c.locator(".tune").count() == 0:
        c.locator(".chev").click()
    c.get_by_role("button", name="Edit thresholds").click()
    p.page.wait_for_selector(f'[data-card="{cid}"] table.thr-t')
    return c


def _edit_rows(c) -> list:
    return c.locator("table.thr-t tbody tr").evaluate_all("""rows => rows.map((r) => [
        r.querySelector('.lb').textContent, r.cells[1].textContent, r.querySelector('input').value,
        r.cells[3].textContent, r.className])""")


def test_threshold_notes_at_rest(open_page):
    p = open_page()
    over = by_id(p, NAS_OVER)
    # Closed: the flagged row's note names the threshold in force.
    row = over.locator(".pw-row").first
    assert row.locator(".pw-label").inner_text() == "Reallocated sectors (alert above 5, default 0)"
    assert row.locator(".pw-val").inner_text() == "8"
    assert "crit" in row.get_attribute("class")
    # Within its threshold: Health OK, the row marked accepted, not flagged.
    ok = by_id(p, NAS_ACCEPTED)
    assert ok.locator(".h-ok").count() == 1 and ok.locator(".pw-row").count() == 0
    ok.locator(".chev").click()
    attr = ok.locator("table.attrs tr", has_text="Reallocated_Sector_Ct")
    assert attr.locator("small").inner_text().strip() == "(accepted, alert above 4, default 0)"
    assert attr.get_attribute("class") in ("", None)
    # The tune button shows only with Details open, by the chevron.
    assert by_id(p, NVME).locator(".tune").count() == 0
    tune = ok.locator(".tune").bounding_box()
    chev = ok.locator(".chev").bounding_box()
    assert 0 < chev["x"] - (tune["x"] + tune["width"]) < 16 and abs(tune["y"] + tune["height"] / 2 - chev["y"] - chev["height"] / 2) < 3
    assert_colours(p)


def test_threshold_edit_mode(open_page):
    p = open_page()
    c = _edit(p, BARRACUDA)
    assert c.locator(".tune").get_attribute("aria-pressed") == "true"
    rows = _edit_rows(c)
    # The watched rows the drive reports, in the table's order, flagged ones marked.
    assert [r[0] for r in rows[:2]] == ["Reallocated sectors", "Pending sectors"]
    assert rows[0][1:] == ["24", "0", "0", "crit"] and rows[1][1:] == ["3", "0", "0", "crit"]
    assert ["Command timeout", "0", "100", "100", ""] in rows
    assert c.locator("table.thr-t td.def").first.evaluate("e => getComputedStyle(e).color") == DIM
    assert c.locator(".thr-where").inner_text() == "Saved in Home Assistant"
    assert c.locator("table.attrs:not(.thr-t)").count() == 0  # the edit table takes the table's place
    assert [b.inner_text() for b in c.locator(".thr-btns button").all()] == [
        "Save", "Accept current readings", "Reset to defaults", "Cancel"]
    assert_colours(p)
    assert_no_dashes(p)
    # A value that is not a whole number is refused on the page.
    c.get_by_label("Threshold of Reallocated sectors, alert above").fill("x")
    c.get_by_role("button", name="Save", exact=True).click()
    assert "whole number" in c.locator(".thr-err").inner_text()
    assert not p.api("PUT", r"/thresholds$")
    c.get_by_label("Threshold of Reallocated sectors, alert above").fill("30")
    c.get_by_role("button", name="Save", exact=True).click()
    p.page.wait_for_selector(f'[data-card="{BARRACUDA}"] table.thr-t', state="detached")
    sent = json.loads(p.api("PUT", r"/thresholds$")[0][2])["values"]
    assert sent["Reallocated Sector Count"] == 30 and sent["Current Pending Sector Count"] == 0
    # Saved: the card is judged again; 24 is within 30, the pending sectors still flag.
    p.page.wait_for_function(f"""() => [...document.querySelectorAll('[data-card="{BARRACUDA}"] table.attrs tr')]
        .some((r) => r.textContent.includes('accepted, alert above 30, default 0'))""")
    c = _edit(p, BARRACUDA)
    assert _edit_rows(c)[0][2] == "30"
    # Cancel leaves without saving; the tune button again too.
    c.get_by_label("Threshold of Reallocated sectors, alert above").fill("99")
    c.get_by_role("button", name="Cancel").click()
    assert c.locator("table.thr-t").count() == 0 and len(p.api("PUT", r"/thresholds$")) == 1
    # Not in Home Assistant: the numbers live in the app.
    p = open_page("HA_SELF=0")
    c = _edit(p, BARRACUDA)
    assert c.locator(".thr-where").inner_text() == "Saved in this app"


def test_threshold_accept_confirm(open_page):
    p = open_page()
    c = _edit(p, BARRACUDA)
    c.get_by_role("button", name="Accept current readings").click()
    ask = c.get_by_role("group", name="Confirm accept")
    assert ask.inner_text().startswith(
        "Accept 24 reallocated sectors and 3 pending sectors as normal for this drive?")
    ask.get_by_role("button", name="Cancel").click()
    assert c.get_by_role("group", name="Confirm accept").count() == 0
    assert not p.api("POST", r"/thresholds/accept$")
    c.get_by_role("button", name="Accept current readings").click()
    c.get_by_role("group", name="Confirm accept").get_by_role("button", name="Accept").click()
    p.page.wait_for_selector(f'[data-card="{BARRACUDA}"] .h-ok')
    assert len(p.api("POST", r"/thresholds/accept$")) == 1
    # Nothing flagged now: nothing to accept.
    c = _edit(p, BARRACUDA)
    assert c.get_by_role("button", name="Accept current readings").is_disabled()
    c = _edit(p, NVME)
    assert c.get_by_role("button", name="Accept current readings").is_disabled()


def test_threshold_reset_confirm(open_page):
    p = open_page()
    c = _edit(p, NAS_ACCEPTED)
    assert ["Reallocated sectors", "2", "4", "0", ""] == _edit_rows(c)[0]
    c.get_by_role("button", name="Reset to defaults").click()
    ask = c.get_by_role("group", name="Confirm reset")
    assert ask.inner_text().startswith("Put every threshold of this drive back to its default?")
    p.page.keyboard.press("Escape")
    assert c.get_by_role("group", name="Confirm reset").count() == 0
    c.get_by_role("button", name="Reset to defaults").click()
    c.get_by_role("group", name="Confirm reset").get_by_role("button", name="Reset").click()
    p.page.wait_for_selector(f'[data-card="{NAS_ACCEPTED}"] .h-att')
    assert len(p.api("POST", r"/thresholds/reset$")) == 1


def _conn(c) -> list:
    return c.locator(".conn .cp").evaluate_all(
        "els => els.map((e) => [e.querySelector('.cp-k').textContent, e.querySelector('.cp-v').textContent])")


def test_connect_by_hand(open_page):
    p = open_page()
    c = by_id(p, BARRACUDA)
    assert c.locator(".conn").count() == 0  # only with Details open
    c.locator(".chev").click()
    assert c.locator(".conn-h").inner_text() == "Connect by hand"
    assert _conn(c) == [["Host", "local-smart-sniffer-agent"], ["Port", "9099"], ["mDNS", "smartha-homeassistant"]]
    # It sits in the summary area, above the table.
    assert c.locator(".conn").bounding_box()["y"] < c.locator("table.attrs").bounding_box()["y"]
    assert c.locator(".conn button.copy").count() == 3
    nas = by_id(p, NAS_ATT)
    nas.locator(".chev").click()
    assert _conn(nas) == [["Host", "10.0.10.20"], ["Port", "9099"]]
    assert nas.locator(".conn-note").count() == 0
    pool = by_id(p, NAS_POOL)
    pool.locator(".chev").click()
    assert _conn(pool) == [["Host", "10.0.10.20"], ["Port", "9099"]]
    rep = by_id(p, REP_FAILED)
    rep.locator(".chev").click()
    assert rep.locator(".conn").count() == 0  # a replica is not added by hand
    assert_colours(p)
    # A machine with a password says so.
    p = open_page("HOSTS=one")
    c = p.page.locator("article.card", has_text="proxmox-01").first
    c = by_id(p, c.get_attribute("data-card"))
    c.locator(".chev").click()
    assert c.locator(".conn-note").inner_text() == "This agent wants its password (the token: line in its config)"


def test_top_of_page(open_page):
    p = open_page()
    # No intro paragraph; the agent line, then the Drives label, then the grid.
    assert p.page.locator(".app-tag").count() == 0
    assert "Drive health for this machine" not in p.page.locator("main").inner_text()
    line = p.page.locator(".agent-line")
    assert line.inner_text().replace("\n", " ").split() == [
        "HAOS", "SMART", "AGENT:", "local-smart-sniffer-agent:9099", "mDNS", "smartha-homeassistant"]
    assert "answering" not in line.inner_text() and "0.8.1" not in line.inner_text()
    assert line.locator("button.copy").count() == 2
    label = p.page.locator("p.sec-drives")
    assert label.inner_text().strip() == "DRIVES:"
    # Both section headers: uppercase, a colon, the same element and font.
    heads = p.page.locator(".sec-label")
    assert heads.count() == 2
    assert [h.inner_text().strip() for h in heads.all()] == ["HAOS SMART AGENT:", "DRIVES:"]
    fonts = [h.evaluate("e => { const c = getComputedStyle(e); return [c.fontFamily, c.fontSize, c.fontWeight, "
                        "c.letterSpacing, c.textTransform, c.color].join('|') }") for h in heads.all()]
    assert fonts[0] == fonts[1], fonts
    # No rules line under the grid; the rules version sits in the header.
    assert p.page.locator(".rules-line").count() == 0
    assert "rules from integration" not in p.page.locator("main").inner_text()
    vers = p.page.locator(".versions")
    words = vers.inner_text().replace("\n", " ").split()
    assert words[:5] == ["app", "0.3.0", "agent", "0.8.1", "rules"] and len(words) == 6, words
    assert re.fullmatch(r"v?\d+(\.\d+)+\S*", words[5]), words
    rules = vers.locator("span[title]", has_text="rules")
    assert rules.get_attribute("title") == "Integration rules this app judges drives with"
    dot = vers.locator(".agent-dot")
    assert dot.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(93, 163, 114)"
    assert dot.get_attribute("title") == "This app's agent is answering"
    box = dot.bounding_box()
    assert round(box["width"]) == 7 and round(box["height"]) == 7
    assert dot.bounding_box()["x"] < vers.get_by_text("0.8.1").bounding_box()["x"]
    head = p.page.locator("header.app-head").bounding_box()
    tops = [line.bounding_box()["y"], label.bounding_box()["y"], p.page.locator(".grid").bounding_box()["y"]]
    assert head["y"] < tops[0] < tops[1] < tops[2]
    # The footer no longer carries the agent.
    foot = p.page.locator("footer.app-foot")
    assert "This app's agent" not in foot.inner_text() and foot.locator(".foot-agent").count() == 0
    p = open_page("MDNS=off")
    line = p.page.locator(".agent-line")
    assert "mDNS" not in line.inner_text() and line.locator("button.copy").count() == 1
    p = open_page("AGENT=down")
    dot = p.page.locator(".versions .agent-dot")
    assert dot.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(204, 107, 99)"
    assert dot.get_attribute("title") == "This app's agent is not answering"
    assert "answering" not in p.page.locator(".agent-line").inner_text()
    assert_colours(p)


@pytest.mark.parametrize("width", [390])
def test_agent_line_wraps_on_a_phone(open_page, width):
    p = open_page("", width)
    assert_no_scroll(p)
    assert_no_overflow(p)
    # Each part wraps whole: no part is split over two lines.
    heights = p.page.eval_on_selector_all(".agent-line > *", "els => els.map((e) => e.getBoundingClientRect().height)")
    assert max(heights) < 24, heights


def test_footer_links(open_page):
    p = open_page()
    foot = p.page.locator("footer.app-foot")
    assert foot.locator("a:visible").all_inner_texts() == [
        "Mission Log", "App configuration", "Integration", "GitHub", "HAIR, infrared remotes in Home Assistant"]
    assert foot.get_by_role("link", name="Mission Log").get_attribute("title") == "The app's log in Home Assistant"
    hair = foot.get_by_role("link", name="HAIR, infrared remotes in Home Assistant")
    assert hair.get_attribute("href") == "https://github.com/DAB-LABS/HAIR"
    assert hair.get_attribute("target") == "_blank" and hair.get_attribute("rel") == "noopener"
    assert "Also from DAB-LABS:" in foot.locator(".also").inner_text()
    assert foot.locator(".foot-sep").count() == 1
    # Quiet, like the other links.
    colour = "e => getComputedStyle(e).color"
    assert hair.evaluate(colour) == foot.get_by_role("link", name="GitHub").evaluate(colour)


OPACITY = "e => Number(getComputedStyle(e).opacity)"


def test_plus_card_rests_dim(open_page):
    p = open_page()
    plus = p.page.locator("button.card.ghost")
    assert plus.inner_text().split("\n")[-1].strip() == "Add a drive, or a replica for testing"
    assert abs(plus.evaluate(OPACITY) - 0.3) < 0.01
    plus.hover()
    p.page.wait_for_timeout(300)
    assert plus.evaluate(OPACITY) == 1
    p.page.mouse.move(2, 2)
    p.page.wait_for_timeout(300)
    assert abs(plus.evaluate(OPACITY) - 0.3) < 0.01
    plus.focus()
    p.page.keyboard.press("Shift+Tab")
    p.page.keyboard.press("Tab")
    p.page.wait_for_timeout(300)
    assert plus.evaluate(OPACITY) == 1


def test_plus_card_rests_brighter_on_touch(browser, server):
    urllib.request.urlopen(urllib.request.Request(f"{server}/dev/reset?", method="POST"), timeout=5)
    ctx = browser.new_context(viewport={"width": 390, "height": 900}, has_touch=True, is_mobile=True)
    page = ctx.new_page()
    page.goto(server + "/")
    page.wait_for_selector("article.card")
    try:
        assert page.evaluate("matchMedia('(hover: none)').matches")
        assert abs(page.locator("button.card.ghost").evaluate(OPACITY) - 0.45) < 0.01
        grip = page.locator("article.card .grip").first
        assert abs(grip.evaluate(OPACITY) - 0.3) < 0.01
    finally:
        ctx.close()


def test_card_controls_dim_until_hover(open_page):
    p = open_page()
    c = by_id(p, NAS_MAYBE)
    other = by_id(p, NVME)
    controls = lambda card: [card.locator(".grip")] + card.locator(".c-foot .icon").all()  # noqa: E731
    assert len(controls(c)) == 4  # grip, download, eye, trash
    p.page.mouse.move(2, 2)
    p.page.wait_for_timeout(250)
    for el in controls(c):
        assert abs(el.evaluate(OPACITY) - 0.3) < 0.01
    # The exit icon, the pill and the health box stay as they are.
    for sel in (".exit", ".pill", ".hbox"):
        assert c.locator(sel).evaluate(OPACITY) == 1
    # The pointer anywhere on the card brings them all up, on that card only.
    box = c.locator(".c-vitals").bounding_box()
    p.page.mouse.move(box["x"] + 4, box["y"] + 4)
    p.page.wait_for_timeout(250)
    assert all(el.evaluate(OPACITY) == 1 for el in controls(c))
    assert all(abs(el.evaluate(OPACITY) - 0.3) < 0.01 for el in controls(other))
    # Keyboard focus brings one up without the pointer.
    p.page.mouse.move(2, 2)
    c.locator(".grip").focus()
    p.page.keyboard.press("Shift+Tab")
    p.page.keyboard.press("Tab")
    p.page.wait_for_timeout(250)
    assert c.locator(".grip").evaluate(OPACITY) == 1
    assert abs(c.locator(".c-foot .icon").first.evaluate(OPACITY) - 0.3) < 0.01


@pytest.mark.parametrize("width,span", [(1280, 2), (740, 2), (390, 1)])
def test_open_plus_card_spans(open_page, width, span):
    p = open_page("", width)
    open_add(p)
    region = p.page.get_by_role("region", name="Add", exact=True)
    cols, spans = region.evaluate(SPAN)
    assert spans == span and (cols == 3 if width == 1280 else spans == cols), (cols, spans)
    areas = region.locator("section.area").all()
    if width == 390:
        tops = [a.bounding_box()["y"] for a in areas]
        assert tops == sorted(tops) and len(set(tops)) == 3
    assert_no_scroll(p)


def _check(region, host: str, text: str):
    region.get_by_label("Host or IP").fill(host)
    region.get_by_role("button", name="Check").click()
    msg = region.locator(".check-msg")
    msg.filter(has_text=text).wait_for(timeout=4000)
    return msg


def test_check_answers_every_time(open_page):
    p = open_page("HOSTS=one")
    open_add(p)
    region = p.page.get_by_role("region", name="Add", exact=True)
    p.page.wait_for_function("() => document.querySelectorAll('.pick-row').length === 3", timeout=6000)
    p.page.wait_for_timeout(500)
    # (a) A machine already in the scan's list: ticked again, flashed, said.
    row = region.locator(".pick-row", has_text="10.0.10.61")
    row.click()
    assert not row.locator("input").is_checked()
    msg = _check(region, "10.0.10.61", "10.0.10.61 is in the list above, ticked.")
    assert msg.inner_text() == "10.0.10.61 is in the list above, ticked."
    region.locator(".pick-row.flash", has_text="10.0.10.61").wait_for(timeout=1000)
    assert row.locator("input").is_checked()
    assert region.locator(".pick-row").count() == 3
    # The flash is an outline in white or grey only.
    assert_colours(p)
    # (b) A new machine joins the list, ticked.
    msg = _check(region, "nas.lan", "Added nas.lan to the list, ticked.")
    assert region.locator(".pick-row.on", has_text="nas.lan:9099").count() == 1
    # (c) A stored machine with cards on the page.
    msg = _check(region, "10.0.10.21", "10.0.10.21 is already on the page.")
    # (d) Nothing answered, muted.
    msg = _check(region, "10.0.10.99", "Nothing answered at 10.0.10.99:9099.")
    assert msg.evaluate("e => getComputedStyle(e).color") == "rgb(143, 143, 143)"
    # (e) Something answered that is no agent.
    msg = _check(region, "10.0.10.98", "10.0.10.98:9099 answered, but it is not a SMART Sniffer agent.")
    assert msg.evaluate("e => getComputedStyle(e).color") != "rgb(143, 143, 143)"
    # The scan stays; Check never starts one.
    assert len(p.api("POST", r"/fleet/scan$")) == 1
    assert region.locator(".pick-row").count() == 4


def test_copy_buttons(open_page):
    p = open_page()
    p.ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    p.page.locator(".agent-line button.copy").first.click()
    assert p.page.evaluate("navigator.clipboard.readText()") == "local-smart-sniffer-agent:9099"
    assert p.page.locator(".agent-line .copied:visible").inner_text() == "copied"
    c = by_id(p, NAS_ATT)
    c.locator(".chev").click()
    c.get_by_role("button", name="Copy Host").click()
    assert p.page.evaluate("navigator.clipboard.readText()") == "10.0.10.20"
    # No Clipboard API (a frame without the permission): the text is selected.
    p.page.evaluate("Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })")
    c.get_by_role("button", name="Copy Port").click()
    assert p.page.evaluate("window.getSelection().toString()") == "9099"
    assert c.locator(".copied:visible").inner_text() in ("copied", "selected")


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


def test_screenshots(open_page):
    for width in WIDTHS:
        p = open_page("", width)
        _shot(p, "grid", None)
        _shot(p, "header", p.page.locator("header.app-head"))
        _shot(p, "attention-three-rows-and-more", by_id(p, NAS_ATT))
        _shot(p, "maybe-space-91-97", by_id(p, NAS_MAYBE))
        _shot(p, "health-ok-haos-space", by_id(p, NVME))
        _shot(p, "health-not-known", by_id(p, NAS_USB))
        _shot(p, "attention-health-failed-replica", by_id(p, REP_FAILED))
        _shot(p, "pool-online-datasets", by_id(p, NAS_POOL))
        _shot(p, "pool-degraded-replica", by_id(p, REP_POOL))
        _shot(p, "filesystem-card", by_id(p, NAS_FS))
        _shot(p, "unreachable", by_id(p, STUDIO))
        _shot(p, "password-placeholder", by_id(p, BACKUP_PI))
        _shot(p, "replica-scenario-picker", by_id(p, REP_NVME))
        for cid, name in ((BARRACUDA, "details-drive"), (NAS_POOL, "details-pool"), (REP_NVME, "details-replica"),
                          (NAS_ATT, "details-attention")):
            by_id(p, cid).locator(".chev").click()
            _shot(p, name, by_id(p, cid))
            by_id(p, cid).locator(".chev").click()
        by_id(p, NAS_ATT).locator(".chev").click()
        _shot(p, "grid-details-open", None)
        by_id(p, NAS_ATT).locator(".chev").click()
        usb = by_id(p, NAS_USB)
        usb.get_by_role("button", name="Not in Home Assistant yet. Add it").click()
        _shot(p, "exit-grey-confirm", usb)
        by_id(p, NAS_MAYBE).get_by_role("button", name="Remove this card").click()
        _shot(p, "remove-confirm", by_id(p, NAS_MAYBE))
        p.page.keyboard.press("Escape")
        by_id(p, NVME).get_by_role("button", name="Hide this card").click()
        _shot(p, "header-hidden-1", p.page.locator("header.app-head"))
        p.page.locator(".eye-btn").click()
        _shot(p, "hidden-revealed", p.page.locator(".grid"))
        # The drag: lifted card and drop gap.
        grip = by_id(p, BARRACUDA).locator(".grip").bounding_box()
        target = by_id(p, NAS_MAYBE).bounding_box()
        p.page.mouse.move(grip["x"] + 4, grip["y"] + 4)
        p.page.mouse.down()
        for i in range(1, 11):
            p.page.mouse.move(grip["x"] + (target["x"] + target["width"] * 0.3 - grip["x"]) * i / 10,
                              grip["y"] + (target["y"] + 40 - grip["y"]) * i / 10)
        _shot(p, "drag", None)
        p.page.mouse.up()

        p = open_page("", width)
        _shot(p, "threshold-override-closed", by_id(p, NAS_OVER))
        c = by_id(p, BARRACUDA)
        c.locator(".chev").click()
        _shot(p, "connect-by-hand", c.locator(".conn"))
        c = _edit(p, BARRACUDA)
        _shot(p, "threshold-edit", c)
        c.get_by_role("button", name="Accept current readings").click()
        _shot(p, "threshold-accept-confirm", c)
        _shot(p, "footer", p.page.locator("footer.app-foot"))
        _shot(p, "agent-line", p.page.locator(".agent-line"))
        p = open_page("POOL=degraded", width)
        _shot(p, "pool-degraded", by_id(p, NAS_POOL))
        p = open_page("HA=not_installed", width)
        _shot(p, "exit-absent", by_id(p, NVME))
        p = open_page("AGENT=down", width)
        _shot(p, "haos-not-answering", by_id(p, NVME))
        p = open_page("", width)
        pi = by_id(p, BACKUP_PI)
        pi.get_by_label("Agent password").fill("wrong")
        pi.get_by_role("button", name="Authenticate").click()
        p.page.wait_for_selector("text=That password was refused.")
        _shot(p, "password-refused", pi)
        p = open_page("SCAN=slow", width)
        open_add(p)
        p.page.wait_for_function("() => document.querySelectorAll('.pick-row').length >= 1", timeout=15000)
        _shot(p, "plus-open-scanning", p.page.get_by_role("region", name="Add", exact=True))
        p = open_page("HOSTS=none&REPLICAS=0&HA=not_installed&DRIVES=1", width)
        _shot(p, "first-run", None)
