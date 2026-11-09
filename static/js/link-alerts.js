/**
 * Link Alerts section on the Diagnostics tab (link_alerts.py, TODO #73):
 * alert rules on single RF links -- typically each client of a PtMP sector --
 * with SNR / LQ / rate / MCS / drop limits. Pick a sector radio to add one
 * rule per client in one go. Rules raise `link_alert` events, which the
 * "Per-link alerts" notification rule sends.
 */
const LinkAlerts = {
    initialized: false,

    init() {
        if (this.initialized || !document.getElementById('la-report')) return;
        this.initialized = true;
        document.getElementById('la-run')?.addEventListener('click', () => this.load());
        document.getElementById('la-sector-load')?.addEventListener('click', () => this.loadClients());
        document.getElementById('la-sector-add')?.addEventListener('click', () => this.addForClients());
        document.getElementById('la-report').addEventListener('click', async (event) => {
            const del = event.target.closest('[data-la-del]');
            const tog = event.target.closest('[data-la-toggle]');
            if (del) {
                await fetch(`/api/link-alerts/${del.dataset.laDel}`, { method: 'DELETE' });
                this.load();
            } else if (tog) {
                await fetch(`/api/link-alerts/${tog.dataset.laToggle}`, {
                    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: tog.dataset.enabled !== '1' })
                });
                this.load();
            }
        });
    },

    async load() {
        this.init();
        const el = document.getElementById('la-report');
        try {
            const d = await (await fetch('/api/link-alerts')).json();
            const rules = d.rules;
            el.innerHTML = rules.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Link (radio hearing client)</th><th>Alert when</th><th>Now</th><th>State</th><th></th></tr></thead>
                <tbody>${rules.map(r => `<tr${r.active ? ' class="rel-row rel-row-adjacent"' : ''}>
                    <td>${this.esc(r.source)} hearing ${this.esc(r.target)}${r.note ? `<div class="form-hint">${this.esc(r.note)}</div>` : ''}</td>
                    <td>${this.esc(LinkProm.describe(r))} <small class="form-hint">(${this.esc(r.severity)})</small></td>
                    <td>${this.now(r.values)}</td>
                    <td>${!r.enabled ? '<span class="form-hint">off</span>' : r.active ? `<strong class="sc-down">alerting</strong> since ${this.esc((r.since || '').slice(5, 16))}<div class="form-hint">${this.esc(r.detail || '')}</div>`
                        : r.last_check ? '<span class="sc-up">ok</span>' : '<span class="form-hint">not checked yet</span>'}</td>
                    <td><button class="btn btn-small btn-secondary" data-la-toggle="${r.id}" data-enabled="${r.enabled ? 1 : 0}">${r.enabled ? 'Disable' : 'Enable'}</button>
                        <button class="btn btn-small btn-secondary" data-la-del="${r.id}">Remove</button></td></tr>`).join('')}</tbody></table></div>`
                : '<p class="log-empty">No per-link alert rules yet. Add them for a sector below, or from a link\'s diagnosis on the Links page.</p>';
            this.setStatus(`${rules.length} rule(s), ${rules.filter(r => r.active).length} alerting · checked after every scan`);
            this.fillSectors();
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Failed to load.</p>';
        }
    },

    now(v) {
        if (!v || !Object.keys(v).length) return '-';
        if (!v.present) return '<span class="sc-down">not reported</span>';
        const parts = [];
        if (v.snr != null) parts.push(`SNR ${v.snr}`);
        if (v.quality != null) parts.push(`LQ ${v.quality}%`);
        if (v.rate_mbps != null) parts.push(`${v.rate_mbps} Mbps`);
        else if (v.lqm_rate_mbps != null) parts.push(`${v.lqm_rate_mbps} Mbps`);
        if (v.mcs != null) parts.push(`MCS ${v.mcs}`);
        if (v.flaps) parts.push(`${v.flaps} drop(s)`);
        return this.esc(parts.join(' · '));
    },

    async fillSectors() {
        const sel = document.getElementById('la-sector');
        if (!sel || sel.options.length > 1) return;
        const names = new Set();
        try {
            const infra = await (await fetch('/api/nodes/infrastructure')).json();
            Object.keys(infra.infrastructure_nodes || {}).forEach(n => names.add(n));
            const d = await (await fetch('/api/reports/airtime?hours=24')).json();
            (d.sectors || []).map(s => s.name).filter(Boolean).forEach(n => names.add(n));
        } catch (e) { /* the box still accepts a typed name */ }
        [...names].sort().forEach(n => sel.add(new Option(n, n)));
    },

    async loadClients() {
        const node = document.getElementById('la-sector').value || document.getElementById('la-sector-name').value.trim();
        const box = document.getElementById('la-clients');
        if (!node) return;
        const d = await (await fetch(`/api/link-alerts/clients/${encodeURIComponent(node)}`)).json();
        box.dataset.node = d.node;
        box.innerHTML = d.clients.length ? d.clients.map(c => `<label class="la-client"><input type="checkbox" value="${this.esc(c.peer)}" checked>
            ${this.esc(c.peer)} <small class="form-hint">SNR ${c.snr ?? '-'} · LQ ${c.quality ?? '-'}%</small></label>`).join('')
            : '<p class="form-hint">No current RF clients.</p>';
    },

    async addForClients() {
        const box = document.getElementById('la-clients');
        const node = box.dataset.node;
        const peers = [...box.querySelectorAll('input:checked')].map(i => i.value);
        if (!node || !peers.length) return;
        const num = id => { const v = document.getElementById(id).value; return v === '' ? null : Number(v); };
        for (const peer of peers) {
            await fetch('/api/link-alerts', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source: node, target: peer, min_snr: num('la-snr'), min_quality: num('la-lq'),
                                       min_rate_mbps: num('la-rate'), max_flaps: num('la-flaps'),
                                       alert_missing: document.getElementById('la-missing').checked,
                                       severity: document.getElementById('la-sev').value })
            });
        }
        this.load();
    },

    setStatus(text) {
        const el = document.getElementById('la-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => LinkAlerts.init());
