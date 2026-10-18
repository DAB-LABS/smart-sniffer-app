/* SMART Sniffer Control Center: the page's Alpine component, its API client
   and its formatting. Served as a static file by the proxy; no build step.

   One grid of cards: one per drive, per ZFS pool and per filesystem that
   belongs to no drive, from this machine (HAOS), every field agent and the
   replica agent, then the + card. The proxy does the judging: every drive
   arrives with the integration's verdict, a "view" that names its rows and
   their marks, its filesystems and a card id that stays the same across
   reloads, so nothing here matches rules. The card order and the hidden
   cards are kept by the proxy in the app options.

   The page only reads the network when asked: Scan, Rescan and Check, and
   reads of stored hosts. */

// Row keys of the drive view, and where each one sits in the raw table.
const ATA_IDS = { reallocated: 5, pending: 197, offline_unc: 198, reported_unc: 187, cmd_timeout: 188,
  realloc_events: 196, spin_retries: 10, wear: 177 };
// Device Statistics rows: page, offset, and the watched row they stand in for.
const DEVSTAT_ROWS = { reported_unc_devstat: [4, 8, 'reported_unc'], reallocated_devstat: [3, 32, 'reallocated'],
  wear_devstat: [7, 8, 'wear'] };
// The wear attribute names the rules read, as the proxy has them.
const WEAR_NAMES = ['Wear_Leveling_Count', 'Media_Wearout_Indicator', 'SSD_Life_Left', 'Remaining_Lifetime_Perc',
  'Percent_Lifetime_Remain', 'Perc_Rated_Life_Remain', 'Percent_Life_Remaining'];
const NVME_FIELDS = { critical_warning: 'critical_warning', media_errors: 'media_errors',
  available_spare: 'available_spare', wear: 'percentage_used' };
const NONE = { kind: 'none', href: null };
const COMPARE_WORDS = { gt: 'alert above', ge: 'alert at', lt: 'alert below' };
const LONG_PRESS_MS = 450;
const ARM_MS = 4000; // how long the trash's Remove? waits for its second click
const TOAST_MS = 4000; // how long a removal's Home Assistant message shows
const HA_NAME_FAILED = 'Saved in this app; Home Assistant did not take the name.';
const enc = encodeURIComponent;

function parseBlock(text) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:\/(\d{1,2}))?$/.exec(String(text).trim());
  if (!m) return null;
  const octets = m.slice(1, 5).map(Number);
  const prefix = m[5] === undefined ? 32 : Number(m[5]);
  if (octets.some((o) => o > 255) || prefix > 32) return null;
  return { octets, prefix };
}

function block24(octets) {
  return `${octets[0]}.${octets[1]}.${octets[2]}.0/24`;
}

function cc() {
  // Row heights while a card is open, kept off the reactive state: each
  // open card's closed height at the card width it was measured at, and
  // each closed card's content height when its row was last pinned.
  const rows = { closedH: {}, sig: {}, busy: false, queued: false, full: false, check: new Set() };
  // Thresholds reads in flight, by card id, and a count per card that a
  // save moves on, so a read begun before the save is not kept.
  const thrReads = new Map();
  const thrSeq = new Map();
  return {
    info: null,
    self: null,
    selfFailed: false,
    selfLoaded: false,
    selfBusy: false,
    selfNext: 0,
    selfLastGood: null,
    selfLastAnswer: null,
    replicas: null,
    repBusy: false,
    repFailed: false,
    replicasNext: 0,
    replicaFiles: null,
    hosts: [],
    hostsNext: 0,
    hostData: {},
    optimistic: {},
    ha: null,
    haTimers: [],
    toastTimer: null,
    scanTimer: null,
    scanPolling: false,
    now: Date.now(),
    // Cards: the stored order and hidden ids, cards removed while their
    // source reads again, and the drag in progress.
    order: [],
    hidden: [],
    // Names given here: card id -> name, field agent id -> name.
    names: {},
    hostNames: {},
    gone: {},
    showHidden: false,
    pageErr: '',
    hostsErr: '',
    drag: null,
    dragOrder: null,
    ui: { open: {}, cols: 1, confirm: null, arm: null, rename: null, pw: {}, pwState: {}, busy: {}, note: {}, thr: null, copied: '', copiedText: '', toast: '' },
    // Each drive's thresholds as last read, by card id: { data, err }.
    thrCache: {},
    add: {
      open: false, block: '', port: '9099', scan: null, scanId: null, last: null, wide: null, error: '',
      ticks: {}, manualHost: '', manualPort: '9099', manualRows: [], checkMsg: null, checking: false,
      repTicks: {}, upMsg: '', uploading: false, busy: false, err: '',
    },

    // ── Start and timers ────────────────────────────────────────────
    init() {
      this.loadInfo();
      this.loadCards();
      this.loadSelf();
      this.loadReplicas();
      this.loadHosts();
      this.loadHa();
      setInterval(() => this.tick(), 1000);
      this.watchCols();
      this.watchRows();
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) return;
        this.tick();
        this.resumeScan();
      });
      document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (this.ui.confirm) this.closeConfirm();
        if (this.ui.arm) this.disarm();
      });
    },

    // Every timer waits while the page is hidden.
    tick() {
      this.now = Date.now();
      if (document.hidden) return;
      if (!this.selfBusy && this.now >= this.selfNext) this.loadSelf();
      if (!this.repBusy && this.now >= this.replicasNext) this.loadReplicas();
      if (this.hostsNext && this.now >= this.hostsNext) {
        this.loadHosts();
        this.loadHa();
      }
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
      if (r.ok && r.data) {
        this.info = r.data;
        this.hostsNext = Date.now() + (r.data.refresh_minutes || 15) * 60000;
      }
    },

    async loadCards() {
      const r = await this.request('cards');
      if (r.ok && r.data) {
        this.order = r.data.order || [];
        this.hidden = r.data.hidden || [];
        if (!this.ui.rename) {
          this.names = r.data.names || {};
          this.hostNames = r.data.host_names || {};
        }
        if (!this.hidden.length) this.showHidden = false;
      }
    },

    async loadSelf() {
      this.selfBusy = true;
      const r = await this.request('fleet/hosts/self/drives');
      this.selfBusy = false;
      this.selfLoaded = true;
      this.selfNext = Date.now() + 8000;
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
    },

    async loadReplicas() {
      this.repBusy = true;
      const r = await this.request('fleet/hosts/replicas/drives');
      this.repBusy = false;
      this.replicasNext = Date.now() + 8000;
      if (r.ok && r.data) {
        this.replicas = r.data;
        this.repFailed = r.data.status !== 'ok';
      } else {
        this.repFailed = true;
      }
    },

    async loadReplicaFiles() {
      const r = await this.request('replicas');
      if (r.ok && r.data) this.replicaFiles = r.data;
      else this.replicaFiles = { agent: 'down', folder: { status: 'missing' }, files: [] };
    },

    async loadHosts() {
      this.hostsNext = Date.now() + ((this.info && this.info.refresh_minutes) || 15) * 60000;
      const r = await this.request('fleet/hosts');
      if (!r.ok || !r.data) {
        this.hostsErr = 'The stored field agents could not be read. The Supervisor did not answer.';
        return;
      }
      this.hostsErr = '';
      this.hosts = r.data.hosts || [];
      const migrating = this.hosts.some((h) => h.hidden);
      await Promise.all(this.hosts.map((h) => this.loadHost(h.id)));
      // A machine hidden as a whole becomes hidden cards on its first read.
      if (migrating) this.loadCards();
    },

    async loadHost(id) {
      const r = await this.request(`fleet/hosts/${enc(id)}/drives`);
      if (r.ok && r.data) this.hostData[id] = r.data;
    },

    async loadHa() {
      const r = await this.request('ha/devices');
      if (r.ok && r.data) this.ha = r.data;
    },

    haSoon() {
      this.haTimers.forEach(clearTimeout);
      this.haTimers = [setTimeout(() => this.loadHa(), 3000), setTimeout(() => this.loadHa(), 12000)];
    },

    refresh() {
      this.loadHosts();
      this.loadHa();
      this.loadCards();
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
    get integrationInstalled() {
      if (this.ha && this.ha.reachable !== false && typeof this.ha.installed === 'boolean') return this.ha.installed;
      return !!(this.info && this.info.integration_installed === true);
    },
    get integrationUrl() {
      return (this.ha && this.ha.integration_url) || '/config/integrations/integration/smart_sniffer';
    },

    // ── Formatting ──────────────────────────────────────────────────
    num(n) {
      return typeof n === 'number' ? n.toLocaleString('en-US') : String(n);
    },
    tb(bytes) {
      if (typeof bytes !== 'number') return '';
      const v = bytes / 1e12;
      return `${v >= 100 ? v.toFixed(1) : v.toFixed(2)} TB`;
    },
    // Disk space: decimal units, as df -H and the agent's totals.
    bytes(n) {
      if (typeof n !== 'number') return '';
      const units = [[1e12, 'TB'], [1e9, 'GB'], [1e6, 'MB'], [1e3, 'kB']];
      for (const [size, unit] of units) {
        if (n >= size) {
          const v = n / size;
          return `${v >= 100 ? Math.round(v) : v.toFixed(1).replace(/\.0$/, '')} ${unit}`;
        }
      }
      return `${n} B`;
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
    // A card: { id, kind, src, it, h, fs }. kind is drive, pool,
    // filesystem, machine (a field agent with nothing to show: waiting for
    // its password, never read, not answering), down (this app's agent
    // not answering before its first answer) or loading (this app's agent
    // not asked yet: its first answer is still on the way). src is self, field or replicas.
    cardsFrom(src, data, h) {
      const out = [];
      ((data && data.items) || []).forEach((it) => {
        out.push({ id: it.card_id || `${src === 'field' ? h.id : src}|${it.id}`, kind: it.type, src, it, h, fs: null });
      });
      ((data && data.filesystem_cards) || []).forEach((f) => {
        out.push({ id: f.card_id, kind: 'filesystem', src, it: f, h, fs: f.filesystem });
      });
      return out;
    },
    get allCards() {
      const out = [];
      // Until this machine's agent first answers, one card says it is
      // being read, as a field agent's does, so the page is never blank.
      if (!this.selfLoaded) out.push({ id: 'self', kind: 'loading', src: 'self', it: null, h: null, fs: null });
      if (this.selfLoaded) {
        const live = this.self && this.self.status === 'ok' ? this.self : null;
        const data = live || this.selfLastGood;
        if (data && ((data.items || []).length || (data.filesystem_cards || []).length)) {
          out.push(...this.cardsFrom('self', data, null));
        } else if (this.agentDown) {
          out.push({ id: 'self', kind: 'down', src: 'self', it: null, h: null, fs: null });
        }
      }
      this.hosts.forEach((h) => {
        const d = this.hostData[h.id];
        const cards = this.pending(h) ? [] : this.cardsFrom('field', d, h);
        if (cards.length) out.push(...cards);
        else if (!(d && d.status === 'ok')) out.push({ id: h.id, kind: 'machine', src: 'field', it: null, h, fs: null });
      });
      if (this.replicas) out.push(...this.cardsFrom('replicas', this.replicas, null));
      return out.filter((c) => !this.gone[c.id]);
    },
    // The stored order first; cards it does not know keep their place among
    // themselves, after it and before the + card.
    get orderedCards() {
      const cards = this.allCards;
      const order = this.dragOrder || this.order;
      const at = new Map(order.map((id, i) => [id, i]));
      return cards.map((c, i) => ({ c, i, o: at.has(c.id) ? at.get(c.id) : Infinity }))
        .sort((a, b) => (a.o - b.o) || (a.i - b.i)).map((x) => x.c);
    },
    get shownCards() {
      return this.orderedCards.filter((c) => this.showHidden || !this.isHidden(c));
    },
    isHidden(c) {
      return this.hidden.includes(c.id);
    },
    get hiddenCount() {
      const ids = new Set(this.allCards.map((c) => c.id));
      return this.hidden.filter((id) => ids.has(id)).length;
    },
    toggleShowHidden() {
      if (!this.hiddenCount && !this.showHidden) return;
      this.showHidden = !this.showHidden;
    },
    cardClass(c) {
      return {
        machine: ['machine', 'down', 'loading'].includes(c.kind),
        stale: this.unanswered(c),
        'is-hidden': this.isHidden(c),
        'drop-gap': !!(this.drag && this.drag.id === c.id),
        open: this.isOpen(c),
      };
    },
    // The grid's column count, kept up to date, so the open + card can
    // tell how wide to be.
    watchCols() {
      const grid = document.querySelector('.grid');
      if (!grid) return;
      const read = () => {
        const n = getComputedStyle(grid).gridTemplateColumns.split(' ').filter(Boolean).length || 1;
        if (n !== this.ui.cols) this.ui.cols = n;
      };
      read();
      if (window.ResizeObserver) new ResizeObserver(read).observe(grid);
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

    // Where a card comes from.
    machine(c) {
      if (c.src === 'self') return 'HAOS';
      if (c.src === 'replicas') return 'REPLICA';
      return this.hostName(c.h).toUpperCase();
    },
    // Every card's pill names its machine; a pool or filesystem card says
    // what it is on the line under its title (kindWord).
    pill(c) {
      return this.machine(c);
    },
    kindWord(c) {
      if (c.kind === 'pool') return 'ZFS pool';
      if (c.kind === 'filesystem') return 'Filesystem';
      return null;
    },
    // The name the card had before anyone renamed it: the model, the pool's
    // name, the mount point.
    baseTitle(c) {
      if (c.kind === 'down') return "This app's agent is not answering";
      if (c.kind === 'loading') return "This machine's drives";
      if (c.kind === 'machine') return this.hostName(c.h);
      if (c.kind === 'filesystem') return c.fs.mountpoint;
      if (c.kind === 'pool') return c.it.pool.name;
      return (c.it.drive && c.it.drive.model) || (c.it.lab && c.it.lab.name) || c.it.id;
    },
    // Shown: the Home Assistant device's own name when the user gave it one,
    // else the name given here, else the original.
    title(c) {
      if (['down', 'loading', 'machine'].includes(c.kind)) return this.baseTitle(c);
      const ha = this.haNames(c);
      return (ha && ha.name_by_user) || this.names[c.id] || this.baseTitle(c);
    },
    renamed(c) {
      return ['drive', 'pool'].includes(c.kind) && this.title(c) !== this.baseTitle(c);
    },
    modelLine(c) {
      return `${c.kind === 'pool' ? 'Pool' : 'Model'}: ${this.baseTitle(c)}`;
    },

    // ── Rename in place ─────────────────────────────────────────────
    // A card's title (drive, pool, filesystem), and a field agent's machine:
    // its pill, or the title of its card while it has nothing else to show.
    canRename(c) {
      return ['drive', 'pool', 'filesystem', 'machine'].includes(c.kind);
    },
    canRenameHost(c) {
      return c.src === 'field' && ['drive', 'machine', 'pool', 'filesystem'].includes(c.kind);
    },
    renaming(c, what) {
      const r = this.ui.rename;
      return !!(r && r.id === c.id && r.what === what);
    },
    // A click or tap on the title or an editable pill opens the field in
    // the element's own box, so nothing around it moves: the title's field
    // keeps the title's height, the pill's keeps the pill's width.
    startRename(c, what, ev) {
      if (what === 'title' && !this.canRename(c)) return;
      if (what === 'pill' && !this.canRenameHost(c)) return;
      if (this.ui.rename) this.finishRename(true);
      const el = ev && ev.currentTarget;
      const box = el ? (what === 'title' ? el.closest('.c-title') : el).getBoundingClientRect() : null;
      const host = what === 'pill' || c.kind === 'machine';
      const value = host ? this.hostName(c.h) : this.title(c);
      const kb = !!(ev && ev.type === 'keydown');
      this.ui.rename = { id: c.id, what, host, value, was: value, kb, t0: Date.now(),
                         h: box ? box.height : 0, w: box ? box.width : 0 };
      this.$nextTick(() => {
        const f = document.querySelector(`article.card[data-card="${CSS.escape(c.id)}"] .name-in`);
        if (f) { f.focus(); f.select(); }
      });
    },
    // Enter or F2 on the focused title or pill starts editing.
    nameKey(e, c, what) {
      if (e.key !== 'Enter' && e.key !== 'F2') return;
      e.preventDefault();
      this.startRename(c, what, e);
    },
    // The second click of a double-click that opened the field keeps the
    // whole name selected instead of picking one word.
    renameDown(e) {
      const r = this.ui.rename;
      if (r && e.detail > 1 && Date.now() - r.t0 < 700) { e.preventDefault(); e.target.select(); }
    },
    renameKey(e) {
      if (e.key === 'Enter') { e.preventDefault(); this.finishRename(true, true); }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.finishRename(false, true); }
    },
    finishRename(save, byKey) {
      const r = this.ui.rename;
      if (!r) return;
      this.ui.rename = null;
      // Ended from the keyboard: focus goes back to what was renamed.
      if (byKey && r.kb) {
        this.$nextTick(() => {
          const card = document.querySelector(`article.card[data-card="${CSS.escape(r.id)}"]`);
          const back = card && card.querySelector(r.what === 'pill' ? '.pill-ed' : '.t-name');
          if (back) back.focus({ preventScroll: true });
        });
      }
      const name = String(r.value || '').replace(/\s+/g, ' ').trim();
      if (!save || name === r.was) return;
      const c = this.allCards.find((x) => x.id === r.id);
      if (!c) return;
      if (r.host) this.saveHostName(c.h, name);
      else this.saveCardName(c, name);
    },
    async saveHostName(h, name) {
      const was = this.hostNames[h.id];
      if (name) this.hostNames[h.id] = name; else delete this.hostNames[h.id];
      this.pageErr = '';
      const r = await this.request(`hosts/${enc(h.id)}/name`, { method: 'PUT', body: { name } });
      if (!r.ok) {
        if (was) this.hostNames[h.id] = was; else delete this.hostNames[h.id];
        this.pageErr = 'That name was not saved. Try again.';
      }
    },
    async saveCardName(c, name) {
      const was = this.names[c.id];
      const ha = this.haNames(c);
      const haWas = ha ? ha.name_by_user : null;
      if (name) this.names[c.id] = name; else delete this.names[c.id];
      if (ha) ha.name_by_user = name || null;
      this.ui.note[`nm:${c.id}`] = '';
      this.pageErr = '';
      const r = await this.request(`cards/${enc(c.id)}/name`, { method: 'PUT', body: { name } });
      if (!r.ok) {
        if (was) this.names[c.id] = was; else delete this.names[c.id];
        if (ha) ha.name_by_user = haWas;
        this.pageErr = 'That name was not saved. Try again.';
        return;
      }
      if (ha && r.data && r.data.home_assistant === 'failed') {
        ha.name_by_user = haWas;
        this.ui.note[`nm:${c.id}`] = HA_NAME_FAILED;
      }
    },
    // The names Home Assistant has for a card's device, when it has one.
    haNames(c) {
      if (!['drive', 'pool'].includes(c.kind) || this.glyph(c).kind !== 'blue') return null;
      const t = this.ha && this.ha.targets && this.ha.targets[this.target(c)];
      const key = this.haKey(c);
      return (t && t.names && key && t.devices && t.devices[key] && t.names[key]) || null;
    },
    haKey(c) {
      const it = c.it;
      return c.kind === 'pool' ? `pool:${it.pool.name}` : it.drive && (it.drive.serial || `id:${it.id}`);
    },
    footNote(c) {
      return this.ui.note[`dl:${c.id}`] || this.ui.note[`rm:${c.id}`] || this.ui.note[`nm:${c.id}`] || '';
    },
    // The where line, as parts shown with a gap between them.
    where(c) {
      const it = c.it || {};
      const addr = c.h ? `${c.h.address}:${c.h.port}` : '';
      if (c.kind === 'down' || c.kind === 'loading') return [this.agentAddress];
      if (c.kind === 'machine') return [addr];
      // A filesystem or pool card starts with what it is; its pill names
      // the machine, as a drive card's does.
      if (c.kind === 'filesystem') {
        return [this.kindWord(c), c.src === 'field' ? addr : null, c.fs.device, c.fs.fstype].filter(Boolean);
      }
      if (c.kind === 'pool') {
        const layout = this.poolLayout(it);
        // A replica's scenario is the picker drawn ahead of these parts.
        if (c.src === 'replicas') return [this.kindWord(c), this.scenOn(c) ? null : this.scenarioName(it), layout].filter(Boolean);
        if (c.src === 'self') return [this.kindWord(c), layout].filter(Boolean);
        return [this.kindWord(c), addr, layout].filter(Boolean);
      }
      if (c.src === 'replicas') return this.scenOn(c) ? [] : [this.scenarioName(it) || (it.lab && it.lab.name) || ''];
      const drive = it.drive || {};
      if (c.src === 'field') return [addr, drive.device_path].filter(Boolean);
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
      if (c.kind !== 'drive') return [];
      const s = (it.view && it.view.stats) || {};
      const out = [];
      if (typeof s.temp_c === 'number') out.push(`${s.temp_c} C`);
      if (typeof s.power_on_hours === 'number') out.push(`${this.num(s.power_on_hours)} h`);
      const w = this.vol(it, 'written');
      if (w) out.push(`${this.tb(w.bytes)} written`);
      return out;
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
      if (!this.ui.open[c.id] && this.thrEditing(c)) this.ui.thr = null;
      // Read its thresholds now, so edit mode opens with them at hand.
      if (this.ui.open[c.id] && this.canTune(c)) this.thrRead(c);
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
    volNote(vol) {
      const src = String(vol.source || '');
      if (src.includes('attribute')) return `from attribute ${vol.attribute_id || 241}`;
      if (src.includes('nvme')) return 'from the NVMe log';
      return 'from Device Statistics';
    },
    // The Device Statistics row: a value ("present", "absent", "off",
    // "unavailable", "not applicable") and the note for after the label.
    devstat(it) {
      const ds = (it.drive && it.drive.device_statistics) || {};
      const view = it.view || {};
      const v = String(view.devstat || ds.status || 'absent');
      const reason = view.devstat_reason || ds.reason || '';
      if (v === 'not_applicable') {
        const nvme = view.protocol === 'nvme' || (it.drive && it.drive.protocol === 'NVMe');
        return { value: 'not applicable', note: nvme ? 'NVMe' : '' };
      }
      if (v === 'off' || v === 'unavailable') return { value: v, note: reason };
      return { value: v, note: '' };
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
    // watched row's mark and note on the attribute it reads, a watched
    // attribute the drive does not report as a row of its own, and a Device
    // Statistics reading after the row it stands in for. An NVMe drive: its
    // health log, the same way. Anything else: the watched rows.
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
          mark: r.mark === 'crit' || r.mark === 'warn' ? r.mark : null, sortId,
          edit: r.key === 'reported_unc_devstat' ? 'devstat' : null, rawValue: this.devstatValue(it, r) });
      };
      if (kind === 'ata') {
        const pad = (n) => (typeof n === 'number' ? String(n).padStart(3, '0') : '');
        ((sd.ata_smart_attributes || {}).table || []).forEach((a) => entries.push({
          key: `a:${a.id}`, id: a.id, name: a.name, value: pad(a.value), worst: pad(a.worst), thresh: pad(a.thresh),
          raw: (a.raw && (a.raw.string ?? String(a.raw.value))) || '', rawValue: a.raw ? a.raw.value : '',
          mark: null, note: '', edit: 'ata', field: a.name,
        }));
        this.rows(it).forEach((r) => {
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
        return { kind, rows: entries };
      }
      if (kind === 'nvme') {
        const log = sd.nvme_smart_health_information_log || {};
        Object.entries(log).forEach(([k, v]) => entries.push({
          key: `n:${k}`, name: k, value: Array.isArray(v) ? v.join(' ') : String(v), mark: null, note: '',
          edit: typeof v === 'number' ? 'nvme' : null, field: k, rawValue: v,
        }));
        this.rows(it).forEach((r) => {
          const hit = r.value === 'not known' ? null : entries.find((e) => e.field === NVME_FIELDS[r.key]);
          if (hit) place(r, hit); else extra(r, 1000);
        });
        extras.forEach((x) => { x.edit = null; entries.push(x); });
        return { kind, rows: entries };
      }
      // An unsupported drive: its watched rows are not known, not missing.
      this.rows(it).forEach((r) => extra(r, 1000));
      extras.forEach((x) => { x.edit = null; x.gap = 'not known'; });
      return { kind, rows: extras };
    },
    // The number behind a Device Statistics row, for a replica's input.
    devstatValue(it, r) {
      const at = DEVSTAT_ROWS[r.key];
      if (!at) return '';
      const page = (((it.drive && it.drive.device_statistics) || {}).pages || []).find((p) => p.number === at[0]);
      const e = page && (page.table || []).find((x) => x.offset === at[1]);
      return e ? e.value : String(r.value).replace(/,/g, '');
    },

    // ── Thresholds ──────────────────────────────────────────────────
    // A drive's thresholds, edited in its open Details. The proxy keeps
    // them where they live: in Home Assistant for a drive in it, else in
    // the app's options.
    canTune(c) {
      return c.kind === 'drive' && !!(c.it.verdict && (c.it.verdict.limits || []).length) && !c.it.stale;
    },
    // The edit state, never null, for the template's bindings.
    get thrS() {
      return this.ui.thr || { values: {}, data: null, busy: false, err: '', loading: false };
    },
    thrSet(label, value) {
      if (this.ui.thr) this.ui.thr.values[label] = value;
    },
    thrEditing(c) {
      return !!(this.ui.thr && this.ui.thr.id === c.id);
    },
    // Edit mode shows in place of the card's table once the thresholds are
    // at hand; while they are still being read the table stays and only the
    // tune button shows it is waiting.
    thrShown(c) {
      return this.thrEditing(c) && !this.ui.thr.loading;
    },
    thrWaiting(c) {
      return this.thrEditing(c) && this.ui.thr.loading;
    },
    // Read a drive's thresholds into its own cache entry; one read at a time
    // per card. Nothing else on the page changes.
    thrRead(c) {
      if (thrReads.has(c.id)) return thrReads.get(c.id);
      const id = c.id;
      const seq = thrSeq.get(id) || 0;
      const read = this.request(`cards/${enc(id)}/thresholds`).then((r) => {
        if (thrReads.get(id) === read) thrReads.delete(id);
        const entry = this.thrCache[id] || (this.thrCache[id] = { data: null, err: '' });
        if ((thrSeq.get(id) || 0) !== seq) return entry;
        if (r.ok && r.data) { entry.data = r.data; entry.err = ''; } else entry.err = 'The thresholds could not be read. Try again.';
        const t = this.ui.thr;
        if (t && t.id === id && t.loading) {
          t.loading = false;
          if (entry.err) t.err = entry.err;
          else this.thrLoad(t, entry.data);
        }
        return entry;
      });
      thrReads.set(id, read);
      return read;
    },
    toggleThr(c) {
      if (this.thrEditing(c)) { this.ui.thr = null; return; }
      const entry = this.thrCache[c.id];
      const ready = !!(entry && entry.data);
      this.ui.thr = { id: c.id, loading: !ready, data: null, values: {}, busy: false, err: '' };
      if (ready) this.thrLoad(this.ui.thr, entry.data);
      else this.thrRead(c);
    },
    thrLoad(t, data) {
      t.data = data;
      t.values = {};
      (data.labels || []).forEach((l) => { t.values[l.label] = String(l.value); });
    },
    // The rows edit mode shows: the labels the drive reports (and any with
    // an override), in the order of the drive's watched rows.
    thrRows(c) {
      const t = this.ui.thr;
      if (!t || !t.data) return [];
      const keys = this.rows(c.it).map((r) => r.key);
      const at = (l) => { const i = keys.indexOf(l.row); return i < 0 ? 1000 : i; };
      return (t.data.labels || []).filter((l) => l.reading !== null && l.reading !== undefined || l.overridden)
        .map((l, i) => ({ l, i })).sort((a, b) => (at(a.l) - at(b.l)) || (a.i - b.i)).map((x) => x.l);
    },
    thrMark(c, l) {
      if (!l.flagged) return '';
      const row = this.rows(c.it).find((r) => r.key === l.row);
      return row && row.mark === 'warn' ? 'warn' : (row && row.mark === 'crit' ? 'crit' : 'warn');
    },
    thrReading(l) {
      if (l.reading === null || l.reading === undefined) return 'not reported';
      return `${this.num(l.reading)}${l.unit || ''}`;
    },
    thrWord(l) {
      return COMPARE_WORDS[l.compare] || 'alert above';
    },
    thrWhere() {
      const t = this.ui.thr;
      if (!t || !t.data) return '';
      return t.data.source === 'integration' ? 'Saved in Home Assistant' : 'Saved in this app';
    },
    thrFlagged(c) {
      return this.thrRows(c).filter((l) => l.flagged);
    },
    // A field whose text is not the value saved: changed here, not yet sent.
    thrPending(l) {
      const t = this.ui.thr;
      return !!(t && t.data && String(t.values[l.label] ?? '').trim() !== String(l.value));
    },
    thrAnyPending(c) {
      return this.thrRows(c).some((l) => this.thrPending(l));
    },
    async thrSend(c, method, path, body) {
      const t = this.ui.thr;
      if (!t || t.busy) return;
      t.busy = true;
      t.err = '';
      const r = await this.request(`cards/${enc(c.id)}/thresholds${path}`, body === undefined ? { method } : { method, body });
      t.busy = false;
      if (!r.ok || !r.data) {
        const msg = r.data && r.data.message;
        t.err = r.status === 502 ? `Home Assistant did not save them${msg ? `: ${msg}` : ''}.`
          : r.status === 400 && msg ? `Not saved: ${msg}.` : 'They were not saved. Try again.';
        return;
      }
      this.ui.thr = null;
      thrSeq.set(c.id, (thrSeq.get(c.id) || 0) + 1);
      thrReads.delete(c.id);
      const entry = this.thrCache[c.id] || (this.thrCache[c.id] = { data: null, err: '' });
      entry.data = r.data;
      entry.err = '';
      if (c.src === 'self') await this.loadSelf();
      else if (c.src === 'replicas') await this.loadReplicas();
      else await this.loadHost(c.h.id);
      // The drive judged again: its readings in the thresholds too.
      if (this.isOpen(c)) this.thrRead(c);
    },
    thrSave(c) {
      const t = this.ui.thr;
      if (!t || !t.data) return;
      const values = {};
      for (const l of this.thrRows(c)) {
        const text = String(t.values[l.label] ?? '').trim();
        if (!/^\d+$/.test(text)) { t.err = `${l.name}: a whole number from 0 up.`; return; }
        values[l.label] = Number(text);
      }
      this.thrSend(c, 'PUT', '', { values });
    },
    // Accept and Reset only fill the fields; Save is what writes. Accept
    // gives each flagged reading's field the value the proxy's accept
    // would store: the reading, or one more for an "alert at" threshold.
    thrAccept(c) {
      const t = this.ui.thr;
      if (!t || !t.data || t.busy) return;
      t.err = '';
      this.thrFlagged(c).forEach((l) => {
        const reading = Number(l.reading);
        t.values[l.label] = String(l.compare === 'ge' ? reading + 1 : reading);
      });
    },
    thrReset() {
      const t = this.ui.thr;
      if (!t || !t.data || t.busy) return;
      t.err = '';
      (t.data.labels || []).forEach((l) => { t.values[l.label] = String(l.default); });
    },

    // ── Connect by hand ─────────────────────────────────────────────
    // What to type into the integration to add a machine by hand: this
    // app's agent as Home Assistant reaches it, or a field agent's address.
    connect(c) {
      if (!['drive', 'pool'].includes(c.kind) || c.src === 'replicas') return null;
      const i = this.info || {};
      if (c.src === 'self') {
        const fields = [{ k: 'Host', v: i.hostname || '' }, { k: 'Port', v: String(i.agent_port || 9099) }];
        if (i.mdns_name) fields.push({ k: 'mDNS', v: i.mdns_name });
        return { fields: fields.filter((f) => f.v), password: false };
      }
      if (!c.h) return null;
      return { fields: [{ k: 'Host', v: c.h.address }, { k: 'Port', v: String(c.h.port) }], password: !!c.h.has_password };
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

    // ── Footer and corner icons ─────────────────────────────────────
    unanswered(c) {
      if (c.src === 'self') return this.agentDown;
      if (c.src === 'field') {
        const d = this.hostData[c.h.id];
        return !!(d && d.status === 'unreachable');
      }
      return this.repFailed;
    },
    lastSeen(c) {
      let at = null;
      if (c.src === 'self') {
        at = this.agentDown ? this.selfLastAnswer
          : (c.it && c.it.drive && c.it.drive.last_updated) || (this.self && this.self.read_at);
      } else if (c.src === 'field') {
        const d = this.hostData[c.h.id];
        if (!d) {
          const added = Date.parse(c.h.added_at);
          return !added || this.now - added < 60000 ? 'Added just now' : `Added ${this.ago(added)}`;
        }
        at = d.status === 'ok' ? d.read_at : d.last_answer;
      } else {
        at = this.replicas && (this.replicas.read_at || this.replicas.last_answer);
      }
      const when = at ? this.ago(at) : 'not yet';
      return `Last seen: ${when}${this.unanswered(c) ? ', not answering' : ''}`;
    },
    canDownload(c) {
      return c.kind === 'drive' || c.kind === 'pool';
    },
    canHide(c) {
      return c.src !== 'replicas' && c.kind !== 'down' && c.kind !== 'loading';
    },
    canRemove(c) {
      return c.src !== 'self';
    },
    removeText(c) {
      if (c.src === 'replicas') return 'Remove this replica?';
      if (c.kind === 'pool') return 'Remove this pool?';
      if (c.kind === 'filesystem') return 'Remove this filesystem?';
      if (c.kind === 'machine') return `Remove ${this.hostName(c.h)}?`;
      return 'Remove this drive?';
    },

    async setHidden(c, hidden) {
      const was = this.hidden.slice();
      this.hidden = hidden ? was.concat([c.id]) : was.filter((id) => id !== c.id);
      if (!this.hiddenCount) this.showHidden = false;
      this.pageErr = '';
      const r = await this.request(`cards/${enc(c.id)}`, { method: 'PATCH', body: { hidden } });
      if (!r.ok) {
        this.hidden = was;
        this.pageErr = 'That change was not saved. Try again.';
      }
    },

    // The trash: the first click turns it into Remove? in the same place,
    // the second removes the card. A card in Home Assistant (blue exit
    // icon) arms two buttons instead, growing left: Remove here, and the
    // same with what Home Assistant can do (ha_removal from /ha/devices:
    // delete its device or the machine's entry, or disable its device).
    // A replica in Home Assistant keeps the single Remove?, and its second
    // click takes the replica's device and entities out of Home Assistant
    // too (ha_removal "replica").
    // Leaving them, Esc, focus going elsewhere or ARM_MS put the trash back.
    armed(c) {
      return !!(this.ui.arm && this.ui.arm.id === c.id);
    },
    haRemoval(c) {
      if (this.glyph(c).kind !== 'blue') return null;
      const mode = this.ha && this.ha.ha_removal && this.ha.ha_removal[c.id];
      return ['delete', 'disable', 'entry', 'replica'].includes(mode) ? mode : null;
    },
    armedHa(c) {
      return this.armed(c) && !!this.ui.arm.ha && this.ui.arm.ha !== 'replica';
    },
    armedLabel(c) {
      const also = this.ui.arm && this.ui.arm.ha === 'replica' ? ' It leaves Home Assistant too.' : '';
      return `${this.removeText(c)} Press again to remove.${also}`;
    },
    haRemoveText() {
      const a = this.ui.arm;
      return a && a.ha === 'disable' ? 'Remove here + disable entities' : 'Remove here + delete entities';
    },
    trashClick(c) {
      if (this.armed(c)) { this.confirmRemove(c, this.ui.arm.ha === 'replica' ? 'replica' : null); return; }
      this.disarm();
      this.ui.confirm = null;
      const arm = { id: c.id, busy: false, timer: null, ha: this.haRemoval(c) };
      arm.timer = setTimeout(() => { if (this.ui.arm && this.ui.arm.id === c.id && !this.ui.arm.busy) this.disarm(); }, ARM_MS);
      this.ui.arm = arm;
      this.ui.note[`rm:${c.id}`] = '';
    },
    disarm(c) {
      const a = this.ui.arm;
      if (!a || a.busy || (c && a.id !== c.id)) return;
      clearTimeout(a.timer);
      this.ui.arm = null;
    },
    // Focus moving between the two armed buttons keeps them armed.
    rmFocusOut(c, e) {
      if (e.currentTarget.contains(e.relatedTarget)) return;
      this.disarm(c);
    },
    showToast(text) {
      clearTimeout(this.toastTimer);
      this.ui.toast = text;
      this.toastTimer = setTimeout(() => { this.ui.toast = ''; }, TOAST_MS);
    },
    haRemovedText(c, data) {
      if (data && data.home_assistant === 'disabled') {
        return `Disabled in Home Assistant. It cannot be deleted while the agent still sees the ${c.kind === 'pool' ? 'pool' : 'drive'}.`;
      }
      if (data && data.home_assistant === 'deleted') return 'Deleted from Home Assistant.';
      if (c.src === 'replicas') return 'Removed here. Home Assistant kept its entities; remove them there.';
      const reason = String((data && data.ha_error) || 'no answer').replace(/[.\s]+$/, '');
      return `Removed here. Home Assistant did not take the change: ${reason}.`;
    },
    async confirmRemove(c, ha) {
      const a = this.ui.arm;
      if (!a || a.busy) return;
      clearTimeout(a.timer);
      a.busy = true;
      const path = `cards/${enc(c.id)}` + (ha ? `?ha=${enc(ha)}` : '');
      const r = await this.request(path, { method: 'DELETE' });
      a.busy = false;
      this.ui.arm = null;
      if (!r.ok) {
        const key = `rm:${c.id}`;
        this.ui.note[key] = 'It was not removed. Try again.';
        setTimeout(() => { this.ui.note[key] = ''; }, 5000);
        return;
      }
      this.gone[c.id] = true;
      this.order = this.order.filter((id) => id !== c.id);
      this.hidden = this.hidden.filter((id) => id !== c.id);
      // A replica says nothing when Home Assistant let it go too.
      if (ha && !(ha === 'replica' && r.data && r.data.home_assistant === 'deleted')) {
        this.showToast(this.haRemovedText(c, r.data));
      }
      if (c.src === 'replicas') {
        await this.loadReplicas();
      } else if (r.data && r.data.host_removed) {
        this.hosts = this.hosts.filter((h) => h.id !== c.h.id);
        delete this.hostData[c.h.id];
      } else {
        await this.loadHost(c.h.id);
      }
      delete this.gone[c.id];
      // The machine's last card, and what Home Assistant has, changed.
      if (this.ha && this.ha.installed === true) this.loadHa();
    },

    async download(c) {
      const key = `dl:${c.id}`;
      this.ui.note[key] = '';
      try {
        const resp = await fetch(`${this.baseUrl}cards/${enc(c.id)}/file`);
        if (!resp.ok) {
          let code = '';
          try { code = (await resp.json()).error; } catch (e) { code = ''; }
          this.ui.note[key] = {
            replica_folder_unavailable: 'Your replica folder is not available.',
            unsupported_drive: 'This drive has no SMART data to copy.',
            not_answering: 'The agent did not answer.',
          }[code] || 'It was not saved. Try again.';
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

    // ── Exit icon and its confirm ───────────────────────────────────
    target(c) {
      if (c.src === 'self') return 'self';
      if (c.src === 'replicas') return 'replicas';
      return c.h.id;
    },
    glyph(c) {
      if (!['drive', 'pool'].includes(c.kind)) return NONE;
      const ha = this.ha;
      if (!ha || ha.installed !== true || ha.reachable === false) return NONE;
      const t = ha.targets && ha.targets[this.target(c)];
      if (!t) return { kind: 'grey', href: null };
      const key = this.haKey(c);
      const dev = key && t.devices && t.devices[key];
      if (dev) return { kind: 'blue', href: `/config/devices/device/${dev}` };
      if (ha.lookup === 'entries_only') return { kind: 'blue', href: t.entry_url };
      return { kind: 'grey', href: null };
    },
    askText(c) {
      if (c.src === 'replicas') {
        let text = 'Add this replica to Home Assistant?';
        const others = ((this.replicas && this.replicas.items) || []).length - 1;
        const entry = this.ha && this.ha.targets && this.ha.targets.replicas;
        if (!entry && others > 0) text += ` The other ${this.plural(others, 'replica comes', 'replicas come')} with it.`;
        return text;
      }
      if (c.src === 'field') return `Add ${this.machine(c)} to Home Assistant?`;
      return 'Add this drive to Home Assistant?';
    },
    askAdd(c) {
      this.disarm();
      if (this.ui.confirm && this.ui.confirm.id === c.id) { this.closeConfirm(); return; }
      this.ui.confirm = { id: c.id, target: this.target(c), text: this.askText(c), busy: false, err: '' };
    },
    asking(c) {
      return !!(this.ui.confirm && this.ui.confirm.id === c.id);
    },
    closeConfirm() {
      this.ui.confirm = null;
    },
    async confirmAdd() {
      const c = this.ui.confirm;
      if (!c || c.busy) return;
      c.busy = true;
      c.err = '';
      const r = await this.request('ha/add', { method: 'POST', body: { target: c.target } });
      c.busy = false;
      const result = r.data && r.data.result;
      if (r.ok && ['created', 'already_added', 'reloaded'].includes(result)) {
        this.ui.confirm = null;
        this.loadHa();
        this.haSoon();
        return;
      }
      const why = r.data && (r.data.message || r.data.error);
      c.err = {
        cannot_connect: 'Home Assistant could not reach this agent.',
        integration_not_loaded: 'The integration is not installed.',
        invalid_auth: 'Home Assistant refused the password.',
        unreachable: 'Home Assistant is not answering.',
      }[why] || 'Home Assistant did not add it. The App log may say why.';
    },

    // ── Field agents ────────────────────────────────────────────────
    data(h) {
      return this.hostData[h.id] || null;
    },
    status(h) {
      const d = this.data(h);
      if (d) return d.status;
      const o = this.optimistic[h.id];
      return o && o.auth === 'password' && !h.has_password ? 'password_needed' : 'reading';
    },
    pending(h) {
      const s = this.status(h);
      return s === 'password_needed' || s === 'password_refused';
    },
    hostName(h) {
      const d = this.data(h);
      const o = this.optimistic[h.id];
      return this.hostNames[h.id] || (d && d.agent && d.agent.hostname) || (o && o.hostname) || h.address;
    },
    machineText(c) {
      const h = c.h;
      const s = this.status(h);
      if (this.pending(h)) return '';
      if (s === 'unreachable') return 'Not answering, and not seen yet.';
      if (s === 'reading') return 'Reading its drives.';
      return 'It reports no drives.';
    },
    pwRefused(h) {
      return this.ui.pwState[h.id] === 'refused' || (this.status(h) === 'password_refused' && !this.ui.pwState[h.id]);
    },
    pwMessage(h) {
      const s = this.ui.pwState[h.id];
      if (s === 'noanswer') return 'That agent is not answering. Try again in a moment.';
      if (s === 'error') return 'The password was not saved. Try again.';
      return '';
    },
    async authenticate(h) {
      const pw = this.ui.pw[h.id] || '';
      if (!pw) return;
      this.ui.busy[`pw:${h.id}`] = true;
      const r = await this.request(`fleet/hosts/${enc(h.id)}`, { method: 'PATCH', body: { password: pw } });
      this.ui.busy[`pw:${h.id}`] = false;
      if (r.ok) {
        this.ui.pw[h.id] = '';
        this.ui.pwState[h.id] = null;
        if (r.data && r.data.host) Object.assign(h, r.data.host);
        await this.loadHost(h.id);
        return;
      }
      const code = r.data && r.data.error;
      this.ui.pwState[h.id] = code === 'password_refused' ? 'refused' : code === 'not_answering' ? 'noanswer' : 'error';
    },

    // ── Replica scenarios ──────────────────────────────────────────
    scenOptions(it) {
      const opts = (it.lab && it.lab.scenario && it.lab.scenario.options) || [];
      // A pool offers Healthy, Degraded and Faulted.
      return it.pool ? opts.filter((o) => o.id !== 'vanished') : opts;
    },
    scenCurrent(it) {
      return (it.lab && it.lab.scenario && it.lab.scenario.current) || '';
    },
    scenarioName(it) {
      const cur = this.scenCurrent(it);
      const o = ((it.lab && it.lab.scenario && it.lab.scenario.options) || []).find((x) => x.id === cur);
      return o ? o.label : '';
    },
    // What the picker shows: the scenario's name, or "Choose scenario" for a
    // replica that has none yet (one carried over from the 0.2.x mock drives).
    scenLabel(it) {
      return this.scenarioName(it) || 'Choose scenario';
    },
    // The picker on a replica's where line, in place of the scenario's name.
    scenOn(c) {
      return c.src === 'replicas' && c.kind !== 'filesystem' && !!c.it && this.scenOptions(c.it).length > 0;
    },
    pickScenario(c, id) {
      const o = this.scenOptions(c.it).find((x) => x.id === id);
      if (o) this.applyPreset(c, o);
    },
    mockPath(it) {
      return it.pool ? `mock/pools/${enc(it.pool.name)}` : `mock/drives/${enc(it.drive.id)}`;
    },
    async applyPreset(c, o) {
      const it = c.it;
      if (this.ui.busy[`scen:${c.id}`] || o.id === this.scenCurrent(it)) return;
      this.ui.busy[`scen:${c.id}`] = true;
      await this.request(`${this.mockPath(it)}/scenario`, { method: 'POST', body: { id: o.id } });
      await this.loadReplicas();
      this.ui.busy[`scen:${c.id}`] = false;
    },
    async editRaw(it, kind, field, text) {
      const n = Number(String(text).trim());
      if (!Number.isFinite(n)) return;
      if (kind === 'ata' || kind === 'nvme') await this.request(`${this.mockPath(it)}/smart`, { method: 'PATCH', body: { [field]: n } });
      else if (kind === 'devstat') await this.request(`${this.mockPath(it)}/devstat`, { method: 'PATCH', body: { reported_uncorrectable: n } });
      this.loadReplicas();
    },

    // ── Drag to reorder ─────────────────────────────────────────────
    // Pointer events, not HTML5 drag and drop, so it works on touch in the
    // Home Assistant app. A mouse drags from the grip at once; a finger
    // must hold the grip first, so a scroll never moves a card.
    gripDown(e, c) {
      if (e.button !== undefined && e.button !== 0) return;
      const el = e.currentTarget.closest('article');
      if (e.pointerType !== 'touch') {
        e.preventDefault();
        this.startDrag(c, el, e.clientX, e.clientY);
        return;
      }
      const x0 = e.clientX;
      const y0 = e.clientY;
      let timer = null;
      const stop = () => {
        clearTimeout(timer);
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', stop);
        window.removeEventListener('pointercancel', stop);
      };
      const move = (ev) => { if (Math.hypot(ev.clientX - x0, ev.clientY - y0) > 8) stop(); };
      timer = setTimeout(() => { stop(); this.startDrag(c, el, x0, y0); }, LONG_PRESS_MS);
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', stop);
      window.addEventListener('pointercancel', stop);
    },
    startDrag(c, el, x, y) {
      if (this.drag || !el) return;
      const r = el.getBoundingClientRect();
      const lifted = el.cloneNode(true);
      lifted.classList.add('lifted');
      lifted.removeAttribute('data-card');
      Object.assign(lifted.style, { width: `${r.width}px`, height: `${r.height}px`, left: `${r.left}px`, top: `${r.top}px` });
      document.getElementById('drag-layer').appendChild(lifted);
      const start = this.orderedCards.map((x2) => x2.id);
      this.dragOrder = start.slice();
      const onMove = (ev) => this.dragMove(ev);
      const onUp = () => this.dragEnd();
      this.drag = { id: c.id, offX: x - r.left, offY: y - r.top, lifted, start, onMove, onUp };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      window.addEventListener('pointercancel', onUp);
    },
    dragMove(ev) {
      const d = this.drag;
      if (!d) return;
      ev.preventDefault();
      d.lifted.style.left = `${ev.clientX - d.offX}px`;
      d.lifted.style.top = `${ev.clientY - d.offY}px`;
      const edge = 60;
      if (ev.clientY < edge) window.scrollBy(0, -12);
      else if (ev.clientY > window.innerHeight - edge) window.scrollBy(0, 12);
      for (const el of document.querySelectorAll('.grid > article[data-card]')) {
        const id = el.dataset.card;
        if (id === d.id) continue;
        const r = el.getBoundingClientRect();
        if (ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom) continue;
        const before = ev.clientX < r.left + r.width / 2;
        const order = this.dragOrder.filter((x) => x !== d.id);
        const at = order.indexOf(id) + (before ? 0 : 1);
        order.splice(at, 0, d.id);
        if (order.join('\n') !== this.dragOrder.join('\n')) this.dragOrder = order;
        break;
      }
    },
    dragEnd() {
      const d = this.drag;
      if (!d) return;
      window.removeEventListener('pointermove', d.onMove);
      window.removeEventListener('pointerup', d.onUp);
      window.removeEventListener('pointercancel', d.onUp);
      d.lifted.remove();
      const order = this.dragOrder;
      this.drag = null;
      this.dragOrder = null;
      if (order.join('\n') !== d.start.join('\n')) this.saveOrder(order);
    },
    // The grip by keyboard: arrows move the card one place.
    gripKey(e, c) {
      const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
      if (!step) return;
      e.preventDefault();
      const shown = this.shownCards.map((x) => x.id);
      const i = shown.indexOf(c.id);
      const j = i + step;
      if (i < 0 || j < 0 || j >= shown.length) return;
      const order = this.orderedCards.map((x) => x.id).filter((x) => x !== c.id);
      const at = order.indexOf(shown[j]) + (step > 0 ? 1 : 0);
      order.splice(at, 0, c.id);
      this.saveOrder(order);
      this.$nextTick(() => {
        const el = document.querySelector(`.grid > article[data-card="${CSS.escape(c.id)}"] .grip`);
        if (el) el.focus();
      });
    },
    async saveOrder(order) {
      const was = this.order;
      this.order = order;
      this.pageErr = '';
      const r = await this.request('cards/order', { method: 'PUT', body: { order } });
      if (!r.ok) {
        this.order = was;
        this.pageErr = 'The new order was not saved. Try again.';
      }
    },

    // ── The + card ──────────────────────────────────────────────────
    defaultBlock() {
      const b = this.info && this.info.network && this.info.network.block;
      if (b) return b;
      const p = parseBlock(window.location.hostname);
      return p ? block24(p.octets) : '';
    },
    async openAdd() {
      this.add.open = true;
      if (!this.add.block) this.add.block = this.defaultBlock();
      this.loadReplicaFiles();
      if (this.add.block) this.scan();
    },
    closeAdd() {
      this.stopScan();
      Object.assign(this.add, {
        open: false, scan: null, last: null, wide: null, error: '', ticks: {}, manualRows: [], checkMsg: null,
        manualHost: '', repTicks: {}, upMsg: '', err: '',
      });
    },
    get scanLabel() {
      const l = this.add.last;
      return l && l.block === this.add.block.trim() && l.port === String(this.add.port).trim() ? 'Rescan' : 'Scan';
    },
    fieldsChanged() {
      if (this.scanLabel === 'Rescan') return;
      this.stopScan();
      this.add.scan = null;
      this.add.wide = null;
      this.add.error = '';
    },
    suggestBlock(parsed) {
      const own = this.info && this.info.network && this.info.network.address;
      const p = own ? parseBlock(own) : null;
      return block24(p ? p.octets : parsed.octets);
    },
    stopScan() {
      const id = this.add.scanId;
      if (id && this.add.scan && this.add.scan.state === 'running') {
        this.request(`fleet/scan/${enc(id)}`, { method: 'DELETE' });
      }
      this.add.scanId = null;
    },
    async scan() {
      const block = this.add.block.trim();
      const portText = String(this.add.port).trim();
      const port = Number(portText);
      this.stopScan();
      this.add.error = '';
      this.add.wide = null;
      this.add.scan = null;
      const parsed = parseBlock(block);
      if (!parsed) { this.add.error = 'That is not a network block. Write it as 10.0.10.0/24.'; return; }
      if (!Number.isInteger(port) || port < 1 || port > 65535) { this.add.error = 'The port is a number from 1 to 65535.'; return; }
      if (parsed.prefix < 22) {
        this.add.wide = { addresses: 2 ** (32 - parsed.prefix) - 2, suggest: this.suggestBlock(parsed) };
        return;
      }
      this.add.last = { block, port: portText };
      this.add.scan = { state: 'running', probed: 0, total: 0, found: [], block, port };
      const r = await this.request('fleet/scan', { method: 'POST', body: { block, port } });
      if (r.status === 422 && r.data && r.data.error === 'too_wide') {
        this.add.scan = null;
        this.add.last = null;
        this.add.wide = { addresses: r.data.addresses, suggest: r.data.suggest };
        return;
      }
      if (!r.ok || !r.data) {
        this.add.scan = null;
        this.add.error = r.data && r.data.error === 'bad_block' ? 'That is not a network block. Write it as 10.0.10.0/24.'
          : 'The scan did not start. Try again.';
        return;
      }
      this.add.scanId = r.data.scan_id;
      this.add.scan.total = r.data.total;
      this.pollScan(r.data.scan_id);
    },
    // One poll every 400 ms while the page is visible. A hidden page waits;
    // when it shows again resumeScan() polls at once so the bar catches up.
    pollScan(id, delay = 400) {
      clearTimeout(this.scanTimer);
      this.scanTimer = setTimeout(async () => {
        this.scanTimer = null;
        if (this.add.scanId !== id) return;
        if (document.hidden) { this.pollScan(id); return; }
        this.scanPolling = true;
        const r = await this.request(`fleet/scan/${enc(id)}`);
        this.scanPolling = false;
        if (this.add.scanId !== id) return;
        if (!r.ok || !r.data) {
          if (this.add.scan) this.add.scan.state = 'done';
          this.add.scanId = null;
          return;
        }
        (r.data.found || []).forEach((row) => {
          if (!row.added && !(row.id in this.add.ticks)) this.add.ticks[row.id] = true;
        });
        this.add.scan = r.data;
        if (r.data.state === 'running') this.pollScan(id);
        else this.add.scanId = null;
      }, delay);
    },
    resumeScan() {
      // A request already on its way schedules the next poll itself.
      if (this.add.scanId && !this.scanPolling) this.pollScan(this.add.scanId, 0);
    },
    useSuggest() {
      this.add.block = this.add.wide.suggest;
      this.scan();
    },
    get scanPct() {
      const s = this.add.scan;
      if (!s) return 0;
      if (s.state !== 'running') return 100;
      return s.total ? Math.min(100, Math.round((100 * s.probed) / s.total)) : 0;
    },
    get foundCount() {
      return ((this.add.scan && this.add.scan.found) || []).filter((r) => !r.added).length;
    },
    // Rows the scan listed that are on the board already, named after the
    // new count so a scan of known machines does not read as nothing found.
    get addedCount() {
      return ((this.add.scan && this.add.scan.found) || []).filter((r) => r.added).length;
    },
    get scanNone() {
      const s = this.add.scan;
      return !!s && s.state !== 'running' && !(s.found || []).length;
    },
    get pickRows() {
      const seen = new Set();
      const out = [];
      [...((this.add.scan && this.add.scan.found) || []), ...this.add.manualRows].forEach((r) => {
        if (seen.has(r.id)) return;
        seen.add(r.id);
        out.push(r);
      });
      return out;
    },
    driveCount(n) {
      return typeof n === 'number' ? this.plural(n, 'drive', 'drives') : '';
    },
    // Check one machine by hand. It always answers in a line under the
    // fields: a machine already in the list is ticked, scrolled to and
    // flashed there; a new one joins the list, ticked; a stored machine with
    // cards is already on the page; else nothing answered, or what answered
    // is not an agent. The list drops a second row for a machine it has, so
    // without this line a Check of a listed machine showed nothing at all.
    async check() {
      const host = this.add.manualHost.trim();
      const port = Number(String(this.add.manualPort).trim()) || 9099;
      if (!host || this.add.checking) return;
      this.add.checkMsg = null;
      this.add.checking = true;
      const r = await this.request('fleet/probe', { method: 'POST', body: { host, port } });
      this.add.checking = false;
      if (!(r.ok && r.data && r.data.id)) {
        const notAgent = r.status === 404 && r.data && r.data.error === 'not_an_agent';
        this.add.checkMsg = notAgent
          ? { text: `${host}:${port} answered, but it is not a SMART Sniffer agent.`, muted: false }
          : { text: `Nothing answered at ${host}:${port}.`, muted: true };
        return;
      }
      const row = r.data;
      this.add.manualHost = '';
      if (row.added) {
        this.add.checkMsg = { text: `${host} is already on the page.`, muted: false };
        return;
      }
      const same = (x) => x.id === row.id || (Number(x.port) === Number(row.port)
        && [row.address, row.resolved].filter(Boolean).map((a) => String(a).toLowerCase())
          .includes(String(x.address).toLowerCase()));
      const listed = this.pickRows.find((x) => !x.added && same(x));
      if (listed) {
        this.add.ticks[listed.id] = true;
        this.add.checkMsg = { text: `${host} is in the list above, ticked.`, muted: false };
        this.flashRow(listed.id);
        return;
      }
      this.add.manualRows = this.add.manualRows.concat([row]);
      this.add.ticks[row.id] = true;
      this.add.checkMsg = { text: `Added ${host} to the list, ticked.`, muted: false };
    },
    // Scroll a Network row into view and pulse its outline once.
    flashRow(id) {
      this.$nextTick(() => {
        const el = [...document.querySelectorAll('.pick-row')].find((e) => e.dataset.id === id);
        if (!el) return;
        el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        el.classList.remove('flash');
        void el.offsetWidth;  // restart the animation on a second Check
        el.classList.add('flash');
        el.addEventListener('animationend', () => el.classList.remove('flash'), { once: true });
      });
    },
    get folderOk() {
      const f = this.replicaFiles && this.replicaFiles.folder;
      if (f && f.status) return f.status === 'ok';
      return !this.info || !this.info.replicas || this.info.replicas.folder === 'ok';
    },
    get pickFiles() {
      const files = ((this.replicaFiles && this.replicaFiles.files) || []).filter((f) => f.valid !== false && f.name);
      const shipped = files.filter((f) => f.source === 'shipped').sort((a, b) => (a.order || 0) - (b.order || 0));
      const user = files.filter((f) => f.source !== 'shipped').sort((a, b) => a.name.localeCompare(b.name));
      return shipped.concat(user);
    },
    get tickedHosts() {
      return this.pickRows.filter((r) => !r.added && this.add.ticks[r.id]);
    },
    get tickedFiles() {
      return this.pickFiles.filter((f) => this.add.repTicks[f.id]).map((f) => f.id);
    },
    get tickCount() {
      return this.tickedHosts.length + this.tickedFiles.length;
    },
    get tickedLock() {
      return this.tickedHosts.some((r) => r.auth === 'password');
    },
    upload(ev) {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (!file) return;
      this.add.upMsg = '';
      if (file.size > 1024 * 1024) { this.add.upMsg = 'That file is over 1 MB.'; return; }
      const reader = new FileReader();
      reader.onload = async () => {
        let doc;
        try { doc = JSON.parse(reader.result); } catch (e) { this.add.upMsg = 'That file is not JSON.'; return; }
        this.add.uploading = true;
        const r = await this.request('replicas', { method: 'POST', body: doc });
        this.add.uploading = false;
        if (r.ok && r.data && r.data.id) {
          if (!this.replicaFiles) this.replicaFiles = { files: [] };
          this.replicaFiles.files = this.replicaFiles.files.filter((f) => f.id !== r.data.id).concat([r.data]);
          this.add.repTicks[r.data.id] = true;
          return;
        }
        const code = r.data && r.data.error;
        this.add.upMsg = code === 'replica_folder_unavailable' ? 'Your replica folder is not available.'
          : code === 'too_large' ? 'That file is over 1 MB.'
            : (r.data && r.data.message) || 'That is not a replica file.';
      };
      reader.readAsText(file);
    },
    async addTicked() {
      const rows = this.tickedHosts;
      const files = this.tickedFiles;
      if (!rows.length && !files.length) return;
      this.add.busy = true;
      this.add.err = '';
      if (rows.length) {
        const r = await this.request('fleet/hosts', { method: 'POST',
          body: { hosts: rows.map((x) => ({ address: x.address, port: x.port })) } });
        if (!r.ok) {
          this.add.busy = false;
          const code = r.data && r.data.error;
          this.add.err = code === 'supervisor_unavailable' ? 'The Supervisor is not available, so field agents cannot be saved.'
            : 'The field agents were not saved. Try again.';
          return;
        }
        rows.forEach((x) => { this.optimistic[x.id] = x; });
        const known = new Set(this.hosts.map((h) => h.id));
        this.hosts = this.hosts.concat(((r.data && r.data.added) || []).filter((h) => !known.has(h.id)));
      }
      if (files.length) {
        const r = await this.request('replicas/add', { method: 'POST', body: { files } });
        if (!r.ok) {
          this.add.busy = false;
          this.add.err = 'The replicas were not added. Try again.';
          return;
        }
      }
      this.add.busy = false;
      this.closeAdd();
      if (rows.length) this.loadHosts();
      if (files.length) this.loadReplicas();
    },
  };
}
