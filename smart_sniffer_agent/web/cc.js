/* SMART Sniffer App page: the page's Alpine component, its API client and
   its formatting. Served as a static file by the proxy; no build step.

   This box only, read only: one card per drive, ZFS pool and filesystem of
   this app's own agent, then one card across the grid that leads to the
   SMART Sniffer integration's Control Center, where every machine lives
   (or says how to install or update the integration). The proxy does the
   judging: every drive arrives with the integration's verdict, a "view"
   that names its rows and their marks, its filesystems and a card id that
   stays the same across reloads, so nothing here matches rules. */

// Row keys of the drive view, and where each one sits in the raw table.
const ATA_IDS = { reallocated: 5, pending: 197, offline_unc: 198, reported_unc: 187, cmd_timeout: 188,
  realloc_events: 196, spin_retries: 10, wear: 177 };
// Device Statistics rows: page, offset, and the watched row they stand in for.
const DEVSTAT_ROWS = { reported_unc_devstat: [4, 8, 'reported_unc'], reallocated_devstat: [3, 32, 'reallocated'],
  wear_devstat: [7, 8, 'wear'] };
// Device Statistics entries by page:offset, as the Details table names them,
// with how the value reads: count, hours, sectors (a size), temp (degrees C),
// pct, minutes. null leaves an entry out; one not listed shows smartctl's
// own name as a count. The same list as the integration's Control Center.
const DEVSTAT_NAMES = {
  '1:8': ['Power cycles', 'count'], '1:16': ['Power-on hours', 'hours'], '1:24': ['Data written', 'sectors'],
  '1:32': ['Write commands', 'count'], '1:40': ['Data read', 'sectors'], '1:48': ['Read commands', 'count'],
  '1:56': null, '1:64': ['Pending errors', 'count'],
  '2:8': ['Free-fall events', 'count'], '2:16': ['Overlimit shock events', 'count'],
  '3:8': ['Spindle motor hours', 'hours'], '3:16': ['Head flying hours', 'hours'], '3:24': ['Head load events', 'count'],
  '3:32': ['Reallocated sectors', 'count'], '3:40': ['Read recovery attempts', 'count'],
  '3:48': ['Mechanical start failures', 'count'], '3:56': ['Reallocation candidates', 'count'],
  '3:64': ['High priority unloads', 'count'],
  '4:8': ['Reported uncorrectable', 'count'], '4:16': ['Command resets', 'count'],
  '4:24': ['Physical element status changes', 'count'],
  '5:8': ['Temperature', 'temp'], '5:16': ['Temperature, short-term average', 'temp'],
  '5:24': ['Temperature, long-term average', 'temp'], '5:32': ['Temperature, lifetime max', 'temp'],
  '5:40': ['Temperature, lifetime min', 'temp'], '5:48': ['Highest short-term average', 'temp'],
  '5:56': ['Lowest short-term average', 'temp'], '5:64': ['Highest long-term average', 'temp'],
  '5:72': ['Lowest long-term average', 'temp'], '5:80': ['Time above the operating maximum', 'minutes'],
  '5:88': ['Operating maximum', 'temp'], '5:96': ['Time below the operating minimum', 'minutes'],
  '5:104': ['Operating minimum', 'temp'],
  '6:8': ['Hardware resets', 'count'], '6:16': ['ASR events', 'count'], '6:24': ['Interface CRC errors', 'count'],
  '7:8': ['Endurance used', 'pct'],
};
// Device Statistics entries the card's stat row already shows (current
// temperature, power-on hours, data written, data read, endurance used):
// left out of the Device Statistics table in Details.
const DEVSTAT_IN_STATS = new Set(['5:8', '1:16', '1:24', '1:40', '7:8']);
// The wear attribute names the rules read, as the proxy has them.
const WEAR_NAMES = ['Wear_Leveling_Count', 'Media_Wearout_Indicator', 'SSD_Life_Left', 'Remaining_Lifetime_Perc',
  'Percent_Lifetime_Remain', 'Perc_Rated_Life_Remain', 'Percent_Life_Remaining'];
const NVME_FIELDS = { critical_warning: 'critical_warning', media_errors: 'media_errors',
  available_spare: 'available_spare', wear: 'percentage_used' };
const POLL_MS = 30000; // this box's drives, read again while the page is open
const DOWN_POLL_MS = 8000; // while its agent does not answer
const enc = encodeURIComponent;
// Where the integration comes from: its HACS page (through My Home
// Assistant, which opens the user's own instance), or HACS itself first.
const HACS_REPO_URL = 'https://my.home-assistant.io/redirect/hacs_repository/?owner=DAB-LABS&repository=smart-sniffer&category=integration';
const HACS_DOCS_URL = 'https://hacs.xyz/docs/use/';
// The integration's Control Center in the sidebar, unless it names another.
const PANEL_PATH = '/smart-sniffer';
// Open asks Home Assistant to go to the panel (home-assistant/navigate to
// the parent frame). Where Home Assistant does not act on it within this
// time, the top window goes there instead, as the plain link would.
const NAVIGATE_FALLBACK_MS = 700;

// The card under the grid, by the integration's state (design rev 7, B2).
const SEE_ALL = 'See all your machines in the SMART Sniffer Control Center';
const GET_MORE = 'Get more out of SMART Sniffer';
const LINK_CARDS = {
  loaded: { title: SEE_ALL, body: '', open: true },
  no_entries: { title: SEE_ALL, body: 'It opens once SMART Sniffer has its first machine. Create sensors and alerts for HAOS above, or add a machine in Settings, Devices & services.' },
  restart: { title: SEE_ALL, body: 'Restart Home Assistant to finish the update.' },
  panel_failed: { title: SEE_ALL, body: 'The Control Center did not start. Check the Home Assistant log.' },
  // "this drive" or "these drives", by how many drives this box has.
  missing: { title: GET_MORE, body: (one) => `Install the SMART Sniffer integration to get ${one ? 'this drive' : 'these drives'} with sensors and alerts, and to also add other machines.`, close: 'install' },
  update: { title: GET_MORE, body: 'Update the SMART Sniffer integration to 0.9 to add your other machines and see them all in the Control Center.', close: 'update' },
};

function cc() {
  // Row heights while a card is open, kept off the reactive state: each
  // open card's closed height at the card width it was measured at, and
  // each closed card's content height when its row was last pinned.
  const rows = { closedH: {}, sig: {}, busy: false, queued: false, full: false, check: new Set() };
  return {
    info: null,
    self: null,
    selfFailed: false,
    selfLoaded: false,
    selfBusy: false,
    selfNext: 0,
    selfLastGood: null,
    selfLastAnswer: null,
    ha: null,
    haNext: 0,
    closed: {},
    haTimers: [],
    toastTimer: null,
    now: Date.now(),
    pageErr: '',
    ui: { open: {}, confirm: null, copied: '', copiedText: '', toast: '', note: {}, pop: null },
    popOpenedAt: 0,

    // ── Start and timers ────────────────────────────────────────────
    init() {
      this.loadInfo();
      this.loadSelf();
      this.loadHa();
      setInterval(() => this.tick(), 1000);
      this.watchRows();
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) this.tick();
      });
      // The exact-number popover closes on any click outside it, a scroll
      // or Escape.
      document.addEventListener('click', (e) => {
        if (!this.ui.pop) return;
        if (e.target.closest && (e.target.closest('.numpop') || e.target.closest('.bign'))) return;
        this.ui.pop = null;
      }, true);
      // A scroll that lands just after the popover opened is the one that
      // brought the value into view (a tap at the edge, the keyboard's focus
      // scroll): it keeps the popover open.
      window.addEventListener('scroll', () => {
        if (this.ui.pop && Date.now() - this.popOpenedAt > 300) this.ui.pop = null;
      }, { passive: true });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.ui.pop) this.ui.pop = null;
        if (e.key === 'Escape' && this.ui.confirm) this.closeConfirm();
      });
    },

    // Every timer waits while the page is hidden.
    tick() {
      this.now = Date.now();
      if (document.hidden) return;
      if (!this.selfBusy && this.now >= this.selfNext) this.loadSelf();
      if (this.haNext && this.now >= this.haNext) this.loadHa();
    },

    // Home Assistant ingress serves the page at /api/hassio_ingress/<token>/,
    // so every request is relative to the page's own directory.
    get baseUrl() {
      const path = window.location.pathname;
      return path.endsWith('/') ? path : path.substring(0, path.lastIndexOf('/') + 1);
    },

    async request(url, opts = {}) {
      try {
        const init = { method: opts.method || 'GET', headers: {} };
        if (opts.body !== undefined) {
          init.headers['Content-Type'] = 'application/json';
          init.body = JSON.stringify(opts.body);
        }
        const resp = await fetch(this.baseUrl + url, init);
        let data = null;
        try { data = await resp.json(); } catch (e) { data = null; }
        return { ok: resp.ok, status: resp.status, data };
      } catch (e) {
        return { ok: false, status: 0, data: null };
      }
    },

    // ── Loads ───────────────────────────────────────────────────────
    async loadInfo() {
      const r = await this.request('app/info');
      if (r.ok && r.data) this.info = r.data;
    },

    // This box's drives every 30 seconds; every 8 while its agent does not
    // answer, so the page comes back as soon as the app has started.
    async loadSelf() {
      this.selfBusy = true;
      const r = await this.request('fleet/hosts/self/drives');
      this.selfBusy = false;
      this.selfLoaded = true;
      if (r.ok && r.data) {
        this.self = r.data;
        this.selfFailed = false;
        if (r.data.status === 'ok') {
          this.selfLastGood = r.data;
          this.selfLastAnswer = Date.now();
        } else {
          if ((r.data.items || []).length) this.selfLastGood = r.data;
          if (r.data.last_answer) this.selfLastAnswer = Date.parse(r.data.last_answer);
        }
      } else {
        this.selfFailed = true;
      }
      this.selfNext = Date.now() + (this.agentDown ? DOWN_POLL_MS : POLL_MS);
    },

    // The integration's state, with the page's own poll.
    async loadHa() {
      this.haNext = Date.now() + POLL_MS;
      const r = await this.request('ha/integration');
      if (r.ok && r.data) this.ha = r.data;
    },

    haSoon() {
      this.haTimers.forEach(clearTimeout);
      this.haTimers = [setTimeout(() => this.loadHa(), 3000), setTimeout(() => this.loadHa(), 12000)];
    },

    // ── App pages in Home Assistant ─────────────────────────────────
    // Home Assistant 2026 moved the app pages from /hassio/addon/<slug>/...
    // to /config/app/<slug>/...; before 2026 the old paths, otherwise (and
    // when the version is unknown) the new ones.
    get haLegacyPaths() {
      const m = /^(\d{4})\./.exec(String((this.info && this.info.ha_version) || ''));
      return !!m && Number(m[1]) < 2026;
    },
    appPage(page) {
      const legacy = this.haLegacyPaths;
      const slug = this.info && this.info.slug;
      if (!slug) return legacy ? '/hassio/dashboard' : '/config/apps';
      return legacy ? `/hassio/addon/${enc(slug)}/${page}` : `/config/app/${enc(slug)}/${page}`;
    },

    // ── Formatting ──────────────────────────────────────────────────
    num(n) {
      return typeof n === 'number' ? n.toLocaleString('en-US') : String(n);
    },
    // The one rule for a shortened number, everywhere on the page (the
    // stat row, the attribute table, Device Statistics, disk space): the
    // largest unit that leaves at least 1, then three significant figures
    // ("5.10 TB", "19.8 TB", "898 GB", "344 M"). Below the smallest unit
    // the number as it is. Only what is shown changes.
    scaled(n, units, below) {
      if (typeof n !== 'number' || !Number.isFinite(n)) return '';
      for (let i = 0; i < units.length; i += 1) {
        const [size, unit] = units[i];
        if (n < size) continue;
        const v = Number((n / size).toPrecision(3));
        // 999.6 GB rounds to 1000: say 1.00 TB instead.
        if (v >= 1000 && i > 0) return `${(n / units[i - 1][0]).toPrecision(3)} ${units[i - 1][1]}`;
        return `${(n / size).toPrecision(3)} ${unit}`;
      }
      return below(n);
    },
    // Sizes: decimal units, as df -H, the drive makers and the agent's totals.
    bytes(n) {
      return this.scaled(n, [[1e15, 'PB'], [1e12, 'TB'], [1e9, 'GB'], [1e6, 'MB'], [1e3, 'kB']], (x) => `${x} B`);
    },
    ago(t) {
      const ms = typeof t === 'number' ? t : Date.parse(t);
      if (!ms) return '';
      const s = Math.max(0, Math.round((this.now - ms) / 1000));
      if (s < 60) return `${s} s ago`;
      if (s < 3600) return `${Math.floor(s / 60)} min ago`;
      if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
      const d = Math.floor(s / 86400);
      return d === 1 ? '1 day ago' : `${d} days ago`;
    },
    plural(n, one, many) {
      return `${n} ${n === 1 ? one : many}`;
    },

    // ── Header ──────────────────────────────────────────────────────
    get agentVersion() {
      return this.self && this.self.status === 'ok' && this.self.agent ? this.self.agent.version : null;
    },
    get agentDown() {
      return this.selfLoaded && (this.selfFailed || (this.self && this.self.status !== 'ok'));
    },
    get agentAddress() {
      const i = this.info || {};
      return `${i.hostname || 'the agent'}:${i.agent_port || 9099}`;
    },
    get countdown() {
      if (this.selfBusy) return 'asking now';
      const s = Math.max(0, Math.ceil((this.selfNext - this.now) / 1000));
      return `asking again in ${s} s`;
    },
    get rulesVersion() {
      return (this.self && this.self.rules_version) || (this.info && this.info.rules_version) || '';
    },
    // The dot before the agent version: unknown, up or down.
    get agentState() {
      if (!this.selfLoaded) return 'unknown';
      return this.agentDown ? 'down' : 'up';
    },
    get agentDotTitle() {
      if (!this.selfLoaded) return "Asking this app's agent";
      return this.agentDown ? "This app's agent is not answering" : "This app's agent is answering";
    },
    get selfEmpty() {
      return !this.agentDown && this.self && this.self.status === 'ok'
        && !(this.self.items || []).length && !(this.self.filesystem_cards || []).length;
    },

    // ── The cards ───────────────────────────────────────────────────
    // A card: { id, kind, src, it, fs }. kind is drive, pool, filesystem,
    // down (this app's agent not answering before its first answer) or
    // loading (its first answer is still on the way). src is always self.
    cardsFrom(src, data) {
      const out = [];
      ((data && data.items) || []).forEach((it) => {
        out.push({ id: it.card_id || `${src}|${it.id}`, kind: it.type, src, it, fs: null });
      });
      ((data && data.filesystem_cards) || []).forEach((f) => {
        out.push({ id: f.card_id, kind: 'filesystem', src, it: f, fs: f.filesystem });
      });
      return out;
    },
    // In the agent's order. Until the agent first answers, one card says it
    // is being read, so the page is never blank.
    get shownCards() {
      if (!this.selfLoaded) return [{ id: 'self', kind: 'loading', src: 'self', it: null, fs: null }];
      const live = this.self && this.self.status === 'ok' ? this.self : null;
      const data = live || this.selfLastGood;
      if (data && ((data.items || []).length || (data.filesystem_cards || []).length)) {
        return this.cardsFrom('self', data);
      }
      if (this.agentDown) return [{ id: 'self', kind: 'down', src: 'self', it: null, fs: null }];
      return [];
    },
    cardClass(c) {
      return {
        machine: ['down', 'loading'].includes(c.kind),
        stale: this.unanswered(c),
        open: this.isOpen(c),
      };
    },
    // A card with Details open grows down; every other card in its grid
    // row keeps the height that row had while it was closed, so nothing
    // beside it shrinks or grows. Rows with no card open stretch as usual.
    // Rows are read from the page (cards that share a top), so this holds
    // at any column count, after a drag and after a resize.
    // Every row is pinned again only when the cards themselves change (added,
    // removed, moved, opened, closed) or the grid's width does. A change
    // inside an open card leaves its row's pins as they are; one inside a
    // closed card pins its row again, and only when the card's size changed.
    watchRows() {
      const grid = document.querySelector('.grid');
      if (!grid) return;
      const later = (full, cards) => {
        if (full) rows.full = true;
        (cards || []).forEach((e) => rows.check.add(e));
        if (rows.queued) return;
        rows.queued = true;
        requestAnimationFrame(() => {
          const all = rows.full;
          const check = [...rows.check];
          rows.queued = false;
          rows.full = false;
          rows.check.clear();
          if (all) this.pinRows();
          else this.checkRows(check);
        });
      };
      let width = null;
      if (window.ResizeObserver) {
        new ResizeObserver(() => {
          const w = this.cardW(grid);
          if (w !== width) { width = w; later(true); } else later(false, this.gridItems());
        }).observe(grid);
      }
      const cardOf = (n) => {
        const e = n.nodeType === 1 ? n : n.parentElement;
        return e && e.closest ? e.closest('.grid > [data-card]') : null;
      };
      new MutationObserver((ms) => {
        const cards = new Set();
        for (const m of ms) {
          if (m.target === grid || (m.type === 'attributes' && m.target.parentElement === grid)) { later(true); return; }
          const card = cardOf(m.target);
          if (card && !card.classList.contains('open')) cards.add(card);
        }
        if (cards.size) later(false, [...cards]);
      }).observe(grid, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class'] });
    },
    gridItems() {
      const grid = document.querySelector('.grid');
      if (!grid) return [];
      return [...grid.children].filter((e) => e.tagName !== 'TEMPLATE' && e.getClientRects().length);
    },
    // A card's height at its own content, as a closed card in a closed row.
    naturalH(e) {
      e.style.height = '';
      e.style.alignSelf = 'start';
      return e.getBoundingClientRect().height;
    },
    cardW(e) {
      return Math.round(e.getBoundingClientRect().width);
    },
    pinRows() {
      if (rows.busy) return;
      const items = this.gridItems();
      const isOpen = (e) => e.classList.contains('open');
      if (!items.some(isOpen)) {
        items.forEach((e) => { e.style.height = ''; e.style.alignSelf = ''; });
        return;
      }
      // An open card's closed height is known only at the width it was
      // measured at; after a resize, measure it again.
      if (items.some((e) => isOpen(e) && !this.knownClosed(e))) {
        this.remeasure();
        return;
      }
      this.pinGroup(items);
    },
    // The rows of these cards, pinned: natural heights first (all writes,
    // then all reads), then each row.
    pinGroup(items) {
      const isOpen = (e) => e.classList.contains('open');
      items.forEach((e) => { e.style.height = ''; e.style.alignSelf = 'start'; });
      const got = items.map((e) => ({
        e, open: isOpen(e), top: Math.round(e.offsetTop),
        h: isOpen(e) ? rows.closedH[e.dataset.card].h : e.getBoundingClientRect().height,
      }));
      const byRow = new Map();
      got.forEach((g) => { if (!byRow.has(g.top)) byRow.set(g.top, []); byRow.get(g.top).push(g); });
      byRow.forEach((row) => {
        if (!row.some((g) => g.open)) {
          row.forEach((g) => { g.e.style.alignSelf = ''; });
          return;
        }
        const h = Math.max(...row.map((g) => g.h));
        row.forEach((g) => {
          if (g.open) { g.e.style.alignSelf = ''; return; }
          g.e.style.height = h + 'px';
        });
      });
      got.forEach((g) => { if (!g.open) rows.sig[g.e.dataset.card] = this.contentH(g.e); });
    },
    // A card's content height, read without touching its style: what its
    // children take, the stretching spacer left out. It changes only when
    // the card's own size would.
    contentH(e) {
      let h = 0;
      for (const k of e.children) {
        if (k.tagName === 'TEMPLATE' || k.classList.contains('c-spacer')) continue;
        h += k.getBoundingClientRect().height;
      }
      return Math.round(h * 2) / 2;
    },
    // Closed cards whose content changed: their rows are pinned again, only
    // where one of them changed size and the row has an open card.
    checkRows(cards) {
      if (rows.busy) return;
      const items = this.gridItems();
      const isOpen = (e) => e.classList.contains('open');
      if (!items.some(isOpen)) return;
      if (items.some((e) => isOpen(e) && !this.knownClosed(e))) { this.remeasure(); return; }
      const tops = new Set();
      cards.forEach((e) => {
        if (!e.isConnected || isOpen(e) || !e.getClientRects().length) return;
        const h = this.contentH(e);
        if (rows.sig[e.dataset.card] === h) return;
        rows.sig[e.dataset.card] = h;
        tops.add(Math.round(e.offsetTop));
      });
      tops.forEach((top) => {
        const row = items.filter((e) => Math.round(e.offsetTop) === top);
        if (row.some(isOpen)) this.pinGroup(row);
      });
    },
    knownClosed(e) {
      const m = rows.closedH[e.dataset.card];
      return !!(m && m.w === this.cardW(e));
    },
    // Close the open cards for one microtask, read their closed heights at
    // the new width, open them again. No frame is painted in between.
    async remeasure() {
      rows.busy = true;
      const flush = () => new Promise((r) => queueMicrotask(r));
      const ids = Object.keys(this.ui.open).filter((k) => this.ui.open[k]);
      const act = document.activeElement;
      const actCard = act && act.closest && act.closest('[data-card]');
      const actKey = actCard && [actCard.dataset.card, act.getAttribute('aria-label'), act.className];
      try {
        ids.forEach((k) => { this.ui.open[k] = false; });
        await flush(); await flush();
        const grid = document.querySelector('.grid');
        ids.forEach((k) => {
          const e = grid && grid.querySelector(`[data-card="${CSS.escape(k)}"]`);
          if (e) rows.closedH[k] = { w: this.cardW(e), h: this.naturalH(e) };
        });
      } finally {
        ids.forEach((k) => { this.ui.open[k] = true; });
        await flush(); await flush();
        // Its closed height belongs to the width it has now, open: a width
        // that moves when the card opens (a scrollbar that comes with the
        // taller page) must not send it round again. Measuring on every
        // pass froze the page: close, no scrollbar, wider; open, scrollbar,
        // narrower; never the same width, never a frame in between.
        const grid = document.querySelector('.grid');
        ids.forEach((k) => {
          const e = grid && grid.querySelector(`[data-card="${CSS.escape(k)}"]`);
          if (e && rows.closedH[k]) rows.closedH[k].w = this.cardW(e);
        });
        rows.busy = false;
      }
      if (actKey) {
        const card = document.querySelector(`.grid [data-card="${CSS.escape(actKey[0])}"]`);
        const back = card && [...card.querySelectorAll('button, input, a, [tabindex]')]
          .find((x) => x.getAttribute('aria-label') === actKey[1] && x.className === actKey[2]);
        if (back) back.focus({ preventScroll: true });
      }
      this.pinRows();
    },

    // Every card's pill names its machine: HAOS. A pool or filesystem card
    // says what it is on the line under its title (kindWord).
    machine() {
      return 'HAOS';
    },
    pill(c) {
      return this.machine(c);
    },
    kindWord(c) {
      if (c.kind === 'pool') return 'ZFS pool';
      if (c.kind === 'filesystem') return 'Filesystem';
      return null;
    },
    // The card's name: the model, the pool's name, the mount point.
    baseTitle(c) {
      if (c.kind === 'down') return "This app's agent is not answering";
      if (c.kind === 'loading') return "This machine's drives";
      if (c.kind === 'filesystem') return c.fs.mountpoint;
      if (c.kind === 'pool') return c.it.pool.name;
      return (c.it.drive && c.it.drive.model) || c.it.id;
    },
    title(c) {
      return this.baseTitle(c);
    },
    renamed() {
      return false;
    },
    modelLine(c) {
      return `${c.kind === 'pool' ? 'Pool' : 'Model'}: ${this.baseTitle(c)}`;
    },
    footNote(c) {
      return this.ui.note[`dl:${c.id}`] || '';
    },
    // The where line, as parts shown with a gap between them.
    where(c) {
      const it = c.it || {};
      if (c.kind === 'down' || c.kind === 'loading') return [this.agentAddress];
      // A filesystem or pool card starts with what it is; its pill names
      // the machine, as a drive card's does.
      if (c.kind === 'filesystem') return [this.kindWord(c), c.fs.device, c.fs.fstype].filter(Boolean);
      if (c.kind === 'pool') return [this.kindWord(c), this.poolLayout(it)].filter(Boolean);
      const drive = it.drive || {};
      return [drive.device_path, this.proto(drive)].filter(Boolean);
    },
    proto(drive) {
      const dev = (drive.smart_data && drive.smart_data.device) || {};
      if (drive.protocol === 'ATA' && dev.type === 'sat') return 'SATA';
      return drive.protocol || '';
    },
    vitals(c) {
      const it = c.it || {};
      if (c.kind === 'pool') {
        const p = it.pool || {};
        const out = [this.poolState(it)];
        if (p.last_scrub_end) out.push(`scrub ${this.ago(p.last_scrub_end)}`);
        return out.filter(Boolean);
      }
      return [];
    },
    // A drive's stat row: Temp, Powered, Written, Read, Worn, each a tiny
    // label over its value (number, then its unit). A value the drive does
    // not report is left out, tile and all; none at all, no row. A
    // temperature of 0 C is taken as not reported (virtual and unknown
    // disks send 0 for "no sensor"). Powered, written and read keep the
    // exact number behind them (x). Plain: no colours.
    stats(c) {
      if (c.kind !== 'drive') return [];
      const it = c.it || {};
      const s = (it.view && it.view.stats) || {};
      const out = [];
      const tile = (k, text, x) => {
        const m = /^(-?[\d.,]+)(\s*)(.*)$/.exec(text);
        out.push({ k, n: m ? m[1] : text, u: m ? `${m[2]}${m[3]}` : '', x: x || '' });
      };
      if (typeof s.temp_c === 'number' && Number.isFinite(s.temp_c) && s.temp_c !== 0) tile('Temp', `${s.temp_c} C`);
      if (typeof s.power_on_hours === 'number') {
        tile('Powered', this.hoursText(s.power_on_hours), `${this.num(s.power_on_hours)} hours powered on`);
      }
      ['written', 'read'].forEach((which) => {
        const b = this.volBytes(it, which);
        if (b !== null) tile(which === 'written' ? 'Written' : 'Read', this.bytes(b), `${this.num(b)} bytes ${which}`);
      });
      const worn = this.worn(it);
      if (worn !== null) tile('Worn', `${worn}%`);
      return out;
    },
    // Bytes written or read: the agent's volume (Device Statistics, the
    // vendor attribute or the NVMe log), else the NVMe log's data units or
    // the drive's Total LBAs attribute (241, 242) times its sector size.
    volBytes(it, which) {
      const v = this.vol(it, which);
      if (v) return v.bytes;
      const sd = this.smartOf(it);
      const log = sd.nvme_smart_health_information_log;
      const units = log && log[`data_units_${which}`];
      if (typeof units === 'number' && units >= 0) return units * 512000;
      const id = which === 'written' ? 241 : 242;
      const a = ((sd.ata_smart_attributes || {}).table || []).find((x) => x.id === id && /LBA/i.test(String(x.name)));
      const raw = a && a.raw && a.raw.value;
      if (typeof raw === 'number' && raw >= 0) return raw * ((Number(sd.logical_block_size)) || 512);
      return null;
    },
    // Percent of rated endurance used: the NVMe log's percentage_used or the
    // ATA Device Statistics Percentage Used Endurance Indicator (page 7).
    // Never worked out from a vendor's wear attribute.
    worn(it) {
      const log = this.smartOf(it).nvme_smart_health_information_log;
      if (log && typeof log.percentage_used === 'number') return log.percentage_used;
      const e = this.devEntry(it, 7, 8);
      return e && typeof e.value === 'number' ? e.value : null;
    },
    smartOf(it) {
      return (it && it.drive && it.drive.smart_data) || {};
    },
    // Device Statistics as the agent sent them, when it read them.
    devPages(it) {
      const ds = (it && it.drive && it.drive.device_statistics) || {};
      return ds.status === 'present' && Array.isArray(ds.pages) ? ds.pages.filter((p) => p && Array.isArray(p.table)) : [];
    },
    devEntry(it, page, offset) {
      const p = this.devPages(it).find((x) => x.number === page);
      const e = p && p.table.find((x) => x.offset === offset);
      return e && e.flags && e.flags.valid === true ? e : null;
    },

    // Health: attention, maybe, ok, unknown; null for a card with no health line.
    health(c) {
      if (['filesystem', 'machine', 'loading'].includes(c.kind)) return null;
      if (c.kind === 'down') return 'unknown';
      const it = c.it;
      if (c.kind === 'pool') {
        if (this.vanished(it)) return 'unknown';
        const s = it.verdict && it.verdict.state;
        return s === 'YES' ? 'attention' : s === 'NO' ? 'ok' : 'unknown';
      }
      return (it.view && it.view.header) || 'unknown';
    },
    failed(c) {
      return c.kind === 'drive' && !!(c.it.view && c.it.view.health_failed);
    },
    vanished(it) {
      return !!((it.view && it.view.vanished) || (it.lab && it.lab.vanished));
    },
    hasDetails(c) {
      return c.kind === 'drive' || c.kind === 'pool';
    },
    isOpen(c) {
      return !!this.ui.open[c.id];
    },
    toggleOpen(c) {
      // Before a card opens, note its closed height for its row.
      if (!this.ui.open[c.id]) {
        const e = document.querySelector(`.grid [data-card="${CSS.escape(c.id)}"]`);
        if (e) {
          const keep = [e.style.height, e.style.alignSelf];
          rows.closedH[c.id] = { w: this.cardW(e), h: this.naturalH(e) };
          [e.style.height, e.style.alignSelf] = keep;
        }
      }
      this.ui.open[c.id] = !this.ui.open[c.id];
    },

    // The problem window: every flagged row, no paging.
    problems(c) {
      if (c.kind === 'drive') {
        return this.rows(c.it).filter((r) => r.mark === 'crit' || r.mark === 'warn')
          .map((r) => ({ key: r.key, label: r.name, note: this.rowNote(r), value: r.value, mark: r.mark }));
      }
      if (c.kind === 'pool') {
        if (!this.poolBad(c.it)) return [];
        const out = [];
        const state = this.poolState(c.it);
        if (state && state !== 'ONLINE') {
          out.push({ key: 'state', label: 'Pool state', note: 'expected ONLINE', value: state, mark: 'crit' });
        }
        this.poolDisks(c.it).filter((d) => d.bad).forEach((d) => {
          const errs = [['read', d.read], ['write', d.write], ['cksum', d.cksum]].filter(([, n]) => n)
            .map(([k, n]) => `${this.num(n)} ${k}`);
          out.push({ key: `disk:${d.name}`, label: d.name, note: 'expected ONLINE, no errors',
            value: [d.state, ...errs].join(', '), mark: 'crit' });
        });
        return out;
      }
      return [];
    },
    problemTone(c) {
      return this.problems(c).some((p) => p.mark === 'crit') ? 'crit' : 'warn';
    },

    rows(it) {
      return (it.view && it.view.rows) || [];
    },
    // What sits in brackets after a row's label: "accepted" for a reading
    // a threshold keeps from being flagged; the threshold when it is not the
    // default, else the expectation when the row is flagged; then where the
    // reading came from. Never by the value.
    rowNote(r) {
      const flagged = r.mark === 'crit' || r.mark === 'warn';
      const limit = r.limit ? r.limit.text : (flagged ? r.expect : null);
      return [r.accepted ? 'accepted' : null, limit, r.note].filter(Boolean).join(', ');
    },
    vol(it, which) {
      const v = it.view && it.view.volumes && it.view.volumes[which];
      return v && typeof v.bytes === 'number' ? v : null;
    },
    // The top of a drive's Details: what it is and how it is attached. A
    // field the drive's data does not have is left out. Model, serial and
    // firmware (t) open the exact-number popover with the full text and Copy.
    ident(c) {
      const it = c.it || {};
      const d = it.drive || {};
      const sd = this.smartOf(it);
      const out = [];
      const model = d.model || sd.model_name;
      if (model) out.push({ k: 'Model', v: String(model), t: true });
      const serial = d.serial || sd.serial_number;
      if (serial) out.push({ k: 'Serial', v: String(serial), t: true });
      if (sd.firmware_version) out.push({ k: 'Firmware', v: String(sd.firmware_version), t: true });
      const cap = (sd.user_capacity && sd.user_capacity.bytes) || sd.nvme_total_capacity;
      if (typeof cap === 'number' && cap > 0) out.push({ k: 'Capacity', v: this.bytes(cap), x: `${this.num(cap)} bytes` });
      const link = this.link(d, sd);
      if (link) out.push({ k: 'Interface', v: link });
      return out;
    },
    // NVMe; SATA with the link speed smartctl reports (and the drive's top
    // speed when the link runs slower); else the protocol the agent names.
    link(d, sd) {
      const dev = sd.device || {};
      const proto = String(d.protocol || dev.protocol || '');
      if (sd.nvme_smart_health_information_log || /nvme/i.test(proto) || dev.type === 'nvme') return 'NVMe';
      if (/^ata$/i.test(proto) || dev.type === 'sat' || dev.type === 'ata') {
        const speed = sd.interface_speed || {};
        const cur = speed.current && speed.current.string;
        const max = speed.max && speed.max.string;
        if (cur) return max && max !== cur ? `SATA ${cur} (max ${max})` : `SATA ${cur}`;
        return max ? `SATA, max ${max}` : 'SATA';
      }
      return proto;
    },
    unsupportedNote(c) {
      return c.kind === 'drive' && this.health(c) === 'unknown' && !!c.it.verdict;
    },
    reportKind(it) {
      const sd = (it.drive && it.drive.smart_data) || {};
      if (sd.nvme_smart_health_information_log) return 'nvme';
      if (((sd.ata_smart_attributes || {}).table || []).length) return 'ata';
      return 'none';
    },
    // Details holds one table. An ATA drive: its attribute table, with each
    // watched row's mark and note on the attribute it reads, and a watched
    // attribute the drive does not report as a row of its own. A Device
    // Statistics reading that stands in for one is marked in the Device
    // Statistics table below (devstatTable); only when the pages are not at
    // hand does it sit here, after the row it stands in for. An NVMe drive:
    // its health log, the same way. Anything else: the watched rows.
    detailTable(it) {
      const kind = this.reportKind(it);
      const sd = (it.drive && it.drive.smart_data) || {};
      const entries = [];
      const extras = [];
      const place = (r, entry) => {
        if (r.mark === 'crit' || r.mark === 'warn') entry.mark = r.mark;
        const note = this.rowNote(r);
        if (note) entry.note = note;
      };
      const extra = (r, sortId) => {
        const missing = r.value === 'not known';
        extras.push({ key: `r:${r.key}:${r.name}`, id: (r.key !== 'wear' && ATA_IDS[r.key]) || '', name: r.name, value: missing ? '' : r.value,
          worst: '', thresh: '', raw: missing ? '' : r.value, missing, gap: 'not reported', note: this.rowNote(r),
          mark: r.mark === 'crit' || r.mark === 'warn' ? r.mark : null, sortId });
      };
      if (kind === 'ata') {
        const pad = (n) => (typeof n === 'number' ? String(n).padStart(3, '0') : '');
        ((sd.ata_smart_attributes || {}).table || []).forEach((a) => entries.push({
          key: `a:${a.id}`, id: a.id, name: a.name, value: pad(a.value), worst: pad(a.worst), thresh: pad(a.thresh),
          raw: (a.raw && (a.raw.string ?? String(a.raw.value))) || '', rawValue: a.raw ? a.raw.value : '',
          mark: null, note: '', field: a.name,
        }));
        this.rows(it).forEach((r) => {
          // In the Device Statistics table, unless that entry is one the
          // stat row shows (endurance used): then it stays a row here.
          const at = DEVSTAT_ROWS[r.key];
          if (this.devRowEntry(it, r) && !DEVSTAT_IN_STATS.has(`${at[0]}:${at[1]}`)) return;
          const id = r.key === 'wear' ? null : ATA_IDS[r.key];
          const hit = r.value === 'not known' ? null : r.key === 'wear'
            ? entries.find((e) => WEAR_NAMES.includes(e.name)) : entries.find((e) => e.id === id);
          if (hit) { place(r, hit); return; }
          const base = DEVSTAT_ROWS[r.key] ? DEVSTAT_ROWS[r.key][2] : r.key;
          extra(r, ATA_IDS[base] && base !== 'wear' ? ATA_IDS[base] : 1000);
        });
        // Each extra row by the attribute number it reads, else at the end.
        extras.forEach((x) => {
          x.value = '';
          const at = entries.findIndex((e) => typeof e.id === 'number' && e.sortId === undefined && e.id > x.sortId);
          if (at < 0) entries.push(x); else entries.splice(at, 0, x);
        });
        return { kind, rows: this.cleanRaw(kind, entries, sd) };
      }
      if (kind === 'nvme') {
        const log = sd.nvme_smart_health_information_log || {};
        Object.entries(log).forEach(([k, v]) => entries.push({
          key: `n:${k}`, name: k, value: Array.isArray(v) ? v.join(' ') : String(v), mark: null, note: '',
          field: k, rawValue: v,
        }));
        this.rows(it).forEach((r) => {
          const hit = r.value === 'not known' ? null : entries.find((e) => e.field === NVME_FIELDS[r.key]);
          if (hit) place(r, hit); else extra(r, 1000);
        });
        extras.forEach((x) => entries.push(x));
        return { kind, rows: this.cleanRaw(kind, entries, sd) };
      }
      // An unsupported drive: its watched rows are not known, not missing.
      this.rows(it).forEach((r) => extra(r, 1000));
      extras.forEach((x) => { x.gap = 'not known'; });
      return { kind, rows: this.cleanRaw(kind, extras, sd) };
    },
    // What the Raw (or Value) cell shows, the exact number in its tooltip
    // and in a small popover on click or tap (with Copy). Counts of LBAs
    // (Total LBAs Written and Read, 241 and 242 on most drives) as a size:
    // LBAs times the drive's logical sector size (512 when it does not say);
    // NVMe data units the same way (one unit is 512,000 bytes); power-on
    // hours as days; other large counts with separators, or M, B, T past a
    // million. Only what is shown changes: the data, sensors and Agentic
    // access keep the numbers as the drive gave them.
    cleanRaw(kind, rows, sd) {
      const sector = (sd && Number(sd.logical_block_size)) || 512;
      rows.forEach((e) => {
        const text = kind === 'ata' ? e.raw : e.value;
        e.disp = text;
        e.exact = '';
        if (e.missing || !e.field) return;
        const n = typeof e.rawValue === 'number' ? e.rawValue : NaN;
        if (!Number.isFinite(n) || n < 0 || !/^\d+$/.test(String(text).trim())) return;
        const field = String(e.field);
        if (kind === 'ata' && /LBA/i.test(field)) {
          e.disp = this.bytes(n * sector);
          e.exact = `${this.num(n)} LBAs`;
        } else if (kind === 'nvme' && /^data_units_(read|written)$/.test(field)) {
          e.disp = this.bytes(n * 512000);
          e.exact = `${this.num(n)} data units`;
        } else if ((kind === 'ata' && (e.id === 9 || /^Power_On_Hours/i.test(field))) || (kind === 'nvme' && field === 'power_on_hours')) {
          e.disp = this.hoursText(n);
          e.exact = `${this.num(n)} hours`;
        } else if (n >= 10000) {
          e.disp = this.compact(n);
          e.exact = this.num(n);
        }
      });
      return rows;
    },
    // A table name in pieces, each ending at an underscore, so it can wrap
    // there (a <wbr> after each piece) and never inside a word.
    nameParts(name) {
      return String(name ?? '').split(/(?<=_)/);
    },
    // Hours as days ("887 d"), under 48 hours as hours.
    hoursText(h) {
      return h < 48 ? `${this.num(h)} h` : `${this.num(Math.round(h / 24))} d`;
    },
    // A large count: separators up to a million, then M, B or T, by the
    // same rule as a size.
    compact(n) {
      return this.scaled(n, [[1e12, 'T'], [1e9, 'B'], [1e6, 'M']], (x) => this.num(x));
    },
    // The exact number behind a shown one, in a small popover by it. Copy
    // takes its digits; a text value (model, serial, firmware, host, port)
    // passes its own copy text, the whole value.
    showExact(ev, text, copyText) {
      const el = ev.currentTarget;
      const r = el.getBoundingClientRect();
      if (this.ui.pop && this.ui.pop.el === el) { this.ui.pop = null; return; }
      const left = Math.max(8, Math.min(r.left, window.innerWidth - 240));
      const copy = copyText === undefined ? String(text).replace(/[^0-9]/g, '') : String(copyText);
      this.popOpenedAt = Date.now();
      this.ui.pop = { el, text, copy, x: left, y: r.bottom + 4 };
      // Then by its real width: under the value where it fits, else as far
      // right as the window allows (a long value never runs off the edge).
      this.$nextTick(() => {
        const box = document.querySelector('.numpop');
        if (!box || !this.ui.pop || this.ui.pop.el !== el) return;
        const x = Math.max(8, Math.min(r.left, window.innerWidth - box.getBoundingClientRect().width - 8));
        if (x !== this.ui.pop.x) this.ui.pop.x = x;
      });
    },
    closeExact() {
      this.ui.pop = null;
    },
    // The Device Statistics entry a watched row reads (the integration's
    // stand-in for a missing attribute), when the pages have it.
    devRowEntry(it, r) {
      const at = DEVSTAT_ROWS[r.key];
      return at ? this.devEntry(it, at[0], at[1]) : null;
    },
    // The Device Statistics table: every valid entry of the pages the agent
    // read, by a plain name, its value shown by the same rules as the
    // attribute table (sizes, days, large counts, each with the exact number
    // behind it). A watched row that reads an entry marks it here.
    devstatTable(it) {
      const pages = this.devPages(it);
      if (!pages.length) return [];
      const ds = it.drive.device_statistics || {};
      const sector = Number(ds.logical_block_size) || Number(this.smartOf(it).logical_block_size) || 512;
      const marks = {};
      this.rows(it).forEach((r) => {
        const at = DEVSTAT_ROWS[r.key];
        if (at) marks[`${at[0]}:${at[1]}`] = r;
      });
      const out = [];
      pages.forEach((p) => p.table.forEach((e) => {
        if (!e || !e.flags || e.flags.valid !== true || typeof e.value !== 'number') return;
        const key = `${p.number}:${e.offset}`;
        const known = DEVSTAT_NAMES[key];
        if (known === null) return; // not a reading (the drive's clock)
        if (DEVSTAT_IN_STATS.has(key)) return; // in the card's stat row
        const [name, unit] = known || [String(e.name || key), 'count'];
        const row = { key: `d:${key}`, name, title: `${e.name || name} (page ${p.number})`, disp: '', exact: '', mark: null, note: '' };
        const n = e.value;
        if (unit === 'sectors') { row.disp = this.bytes(n * sector); row.exact = `${this.num(n)} sectors`; }
        else if (unit === 'hours') { row.disp = this.hoursText(n); row.exact = `${this.num(n)} hours`; }
        else if (unit === 'temp') row.disp = `${n} C`;
        else if (unit === 'pct') row.disp = `${n}%`;
        else if (unit === 'minutes') row.disp = `${this.num(n)} min`;
        else if (n >= 10000) { row.disp = this.compact(n); row.exact = this.num(n); }
        else row.disp = this.num(n);
        const r = marks[key];
        if (r) {
          if (r.mark === 'crit' || r.mark === 'warn') row.mark = r.mark;
          row.note = this.rowNote(r).replace(/(, )?from device statistics/, '');
        }
        out.push(row);
      }));
      return out;
    },

    // ── Connect by hand ─────────────────────────────────────────────
    // What to type into the integration to add this box by hand: this
    // app's agent as Home Assistant reaches it.
    connect(c) {
      if (!['drive', 'pool'].includes(c.kind)) return null;
      const i = this.info || {};
      const fields = [{ k: 'Host', v: i.hostname || '' }, { k: 'Port', v: String(i.agent_port || 9099) }];
      if (i.mdns_name) fields.push({ k: 'mDNS', v: i.mdns_name });
      return { fields: fields.filter((f) => f.v), password: false };
    },
    // This app's agent, for the line at the top of the page.
    get topAgent() {
      const i = this.info;
      if (!i || !i.hostname) return null;
      return { addr: `${i.hostname}:${i.agent_port || 9099}`, mdns: i.mdns_name || '' };
    },
    // Copy to the clipboard; where the browser refuses (an insecure page, a
    // frame without the permission) select the text so it can be copied by
    // hand.
    async copy(ev, text, key) {
      const btn = ev.currentTarget;
      let ok = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          ok = true;
        }
      } catch (e) { ok = false; }
      if (!ok) {
        const el = btn && btn.parentElement && btn.parentElement.querySelector('.cp-v');
        if (el) {
          const range = document.createRange();
          range.selectNodeContents(el);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        }
      }
      // Copied: nothing stays selected, so a phone leaves no selection
      // handles over the value. Not copied: the value stays selected, to
      // copy by hand.
      if (ok) {
        try { window.getSelection().removeAllRanges(); } catch (e) { /* nothing to clear */ }
      }
      this.ui.copied = key;
      this.ui.copiedText = ok ? 'copied' : 'selected';
      clearTimeout(this.copyTimer);
      this.copyTimer = setTimeout(() => { if (this.ui.copied === key) this.ui.copied = ''; }, 1600);
    },

    // Pools
    poolBad(it) {
      return !!(it.verdict && it.verdict.state === 'YES');
    },
    poolLayout(it) {
      const v = it.view || {};
      if (v.layout) return v.layout;
      return null;
    },
    poolState(it) {
      return (it.view && it.view.state) || (it.pool && it.pool.state) || '';
    },
    poolDisks(it) {
      const v = it.view || {};
      const disks = v.disks || (it.lab && it.lab.devices);
      const bad = (d) => d.state !== 'ONLINE' || !!(d.read || d.write || d.cksum);
      if (disks) return disks.map((d) => ({ ...d, bad: d.mark === 'crit' || bad(d) }));
      // A real agent lists only its problem vdevs.
      return ((it.pool && it.pool.problem_vdevs) || []).filter((p) => !/^(mirror|raidz|draid|spare|log|cache)/.test(String(p.type || '')))
        .map((p) => {
          const d = { name: p.name, state: p.state, read: p.read_errors || 0, write: p.write_errors || 0,
            cksum: p.checksum_errors || 0 };
          return { ...d, bad: bad(d) };
        });
    },
    poolFoot(it) {
      const p = it.pool || {};
      if (!p.last_scrub_end) return '';
      let s = `Last scrub ${this.ago(p.last_scrub_end)}`;
      if (p.last_scrub_repaired) s += `, ${this.num(p.last_scrub_repaired)} repaired`;
      else if (p.last_scrub_repaired === 0) s += ', nothing repaired';
      return `${s}.`;
    },

    // ── Disk space ──────────────────────────────────────────────────
    spaces(c) {
      if (c.kind === 'filesystem') return [c.fs];
      return (c.it && c.it.filesystems) || [];
    },
    spaceShown(c) {
      return this.spaces(c).slice(0, 3);
    },
    spaceMore(c) {
      return Math.max(0, this.spaces(c).length - 3);
    },
    pct(f) {
      return Math.round(Number(f.use_percent) || 0);
    },
    // The bar's step: lo below 80%; s80, s90, s95 as the fill brightens and
    // the number weighs more; na for a filesystem that is not mounted.
    spaceStep(f) {
      if (f.status === 'unavailable') return 'na';
      const p = this.pct(f);
      if (p >= 95) return 's95';
      if (p >= 90) return 's90';
      if (p >= 80) return 's80';
      return 'lo';
    },
    spaceInside(f) {
      const step = this.spaceStep(f);
      return step === 'lo' || step === 'na' ? '' : `${this.pct(f)}%`;
    },
    spaceText(f) {
      const step = this.spaceStep(f);
      if (step === 'na') return 'not mounted';
      if (step === 'lo') return `${this.pct(f)}% of ${this.bytes(f.total_bytes)}`;
      return `${this.bytes(f.available_bytes)} free`;
    },
    spaceUsed(f) {
      if (f.status === 'unavailable') return 'not mounted';
      return `${this.bytes(f.used_bytes)} used of ${this.bytes(f.total_bytes)} total`;
    },
    spaceFill(f) {
      return f.status === 'unavailable' ? 0 : Math.min(100, Math.max(0, Number(f.use_percent) || 0));
    },

    // ── Footer and corner icon ──────────────────────────────────────
    unanswered() {
      return this.agentDown;
    },
    lastSeen(c) {
      const at = this.agentDown ? this.selfLastAnswer
        : (c.it && c.it.drive && c.it.drive.last_updated) || (this.self && this.self.read_at);
      const when = at ? this.ago(at) : 'not yet';
      return `Last seen: ${when}${this.unanswered(c) ? ', not answering' : ''}`;
    },
    canDownload(c) {
      return c.kind === 'drive' || c.kind === 'pool';
    },
    showToast(text) {
      clearTimeout(this.toastTimer);
      this.ui.toast = text;
      this.toastTimer = setTimeout(() => { this.ui.toast = ''; }, 4000);
    },

    // The card as a JSON file: what the agent sent for it.
    async download(c) {
      const key = `dl:${c.id}`;
      this.ui.note[key] = '';
      try {
        const resp = await fetch(`${this.baseUrl}cards/${enc(c.id)}/file`);
        if (!resp.ok) {
          this.ui.note[key] = 'It was not saved. Try again.';
          setTimeout(() => { this.ui.note[key] = ''; }, 5000);
          return;
        }
        const blob = await resp.blob();
        const cd = resp.headers.get('Content-Disposition') || '';
        const m = /filename="?([^";]+)"?/.exec(cd);
        const name = m ? m[1] : `${String(c.id).replace(/[^a-z0-9]+/gi, '-')}.json`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
      } catch (e) { /* the next read shows the state */ }
    },

    // ── The integration ─────────────────────────────────────────────
    get integrationInstalled() {
      const s = this.ha && this.ha.reachable !== false ? this.ha.state : null;
      return !!s && s !== 'missing';
    },
    get integrationUrl() {
      return (this.ha && this.ha.integration_url) || '/config/integrations/integration/smart_sniffer';
    },
    get panelPath() {
      const p = this.ha && this.ha.panel_path;
      return typeof p === 'string' && /^\/[a-z0-9][a-z0-9_-]*$/.test(p) ? p : PANEL_PATH;
    },
    // The card under the grid: null while the integration's state is not
    // known, or once its install or update card is closed.
    get linkCard() {
      const ha = this.ha;
      if (!ha || ha.reachable === false || !ha.state) return null;
      const state = ha.state === 'loaded' && ha.panel === false ? 'panel_failed' : ha.state;
      const base = LINK_CARDS[state];
      if (!base) return null;
      if (base.close && ((ha.dismissed || {})[base.close] || this.closed[base.close])) return null;
      const data = this.self || this.selfLastGood;
      const drives = ((data && data.items) || []).filter((i) => i.type === 'drive').length;
      const body = typeof base.body === 'function' ? base.body(drives === 1) : base.body;
      const card = { state, title: base.title, body, open: !!base.open, close: base.close || null, link: null };
      if (state === 'missing') {
        card.link = { text: 'How to install', href: ha.hacs === false ? HACS_DOCS_URL : HACS_REPO_URL };
      } else if (state === 'update') {
        card.link = { text: 'Open in HACS', href: HACS_REPO_URL };
      }
      return card;
    },
    async dismissCard(which) {
      this.closed[which] = true;
      const r = await this.request('app/notice/dismiss', { method: 'POST', body: { notice: which } });
      if (r.ok && this.ha) this.ha.dismissed = { ...(this.ha.dismissed || {}), [which]: true };
    },
    // Open: Home Assistant goes to the integration's panel. Framed in Home
    // Assistant, the page asks it with home-assistant/navigate; when Home
    // Assistant does not act on that, the top window goes there as the
    // plain link does. In its own tab the link is all there is.
    openPanel(ev) {
      const path = this.panelPath;
      let parent = null;
      try { parent = window.parent !== window ? window.parent : null; } catch (e) { parent = null; }
      if (!parent) return;
      ev.preventDefault();
      try {
        parent.postMessage({ type: 'home-assistant/navigate', path }, window.location.origin);
      } catch (e) { /* the fallback below goes there */ }
      setTimeout(() => {
        let at = null;
        try { at = window.top.location.pathname; } catch (e) { at = null; }
        if (at === path) return;
        try { window.top.location.assign(path); } catch (e) { window.open(path, '_top'); }
      }, NAVIGATE_FALLBACK_MS);
    },

    // ── Create sensors and alerts for HAOS ──────────────────────────
    get createShown() {
      return !!(this.ha && this.ha.reachable !== false && this.ha.create);
    },
    // The confirm under the line: what Create makes, in the user's words.
    // The page lives inside Home Assistant, so it never says "add to Home
    // Assistant".
    askText() {
      const data = this.self || this.selfLastGood;
      const n = ((data && data.items) || []).filter((i) => i.type === 'drive').length;
      const what = 'health sensors for dashboards and automations, and an alert when it needs attention.';
      let line = `Each of its ${n} drives gets ${what}`;
      if (n === 1) line = `Its drive gets ${what}`;
      if (n === 0) line = `It gets ${what}`;
      return { title: 'Create sensors and alerts for HAOS?', line };
    },
    askCreate() {
      if (this.ui.confirm) { this.closeConfirm(); return; }
      const ask = this.askText();
      this.ui.confirm = { target: 'self', text: ask.title, line: ask.line, busy: false, err: '' };
    },
    closeConfirm() {
      this.ui.confirm = null;
    },
    async confirmAdd() {
      const c = this.ui.confirm;
      if (!c || c.busy) return;
      c.busy = true;
      c.err = '';
      const r = await this.request('ha/add', { method: 'POST', body: { target: 'self' } });
      c.busy = false;
      const result = r.data && r.data.result;
      if (r.ok && ['created', 'already_added'].includes(result)) {
        this.ui.confirm = null;
        // The line goes at once; the card follows the integration.
        if (this.ha) this.ha.create = false;
        this.loadHa();
        this.haSoon();
        return;
      }
      const why = r.data && (r.data.message || r.data.error);
      c.err = {
        cannot_connect: 'Home Assistant could not reach this agent.',
        integration_not_loaded: 'The integration is not loaded yet. Restart Home Assistant and try again.',
        invalid_auth: 'Home Assistant refused the password.',
        unreachable: 'Home Assistant is not answering.',
      }[why] || 'Home Assistant did not create them. The App log may say why.';
    },
  };
}
