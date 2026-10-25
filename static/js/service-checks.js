/**
 * Service Checks section on the Diagnostics tab (service_check.py): every
 * advertised service with its probe result. An alarm needs consecutive
 * failures while the node itself answers the scanner.
 */
const ServiceChecks = {
    initialized: false,
    data: null,

    init() {
        if (this.initialized || !document.getElementById('sc-report')) return;
        this.initialized = true;
        document.getElementById('sc-refresh')?.addEventListener('click', () => this.load());
        document.getElementById('sc-run')?.addEventListener('click', () => this.run());
        document.getElementById('sc-all')?.addEventListener('change', () => this.render());
    },

    async load() {
        this.init();
        this.setStatus('Loading...');
        try {
            this.data = await (await fetch('/api/services/checks')).json();
            this.render();
        } catch (error) {
            this.setStatus('Failed to load.');
        }
    },

    async run() {
        const btn = document.getElementById('sc-run');
        btn.disabled = true;
        this.setStatus('Checking every service (up to a minute)...');
        try {
            const r = await fetch('/api/services/checks/run', { method: 'POST' });
            const d = await r.json();
            if (!r.ok) throw new Error(d.error || 'check failed');
            this.data = d;
            this.render();
        } catch (error) {
            this.setStatus(`Check failed: ${error.message}`);
        } finally {
            btn.disabled = false;
        }
    },

    render() {
        const d = this.data;
        if (!d) return;
        const all = document.getElementById('sc-all')?.checked;
        const rank = { alarm: 0, down: 1, node_down: 2, up: 3 };
        const rows = d.services.filter(s => all || ['alarm', 'down', 'node_down'].includes(s.state))
            .sort((a, b) => (rank[a.state] ?? 9) - (rank[b.state] ?? 9));
        const label = { alarm: 'alarm', down: 'failing', node_down: 'node down', up: 'up', external: 'outside mesh',
                        none: 'not probed', http: 'unchecked', tcp: 'unchecked' };
        const badge = { alarm: 'critical', down: 'warning', node_down: 'info', up: 'ok' };
        const el = document.getElementById('sc-report');
        el.innerHTML = rows.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>State</th><th>Node</th><th>Service</th><th>Link</th><th>Last result</th><th>Availability</th><th>Last OK</th></tr></thead>
            <tbody>${rows.map(s => `<tr>
                <td><span class="diag-badge infra-badge-${badge[s.state] || 'info'}">${this.esc(label[s.state] || s.state)}</span></td>
                <td><a href="/nodes/${encodeURIComponent(s.node_name)}">${this.esc(s.node_name)}</a></td>
                <td>${this.esc(s.service_name)}</td>
                <td class="sc-link">${this.esc(s.link || '')}</td>
                <td>${s.ok === 1 ? `answered${s.last_ms != null ? ` in ${s.last_ms} ms` : ''}` : this.esc(s.last_error || '-')}</td>
                <td>${s.availability_pct != null ? s.availability_pct + '%' : '-'}</td>
                <td>${this.esc(s.last_ok || '-')}</td></tr>`).join('')}</tbody></table></div>`
            : '<p class="log-empty">Every probed service answers.</p>';
        const c = d.counts;
        this.setStatus(`${c.up || 0} up, ${c.alarm || 0} alarm, ${c.down || 0} failing, ${c.node_down || 0} on down nodes, `
            + `${(c.external || 0) + (c.none || 0)} not probed · checked every ${d.interval_minutes} min`
            + (d.last_check ? ` · last ${d.last_check}` : '') + (d.enabled ? '' : ' · scheduled checks off'));
    },

    setStatus(text) {
        const el = document.getElementById('sc-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => ServiceChecks.init());
