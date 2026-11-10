/**
 * Status page "needs attention" tiles: one line of counts from the checks that
 * already run (starting node, Mesh Health, Link Watch, Route Watch, radio
 * alarms over SSH / Prometheus, services, phones, new nodes, maintenance).
 * Each tile opens the section or page with the details. Read-only; reloads
 * when the Status page is opened and every two minutes while it is shown.
 */
const StatusTiles = {
    REFRESH_MS: 120000,
    health: null,
    uptime: null,
    last: null,
    loading: false,

    init() {
        if (!document.getElementById('status-tiles')) return;
        document.addEventListener('mesh-health-loaded', (event) => { this.health = event.detail; this.load(); });
        document.addEventListener('uptime-loaded', (event) => {
            this.uptime = event.detail;
            if (this.last) this.render(this.last);
        });
        document.querySelector('.tab-btn[data-tab="status"]')?.addEventListener('click', () => this.load());
        setInterval(() => { if (this.visible()) this.load(); }, this.REFRESH_MS);
        document.getElementById('status-tiles').addEventListener('click', (event) => {
            const tile = event.target.closest('[data-go]');
            if (tile) this.go(tile.dataset.go);
        });
        if (this.visible()) this.load();
    },

    visible() {
        return document.getElementById('status-view')?.classList.contains('active');
    },

    async json(url) {
        try {
            const resp = await fetch(url);
            return resp.ok ? await resp.json() : null;
        } catch (e) {
            return null;
        }
    },

    async load() {
        if (this.loading) return;
        this.loading = true;
        try {
            const hours = (typeof GlobalFilter !== 'undefined') ? GlobalFilter.hours() : 24;
            const [collector, oneWay, noise, routes, ssh, prom, services, phones, welcome, maint] = await Promise.all([
                this.json('/api/collector/health?hours=1'),
                this.json(`/api/reports/one-way?hours=${hours}`),
                this.json('/api/reports/noise?hours=72&days=7'),
                this.json(`/api/routes?hours=${hours}`),
                this.json('/api/ssh/status'),
                this.json('/api/prometheus/alarms'),
                this.json('/api/services/checks'),
                this.json(`/api/voip/phones?hours=${hours}`),
                this.json('/api/welcome'),
                this.json('/api/maintenance/active'),
            ]);
            this.render({ collector, oneWay, noise, routes, ssh, prom, services, phones, welcome, maint });
        } finally {
            this.loading = false;
        }
    },

    render(d) {
        this.last = d;
        const tiles = [];
        const add = (label, value, level, go, title) => tiles.push({ label, value, level, go, title });
        const count = (n) => (n == null ? 'n/a' : n);
        const level = (n, crit) => (n == null ? 'unknown' : n === 0 ? 'ok' : crit ? 'crit' : 'warn');

        const state = d.collector?.summary?.state;
        add('Starting node', state ? (state === 'up' ? 'Answering' : state.replace(/_/g, ' ')) : 'n/a',
            !state ? 'unknown' : state === 'up' ? 'ok' : 'crit', 'reports',
            'Path from this PC to the starting node (Reports, Starting Node)');

        const sev = this.health?.summary?.by_severity;
        add('Critical findings', sev ? sev.critical || 0 : '…', sev ? level(sev.critical || 0, true) : 'unknown',
            'section:Mesh Health Check', 'Mesh Health Check, critical');
        add('Warnings', sev ? sev.warning || 0 : '…', sev ? level(sev.warning || 0) : 'unknown',
            'section:Mesh Health Check', 'Mesh Health Check, warnings');

        const up = this.uptime?.summary;
        add('Nodes below 99% up', up ? up.below_99 : '…', up ? level(up.below_99) : 'unknown',
            'section:Node Uptime', up ? `Average uptime ${up.average_pct ?? '-'}% (Node Uptime)` : 'Node Uptime');

        const oneWay = d.oneWay ? (d.oneWay.active || []).length : null;
        add('One-way links', count(oneWay), level(oneWay), 'section:Link Watch', 'Link Watch one-way alarms');
        const noisy = d.noise ? (d.noise.radios || []).filter(r => r.alarm).length : null;
        add('Noise alarms', count(noisy), level(noisy), 'section:Link Watch', 'Link Watch noise-floor alarms');

        // Site-to-site paths only: the starting node's own paths are context.
        const routes = d.routes ? (d.routes.paths || []).filter(p => p.enabled && p.role !== 'access'
            && (p.status === 'detour' || p.flapping)).length : null;
        add('Route problems', count(routes), level(routes), 'routing/route-watch',
            'Site-to-site Route Watch paths on a detour or switching routes often');

        const radio = (d.ssh || d.prom)
            ? (d.ssh?.alarms || []).filter(a => a.active).length + (d.prom?.alarms || []).length : null;
        add('Radio alarms', count(radio), level(radio), 'section:Mesh Health Check',
            'Active SSH radio and Prometheus resource alarms (listed in Mesh Health)');

        const services = d.services ? d.services.counts?.alarm || 0 : null;
        add('Services down', count(services), level(services), 'section:Service Checks', 'Service Checks');
        const phones = d.phones?.enabled === false ? null : d.phones ? d.phones.counts?.alarm || 0 : null;
        add('Phones down', count(phones), level(phones), 'voip', 'Phone Connectivity (VoIP & Phones)');

        const fresh = d.welcome ? (d.welcome.welcomes || []).length : null;
        add('New nodes', count(fresh), fresh ? 'info' : fresh === 0 ? 'ok' : 'unknown', 'section:New Nodes',
            'Nodes discovered in the last few days');
        const maint = d.maint ? (d.maint.active || []).length : null;
        add('Maintenance', maint == null ? 'n/a' : maint ? `${maint} active` : 'None', maint ? 'info' : maint === 0 ? 'ok' : 'unknown',
            'maintenance', 'Maintenance windows');

        document.getElementById('status-tiles').innerHTML = tiles.map(t =>
            `<button type="button" class="rpt-tile status-tile st-${t.level}" data-go="${this.esc(t.go)}" title="${this.esc(t.title)}">` +
            `<span class="rpt-tile-label">${this.esc(t.label)}</span>` +
            `<span class="rpt-tile-value">${this.esc(String(t.value))}</span></button>`).join('');
    },

    /** `section:<title>` scrolls to (and opens) a Status section; anything else is a page path. */
    go(where) {
        if (where.startsWith('section:')) {
            const title = where.slice(8);
            const h3 = [...document.querySelectorAll('#status-view .diag-section > h3')]
                .find(h => h.textContent.trim().startsWith(title));
            if (!h3) return;
            if (h3.parentElement.classList.contains('collapsed')) h3.click();
            h3.scrollIntoView({ block: 'start' });
            return;
        }
        location.hash = `#/${where}`;
    },

    esc(value) {
        return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
};

document.addEventListener('DOMContentLoaded', () => setTimeout(() => StatusTiles.init(), 0));
