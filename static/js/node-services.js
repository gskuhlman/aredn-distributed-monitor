/**
 * Node page Services table (service_check.py): each advertised service with
 * its latest check and its alerts, with Silence / Ignore / Enable buttons
 * (mute-controls.js; a login is asked for on click).
 */
const NodeServices = {
    services: [],
    checks: {},
    mutes: {},

    async render(services) {
        if (services) this.services = services;
        await this.loadChecks();
        this.draw();
    },

    async loadChecks() {
        try {
            const d = await (await fetch('/api/services/checks')).json();
            const node = window.NODE_NAME;
            this.checks = {};
            this.mutes = {};
            for (const s of d.services || []) if (s.node_name === node) this.checks[s.service_name] = s;
            for (const m of d.mutes || []) if (m.node_name === node) this.mutes[m.service_name] = m;
        } catch (e) { /* the table still shows the services */ }
    },

    checkCell(c) {
        if (!c) return '<span class="form-hint">not checked</span>';
        if (c.mute?.mode === 'ignore') return '<span class="form-hint">not checked (ignored)</span>';
        if (c.ok == null) {
            const why = { external: 'outside mesh', none: 'not probed' }[c.kind] || 'not checked yet';
            return `<span class="form-hint">${this.esc(why)}</span>`;
        }
        const pct = c.availability_pct != null ? ` <small>(${c.availability_pct}%)</small>` : '';
        return c.ok
            ? `<strong class="sc-up">answers</strong>${c.last_ms != null ? ` <small>${c.last_ms} ms</small>` : ''}${pct}`
            : `<strong class="sc-down">${this.esc(c.last_error || 'not answering')}</strong>${pct}`;
    },

    draw() {
        const el = document.getElementById('node-services');
        if (!el) return;
        const extra = document.getElementById('node-service-checks');
        if (extra) extra.innerHTML = '';
        if (!this.services.length) {
            el.innerHTML = '<p>No services recorded for this node.</p>';
            return;
        }
        const node = window.NODE_NAME;
        const rows = this.services.map(s => {
            const m = this.mutes[s.name] || null;
            return `<tr>
                <td>${s.link ? `<a href="${this.esc(s.link)}" target="_blank" rel="noopener">${this.esc(s.name)}</a>` : this.esc(s.name)}</td>
                <td>${this.esc(s.protocol || 'N/A')}</td>
                <td>${this.esc(s.ip || 'N/A')}</td>
                <td>${this.checkCell(this.checks[s.name])}</td>
                <td>${MuteControls.label(m)}</td>
                <td>${MuteControls.buttons(node, s.name, m)}</td>
            </tr>`;
        }).join('');
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table">
            <tr><th>Service</th><th>Protocol</th><th>IP</th><th>Check</th><th>Alerts</th><th></th></tr>
            ${rows}</table></div>`;
        MuteControls.bind(el, () => this.render());
    },

    esc(v) { return MuteControls.esc(v); }
};
