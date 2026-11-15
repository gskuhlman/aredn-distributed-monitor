/**
 * Account & Alerts page (/account; users.py, user_alerts.py): log in with
 * callsign + password, set your alert destinations, infrastructure-team
 * choices and which services alert you, change your password. Admins also
 * get the user list: set / remove passwords, grant admin power, put users on
 * the infrastructure team, add users that own no node.
 */
const Account = {
    status: null,
    me: null,
    users: [],
    groups: {},

    esc(v) {
        return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    async json(url, method = 'GET', body) {
        const r = await fetch(url, body === undefined ? { method } : {
            method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || `Failed (${r.status})`);
        return d;
    },

    async init() {
        document.getElementById('acct-login-form').addEventListener('submit', (e) => { e.preventDefault(); this.login(); });
        document.getElementById('acct-logout').addEventListener('click', async () => { await Auth.logout(); this.load(); });
        document.getElementById('me-save').addEventListener('click', () => this.saveMe());
        document.getElementById('me-test').addEventListener('click', () => this.test());
        document.getElementById('me-pw-form').addEventListener('submit', (e) => { e.preventDefault(); this.changePassword(); });
        document.getElementById('me-services').addEventListener('change', (e) => {
            const box = e.target.closest('[data-svc-node]');
            if (box) this.setService(box.dataset.svcNode, box.dataset.svcName, box.checked, box);
        });
        document.getElementById('adm-filter').addEventListener('input', () => this.drawUsers());
        document.getElementById('adm-add').addEventListener('click', () => this.addUser());
        const list = document.getElementById('adm-users');
        list.addEventListener('change', (e) => {
            const el = e.target.closest('[data-flag]');
            if (el) this.setFlag(el.dataset.cs, el.dataset.flag, el.checked, el);
        });
        list.addEventListener('click', (e) => {
            const b = e.target.closest('[data-act]');
            if (b) this.userAction(b.dataset.act, b.dataset.cs, b);
        });
        document.addEventListener('auth-changed', () => this.load());
        await this.load();
    },

    async load() {
        this.status = await Auth.status() || {};
        const s = this.status;
        const cs = s.callsign;
        document.getElementById('acct-who').textContent = s.logged_in
            ? `Logged in as ${s.user}${s.is_admin ? ' (admin)' : ''}` : 'Not logged in';
        document.getElementById('acct-logout').style.display = s.logged_in ? '' : 'none';
        const login = document.getElementById('acct-login');
        login.hidden = !!cs;
        login.querySelector('.form-hint').innerHTML = s.logged_in && !cs
            ? 'You are logged in with the admin password. Log in with your callsign to set up your own alerts.'
            : login.querySelector('.form-hint').innerHTML;
        document.getElementById('acct-me').hidden = !cs;
        document.getElementById('acct-admin').hidden = !(s.is_admin && s.logged_in) && !(s.enabled === false);
        if (cs) await this.loadMe();
        if (!document.getElementById('acct-admin').hidden) await this.loadUsers();
    },

    async login() {
        const msg = document.getElementById('acct-login-msg');
        msg.textContent = '';
        const r = await window.__authFetch('/api/auth/login', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user: document.getElementById('acct-cs').value.trim(),
                                   password: document.getElementById('acct-pw').value })
        });
        const d = await r.json().catch(() => ({}));
        document.getElementById('acct-pw').value = '';
        if (!r.ok) { msg.textContent = d.error || 'Login failed'; return; }
        document.dispatchEvent(new CustomEvent('auth-changed', { detail: d }));
    },

    // ------------------------------------------------------------ my settings
    async loadMe() {
        try {
            const d = await this.json('/api/me');
            this.me = d.user;
            this.groups = d.groups;
            const u = d.user;
            document.getElementById('acct-me-cs').textContent = `${u.callsign.toUpperCase()} · ${u.nodes.length} node${u.nodes.length === 1 ? '' : 's'}`;
            document.getElementById('me-name').value = u.name || '';
            document.getElementById('me-email').value = u.email;
            document.getElementById('me-ntfy').value = u.ntfy_topic;
            document.getElementById('me-enabled').checked = u.alerts_enabled;
            document.getElementById('me-team').innerHTML = u.infra_team ? `<div class="acct-team">
                <h3>Infrastructure team</h3>
                <p class="form-hint">You get alerts for infrastructure nodes in these groups:</p>
                <div class="acct-groups">${Object.entries(this.groups).map(([g, label]) => `<label class="checkbox-label">
                    <input type="checkbox" data-group="${this.esc(g)}"${u.infra_groups.includes(g) ? ' checked' : ''}> ${this.esc(label)}</label>`).join('')}</div>
                <label class="form-hint">Problems at or above <select id="me-sev" class="form-input">
                    ${['info', 'warning', 'critical'].map(v => `<option${v === u.infra_min_severity ? ' selected' : ''}>${v}</option>`).join('')}
                </select> (recoveries follow the problems you were sent)</label></div>`
                : '<p class="form-hint">Not on the infrastructure team (an admin adds people to it).</p>';
            const svc = d.services;
            document.getElementById('me-services').innerHTML = svc.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Alert me</th><th>Node</th><th>Service</th><th></th></tr></thead><tbody>
                ${svc.map(s => `<tr><td><input type="checkbox" data-svc-node="${this.esc(s.node)}" data-svc-name="${this.esc(s.service)}"${s.enabled ? ' checked' : ''}></td>
                    <td><a href="/nodes/${encodeURIComponent(s.node)}#node-services">${this.esc(s.node)}</a></td>
                    <td>${this.esc(s.service)}</td><td><small>${s.owner ? 'your node' : 'added'}</small></td></tr>`).join('')}
                </tbody></table></div>` : '<p class="log-empty">No services alert you yet.</p>';
        } catch (e) {
            document.getElementById('me-msg').textContent = e.message;
        }
    },

    async saveMe() {
        const msg = document.getElementById('me-msg');
        const body = {
            name: document.getElementById('me-name').value.trim(),
            email: document.getElementById('me-email').value.trim(),
            ntfy_topic: document.getElementById('me-ntfy').value.trim(),
            alerts_enabled: document.getElementById('me-enabled').checked,
        };
        if (this.me?.infra_team) {
            body.infra_groups = [...document.querySelectorAll('#me-team [data-group]:checked')].map(b => b.dataset.group);
            body.infra_min_severity = document.getElementById('me-sev').value;
        }
        try {
            await this.json('/api/me', 'PATCH', body);
            msg.textContent = 'Saved.';
            this.loadMe();
        } catch (e) { msg.textContent = e.message; }
    },

    async test() {
        const msg = document.getElementById('me-msg');
        msg.textContent = 'Sending…';
        try {
            const d = await this.json('/api/me/test', 'POST', {});
            msg.textContent = Object.entries(d).map(([ch, r]) => `${ch}: ${r}`).join(' · ');
        } catch (e) { msg.textContent = e.message; }
    },

    async changePassword() {
        const msg = document.getElementById('me-pw-msg');
        try {
            await this.json('/api/me/password', 'POST', {
                current: document.getElementById('me-pw-cur').value, new: document.getElementById('me-pw-new').value });
            msg.textContent = 'Password changed.';
        } catch (e) { msg.textContent = e.message; }
        document.getElementById('me-pw-cur').value = '';
        document.getElementById('me-pw-new').value = '';
    },

    async setService(node, service, enabled, box) {
        try {
            await this.json('/api/service-alerts', 'POST', { node, service, callsign: this.status.callsign, enabled });
        } catch (e) {
            box.checked = !enabled;
            document.getElementById('me-msg').textContent = e.message;
        }
    },

    // ------------------------------------------------------------ admin
    async loadUsers() {
        try {
            const d = await this.json('/api/users');
            this.users = d.users;
            this.drawUsers();
        } catch (e) {
            document.getElementById('adm-users').innerHTML = `<p class="log-empty">${this.esc(e.message)}</p>`;
        }
    },

    drawUsers() {
        const q = document.getElementById('adm-filter').value.trim().toLowerCase();
        const rows = this.users.filter(u => !q || u.callsign.includes(q) || (u.name || '').toLowerCase().includes(q) || u.nodes.some(n => n.includes(q)));
        const me = this.status.callsign;
        document.getElementById('adm-users').innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Callsign</th><th>Name</th><th>Nodes</th><th>Password</th><th>Admin</th><th>Infra team</th><th>Alerts to</th><th>Last login</th><th></th></tr></thead>
            <tbody>${rows.map(u => `<tr>
                <td><strong>${this.esc(u.callsign.toUpperCase())}</strong>${u.callsign === me ? ' <small>(you)</small>' : ''}</td>
                <td>${this.esc(u.name || '')}</td>
                <td title="${this.esc(u.nodes.join(', '))}">${u.nodes.length || '<small>none</small>'}</td>
                <td class="acct-pw-cell">${u.has_password ? '<span class="svc-alerts svc-alerts-on">set</span>' : '<span class="svc-alerts svc-alerts-ignored">none</span>'}
                    <button class="btn btn-small btn-secondary" data-act="pw" data-cs="${this.esc(u.callsign)}">${u.has_password ? 'Reset' : 'Set'}</button>
                    ${u.has_password ? `<button class="btn btn-small btn-secondary" data-act="pw-clear" data-cs="${this.esc(u.callsign)}" title="The user can no longer log in">Remove</button>` : ''}</td>
                <td><input type="checkbox" data-flag="is_admin" data-cs="${this.esc(u.callsign)}"${u.is_admin ? ' checked' : ''} aria-label="Admin"></td>
                <td><input type="checkbox" data-flag="infra_team" data-cs="${this.esc(u.callsign)}"${u.infra_team ? ' checked' : ''} aria-label="Infrastructure team"></td>
                <td><small>${[u.email ? 'e-mail' : '', u.ntfy_topic ? 'ntfy' : ''].filter(Boolean).join(', ') || 'nothing set'}${u.alerts_enabled ? '' : ' (off)'}</small></td>
                <td><small>${this.esc((u.last_login || '').slice(0, 16)) || 'never'}</small></td>
                <td>${!u.nodes.length ? `<button class="btn btn-small btn-secondary" data-act="delete" data-cs="${this.esc(u.callsign)}">Delete</button>` : ''}</td>
            </tr>`).join('')}</tbody></table></div>
            <p class="form-hint">${this.users.length} users · ${this.users.filter(u => u.has_password).length} with a password ·
                ${this.users.filter(u => u.is_admin).length} admins · ${this.users.filter(u => u.infra_team).length} on the infra team</p>`;
    },

    async setFlag(cs, flag, value, el) {
        const msg = document.getElementById('adm-msg');
        try {
            await this.json(`/api/users/${encodeURIComponent(cs)}`, 'PATCH', { [flag]: value });
            msg.textContent = `${cs.toUpperCase()}: ${flag === 'is_admin' ? 'admin' : 'infra team'} ${value ? 'on' : 'off'}.`;
            const u = this.users.find(x => x.callsign === cs);
            if (u) u[flag] = value;
        } catch (e) { el.checked = !value; msg.textContent = e.message; }
    },

    userAction(act, cs, btn) {
        const msg = document.getElementById('adm-msg');
        if (act === 'pw') {
            // Inline password box (no browser dialog).
            const cell = btn.closest('td');
            cell.innerHTML = `<form class="acct-inline" autocomplete="off"><input type="password" class="form-input" placeholder="New password (8+)" autocomplete="new-password">
                <button class="btn btn-small btn-primary">Save</button> <button type="button" class="btn btn-small btn-secondary" data-cancel>Cancel</button></form>`;
            const form = cell.querySelector('form');
            form.querySelector('input').focus();
            form.querySelector('[data-cancel]').addEventListener('click', () => this.drawUsers());
            form.addEventListener('submit', async (e) => {
                e.preventDefault();
                try {
                    await this.json(`/api/users/${encodeURIComponent(cs)}/password`, 'POST', { password: form.querySelector('input').value });
                    msg.textContent = `Password set for ${cs.toUpperCase()}. Tell them to change it on this page.`;
                    this.loadUsers();
                } catch (err) { msg.textContent = err.message; }
            });
            return;
        }
        if (act === 'pw-clear' || act === 'delete') {
            // Second click confirms (no browser dialog).
            if (btn.dataset.armed !== '1') {
                btn.dataset.armed = '1';
                btn.textContent = act === 'delete' ? 'Really delete?' : 'Really remove?';
                setTimeout(() => this.drawUsers(), 4000);
                return;
            }
            const req = act === 'delete'
                ? this.json(`/api/users/${encodeURIComponent(cs)}`, 'DELETE')
                : this.json(`/api/users/${encodeURIComponent(cs)}/password`, 'POST', { password: '' });
            req.then(() => { msg.textContent = `${cs.toUpperCase()}: ${act === 'delete' ? 'deleted' : 'password removed'}.`; this.loadUsers(); })
                .catch(e => { msg.textContent = e.message; });
        }
    },

    async addUser() {
        const msg = document.getElementById('adm-msg');
        const input = document.getElementById('adm-new');
        try {
            await this.json('/api/users', 'POST', { callsign: input.value.trim() });
            msg.textContent = `${input.value.trim().toUpperCase()} added.`;
            input.value = '';
            this.loadUsers();
        } catch (e) { msg.textContent = e.message; }
    },
};

document.addEventListener('DOMContentLoaded', () => Account.init());
