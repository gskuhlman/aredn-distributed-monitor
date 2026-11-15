/**
 * Node page Services table (service_check.py): each advertised service with
 * its latest check and its alerts, with Silence / Ignore / Enable buttons
 * (mute-controls.js; a login is asked for on click), and who it alerts
 * (user_alerts.py): the node owner by default. "Alert me" / "Stop alerting
 * me" change your own; an admin adds (+) or removes (x) anyone.
 */
const NodeServices = {
    services: [],
    checks: {},
    mutes: {},
    recips: {},
    me: null,
    admin: false,

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
        try {
            const a = await (await fetch(`/api/service-alerts?node=${encodeURIComponent(window.NODE_NAME)}`)).json();
            this.recips = a.services || {};
            this.me = a.me;
            this.admin = !!a.admin;
        } catch (e) { this.recips = {}; }
    },

    recipCell(name) {
        const list = this.recips[name] || [];
        const n = this.esc(name);
        const chips = list.map(r => `<span class="svc-recip${r.enabled ? '' : ' off'}" title="${r.owner ? 'node owner' : 'added'}${r.enabled ? '' : ', turned off'}">${
            this.esc(r.callsign.toUpperCase())}${this.admin && r.enabled ? `<button data-recip-off="${this.esc(r.callsign)}" data-svc="${n}" aria-label="Stop alerting ${this.esc(r.callsign)}">×</button>` : ''}</span>`).join('');
        const mine = list.find(r => r.callsign === this.me);
        const meOn = !!(mine && mine.enabled);
        const btn = `<button class="btn btn-small btn-secondary" data-recip-me="${meOn ? 'off' : 'on'}" data-svc="${n}">${meOn ? 'Stop alerting me' : 'Alert me'}</button>`;
        const add = this.admin ? `<button class="btn btn-small btn-secondary" data-recip-add data-svc="${n}" title="Alert another user">+</button>` : '';
        return `<span class="svc-recips">${chips || '<small>nobody</small>'} ${btn} ${add}</span>`;
    },

    async setRecip(service, callsign, enabled) {
        const body = { node: window.NODE_NAME, service, enabled };
        if (callsign) body.callsign = callsign;
        const r = await fetch('/api/service-alerts', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        const d = await r.json().catch(() => ({}));
        await this.render();
        if (!r.ok) {
            const el = document.getElementById('node-services');
            el.insertAdjacentHTML('beforeend', `<p class="form-hint sc-down">${this.esc(d.error || `Failed (${r.status})`)}</p>`);
        }
    },

    bindRecips(el) {
        if (el.dataset.recipBound) return;
        el.dataset.recipBound = '1';
        el.addEventListener('click', (e) => {
            const b = e.target.closest('[data-recip-me], [data-recip-off], [data-recip-add]');
            if (!b) return;
            const svc = b.dataset.svc;
            if (b.dataset.recipMe) return this.setRecip(svc, null, b.dataset.recipMe === 'on');
            if (b.dataset.recipOff) return this.setRecip(svc, b.dataset.recipOff, false);
            // Admin: inline callsign box (no browser dialog).
            const box = document.createElement('span');
            box.className = 'acct-inline';
            box.innerHTML = `<input class="form-input" size="8" placeholder="Callsign"><button class="btn btn-small btn-primary">Add</button>`;
            b.replaceWith(box);
            const input = box.querySelector('input');
            input.focus();
            const go = () => input.value.trim() && this.setRecip(svc, input.value.trim(), true);
            box.querySelector('button').addEventListener('click', go);
            input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') go(); if (ev.key === 'Escape') this.draw(); });
        });
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
                <td>${this.recipCell(s.name)}</td>
            </tr>`;
        }).join('');
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table">
            <tr><th>Service</th><th>Protocol</th><th>IP</th><th>Check</th><th>Alerts</th><th></th><th>Alerts go to</th></tr>
            ${rows}</table></div>`;
        MuteControls.bind(el, () => this.render());
        this.bindRecips(el);
    },

    esc(v) { return MuteControls.esc(v); }
};
