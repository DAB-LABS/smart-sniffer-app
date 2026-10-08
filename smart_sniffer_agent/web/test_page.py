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


def last_is_plus(p: Page) -> bool:
    return p.page.evaluate("""() => [...document.querySelector('.grid').children]
        .filter((e) => e.tagName !== 'TEMPLATE').pop().classList.contains('ghost')""")


def open_add(p: Page):
    p.page.get_by_role("button", name="Add field agents and replicas").click()
    p.page.wait_for_selector("[role=region][aria-label=Add]")


# ── Colour and text checks, run in the page ──────────────────────────────────

COLOUR_AUDIT = """
() => {
  const want = { att: 'rgb(204, 107, 99)', maybe: 'rgb(204, 159, 82)', ok: 'rgb(93, 163, 114)', blue: 'rgb(3, 169, 244)' };
  const allowed = {
    att: (el) => !!el.closest('.h-att, .h-fail b, .chev.attention, .pw-row.crit .lb, .pw-row.crit .pw-val, '
      + '.kv > div.crit .lb, .kv > div.crit .v, table.attrs tr.crit'),
    maybe: (el) => !!el.closest('.h-maybe, .chev.maybe, .pw-row.warn .lb, .pw-row.warn .pw-val, '
      + '.kv > div.warn .lb, .kv > div.warn .v, table.attrs tr.warn'),
    ok: (el) => !!el.closest('.h-ok, .chev.ok'),
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
    # The problem window: its stroke and values carry the state, rows carry no marks.
    pw = by_id(p, NAS_ATT).locator(".pw")
    assert pw.evaluate("e => getComputedStyle(e).borderTopColor") == "rgba(204, 107, 99, 0.15)"
    assert by_id(p, NAS_MAYBE).locator(".pw").evaluate("e => getComputedStyle(e).borderTopColor") \
        == "rgba(204, 159, 82, 0.15)"
    # A flagged row's label and value are in its state colour; its note is not.
    for mark, rgb in (("crit", ATT), ("warn", MAYBE)):
        row = by_id(p, NAS_ATT).locator(f".pw-row.{mark}").first
        assert row.locator(".lb").evaluate("e => getComputedStyle(e).color") == rgb
        assert row.locator(".pw-val").evaluate("e => getComputedStyle(e).color") == rgb
        assert row.locator("small").evaluate("e => getComputedStyle(e).color") not in (ATT, MAYBE)
    assert p.page.locator(".mk, .pw-row .mark").count() == 0
    # Pills and the problem window have sharp corners; cards round ones.
    radii = p.page.evaluate("""() => ['.pill', '.pw', '.card'].map((s) =>
        getComputedStyle(document.querySelector(s)).borderTopLeftRadius)""")
    assert radii[0] == "0px" and radii[1] == "0px" and radii[2] != "0px"


def test_details_flagged_rows_in_state_colour(open_page):
    p = open_page()
    att = by_id(p, NAS_ATT)
    att.locator(".chev").click()
    for mark, rgb in (("crit", ATT), ("warn", MAYBE)):
        rows = att.locator(f".kv > div.{mark}")
        assert rows.count() >= 1, mark
        for row in rows.all():
            assert row.locator("dt .lb").evaluate("e => getComputedStyle(e).color") == rgb
            assert row.locator("dd .v").evaluate("e => getComputedStyle(e).color") == rgb
            note = row.locator("dt small")
            if note.count():
                assert note.evaluate("e => getComputedStyle(e).color") not in (ATT, MAYBE)
    # Healthy rows stay grey.
    plain = att.locator(".kv > div:not(.crit):not(.warn)").first
    assert plain.locator("dd .v").evaluate("e => getComputedStyle(e).color") not in (ATT, MAYBE)
    assert_colours(p)


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


def test_chevron_is_80_percent(open_page):
    p = open_page()
    box = p.page.locator(".chev svg").first.bounding_box()
    assert abs(box["width"] - 22 * 0.8) < 0.05 and abs(box["height"] - 13 * 0.8) < 0.05, box
    # The drawn line is as heavy as before: 2.2 user units at 22 px over 20 units.
    sw = p.page.locator(".chev svg").first.evaluate("e => parseFloat(getComputedStyle(e).strokeWidth)")
    assert abs(sw * box["width"] / 20 - 2.2 * 22 / 20) < 0.01, sw


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


def test_rows_stretch_and_every_problem_row_shows(open_page):
    p = open_page()
    # The third card (five problem rows) sets its row's height for the two beside it.
    heights = p.page.eval_on_selector_all(".grid > article.card", "els => els.slice(0, 3).map((e) => e.offsetHeight)")
    assert len(set(heights)) == 1, heights
    att = by_id(p, NAS_ATT)
    rows = att.locator(".pw-row")
    assert rows.count() == 5
    assert all(r.is_visible() for r in rows.all())
    assert att.locator(".pw-row").first.inner_text().replace("\n", " ").startswith("Reallocated sectors (expected 0)")
    # Details replaces the problem window, in place.
    att.locator(".chev").click()
    assert att.locator(".pw").count() == 0 and att.locator(".details table.attrs").count() == 1
    assert att.locator(".chev").get_attribute("aria-expanded") == "true"
    att.locator(".chev").click()
    assert att.locator(".pw-row").count() == 5
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
    tops = [a.bounding_box()["y"] for a in areas.all()]
    assert tops == sorted(tops) and len(set(tops)) == 3
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
    p.page.wait_for_selector("text=Nothing answered on nowhere.lan, port 9099.")
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
    assert p.page.get_by_role("button", name="Add field agents and replicas").is_visible()
    assert p.page.locator(".app-foot a:visible").all_inner_texts() == ["Mission Log", "App configuration", "GitHub"]
    assert p.page.locator(".exit").count() == 0


def test_raw_table_marks(open_page):
    p = open_page()
    c = by_id(p, BARRACUDA)
    c.locator(".chev").click()
    hits = c.locator("table.attrs tr.crit")
    assert sorted(t.split()[0] for t in hits.all_inner_texts()) == ["197", "5"]


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
        _shot(p, "attention-five-rows", by_id(p, NAS_ATT))
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
