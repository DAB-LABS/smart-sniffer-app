/* SMART Sniffer Control Center: the page's Alpine component, its API client
   and its formatting. Served as a static file by the proxy; no build step.

   The proxy does the judging: every drive arrives with the integration's
   verdict and a "view" that names its rows and their marks, so nothing here
   matches rules. The page only reads the network when asked: Scan, Rescan
   and Check, and reads of stored hosts. */

/* Partials. A <template data-use="tpl-x"> receives a copy of <template
   id="tpl-x">'s content before Alpine starts, so a card's markup is written
   once in index.html and used by more than one list. This script is loaded
   with defer ahead of Alpine, so it runs first. */
(function expandPartials() {
  const done = new Set();
  function source(id) {
    const tpl = document.getElementById(id);
    if (!tpl) return null;
    if (!done.has(id)) {
      done.add(id);
      expand(tpl.content);
    }
    return tpl.content;
  }
  function expand(root) {
    root.querySelectorAll('template').forEach((t) => {
      const id = t.dataset.use;
      if (id && !t.content.childElementCount) {
        const src = source(id);
        if (src) t.content.appendChild(src.cloneNode(true));
      }
      expand(t.content);
    });
  }
  expand(document);
})();

// Row keys of the drive view, and where each one sits in the Field report.
const ATA_IDS = { reallocated: 5, pending: 197, offline_unc: 198, reported_unc: 187, cmd_timeout: 188,
  realloc_events: 196, spin_retries: 10, wear: 177 };
// Device Statistics entries that stand in for a missing attribute: page and
// offset of each gap-fill row of the drive view.
const DEVSTAT_ROWS = { reported_unc_devstat: [4, 8], reallocated_devstat: [3, 32], wear_devstat: [7, 8] };
const NVME_FIELDS = { critical_warning: 'critical_warning', media_errors: 'media_errors',
  available_spare: 'available_spare', wear: 'percentage_used' };
const NONE = { kind: 'none', href: null };
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
    replicasLoaded: false,
    repBusy: false,
    repFailed: false,
    replicasNext: 0,
    replicaFiles: null,
    hosts: [],
    hostsLoaded: false,
    hostsEverRead: false,
    hostsNext: 0,
    hostData: {},
    hostRead: {},
    optimistic: {},
    ha: null,
    haTimers: [],
    now: Date.now(),
    showHidden: false,
    hideErr: '',
    hostsErr: '',
    ui: { open: {}, confirm: null, pw: {}, pwState: {}, pwOpen: {}, more: {}, scen: {}, busy: {}, note: {} },
    add: {
      open: false, block: '', port: '9099', scan: null, scanId: null, last: null, wide: null, error: '',
      ticks: {}, manualHost: '', manualPort: '9099', manualRows: [], manualNone: null, checking: false,
      repTicks: {}, upMsg: '', uploading: false, busy: false, err: '',
    },

    // ── Start and timers ────────────────────────────────────────────
    init() {
      this.loadInfo();
      this.loadSelf();
      this.loadReplicas();
      this.loadHosts();
      this.loadHa();
      setInterval(() => this.tick(), 1000);
      document.addEventListener('visibilitychange', () => { if (!document.hidden) this.tick(); });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.ui.confirm) this.closeConfirm();
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

    async loadSelf() {
      this.selfBusy = true;
      const r = await this.request('fleet/hosts/self/drives');
      this.selfBusy = false;
      this.selfLoaded = true;
      this.selfNext = Date.now() + 8000;
      if (r.ok && r.data) {
        (r.data.items || []).forEach((i) => { i._src = 'self'; });
        this.self = r.data;
        this.selfFailed = false;
        if (r.data.status === 'ok') {
          this.selfLastGood = r.data.items;
          this.selfLastAnswer = Date.now();
        } else {
          if (r.data.items && r.data.items.length) this.selfLastGood = r.data.items;
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
        (r.data.items || []).forEach((i) => { i._src = 'replicas'; });
        this.replicas = r.data;
        this.repFailed = r.data.status !== 'ok';
      } else {
        this.repFailed = true;
      }
      this.replicasLoaded = true;
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
      this.hostsLoaded = true;
      const first = !this.hostsEverRead;
      this.hostsEverRead = true;
      await Promise.all(this.hosts.map((h) => this.loadHost(h.id, first)));
    },

    async loadHost(id, first = false) {
      const r = await this.request(`fleet/hosts/${enc(id)}/drives`);
      if (r.ok && r.data) {
        this.hostData[id] = r.data;
        this.hostRead[id] = { at: Date.now(), first };
      }
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

    // ── Drive cards (Case file and Replicas) ─────────────────────────
    get selfItems() {
      return this.self && this.self.status === 'ok' ? this.self.items || [] : [];
    },
    get lastSeenNames() {
      return (this.selfLastGood || []).map((i) => (i.drive && i.drive.model) || (i.pool && `ZFS pool ${i.pool.name}`))
        .filter(Boolean).join(', ') || 'none yet';
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
    get rulesLine() {
      const v = (this.self && this.self.rules_version) || (this.info && this.info.rules_version);
      if (v) return `rules from integration ${v}`;
      if (this.info) return 'rules from the integration are not loaded, so no drive is judged; the Mission Log says why';
      return '';
    },

    hdr(it) {
      if (it.type === 'pool') {
        if (this.vanished(it)) return 'unknown';
        const s = it.verdict && it.verdict.state;
        return s === 'YES' ? 'attention' : s === 'NO' ? 'ok' : 'unknown';
      }
      return (it.view && it.view.header) || 'unknown';
    },
    vanished(it) {
      return !!((it.view && it.view.vanished) || (it.lab && it.lab.vanished));
    },
    failed(it) {
      return it.type === 'drive' && !!(it.view && it.view.health_failed);
    },
    title(it) {
      if (it.type === 'pool') return `ZFS pool ${it.pool.name}`;
      return (it.drive && it.drive.model) || (it.lab && it.lab.name) || it.id;
    },
    proto(drive) {
      const dev = (drive.smart_data && drive.smart_data.device) || {};
      if (drive.protocol === 'ATA' && dev.type === 'sat') return 'SATA';
      return drive.protocol || '';
    },
    rows(it) {
      return (it.view && it.view.rows) || [];
    },
    marked(it) {
      return this.rows(it).filter((r) => r.mark === 'crit' || r.mark === 'warn');
    },
    stats(it) {
      return (it.view && it.view.stats) || {};
    },
    volNote(vol, replica) {
      const src = String(vol.source || '');
      if (src.includes('attribute')) {
        return replica ? `served as attribute ${vol.attribute_id || 241}` : `from attribute ${vol.attribute_id || 241}`;
      }
      if (src.includes('nvme')) return replica ? 'served in the NVMe log' : 'from the NVMe log';
      return replica ? 'served from Device Statistics' : 'from Device Statistics';
    },
    vol(it, which) {
      const v = it.view && it.view.volumes && it.view.volumes[which];
      return v && typeof v.bytes === 'number' ? v : null;
    },
    // "present", "absent", "not applicable (NVMe)", "off (os)", "unavailable: timeout"
    devstatText(it) {
      const ds = (it.drive && it.drive.device_statistics) || {};
      const view = it.view || {};
      const v = String(view.devstat || ds.status || 'absent');
      const reason = view.devstat_reason || ds.reason || '';
      if (v === 'not_applicable') {
        const nvme = view.protocol === 'nvme' || (it.drive && it.drive.protocol === 'NVMe');
        return nvme ? 'not applicable (NVMe)' : 'not applicable';
      }
      if (v === 'off') return reason ? `off (${reason})` : 'off';
      if (v === 'unavailable') return reason ? `unavailable: ${reason}` : 'unavailable';
      return v;
    },
    unsupportedNote(it) {
      return it.type === 'drive' && this.hdr(it) === 'unknown' && !!it.verdict;
    },
    devstatNote(it) {
      return it._src === 'replicas' && it.lab && it.lab.kind === 'ata_devstat'
        && this.rows(it).some((r) => r.key === 'reported_unc_devstat' && r.mark === 'crit');
    },
    readAt(it) {
      return (it.drive && it.drive.last_updated) || (this.replicas && this.replicas.read_at) || (this.self && this.self.read_at);
    },
    canSave(it) {
      return it.type === 'drive' && !!(it.view && it.view.can_save) && this.folderOk;
    },

    // Field report
    reportSummary(it) {
      const rep = (it.view && it.view.report) || {};
      const replica = it._src === 'replicas';
      if (it.type === 'pool') return replica ? 'pool and disk states, editable' : 'pool and disk states';
      if (this.hdr(it) === 'unknown' && !rep.count) return 'nothing to show';
      if (replica && it.lab && it.lab.kind === 'ata_devstat') return 'attributes and Device Statistics page 4, Raw editable';
      if (rep.kind === 'nvme') return replica ? `every field, ${rep.count}, Raw editable` : `every field the drive reports, ${rep.count}`;
      if (rep.kind === 'ata') return replica ? `every attribute, ${rep.count}, Raw editable` : `every attribute, ${rep.count}`;
      return 'nothing to show';
    },
    reportKind(it) {
      if (it.type === 'pool') return 'pool';
      const sd = (it.drive && it.drive.smart_data) || {};
      if (sd.nvme_smart_health_information_log) return 'nvme';
      if (((sd.ata_smart_attributes || {}).table || []).length) return 'ata';
      return 'none';
    },
    markOf(it) {
      const m = {};
      this.rows(it).forEach((r) => { if (r.mark === 'crit' || r.mark === 'warn') m[r.key] = r.mark; });
      return m;
    },
    ataTable(it) {
      const marks = this.markOf(it);
      const byId = {};
      Object.entries(ATA_IDS).forEach(([k, id]) => { if (marks[k]) byId[id] = marks[k]; });
      const pad = (n) => (typeof n === 'number' ? String(n).padStart(3, '0') : '');
      return (((it.drive.smart_data || {}).ata_smart_attributes || {}).table || []).map((a) => ({
        id: a.id, name: a.name, value: pad(a.value), worst: pad(a.worst), thresh: pad(a.thresh),
        raw: (a.raw && (a.raw.string ?? String(a.raw.value))) || '', rawValue: a.raw ? a.raw.value : '',
        mark: byId[a.id] || null,
      }));
    },
    nvmeTable(it) {
      const marks = this.markOf(it);
      const byField = {};
      Object.entries(NVME_FIELDS).forEach(([k, f]) => { if (marks[k]) byField[f] = marks[k]; });
      const log = (it.drive.smart_data || {}).nvme_smart_health_information_log || {};
      return Object.entries(log).map(([k, v]) => ({
        name: k, value: Array.isArray(v) ? v.join(' ') : String(v), editable: typeof v === 'number', mark: byField[k] || null,
      }));
    },
    // The Device Statistics pages that hold a gap-fill reading (page 4 on a
    // drive with no attribute 187), each entry marked as its row is.
    devstatTable(it) {
      const marks = this.markOf(it);
      const byEntry = {};
      const pages = new Set();
      this.rows(it).forEach((r) => {
        const at = DEVSTAT_ROWS[r.key];
        if (!at) return;
        pages.add(at[0]);
        if (marks[r.key]) byEntry[`${at[0]}.${at[1]}`] = marks[r.key];
      });
      const out = [];
      (((it.drive.device_statistics || {}).pages) || []).forEach((p) => {
        if (!pages.has(p.number)) return;
        (p.table || []).forEach((e) => {
          const key = `${p.number}.${e.offset}`;
          out.push({ key, page: p.number, name: e.name, value: e.value,
            editable: key === '4.8', mark: byEntry[key] || null });
        });
      });
      return out;
    },

    // Pools
    poolBad(it) {
      return !!(it.verdict && it.verdict.state === 'YES');
    },
    poolLayout(it) {
      const v = it.view || {};
      if (v.layout) return v.layout;
      const vdev = it.lab && it.lab.vdev;
      const devs = it.lab && it.lab.devices;
      if (!vdev || !devs) return null;
      if (devs.length === 1) return 'single disk';
      const words = { 2: 'two-disk', 3: 'three-disk', 4: 'four-disk' };
      return `${words[devs.length] || `${devs.length}-disk`} ${vdev.type}`;
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
      return ((it.pool && it.pool.problem_vdevs) || []).map((p) => {
        const d = { name: p.name, state: p.state, read: p.read_errors || 0, write: p.write_errors || 0,
          cksum: p.checksum_errors || 0 };
        return { ...d, bad: bad(d) };
      });
    },
    poolFoot(it) {
      const p = it.pool || {};
      const parts = [];
      if (this.poolBad(it)) parts.push('Expected every disk ONLINE with no errors.');
      if (p.last_scrub_end) {
        let s = `Last scrub ${this.ago(p.last_scrub_end)}`;
        if (it._src !== 'replicas' && p.last_scrub_repaired === 0) s += ', nothing repaired';
        else if (p.last_scrub_repaired) s += `, ${this.num(p.last_scrub_repaired)} repaired`;
        parts.push(`${s}.`);
      }
      return parts.join(' ');
    },

    // Open state of rows and reports, by key, kept across reads.
    isOpen(key, dflt = false) {
      return key in this.ui.open ? this.ui.open[key] : dflt;
    },
    setOpen(key, value) {
      if (this.ui.open[key] !== value) this.ui.open[key] = value;
    },

    // ── Exit glyph and its confirm ──────────────────────────────────
    // scope: card (a Case file or replica card), host (a host card's title),
    // row (a drive or pool row in a host card).
    glyph(target, key, scope) {
      const ha = this.ha;
      if (!ha || ha.installed !== true || ha.reachable === false) return NONE;
      const t = ha.targets && ha.targets[target];
      if (!t) return scope === 'row' ? NONE : { kind: 'grey', href: null };
      if (scope === 'host') return { kind: 'blue', href: t.entry_url };
      const dev = key && t.devices && t.devices[key];
      if (dev) return { kind: 'blue', href: `/config/devices/device/${dev}` };
      if (ha.lookup === 'entries_only') return { kind: 'blue', href: t.entry_url };
      return scope === 'row' ? NONE : { kind: 'grey', href: null };
    },
    cardGlyph(it) {
      const target = it._src === 'replicas' ? 'replicas' : 'self';
      const key = it.type === 'pool' ? `pool:${it.pool.name}` : it.drive && (it.drive.serial || `id:${it.id}`);
      return this.glyph(target, key, 'card');
    },
    cardAskKey(it) {
      return `card:${it._src}:${it.id}`;
    },
    askCard(it) {
      if (it._src === 'replicas') {
        let text = 'Add this replica to Home Assistant?';
        const others = ((this.replicas && this.replicas.items) || []).length - 1;
        const entry = this.ha && this.ha.targets && this.ha.targets.replicas;
        if (!entry && others > 0) text += ` The other ${this.plural(others, 'replica comes', 'replicas come')} with it.`;
        this.ask(this.cardAskKey(it), 'replicas', text);
      } else {
        this.ask(this.cardAskKey(it), 'self', "Add this machine's drives to Home Assistant?");
      }
    },
    ask(key, target, text) {
      if (this.ui.confirm && this.ui.confirm.key === key) { this.closeConfirm(); return; }
      this.ui.confirm = { key, target, text, busy: false, err: '' };
    },
    asking(key) {
      return !!(this.ui.confirm && this.ui.confirm.key === key);
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
        this.haSoon();
        return;
      }
      const why = r.data && (r.data.message || r.data.error);
      c.err = {
        cannot_connect: 'Home Assistant could not reach this agent.',
        integration_not_loaded: 'The integration is not installed.',
        invalid_auth: 'Home Assistant refused the password.',
        unreachable: 'Home Assistant is not answering.',
      }[why] || 'Home Assistant did not add it. The Mission Log may say why.';
    },

    // ── Field agents ────────────────────────────────────────────────
    get hiddenCount() {
      return this.hosts.filter((h) => h.hidden).length;
    },
    get visibleHosts() {
      return this.hosts.filter((h) => !h.hidden || this.showHidden);
    },
    toggleShowHidden() {
      if (!this.hiddenCount) return;
      this.showHidden = !this.showHidden;
    },
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
    hostClass(h) {
      const s = this.status(h);
      return { unreach: s === 'unreachable', pending: this.pending(h), 'is-hidden': h.hidden };
    },
    hostName(h) {
      const d = this.data(h);
      const o = this.optimistic[h.id];
      return (d && d.agent && d.agent.hostname) || (o && o.hostname) || h.address;
    },
    agentVer(h) {
      const d = this.data(h);
      const o = this.optimistic[h.id];
      return (d && d.agent && d.agent.version) || (o && o.version) || '';
    },
    // The OS name when the agent sends one; a card waiting for its password
    // shows only what the scan said (board 3.6).
    osName(h) {
      const d = this.data(h);
      if (this.pending(h)) return '';
      return (d && d.agent && d.agent.os_name) || '';
    },
    hostHeader(h) {
      const d = this.data(h);
      if (!d || this.pending(h)) return null;
      if (d.status === 'unreachable') return 'unknown';
      return (d.summary && d.summary.header) || 'unknown';
    },
    hostGlyph(h) {
      const s = this.status(h);
      if (s !== 'ok') return NONE;
      return this.glyph(h.id, null, 'host');
    },
    rowGlyph(h, it) {
      const key = it.type === 'pool' ? `pool:${it.pool.name}` : it.drive && (it.drive.serial || `id:${it.id}`);
      return this.glyph(h.id, key, 'row');
    },
    hostDrives(h) {
      const d = this.data(h);
      if (d && d.summary && (d.status === 'ok' || d.status === 'unreachable')) return d.summary.drives;
      if (d && d.agent && typeof d.agent.drives === 'number') return d.agent.drives;
      const o = this.optimistic[h.id];
      return o ? o.drives : null;
    },
    hostPools(h) {
      const d = this.data(h);
      return d && d.summary ? d.summary.pools : 0;
    },
    uptime(h) {
      const d = this.data(h);
      const s = d && d.agent && d.agent.uptime_seconds;
      if (typeof s !== 'number') return null;
      return s >= 86400 ? { n: Math.floor(s / 86400), unit: 'days' } : { n: Math.floor(s / 3600), unit: 'h' };
    },
    rank(it) {
      const s = it.verdict && it.verdict.state;
      if (s === 'YES') return 0;
      if (s === 'MAYBE') return 1;
      if (it.type === 'pool') return 2;
      if (s === 'UNSUPPORTED') return 3;
      return 4;
    },
    sortedRows(h) {
      const d = this.data(h);
      const items = (d && d.items) || [];
      return items.map((it, i) => ({ it, i })).sort((a, b) => this.rank(a.it) - this.rank(b.it) || a.i - b.i)
        .map((x) => x.it);
    },
    showRows(h) {
      const rows = this.sortedRows(h);
      return rows.length > 8 && !this.ui.more[h.id] ? rows.slice(0, 8) : rows;
    },
    folded(h) {
      const rows = this.sortedRows(h);
      return rows.length > 8 && !this.ui.more[h.id] ? rows.slice(8) : [];
    },
    foldText(h) {
      const rest = this.folded(h);
      const drives = rest.filter((i) => i.type === 'drive').length;
      const pools = rest.filter((i) => i.type === 'pool').length;
      const parts = [];
      if (drives) parts.push(this.plural(drives, 'drive', 'drives'));
      if (pools) parts.push(this.plural(pools, 'pool', 'pools'));
      const calm = rest.every((i) => this.rank(i) > 1);
      return `${parts.join(' and ')}${calm ? ', none needs attention' : ''}`;
    },
    hasPools(h) {
      const d = this.data(h);
      return !!(d && (d.items || []).some((i) => i.type === 'pool'));
    },
    showCounts(h) {
      return this.sortedRows(h).length > 8;
    },
    rowClass(it) {
      if (it.type === 'pool') return { pool: true, crit: this.poolBad(it) };
      const s = it.verdict && it.verdict.state;
      return { crit: s === 'YES', warn: s === 'MAYBE', unsup: s === 'UNSUPPORTED' };
    },
    rowKey(h, it) {
      return `row:${h.id}:${it.id}`;
    },
    // A pool row's open state: in a host card it starts closed, on a pool
    // replica (no host) it starts open.
    poolKey(h, it) {
      return h ? this.rowKey(h, it) : `rep:${it.id}`;
    },
    tempText(it) {
      if (it.verdict && it.verdict.state === 'UNSUPPORTED') return 'not known';
      const t = this.stats(it).temp_c;
      return typeof t === 'number' ? `${t} C` : '';
    },
    writtenText(it) {
      const v = this.vol(it, 'written');
      return v ? this.tb(v.bytes) : '';
    },
    powerOn(it) {
      const s = this.stats(it);
      const parts = [];
      if (typeof s.power_on_hours === 'number') parts.push(`${this.num(s.power_on_hours)} h`);
      if (typeof s.power_cycles === 'number') parts.push(`${this.num(s.power_cycles)} cycles`);
      return parts.join(', ');
    },
    hostFoot(h) {
      const d = this.data(h);
      if (h.hidden) {
        const r = this.hostRead[h.id];
        return r ? `read ${this.ago(r.at)}` : '';
      }
      if (this.pending(h) || this.status(h) === 'reading') {
        const at = Date.parse(h.added_at);
        return !at || this.now - at < 60000 ? 'added just now' : `added ${this.ago(at)}`;
      }
      if (d && d.status === 'unreachable') return d.last_answer ? `last answer ${this.ago(d.last_answer)}` : 'not answering';
      const r = this.hostRead[h.id];
      if (!r) return '';
      return r.first ? `read when the page opened, ${this.ago(r.at)}` : `read ${this.ago(r.at)}`;
    },
    hostAskText(h) {
      return `Add ${this.hostName(h)} to Home Assistant?`;
    },

    async toggleHidden(h) {
      const was = h.hidden;
      const since = h.hidden_since;
      h.hidden = !was;
      h.hidden_since = h.hidden ? new Date().toISOString() : null;
      if (!this.hiddenCount) this.showHidden = false;
      this.hideErr = '';
      const r = await this.request(`fleet/hosts/${enc(h.id)}`, { method: 'PATCH', body: { hidden: !was } });
      if (!r.ok) {
        h.hidden = was;
        h.hidden_since = since;
        this.hideErr = 'That change was not saved. Try again.';
        return;
      }
      if (r.data && r.data.host) Object.assign(h, r.data.host);
      if (was) this.loadHost(h.id);
    },

    pwShown(h) {
      return this.pending(h) || !!this.ui.pwOpen[h.id];
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
        this.ui.pwOpen[h.id] = false;
        if (r.data && r.data.host) Object.assign(h, r.data.host);
        await this.loadHost(h.id);
        return;
      }
      const code = r.data && r.data.error;
      this.ui.pwState[h.id] = code === 'password_refused' ? 'refused' : code === 'not_answering' ? 'noanswer' : 'error';
    },

    async saveReplica(host, it, key) {
      if (this.ui.busy[key]) return;
      this.ui.busy[key] = true;
      this.ui.note[key] = '';
      const r = await this.request('replicas/save-as', { method: 'POST', body: { host, drive_id: it.drive.id } });
      this.ui.busy[key] = false;
      if (r.ok) {
        this.ui.note[key] = 'Saved to Replicas';
        this.loadReplicas();
      } else {
        const code = r.data && r.data.error;
        this.ui.note[key] = {
          replica_folder_unavailable: 'Your replica folder is not available.',
          unsupported_drive: 'This drive has no SMART data to copy.',
          not_answering: 'The agent did not answer.',
        }[code] || 'It was not saved. Try again.';
      }
      setTimeout(() => { this.ui.note[key] = ''; }, 5000);
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
        open: false, scan: null, last: null, wide: null, error: '', ticks: {}, manualRows: [], manualNone: null,
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
    pollScan(id) {
      setTimeout(async () => {
        if (this.add.scanId !== id) return;
        if (document.hidden) { this.pollScan(id); return; }
        const r = await this.request(`fleet/scan/${enc(id)}`);
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
      }, 400);
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
    async check() {
      const host = this.add.manualHost.trim();
      const port = Number(String(this.add.manualPort).trim()) || 9099;
      if (!host || this.add.checking) return;
      this.add.manualNone = null;
      this.add.checking = true;
      const r = await this.request('fleet/probe', { method: 'POST', body: { host, port } });
      this.add.checking = false;
      if (r.ok && r.data && r.data.id) {
        this.add.manualRows = this.add.manualRows.filter((x) => x.id !== r.data.id).concat([r.data]);
        if (!r.data.added) this.add.ticks[r.data.id] = true;
        this.add.manualHost = '';
      } else {
        this.add.manualNone = { host, port };
      }
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

    // ── Replicas ────────────────────────────────────────────────────
    get repItems() {
      return (this.replicas && this.replicas.items) || [];
    },
    scenOptions(it) {
      return (it.lab && it.lab.scenario && it.lab.scenario.options) || [];
    },
    scenSel(it) {
      return this.ui.scen[it.id] || (it.lab && it.lab.scenario && it.lab.scenario.current) || '';
    },
    mockPath(it) {
      return it.type === 'pool' ? `mock/pools/${enc(it.pool.name)}` : `mock/drives/${enc(it.drive.id)}`;
    },
    async applyScenario(it) {
      const id = this.scenSel(it);
      if (!id) return;
      this.ui.busy[`scen:${it.id}`] = true;
      await this.request(`${this.mockPath(it)}/scenario`, { method: 'POST', body: { id } });
      this.ui.busy[`scen:${it.id}`] = false;
      delete this.ui.scen[it.id];
      this.loadReplicas();
    },
    async editRaw(it, kind, field, text) {
      const n = Number(String(text).trim());
      if (!Number.isFinite(n)) return;
      if (kind === 'ata') await this.request(`${this.mockPath(it)}/smart`, { method: 'PATCH', body: { [field]: n } });
      else if (kind === 'nvme') await this.request(`${this.mockPath(it)}/smart`, { method: 'PATCH', body: { [field]: n } });
      else if (kind === 'devstat') await this.request(`${this.mockPath(it)}/devstat`, { method: 'PATCH', body: { reported_uncorrectable: n } });
      this.loadReplicas();
    },
    async editPool(it, disk, field, text) {
      let body;
      if (disk === null) body = { state: String(text).trim().toUpperCase() };
      else if (field === 'state') body = { devices: [{ name: disk, state: String(text).trim().toUpperCase() }] };
      else {
        const n = Number(String(text).trim());
        if (!Number.isFinite(n)) return;
        body = { devices: [{ name: disk, [field]: n }] };
      }
      await this.request(this.mockPath(it), { method: 'PATCH', body });
      this.loadReplicas();
    },
    async download(it) {
      try {
        const resp = await fetch(`${this.baseUrl}replicas/${enc(it.id)}/file`);
        if (!resp.ok) return;
        const blob = await resp.blob();
        const cd = resp.headers.get('Content-Disposition') || '';
        const m = /filename="?([^";]+)"?/.exec(cd);
        const name = m ? m[1] : `${String(it.id).replace(/[^a-z0-9]+/gi, '-')}.json`;
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
    async removeReplica(it) {
      const r = await this.request(`replicas/${enc(it.id)}`, { method: 'DELETE' });
      if (r.ok && this.replicas) this.replicas.items = this.replicas.items.filter((x) => x.id !== it.id);
      this.loadReplicas();
    },
  };
}
