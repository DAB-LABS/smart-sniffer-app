/**
 * Smart Sniffer Card for Home Assistant  v1.0.0
 * Dashboard card for https://github.com/DAB-LABS/smart-sniffer
 *
 * Install:  copy to  config/www/smart-sniffer-card.js
 * Register: Settings → Dashboards → Resources
 *           URL: /local/smart-sniffer-card.js   Type: JavaScript module
 * Use:      type: custom:smart-sniffer-card
 *
 * Config options:
 *   title:      string  Card heading (default "Drive Health")
 *   columns:    1–4     Drives per row (default 2)
 *   show_ok:    bool    Show healthy drives (default true)
 *   drives:     []      Device IDs to show; empty = show all
 */

const VERSION = "1.0.0";
const DOMAIN  = "smart_sniffer";

/* ─── Entity key suffix patterns ──────────────────────────────────────────
 * These match entity_ids produced by the smart_sniffer integration.
 * Each drive device gets one entity per row in the table below.         */
const ENTITY_KEYS = {
  attention_needed:               /_attention_needed$/,
  health:                         /_health$/,
  temperature:                    /_temperature$/,
  power_on_hours:                 /_power_on_hours$/,
  smart_status:                   /_smart_status$/,
  reallocated_sector_count:       /_reallocated_sector_count$/,
  reported_uncorrectable_errors:  /_reported_uncorrectable_errors$/,
  percentage_used:                /_(percentage_used|wear_leveling_percentage_used)$/,
  power_cycle_count:              /_power_cycle_count$/,
  reallocated_event_count:        /_reallocated_event_count$/,
  spin_retry_count:               /_spin_retry_count$/,
  command_timeout:                /_command_timeout$/,
  available_spare:                /_available_spare$/,
  available_spare_threshold:      /_available_spare_threshold$/,
  pending_sector_count:           /_current_pending_sector_count$/,
};

/* ─── Diagnostic rows shown in the click-through detail panel ───────────── */
const DIAG_ROWS = [
  { key: "temperature",                  label: "Temperature",           unit: "°C" },
  { key: "power_on_hours",               label: "Power-On Hours",        unit: "h"  },
  { key: "smart_status",                 label: "SMART Status",          unit: ""   },
  { key: "reallocated_sector_count",     label: "Reallocated Sectors",   unit: ""   },
  { key: "reported_uncorrectable_errors",label: "Uncorrectable Errors",  unit: ""   },
  { key: "pending_sector_count",         label: "Pending Sectors",       unit: ""   },
  { key: "reallocated_event_count",      label: "Reallocation Events",   unit: ""   },
  { key: "spin_retry_count",             label: "Spin Retries",          unit: ""   },
  { key: "command_timeout",              label: "Command Timeouts",      unit: ""   },
  { key: "percentage_used",              label: "Wear / % Used",         unit: "%"  },
  { key: "available_spare",              label: "Available Spare",       unit: "%"  },
  { key: "available_spare_threshold",    label: "Spare Threshold",       unit: "%"  },
  { key: "power_cycle_count",            label: "Power Cycles",          unit: ""   },
];

/* ─── Styles ──────────────────────────────────────────────────────────────── */
const STYLES = `
:host {
  --c-bg:      var(--card-background-color,      #0d1117);
  --c-surface: var(--secondary-background-color, #161b22);
  --c-border:  rgba(255,255,255,0.09);
  --c-text:    var(--primary-text-color,         #e6edf3);
  --c-muted:   var(--secondary-text-color,       #8b949e);
  --c-dim:     rgba(255,255,255,0.04);
  --c-ok:      #3fb950;
  --c-warn:    #d29922;
  --c-crit:    #f85149;
  --c-info:    #58a6ff;
  --c-unknown: #6e7681;
  --radius:    14px;
  --radius-sm: 9px;
  font-family: 'Noto Sans','Roboto','Helvetica Neue',Arial,sans-serif;
  display: block;
}
* { box-sizing: border-box; margin: 0; padding: 0; }

/* ── Card shell ── */
.card {
  background: var(--c-bg);
  border-radius: var(--radius);
  overflow: hidden;
  color: var(--c-text);
  font-size: 13px;
  line-height: 1.5;
  border: 1px solid var(--c-border);
}

/* ── Header ── */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 13px 18px 11px;
  border-bottom: 1px solid var(--c-border);
  background: var(--c-surface);
}
.header-left { display: flex; align-items: center; gap: 10px; }
.header-icon {
  width: 30px; height: 30px;
  background: linear-gradient(135deg, #1d4ed8, #6366f1);
  border-radius: 7px;
  display: flex; align-items: center; justify-content: center;
  font-size: 16px; line-height: 1;
}
.header-title { font-size: 14px; font-weight: 600; letter-spacing: .03em; }
.header-sub   { font-size: 11px; color: var(--c-muted); }

/* ── Summary stat strip ── */
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border-bottom: 1px solid var(--c-border);
}
.stat {
  padding: 11px 14px;
  border-right: 1px solid var(--c-border);
  position: relative;
  overflow: hidden;
}
.stat:last-child { border-right: none; }
.stat-val {
  font-size: 22px; font-weight: 600;
  letter-spacing: -.02em; line-height: 1;
  margin-bottom: 3px;
}
.stat-val.ok      { color: var(--c-ok);      }
.stat-val.warn    { color: var(--c-warn);     }
.stat-val.crit    { color: var(--c-crit);     }
.stat-val.neutral { color: var(--c-text);     }
.stat-val.unknown { color: var(--c-unknown);  }
.stat-lbl {
  font-size: 9px; color: var(--c-muted);
  letter-spacing: .08em; text-transform: uppercase;
}
.stat-bar {
  position: absolute; bottom: 0; left: 0;
  height: 2px; border-radius: 0 2px 0 0;
  transition: width .4s ease;
}
.stat-bar.ok   { background: var(--c-ok);   }
.stat-bar.warn { background: var(--c-warn); }
.stat-bar.crit { background: var(--c-crit); }

/* ── Alert banner ── */
.alert-banner {
  padding: 8px 18px; font-size: 12px; font-weight: 500;
  display: flex; align-items: center; gap: 8px;
  border-bottom: 1px solid;
}
.alert-banner.crit {
  background: rgba(248,81,73,.1);
  border-color: rgba(248,81,73,.25);
  color: var(--c-crit);
}
.alert-banner.warn {
  background: rgba(210,153,34,.08);
  border-color: rgba(210,153,34,.22);
  color: var(--c-warn);
}

/* ── Drives section ── */
.drives-section { padding: 14px 16px; }
.drives-header {
  display: flex; align-items: center;
  justify-content: space-between; margin-bottom: 10px;
}
.drives-title {
  font-size: 10px; color: var(--c-muted);
  letter-spacing: .08em; text-transform: uppercase;
}
.drives-count {
  font-size: 10px; color: var(--c-muted);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  padding: 2px 8px; border-radius: 4px;
}
.drives-grid { display: grid; gap: 8px; }

/* ── Drive chip ── */
.drive-chip {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-sm);
  padding: 10px 12px 10px 16px;
  cursor: pointer;
  transition: border-color .15s, background .15s, transform .15s, box-shadow .15s;
  position: relative;
  overflow: hidden;
  user-select: none;
}
/* Left accent stripe */
.drive-chip::before {
  content: '';
  position: absolute;
  left: 0; top: 0; bottom: 0; width: 4px;
}
.drive-chip.attn-no::before          { background: var(--c-ok); }
.drive-chip.attn-maybe::before       { background: var(--c-warn); }
.drive-chip.attn-yes::before         { background: var(--c-crit);
                                        animation: pulse-stripe 2s ease-in-out infinite; }
.drive-chip.attn-unsupported::before { background: var(--c-unknown); }
.drive-chip.attn-unknown::before     { background: var(--c-unknown); }

@keyframes pulse-stripe {
  0%, 100% { opacity: 1; }
  50%       { opacity: .35; }
}

.drive-chip:hover {
  border-color: rgba(255,255,255,.22);
  background: rgba(255,255,255,.04);
  transform: translateY(-1px);
  box-shadow: 0 4px 16px rgba(0,0,0,.35);
}
.drive-chip.active {
  border-color: var(--c-info);
  background: rgba(88,166,255,.07);
}
.drive-chip-top {
  display: flex; align-items: flex-start;
  justify-content: space-between;
  gap: 8px; margin-bottom: 7px;
}
.drive-name {
  font-size: 12px; font-weight: 500; line-height: 1.3;
  flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.drive-temp {
  font-size: 11px; color: var(--c-muted);
  white-space: nowrap; flex-shrink: 0;
}
.drive-badges { display: flex; gap: 5px; flex-wrap: wrap; }

/* ── Badges ── */
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 10px; font-weight: 600;
  padding: 2px 7px; border-radius: 4px;
  letter-spacing: .04em; text-transform: uppercase;
}
.badge-dot { width: 5px; height: 5px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
.badge.ok      { background: rgba(63,185,80,.12);   color: var(--c-ok);      border: 1px solid rgba(63,185,80,.3);   }
.badge.warn    { background: rgba(210,153,34,.12);  color: var(--c-warn);    border: 1px solid rgba(210,153,34,.3);  }
.badge.crit    { background: rgba(248,81,73,.12);   color: var(--c-crit);    border: 1px solid rgba(248,81,73,.3);   }
.badge.unknown { background: rgba(110,118,129,.12); color: var(--c-unknown); border: 1px solid rgba(110,118,129,.3); }
.badge.info    { background: rgba(88,166,255,.12);  color: var(--c-info);    border: 1px solid rgba(88,166,255,.3);  }

/* ── Detail panel (click-through) ── */
.detail-wrap {
  padding: 0 16px 14px;
  animation: slideDown .15s ease;
}
@keyframes slideDown {
  from { opacity: 0; transform: translateY(-6px); }
  to   { opacity: 1; transform: translateY(0); }
}
.detail-panel {
  background: rgba(255,255,255,.025);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-sm);
  padding: 13px 14px;
}
.detail-header {
  display: flex; align-items: flex-start;
  justify-content: space-between; gap: 10px;
  margin-bottom: 12px; padding-bottom: 10px;
  border-bottom: 1px solid var(--c-border);
}
.detail-drive-name  { font-size: 13px; font-weight: 600; }
.detail-drive-model { font-size: 11px; color: var(--c-muted); margin-top: 2px; }
.detail-close {
  cursor: pointer; color: var(--c-muted);
  font-size: 18px; line-height: 1;
  padding: 0 3px; flex-shrink: 0;
}
.detail-close:hover { color: var(--c-text); }
.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: 6px;
}
.detail-stat {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 7px; padding: 8px 10px;
}
.detail-stat-label {
  font-size: 9px; color: var(--c-muted);
  letter-spacing: .06em; text-transform: uppercase;
  margin-bottom: 3px;
}
.detail-stat-val { font-size: 14px; font-weight: 500; }
.detail-stat-val.ok      { color: var(--c-ok);      }
.detail-stat-val.warn    { color: var(--c-warn);     }
.detail-stat-val.crit    { color: var(--c-crit);     }
.detail-stat-val.unknown { color: var(--c-unknown);  }
.detail-stat-val.neutral { color: var(--c-text);     }

/* ── Floating tooltip (mouseover) ── */
.tooltip {
  position: fixed;
  z-index: 9999;
  background: #0d1117;
  border: 1px solid rgba(255,255,255,.18);
  border-radius: 9px;
  padding: 11px 14px;
  font-size: 11px;
  color: #e6edf3;
  min-width: 185px; max-width: 250px;
  box-shadow: 0 10px 36px rgba(0,0,0,.65);
  pointer-events: none;
  opacity: 0;
  transition: opacity .1s ease;
  line-height: 1.55;
}
.tooltip.visible { opacity: 1; }
.tooltip-drive {
  font-size: 12px; font-weight: 600;
  margin-bottom: 8px; padding-bottom: 7px;
  border-bottom: 1px solid rgba(255,255,255,.1);
}
.tooltip-row {
  display: flex; justify-content: space-between;
  gap: 10px; margin-top: 3px;
}
.tooltip-key  { color: #8b949e; }
.tooltip-val  { font-weight: 500; }
.tooltip-val.ok   { color: #3fb950; }
.tooltip-val.warn { color: #d29922; }
.tooltip-val.crit { color: #f85149; }
.tooltip-hint {
  margin-top: 8px; padding-top: 7px;
  border-top: 1px solid rgba(255,255,255,.08);
  font-size: 10px; color: #58a6ff;
}

/* ── Empty state ── */
.empty {
  padding: 36px 20px;
  text-align: center;
  color: var(--c-muted);
}
.empty-icon { font-size: 32px; margin-bottom: 10px; }
.empty-text { font-size: 13px; line-height: 1.7; }
.empty-code {
  display: inline-block; margin-top: 8px;
  font-family: monospace; font-size: 11px;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  padding: 3px 9px; border-radius: 4px;
  color: var(--c-info);
}
`;

/* ═══════════════════════════════════════════════════════════════════════════
   Main card element
 ═══════════════════════════════════════════════════════════════════════════ */
class SmartSnifferCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config   = null;
    this._hass     = null;
    this._drives   = [];
    this._selected = null;   // selected device_id for detail panel
    this._tooltip  = null;   // tooltip DOM element
  }

  /* ── HA card protocol ─────────────────────────────────────────────────── */

  static getConfigElement() {
    return document.createElement("smart-sniffer-card-editor");
  }

  static getStubConfig() {
    return { title: "Drive Health", columns: 2, show_ok: true };
  }

  setConfig(config) {
    if (!config) throw new Error("smart-sniffer-card: invalid configuration");
    this._config = {
      title:   "Drive Health",
      columns: 2,
      show_ok: true,
      drives:  [],
      ...config,
    };
  }

  set hass(hass) {
    const prev = this._hass;
    this._hass = hass;
    if (!this._config) return;

    // Avoid re-rendering when nothing relevant has changed
    if (prev && !this._shouldUpdate(prev, hass)) return;

    this._drives = this._collectDrives(hass);
    this._render();
  }

  getCardSize() {
    const rows = Math.ceil((this._drives?.length || 4) / (this._config?.columns || 2));
    return rows + 2;
  }

  /* ── Change detection ─────────────────────────────────────────────────── */

  _shouldUpdate(prev, curr) {
    // Entity registry changed (drives added/removed)
    if (prev.entities !== curr.entities) return true;

    // Any tracked entity state changed
    for (const drive of this._drives) {
      for (const entityId of Object.values(drive.entities)) {
        if (entityId && prev.states[entityId]?.state !== curr.states[entityId]?.state) return true;
      }
    }

    // No tracked entities yet — first render
    if (this._drives.length === 0) return true;

    return false;
  }

  /* ── Drive discovery ──────────────────────────────────────────────────── */

  _collectDrives(hass) {
    const deviceMap = {};  // device_id → { device_id, entities: {key → entityId} }

    if (hass.entities) {
      /* Modern HA (2023.x+): use the entity registry                        */
      for (const [entityId, entry] of Object.entries(hass.entities)) {
        if ((entry.platform || "").toLowerCase() !== DOMAIN) continue;
        if (!hass.states[entityId]) continue;            // skip disabled/removed

        const devId = entry.device_id || "__no_device__";
        if (!deviceMap[devId]) deviceMap[devId] = { device_id: devId, entities: {} };
        this._classifyEntity(deviceMap[devId].entities, entityId);
      }
    } else {
      /* Fallback: scan state keys for attention_needed sentinels           */
      const attnIds = Object.keys(hass.states).filter(id => ENTITY_KEYS.attention_needed.test(id));
      for (const attnId of attnIds) {
        const prefix  = attnId.replace(/_attention_needed$/, "");
        const fakeId  = prefix;
        if (!deviceMap[fakeId]) deviceMap[fakeId] = { device_id: fakeId, entities: {} };
        for (const entityId of Object.keys(hass.states)) {
          if (entityId === prefix || entityId.startsWith(prefix + "_")) {
            this._classifyEntity(deviceMap[fakeId].entities, entityId);
          }
        }
      }
    }

    /* Build normalised drive objects                                        */
    const drives = [];
    for (const [devId, data] of Object.entries(deviceMap)) {
      if (!data.entities.attention_needed && !data.entities.health) continue;

      const device = (hass.devices || {})[devId] || {};
      const name   = device.name
                  || device.name_by_user
                  || this._nameFromEntities(data.entities);
      const model  = [device.manufacturer, device.model].filter(Boolean).join(" · ");

      const attnRaw   = this._state(hass, data.entities.attention_needed) || "";
      const healthRaw = this._state(hass, data.entities.health)           || "";
      const tempRaw   = this._state(hass, data.entities.temperature);
      const tempUnit  = this._attr(hass, data.entities.temperature, "unit_of_measurement") || "°C";

      drives.push({
        device_id: devId,
        name,
        model,
        entities:  data.entities,
        attention: attnRaw.toLowerCase(),    // no | maybe | yes | unsupported
        health:    healthRaw.toLowerCase(),  // ok | problem | unknown
        temp:      tempRaw != null ? `${tempRaw} ${tempUnit}` : null,
      });
    }

    /* Sort worst-first                                                      */
    const ORDER = { yes: 0, maybe: 1, unsupported: 2, no: 3, "": 4 };
    drives.sort((a, b) => (ORDER[a.attention] ?? 4) - (ORDER[b.attention] ?? 4));

    /* Optionally filter to configured drive list                            */
    const filterIds = this._config.drives || [];
    return filterIds.length > 0
      ? drives.filter(d => filterIds.includes(d.device_id))
      : drives;
  }

  _classifyEntity(entityMap, entityId) {
    for (const [key, pattern] of Object.entries(ENTITY_KEYS)) {
      if (pattern.test(entityId)) {
        entityMap[key] = entityId;
        return;
      }
    }
  }

  _nameFromEntities(entities) {
    const src = entities.attention_needed || entities.health || "";
    if (!src) return "Unknown Drive";
    return src
      .replace(/^(sensor|binary_sensor)\./i, "")
      .replace(/_(attention_needed|health|temperature)$/, "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, c => c.toUpperCase());
  }

  /* ── State helpers ────────────────────────────────────────────────────── */

  _state(hass, entityId) {
    if (!entityId) return null;
    const s = hass.states[entityId]?.state;
    return (s === "unavailable" || s === "unknown") ? null : (s ?? null);
  }

  _attr(hass, entityId, attr) {
    if (!entityId) return null;
    return hass.states[entityId]?.attributes?.[attr] ?? null;
  }

  /* ── Render ───────────────────────────────────────────────────────────── */

  _render() {
    const c      = this._config;
    const drives = this._drives;
    const root   = this.shadowRoot;

    /* Counts by attention level */
    const counts = { no: 0, maybe: 0, yes: 0, unsupported: 0 };
    drives.forEach(d => { if (d.attention in counts) counts[d.attention]++; });

    /* Visible set (filter OK when show_ok is false) */
    const visible = (c.show_ok !== false)
      ? drives
      : drives.filter(d => d.attention !== "no" || d.health !== "ok");

    const cols = Math.min(4, Math.max(1, parseInt(c.columns) || 2));

    /* ── Clear shadow root ── */
    root.innerHTML = "";

    /* Styles */
    const styleEl = document.createElement("style");
    styleEl.textContent = STYLES;
    root.appendChild(styleEl);

    /* Tooltip element — lives outside .card to escape overflow:hidden */
    const tooltip = document.createElement("div");
    tooltip.className = "tooltip";
    root.appendChild(tooltip);
    this._tooltip = tooltip;

    /* ── Card shell ── */
    const card = document.createElement("div");
    card.className = "card";
    root.appendChild(card);

    /* Header */
    card.innerHTML = `
      <div class="header">
        <div class="header-left">
          <div class="header-icon">🔍</div>
          <div>
            <div class="header-title">${this._esc(c.title || "Drive Health")}</div>
            <div class="header-sub">SMART Sniffer · ${drives.length} drive${drives.length !== 1 ? "s" : ""} discovered</div>
          </div>
        </div>
      </div>`;

    /* Stats strip */
    const total   = drives.length;
    const pct     = (n) => total ? Math.round(n / total * 100) : 0;
    card.innerHTML += `
      <div class="stats">
        <div class="stat">
          <div class="stat-val ${counts.yes > 0 ? "crit" : "neutral"}">${total}</div>
          <div class="stat-lbl">Drives</div>
        </div>
        <div class="stat">
          <div class="stat-val ok">${counts.no}</div>
          <div class="stat-lbl">Healthy</div>
          <div class="stat-bar ok" style="width:${pct(counts.no)}%"></div>
        </div>
        <div class="stat">
          <div class="stat-val ${counts.maybe > 0 ? "warn" : "neutral"}">${counts.maybe}</div>
          <div class="stat-lbl">Watch</div>
          ${counts.maybe > 0 ? `<div class="stat-bar warn" style="width:${pct(counts.maybe)}%"></div>` : ""}
        </div>
        <div class="stat">
          <div class="stat-val ${counts.yes > 0 ? "crit" : "neutral"}">${counts.yes}</div>
          <div class="stat-lbl">Critical</div>
          ${counts.yes > 0 ? `<div class="stat-bar crit" style="width:${pct(counts.yes)}%"></div>` : ""}
        </div>
      </div>`;

    /* Alert banner */
    if (counts.yes > 0) {
      const names = drives.filter(d => d.attention === "yes").map(d => d.name).join(", ");
      card.innerHTML += `
        <div class="alert-banner crit">
          <span>⛔</span>
          <span><strong>${counts.yes} drive${counts.yes !== 1 ? "s" : ""} need immediate attention</strong> — ${this._esc(names)}</span>
        </div>`;
    } else if (counts.maybe > 0) {
      const names = drives.filter(d => d.attention === "maybe").map(d => d.name).join(", ");
      card.innerHTML += `
        <div class="alert-banner warn">
          <span>⚠️</span>
          <span><strong>${counts.maybe} drive${counts.maybe !== 1 ? "s" : ""} showing early warning signs</strong> — ${this._esc(names)}</span>
        </div>`;
    }

    /* Drives section */
    const drivesSection = document.createElement("div");
    drivesSection.className = "drives-section";
    card.appendChild(drivesSection);

    if (visible.length === 0) {
      drivesSection.innerHTML = `
        <div class="empty">
          <div class="empty-icon">💾</div>
          <div class="empty-text">
            No drives found from the SMART Sniffer integration.<br>
            Install the integration and start at least one agent.<br>
            <span class="empty-code">type: custom:smart-sniffer-card</span>
          </div>
        </div>`;
    } else {
      drivesSection.innerHTML = `
        <div class="drives-header">
          <span class="drives-title">Drives</span>
          <span class="drives-count">
            ${visible.length}${visible.length < drives.length ? ` of ${drives.length}` : ""} shown
          </span>
        </div>`;

      const grid = document.createElement("div");
      grid.className = "drives-grid";
      grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
      drivesSection.appendChild(grid);

      visible.forEach(drive => grid.appendChild(this._buildChip(drive, card)));
    }

    /* Detail panel placeholder — appended after drives section */
    const detailWrap = document.createElement("div");
    detailWrap.id = "ss-detail";
    card.appendChild(detailWrap);

    /* Restore previously selected drive */
    if (this._selected) {
      const still = drives.find(d => d.device_id === this._selected);
      if (still) this._showDetail(still, detailWrap);
    }
  }

  /* ── Build drive chip ─────────────────────────────────────────────────── */

  _buildChip(drive, card) {
    const isActive = this._selected === drive.device_id;

    const chip = document.createElement("div");
    chip.className = `drive-chip attn-${drive.attention || "unknown"}${isActive ? " active" : ""}`;
    chip.dataset.deviceId = drive.device_id;

    chip.innerHTML = `
      <div class="drive-chip-top">
        <div class="drive-name">${this._esc(drive.name)}</div>
        ${drive.temp ? `<div class="drive-temp">🌡 ${this._esc(drive.temp)}</div>` : ""}
      </div>
      <div class="drive-badges">
        ${this._attnBadge(drive.attention)}
        ${this._healthBadge(drive.health)}
      </div>`;

    /* Click → toggle detail panel */
    chip.addEventListener("click", () => {
      const detailWrap = this.shadowRoot.querySelector("#ss-detail");
      const wasActive  = this._selected === drive.device_id;

      /* Deactivate all chips */
      this.shadowRoot.querySelectorAll(".drive-chip.active")
        .forEach(el => el.classList.remove("active"));

      if (wasActive) {
        /* Collapse if clicking the already-active drive */
        this._selected = null;
        if (detailWrap) detailWrap.innerHTML = "";
      } else {
        this._selected = drive.device_id;
        chip.classList.add("active");
        if (detailWrap) this._showDetail(drive, detailWrap);
      }
    });

    /* Hover → floating tooltip */
    chip.addEventListener("mouseenter", (e) => this._showTooltip(e, drive));
    chip.addEventListener("mousemove",  (e) => this._moveTooltip(e));
    chip.addEventListener("mouseleave", ()  => this._hideTooltip());

    return chip;
  }

  /* ── Detail panel ─────────────────────────────────────────────────────── */

  _showDetail(drive, wrapper) {
    wrapper.innerHTML = "";
    const hass = this._hass;

    const wrap = document.createElement("div");
    wrap.className = "detail-wrap";

    const panel = document.createElement("div");
    panel.className = "detail-panel";

    /* Panel header */
    panel.innerHTML = `
      <div class="detail-header">
        <div>
          <div class="detail-drive-name">${this._esc(drive.name)}</div>
          ${drive.model ? `<div class="detail-drive-model">${this._esc(drive.model)}</div>` : ""}
        </div>
        <span class="detail-close" id="ss-close">✕</span>
      </div>`;

    /* Stats grid — Attention + Health always first, then diagnostics */
    const grid = document.createElement("div");
    grid.className = "detail-grid";

    grid.appendChild(this._detailStat(
      "Attention Needed",
      this._attnLabel(drive.attention),
      this._attnClass(drive.attention),
    ));
    grid.appendChild(this._detailStat(
      "Health",
      this._healthLabel(drive.health),
      this._healthClass(drive.health),
    ));

    for (const row of DIAG_ROWS) {
      const entityId = drive.entities[row.key];
      if (!entityId) continue;
      const state = this._state(hass, entityId);
      if (state === null) continue;
      const unit = row.unit || this._attr(hass, entityId, "unit_of_measurement") || "";
      const val  = unit ? `${state} ${unit}` : state;
      grid.appendChild(this._detailStat(row.label, val, this._diagClass(row.key, state)));
    }

    panel.appendChild(grid);
    wrap.appendChild(panel);
    wrapper.appendChild(wrap);

    /* Close button */
    wrapper.querySelector("#ss-close")?.addEventListener("click", (e) => {
      e.stopPropagation();
      this._selected = null;
      wrapper.innerHTML = "";
      this.shadowRoot.querySelectorAll(".drive-chip.active")
        .forEach(el => el.classList.remove("active"));
    });
  }

  _detailStat(label, value, cssClass) {
    const el = document.createElement("div");
    el.className = "detail-stat";
    el.innerHTML = `
      <div class="detail-stat-label">${this._esc(label)}</div>
      <div class="detail-stat-val ${cssClass}">${this._esc(String(value))}</div>`;
    return el;
  }

  /* ── Floating tooltip ─────────────────────────────────────────────────── */

  _showTooltip(e, drive) {
    const t    = this._tooltip;
    const hass = this._hass;
    if (!t) return;

    const poh  = this._state(hass, drive.entities.power_on_hours);
    const pcc  = this._state(hass, drive.entities.power_cycle_count);
    const smart= this._state(hass, drive.entities.smart_status);

    t.innerHTML = `
      <div class="tooltip-drive">${this._esc(drive.name)}</div>
      <div class="tooltip-row">
        <span class="tooltip-key">Attention</span>
        <span class="tooltip-val ${this._attnClass(drive.attention)}">${this._attnLabel(drive.attention)}</span>
      </div>
      <div class="tooltip-row">
        <span class="tooltip-key">Health</span>
        <span class="tooltip-val ${this._healthClass(drive.health)}">${this._healthLabel(drive.health)}</span>
      </div>
      ${drive.temp ? `
      <div class="tooltip-row">
        <span class="tooltip-key">Temperature</span>
        <span class="tooltip-val">${this._esc(drive.temp)}</span>
      </div>` : ""}
      ${poh != null ? `
      <div class="tooltip-row">
        <span class="tooltip-key">Power-On Hours</span>
        <span class="tooltip-val">${poh} h</span>
      </div>` : ""}
      ${pcc != null ? `
      <div class="tooltip-row">
        <span class="tooltip-key">Power Cycles</span>
        <span class="tooltip-val">${pcc}</span>
      </div>` : ""}
      ${smart != null ? `
      <div class="tooltip-row">
        <span class="tooltip-key">SMART</span>
        <span class="tooltip-val ${smart.toLowerCase() === "passed" ? "ok" : "crit"}">${this._esc(smart)}</span>
      </div>` : ""}
      ${drive.model ? `
      <div class="tooltip-row" style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,.08)">
        <span class="tooltip-key" style="font-size:10px">${this._esc(drive.model)}</span>
      </div>` : ""}
      <div class="tooltip-hint">↖ Click for full diagnostics</div>`;

    t.classList.add("visible");
    this._moveTooltip(e);
  }

  _moveTooltip(e) {
    const t = this._tooltip;
    if (!t?.classList.contains("visible")) return;
    const margin = 14;
    let x = e.clientX + margin;
    let y = e.clientY + margin;
    /* Flip if tooltip would overflow the viewport */
    const r = t.getBoundingClientRect();
    if (x + r.width  > window.innerWidth)  x = e.clientX - r.width  - margin;
    if (y + r.height > window.innerHeight) y = e.clientY - r.height - margin;
    t.style.left = `${x}px`;
    t.style.top  = `${y}px`;
  }

  _hideTooltip() {
    this._tooltip?.classList.remove("visible");
  }

  /* ── Badge & classification helpers ──────────────────────────────────── */

  _attnLabel(a) {
    return { no: "NO", maybe: "MAYBE", yes: "YES !", unsupported: "N/A" }[a]
        || (a || "—").toUpperCase();
  }
  _attnClass(a) {
    return { no: "ok", maybe: "warn", yes: "crit", unsupported: "unknown" }[a] || "unknown";
  }
  _attnBadge(a) {
    const cls   = this._attnClass(a);
    const label = this._attnLabel(a);
    return `<span class="badge ${cls}"><span class="badge-dot"></span>Attn: ${label}</span>`;
  }

  _healthLabel(h) {
    return { ok: "OK", problem: "Problem", unknown: "Unknown" }[h] || (h || "—");
  }
  _healthClass(h) {
    return { ok: "ok", problem: "crit", unknown: "unknown" }[h] || "unknown";
  }
  _healthBadge(h) {
    const cls   = this._healthClass(h);
    const label = this._healthLabel(h);
    return `<span class="badge ${cls}"><span class="badge-dot"></span>${label}</span>`;
  }

  /**
   * Colour-code a diagnostic value.
   * Zero is healthy for error counters; high values are bad for wear/spares.
   */
  _diagClass(key, value) {
    const n = parseFloat(value);
    if (isNaN(n)) {
      /* String values */
      const v = String(value).toLowerCase();
      if (v === "passed" || v === "ok")     return "ok";
      if (v === "failed" || v === "problem") return "crit";
      return "neutral";
    }

    const errorKeys = [
      "reallocated_sector_count", "reported_uncorrectable_errors",
      "pending_sector_count", "reallocated_event_count",
      "spin_retry_count", "command_timeout",
    ];
    if (errorKeys.includes(key)) {
      if (n === 0) return "ok";
      return n > 5 ? "crit" : "warn";
    }
    if (key === "percentage_used") {
      if (n >= 90) return "crit";
      if (n >= 70) return "warn";
      return "ok";
    }
    if (key === "available_spare") {
      if (n < 10) return "crit";
      if (n < 25) return "warn";
      return "ok";
    }
    return "neutral";
  }

  /* ── Utility ──────────────────────────────────────────────────────────── */

  _esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Visual config editor (shown in the Lovelace card editor UI)
 ═══════════════════════════════════════════════════════════════════════════ */
class SmartSnifferCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass   = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (this._config) this._render();
  }

  setConfig(config) {
    this._config = { title: "Drive Health", columns: 2, show_ok: true, drives: [], ...config };
    if (this._hass) this._render();
  }

  _render() {
    const c    = this._config;
    const hass = this._hass;

    /* Discover drives for the filter checklist */
    const driveOptions = [];
    if (hass?.entities && hass?.devices) {
      const seen = new Set();
      for (const [, entry] of Object.entries(hass.entities)) {
        if ((entry.platform || "").toLowerCase() !== DOMAIN) continue;
        const devId = entry.device_id;
        if (devId && !seen.has(devId)) {
          seen.add(devId);
          const dev = hass.devices[devId] || {};
          driveOptions.push({ id: devId, name: dev.name || devId });
        }
      }
      driveOptions.sort((a, b) => a.name.localeCompare(b.name));
    }

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; font-family: var(--ha-font-family, Roboto, sans-serif); }
        .field { margin-bottom: 16px; }
        label  { display: block; font-size: 12px; color: var(--secondary-text-color); margin-bottom: 5px; font-weight: 500; }
        input[type="text"], input[type="number"], select {
          width: 100%; padding: 8px 10px;
          border: 1px solid var(--divider-color, #ddd);
          border-radius: 6px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
          font-size: 14px;
        }
        .check-row { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
        .check-row label { margin: 0; font-size: 14px; color: var(--primary-text-color); font-weight: normal; }
        .drive-list {
          border: 1px solid var(--divider-color, #ddd);
          border-radius: 6px; max-height: 170px; overflow-y: auto;
        }
        .drive-item {
          display: flex; align-items: center; gap: 8px;
          padding: 7px 10px; font-size: 13px;
          border-bottom: 1px solid var(--divider-color, #ddd);
          cursor: pointer;
        }
        .drive-item:last-child { border-bottom: none; }
        .drive-item:hover { background: rgba(0,0,0,.04); }
        .hint { font-size: 11px; color: var(--secondary-text-color); margin-top: 5px; }
      </style>

      <div class="field">
        <label>Card Title</label>
        <input type="text" data-key="title" value="${this._esc(c.title || "Drive Health")}">
      </div>

      <div class="field">
        <label>Columns (drives per row)</label>
        <select data-key="columns">
          ${[1, 2, 3, 4].map(n =>
            `<option value="${n}" ${parseInt(c.columns) === n ? "selected" : ""}>${n}</option>`
          ).join("")}
        </select>
      </div>

      <div class="check-row">
        <input type="checkbox" id="ed-show-ok" data-key="show_ok" ${c.show_ok !== false ? "checked" : ""}>
        <label for="ed-show-ok">Show healthy (Attention: NO) drives</label>
      </div>

      <div class="field">
        <label>Filter Drives</label>
        ${driveOptions.length === 0
          ? `<div class="hint">No drives discovered yet — install the SMART Sniffer integration and start an agent first.</div>`
          : `<div class="drive-list">
              ${driveOptions.map(d => `
                <label class="drive-item">
                  <input type="checkbox" data-drive-id="${this._esc(d.id)}"
                    ${(c.drives || []).includes(d.id) ? "checked" : ""}>
                  <span>${this._esc(d.name)}</span>
                </label>`).join("")}
             </div>
             <div class="hint">Leave all unchecked to show every drive.</div>`
        }
      </div>`;

    /* Wire up scalar field changes */
    this.shadowRoot.querySelectorAll("[data-key]").forEach(el => {
      el.addEventListener("change", () => {
        const key   = el.dataset.key;
        const value = el.type === "checkbox" ? el.checked
                    : el.type === "number"   ? Number(el.value)
                    : el.value;
        this._config = { ...this._config, [key]: value };
        this._fire();
      });
    });

    /* Wire up drive filter checkboxes */
    this.shadowRoot.querySelectorAll("[data-drive-id]").forEach(el => {
      el.addEventListener("change", () => {
        const selected = [];
        this.shadowRoot.querySelectorAll("[data-drive-id]:checked")
          .forEach(cb => selected.push(cb.dataset.driveId));
        this._config = { ...this._config, drives: selected };
        this._fire();
      });
    });
  }

  _fire() {
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config: this._config },
      bubbles: true, composed: true,
    }));
  }

  _esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
}

/* ─── Register ────────────────────────────────────────────────────────────── */
customElements.define("smart-sniffer-card",        SmartSnifferCard);
customElements.define("smart-sniffer-card-editor", SmartSnifferCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type:        "smart-sniffer-card",
  name:        "SMART Sniffer Card",
  description: `Drive health dashboard for the SMART Sniffer integration (v${VERSION})`,
  preview:     false,
});

console.info(
  `%c SMART-SNIFFER-CARD %c v${VERSION} `,
  "background:#1d4ed8;color:#fff;font-weight:700;padding:2px 4px;border-radius:3px 0 0 3px",
  "background:#0d1117;color:#58a6ff;font-weight:500;padding:2px 4px;border-radius:0 3px 3px 0"
);
