/**
 * Maintenance windows (#22): header banner while a window is active, the
 * Maintenance section on the Diagnostics tab (list + create form), and the
 * "Start maintenance" quick action on the node page. All coverage rules live
 * in maintenance.py; this only renders /api/maintenance and posts changes.
 */
const Maintenance = {
    data: null,
    active: [],
    loaded: false,
    DURATIONS: [[60, '1 h'], [120, '2 h'], [240, '4 h'], [480, '8 h']],

    init() {
        this.refreshActive();
        setInterval(() => this.refreshActive(), 60000);
        if (typeof socket !== 'undefined' && socket) {
            socket.on('maintenance_update', d => this.setActive(d.active || []));
        }
        const section = document.getElementById('maint-section');
        if (section) {
            section.addEventListener('toggle', () => { if (section.open && !this.loaded) this.load(); });
        }
        if (window.NODE_NAME) this.renderNodeAction();
    },

    async api(url, options = {}) {
        const resp = await fetch(url, {
            headers: { 'Content-Type': 'application/json' }, ...options,
            body: options.body ? JSON.stringify(options.body) : undefined
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        return data;
    },

    // ---------------------------------------------------------- banner ----

    async refreshActive() {
        try {
            const d = await this.api('/api/maintenance/active');
            this.setActive(d.active || []);
        } catch (e) { /* banner is best effort */ }
    },

    setActive(active) {
        this.active = active;
        const el = document.getElementById('maint-banner');
        if (el) {
            if (!active.length) {
                el.hidden = true;
                el.innerHTML = '';
            } else {
                el.hidden = false;
                el.innerHTML = `<strong>&#128679; Maintenance:</strong> ` + active.map(w =>
                    `<span class="maint-banner-item">${this.esc(this.scopeText(w))}` +
                    `${w.note ? ' &ndash; ' + this.esc(w.note) : ''} ` +
                    `<small>${w.status === 'grace' ? `ended ${this.esc(this.hm(w.end_time))}, alarms quiet until ${this.esc(this.hm(w.effective_end))}`
                        : `until ${this.esc(this.hm(w.end_time))}`}</small></span>`).join(' · ') +
                    ` <small>Alarms and notifications for these nodes are held.</small>`;
            }
        }
        const badge = document.getElementById('maint-badge');
        if (badge) {
            badge.textContent = active.length ? `${active.length} active` : '';
            badge.className = 'notify-badge' + (active.length ? ' notify-muted' : '');
        }
        if (window.NODE_NAME) this.renderNodeAction();
    },

    // ------------------------------------------------- Diagnostics section --

    async load() {
        const el = document.getElementById('maint-panel');
        try {
            this.data = await this.api('/api/maintenance');
            this.loaded = true;
            this.setActive(this.data.active || []);
            this.render();
        } catch (error) {
            el.innerHTML = `<p class="log-empty">Could not load maintenance windows: ${this.esc(error.message)}</p>`;
        }
    },

    render() {
        const d = this.data;
        const sites = Object.entries(d.sites || {}).map(([code, name]) =>
            `<option value="${this.esc(code)}">${this.esc(code)} &ndash; ${this.esc(name)}</option>`).join('');
        document.getElementById('maint-panel').innerHTML = `
            <p class="form-hint">During a window, notifications, one-way and noise alarms are held for the covered
                nodes (and links touching them), failed polls count as explained in Reports, and each radio's noise
                baseline restarts afterwards. Alarms stay quiet ${d.grace_minutes} min after a window ends.</p>
            <form id="maint-form" class="maint-form">
                <label>Scope
                    <select id="mt-scope" class="form-input">
                        <option value="site">Site</option><option value="nodes">Nodes</option>
                        <option value="all">Whole mesh</option>
                    </select></label>
                <label id="mt-site-wrap">Site
                    <select id="mt-site" class="form-input">${sites}</select></label>
                <label id="mt-nodes-wrap" hidden>Nodes
                    <input type="text" id="mt-nodes" class="form-input" placeholder="node names, comma separated"></label>
                <label>Start
                    <select id="mt-when" class="form-input">
                        <option value="now">Now</option><option value="at">At...</option>
                    </select></label>
                <label id="mt-start-wrap" hidden>From <input type="datetime-local" id="mt-start" class="form-input" step="60"></label>
                <label>Length
                    <select id="mt-duration" class="form-input">
                        ${this.DURATIONS.map(([m, t]) => `<option value="${m}" ${m === 120 ? 'selected' : ''}>${t}</option>`).join('')}
                        <option value="end">Until...</option>
                    </select></label>
                <label id="mt-end-wrap" hidden>To <input type="datetime-local" id="mt-end" class="form-input" step="60"></label>
                <label class="maint-wide">Note <input type="text" id="mt-note" class="form-input" placeholder="e.g. Re-channel Collins sectors"></label>
                <label class="checkbox-label" title="Also cover nodes with a current RF link to a covered node (sector clients)">
                    <input type="checkbox" id="mt-peers" checked> Include RF clients/peers</label>
                <label class="checkbox-label"><input type="checkbox" id="mt-change"> Also add a change-log entry</label>
                <button type="submit" class="btn btn-primary">Create window</button>
                <span id="mt-status" class="form-hint"></span>
            </form>
            ${this.renderList(d.windows || [])}`;
        this.bindForm();
    },

    renderList(windows) {
        if (!windows.length) return '<p class="log-empty">No maintenance windows in the last 14 days.</p>';
        const order = { active: 0, grace: 1, upcoming: 2, past: 3 };
        windows = [...windows].sort((a, b) => (order[a.status] - order[b.status]) ||
            (a.status === 'past' ? b.start_time.localeCompare(a.start_time) : a.start_time.localeCompare(b.start_time)));
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table maint-table">
            <thead><tr><th>Status</th><th>Scope</th><th>Start</th><th>End</th><th>Note</th><th>Covers</th><th></th></tr></thead>
            <tbody>${windows.map(w => `<tr class="maint-row maint-${w.status}">
                <td><span class="maint-status maint-status-${w.status}">${w.status}</span></td>
                <td>${this.esc(this.scopeText(w))}</td>
                <td>${this.esc(w.start_time.slice(0, 16))}</td><td>${this.esc(w.end_time.slice(0, 16))}</td>
                <td>${this.esc(w.note || '')}${w.author ? ` <small>(${this.esc(w.author)})</small>` : ''}
                    ${w.change_id ? ` <a href="/#changes/${w.change_id}"><small>change</small></a>` : ''}</td>
                <td>${w.scope === 'all' ? 'all nodes' : `<span title="${this.esc(w.covered_nodes.join(', '))}">${w.covered_nodes.length} node(s)</span>`}</td>
                <td class="maint-actions">${this.actions(w)}</td></tr>`).join('')}</tbody></table></div>`;
    },

    actions(w) {
        const btn = (act, label, cls = 'btn-secondary') =>
            `<button class="btn btn-small ${cls}" data-mt-act="${act}" data-mt-id="${w.id}">${label}</button>`;
        if (w.status === 'active') return btn('end', 'End now') + btn('extend', '+1 h');
        if (w.status === 'upcoming') return btn('delete', 'Cancel', 'btn-danger');
        return btn('delete', 'Delete', 'btn-danger');
    },

    bindForm() {
        const $ = id => document.getElementById(id);
        const sync = () => {
            const scope = $('mt-scope').value;
            $('mt-site-wrap').hidden = scope !== 'site';
            $('mt-nodes-wrap').hidden = scope !== 'nodes';
            $('mt-peers').disabled = scope === 'all';
            $('mt-start-wrap').hidden = $('mt-when').value !== 'at';
            $('mt-end-wrap').hidden = $('mt-duration').value !== 'end';
        };
        ['mt-scope', 'mt-when', 'mt-duration'].forEach(id => $(id).addEventListener('change', sync));
        sync();
        $('maint-form').addEventListener('submit', async (ev) => {
            ev.preventDefault();
            const scope = $('mt-scope').value;
            const body = { scope, note: $('mt-note').value, include_rf_peers: $('mt-peers').checked,
                           change_log: $('mt-change').checked };
            if (scope === 'site') body.site = $('mt-site').value;
            if (scope === 'nodes') body.nodes = $('mt-nodes').value;
            if ($('mt-when').value === 'at') body.start = $('mt-start').value;
            if ($('mt-duration').value === 'end') body.end = $('mt-end').value;
            else body.duration_minutes = Number($('mt-duration').value);
            try {
                await this.create(body);
                $('mt-status').textContent = 'Created.';
            } catch (error) {
                $('mt-status').textContent = error.message;
            }
        });
        document.querySelectorAll('#maint-panel [data-mt-act]').forEach(b => b.addEventListener('click', async () => {
            const id = b.dataset.mtId;
            b.disabled = true;
            try {
                if (b.dataset.mtAct === 'end') await this.api(`/api/maintenance/${id}`, { method: 'PATCH', body: { end_now: true } });
                if (b.dataset.mtAct === 'extend') await this.api(`/api/maintenance/${id}`, { method: 'PATCH', body: { extend_minutes: 60 } });
                if (b.dataset.mtAct === 'delete') await this.api(`/api/maintenance/${id}`, { method: 'DELETE' });
                await this.load();
            } catch (error) {
                b.disabled = false;
                this.toast('error', 'Maintenance', error.message);
            }
        }));
    },

    async create(body) {
        const result = await this.api('/api/maintenance', { method: 'POST', body });
        this.toast('success', 'Maintenance window created', this.scopeText(result.window));
        if (document.getElementById('maint-panel')) await this.load();
        else await this.refreshActive();
        return result.window;
    },

    // ------------------------------------------------------- node page ----

    renderNodeAction() {
        const el = document.getElementById('node-maint-action');
        if (!el) return;
        const name = window.NODE_NAME;
        const mine = this.active.filter(w => w.status === 'active' && (w.scope === 'all' || (w.covered_nodes || []).includes(name)));
        if (mine.length) {
            el.innerHTML = `<span class="maint-status maint-status-active" title="${this.esc(mine.map(w => this.scopeText(w)).join(', '))}">in maintenance</span>
                <button class="btn btn-small btn-secondary" id="node-maint-end" data-id="${mine[0].id}">End maintenance</button>`;
            document.getElementById('node-maint-end').addEventListener('click', async (ev) => {
                try {
                    await this.api(`/api/maintenance/${ev.target.dataset.id}`, { method: 'PATCH', body: { end_now: true } });
                    await this.refreshActive();
                } catch (error) { this.toast('error', 'Maintenance', error.message); }
            });
            return;
        }
        if (el.dataset.open) return;
        el.innerHTML = '<button class="btn btn-small btn-secondary" id="node-maint-open">Start maintenance...</button>';
        document.getElementById('node-maint-open').addEventListener('click', () => this.openNodeForm());
    },

    async openNodeForm() {
        const el = document.getElementById('node-maint-action');
        const name = window.NODE_NAME;
        let site = null;
        try {
            const full = await this.api(`/api/nodes/full/${encodeURIComponent(name)}?hours=1&event_limit=1`);
            site = full.maintenance?.site || null;
        } catch (e) { /* node-only scope still works */ }
        el.dataset.open = '1';
        el.innerHTML = `<span class="maint-node-form">
            <select id="nm-scope" class="form-input">
                <option value="nodes">This node</option>
                ${site ? `<option value="site">Site ${this.esc(site)}</option>` : ''}
            </select>
            <select id="nm-duration" class="form-input">
                ${this.DURATIONS.map(([m, t]) => `<option value="${m}" ${m === 120 ? 'selected' : ''}>${t}</option>`).join('')}
            </select>
            <input type="text" id="nm-note" class="form-input" placeholder="Note">
            <label class="header-checkbox"><input type="checkbox" id="nm-change"> Change log</label>
            <button class="btn btn-small btn-primary" id="nm-go">Start</button>
            <button class="btn btn-small btn-secondary" id="nm-cancel">Cancel</button></span>`;
        document.getElementById('nm-cancel').addEventListener('click', () => {
            delete el.dataset.open;
            this.renderNodeAction();
        });
        document.getElementById('nm-go').addEventListener('click', async () => {
            const scope = document.getElementById('nm-scope').value;
            const body = { scope, duration_minutes: Number(document.getElementById('nm-duration').value),
                           note: document.getElementById('nm-note').value,
                           change_log: document.getElementById('nm-change').checked };
            if (scope === 'site') body.site = site; else body.nodes = [name];
            try {
                await this.create(body);
                delete el.dataset.open;
                this.renderNodeAction();
            } catch (error) { this.toast('error', 'Maintenance', error.message); }
        });
    },

    // --------------------------------------------------------- helpers ----

    scopeText(w) {
        if (w.scope === 'all') return 'Whole mesh';
        if (w.scope === 'site') return `Site ${w.site}` + (w.include_rf_peers ? ' + RF clients' : '');
        const nodes = w.nodes || [];
        return nodes.slice(0, 3).join(', ') + (nodes.length > 3 ? ` +${nodes.length - 3}` : '') +
            (w.include_rf_peers ? ' + RF peers' : '');
    },

    hm(ts) {
        return ts ? ts.slice(11, 16) : '';
    },

    toast(type, title, message) {
        if (typeof showToast === 'function') showToast(type, title, message);
        else if (typeof NodePage !== 'undefined' && NodePage.showToast) NodePage.showToast(type, title, message);
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => Maintenance.init());
