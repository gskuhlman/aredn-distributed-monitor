/**
 * Node page Services table (service_check.py): each advertised service with
 * its latest check and its alerts. An admin can silence a service's alerts
 * for a while (1 h .. 1 month, or until enabled again) or ignore it (no
 * checks, no alerts) and enable it again. The buttons show for everyone;
 * without a login they open the login box first.
 */
const NodeServices = {
    services: [],
    checks: {},
    mutes: {},
    admin: false,
    loggedIn: false,
    SILENCE: [[1, '1 hour'], [6, '6 hours'], [24, '1 day'], [168, '1 week'], [720, '1 month'], ['', 'Until enabled again']],

    async render(services) {
        if (services) this.services = services;
        await Promise.all([this.loadChecks(), this.loadAuth()]);
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

    async loadAuth() {
        const s = typeof Auth !== 'undefined' ? await Auth.status() : null;
        this.loggedIn = !!s?.logged_in;
        this.admin = !s || !s.enabled || this.loggedIn;
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

    alertCell(m) {
        if (!m) return '<span class="svc-alerts svc-alerts-on">on</span>';
        if (m.mode === 'ignore') return '<span class="svc-alerts svc-alerts-ignored">ignored</span>';
        return `<span class="svc-alerts svc-alerts-silenced">silenced</span> <small>${
            m.until ? `until ${this.esc(m.until.slice(0, 16))}` : 'until enabled again'}</small>`;
    },

    actionCell(name, m) {
        const n = this.esc(name);
        if (m) return `<button class="btn btn-small btn-secondary" data-svc-act="enable" data-svc="${n}">Enable</button>`;
        return `<button class="btn btn-small btn-secondary" data-svc-act="silence" data-svc="${n}">Silence</button>
            <button class="btn btn-small btn-secondary" data-svc-act="ignore" data-svc="${n}"
                title="Stop checking this service and never alert on it">Ignore</button>`;
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
        const rows = this.services.map(s => {
            const c = this.checks[s.name];
            const m = this.mutes[s.name] || null;
            return `<tr data-svc-row="${this.esc(s.name)}">
                <td>${s.link ? `<a href="${this.esc(s.link)}" target="_blank" rel="noopener">${this.esc(s.name)}</a>` : this.esc(s.name)}</td>
                <td>${this.esc(s.protocol || 'N/A')}</td>
                <td>${this.esc(s.ip || 'N/A')}</td>
                <td>${this.checkCell(c)}</td>
                <td>${this.alertCell(m)}</td>
                <td class="svc-actions">${this.actionCell(s.name, m)}</td>
            </tr>`;
        }).join('');
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table">
            <tr><th>Service</th><th>Protocol</th><th>IP</th><th>Check</th><th>Alerts</th><th></th></tr>
            ${rows}</table></div>
            ${this.admin ? '' : '<p class="form-hint">Silence, Ignore and Enable ask for the admin login.</p>'}
            <p class="svc-msg form-hint" hidden></p>`;
        el.querySelectorAll('[data-svc-act]').forEach(b => b.addEventListener('click', () =>
            this.act(b.dataset.svcAct, b.dataset.svc, b)));
    },

    async act(action, name, btn) {
        if (!this.admin) {
            if (typeof Auth === 'undefined' || !await Auth.prompt('Log in to silence or ignore a service')) return;
            await this.loadAuth();
            this.draw();
            btn = document.querySelector(`#node-services [data-svc-act="${action}"][data-svc="${CSS.escape(name)}"]`);
            if (!btn) return;
        }
        if (action === 'enable') return this.send('DELETE', name);
        if (action === 'ignore') return this.send('POST', name, { mode: 'ignore' });
        // Silence: ask how long, inline (no browser dialog).
        const cell = btn.closest('td');
        cell.innerHTML = `<span class="svc-silence">
            <select class="form-input svc-hours" aria-label="Silence for">
                ${this.SILENCE.map(([v, l]) => `<option value="${v}"${v === 24 ? ' selected' : ''}>${l}</option>`).join('')}
            </select>
            <button class="btn btn-small btn-primary" data-svc-ok>Silence</button>
            <button class="btn btn-small btn-secondary" data-svc-cancel>Cancel</button></span>`;
        cell.querySelector('[data-svc-cancel]').addEventListener('click', () => this.draw());
        cell.querySelector('[data-svc-ok]').addEventListener('click', () => {
            const v = cell.querySelector('.svc-hours').value;
            this.send('POST', name, { mode: 'silence', hours: v ? Number(v) : null });
        });
    },

    async send(method, name, body) {
        const node = window.NODE_NAME;
        let r;
        if (method === 'DELETE') {
            r = await fetch(`/api/services/mute?node=${encodeURIComponent(node)}&service=${encodeURIComponent(name)}`,
                { method: 'DELETE' });
        } else {
            r = await fetch('/api/services/mute', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ node, service: name, ...body })
            });
        }
        const d = await r.json().catch(() => ({}));
        await this.render();
        if (!r.ok) {
            const msg = document.querySelector('#node-services .svc-msg');
            if (msg) { msg.textContent = d.error || `Failed (${r.status})`; msg.hidden = false; }
        }
    },

    esc(v) {
        return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('auth-changed', () => { if (NodeServices.services.length) NodeServices.render(); });
