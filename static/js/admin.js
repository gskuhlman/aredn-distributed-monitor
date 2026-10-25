/**
 * Admin page: login for changes (auth.py), database care (db_care.py) and
 * collectors / distributed status (distributed_status.py).
 */
const Admin = {
    ds: null,

    init() {
        const on = (id, fn) => document.getElementById(id)?.addEventListener('click', fn);
        on('admin-pw-set', () => this.setPassword(document.getElementById('admin-pw-new').value));
        on('admin-pw-clear', () => {
            if (window.confirm('Remove the password? Anyone on the network can then make changes.')) this.setPassword('');
        });
        on('admin-login', async () => { await Auth.prompt('Log in'); this.loadAuth(); });
        on('admin-logout', async () => { await Auth.logout(); this.loadAuth(); });
        on('db-refresh', () => this.loadDb());
        on('db-backup', () => this.dbAction('backup', 'Backing up…'));
        on('db-vacuum', () => {
            if (window.confirm('Run VACUUM now? The database is locked while it runs (a minute or more on a large file); '
                + 'scans and pages wait for it.')) this.dbAction('vacuum', 'VACUUM running…');
        });
        on('ds-refresh', () => this.loadDs());
        document.getElementById('ds-filter')?.addEventListener('change', () => this.renderDs());
        document.addEventListener('auth-changed', () => this.loadAuth());
        this.loadAuth();
        this.loadDb();
        this.loadDs();
    },

    async loadAuth() {
        const s = await Auth.status();
        if (!s) return;
        const el = document.getElementById('admin-auth');
        el.innerHTML = `<p>${s.enabled
            ? `<span class="diag-badge infra-badge-ok">On</span> Actions need a login`
              + (s.source === 'env' ? ' (password from ADMIN_PASSWORD).' : '.')
              + (s.proxy_header ? ` A reverse proxy header ${this.esc(s.proxy_header)} also counts.` : '')
            : '<span class="diag-badge infra-badge-warning">Off</span> No password: anyone on the network can make changes.'}</p>
            <p class="form-hint">${s.logged_in ? `Logged in as ${this.esc(s.user || 'operator')}` + (s.expires ? ` until ${this.esc(s.expires)}` : '') : 'Not logged in.'}</p>`;
        document.getElementById('admin-auth-badge').textContent = s.logged_in ? 'Logged in' : (s.enabled ? 'Read-only' : '');
        document.getElementById('admin-pw-set').disabled = s.source === 'env';
        document.getElementById('admin-pw-clear').disabled = s.source !== 'stored';
    },

    async setPassword(pw) {
        const status = document.getElementById('admin-pw-status');
        const r = await fetch('/api/auth/password', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ new: pw, current: document.getElementById('admin-pw-current').value })
        });
        const d = await r.json().catch(() => ({}));
        status.textContent = r.ok ? (pw ? 'Password set. Log in to make changes.' : 'Password removed.') : (d.error || 'Failed');
        document.getElementById('admin-pw-new').value = '';
        document.getElementById('admin-pw-current').value = '';
        this.loadAuth();
    },

    size(b) {
        if (b == null) return '-';
        const u = ['B', 'KB', 'MB', 'GB'];
        let i = 0;
        while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
        return `${b.toFixed(i ? 1 : 0)} ${u[i]}`;
    },

    async loadDb() {
        try {
            const d = await (await fetch('/api/admin/database')).json();
            document.getElementById('db-summary').innerHTML = `<p>File ${this.esc(d.path)}: <strong>${this.size(d.file_bytes)}</strong>`
                + (d.wal_bytes ? ` + WAL ${this.size(d.wal_bytes)}` : '') + ` · free pages ${this.size(d.free_bytes)}`
                + ` (${d.pages ? Math.round(100 * d.free_pages / d.pages) : 0}% reclaimable by VACUUM) · journal ${this.esc(d.journal_mode)}</p>`
                + (d.task.running ? `<p class="node-warning">${this.esc(d.task.running)} running…</p>` : '')
                + (d.task.last ? `<p class="form-hint">Last task: ${this.esc(d.task.last.task)} at ${this.esc(d.task.last.at)} — `
                    + `${d.task.last.ok ? 'ok' : 'failed: ' + this.esc(d.task.last.error)}</p>` : '');
            document.getElementById('db-tables').innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Table</th><th>Rows</th><th>Size (est.)</th><th>Share</th><th>Rows/day (7 d)</th><th>Growth/day</th><th>Oldest</th></tr></thead>
                <tbody>${d.tables.map(t => `<tr><td>${this.esc(t.table)}</td><td>${t.rows.toLocaleString()}</td>
                    <td>${this.size(t.est_bytes)}</td><td>${t.est_pct}%</td><td>${t.rows_per_day_7d?.toLocaleString() ?? '-'}</td>
                    <td>${this.size(t.bytes_per_day)}</td><td>${this.esc((t.oldest || '').slice(0, 16))}</td></tr>`).join('')}</tbody></table></div>`;
            document.getElementById('db-retention').innerHTML = `<table class="info-table diag-table"><tbody>${d.retention.map(r =>
                `<tr><td>${this.esc(r.key)}</td><td>${this.esc(r.value)} ${this.esc(r.unit)}</td><td class="form-hint">${this.esc(r.applies)}</td></tr>`).join('')}
                </tbody></table><p class="form-hint">Set these as environment variables (or the database retention setting on the main page).</p>`;
            const b = d.backup;
            document.getElementById('db-backups').innerHTML = `<p class="form-hint">${b.enabled ? `Nightly at ${this.esc(b.time)}` : 'Nightly backups off (BACKUP_ENABLED=0)'},
                keeping ${b.keep} in ${this.esc(b.dir)}.</p>` + (d.backups.length ? `<table class="info-table diag-table"><tbody>${d.backups.map(x =>
                `<tr><td>${this.esc(x.file)}</td><td>${this.esc(x.taken_at)}</td><td>${this.size(x.bytes)}</td></tr>`).join('')}</tbody></table>`
                : '<p class="log-empty">No backups yet.</p>');
        } catch (error) {
            document.getElementById('db-summary').innerHTML = '<p class="log-empty">Failed to load.</p>';
        }
    },

    async dbAction(kind, busy) {
        const status = document.getElementById('db-status');
        status.textContent = busy;
        const r = await fetch(`/api/admin/database/${kind}`, { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        status.textContent = r.ok ? (kind === 'backup' ? `Backup ${d.file} (${this.size(d.bytes)}) written.`
            : `VACUUM done: ${this.size(d.before_bytes)} → ${this.size(d.after_bytes)}.`) : (d.error || 'Failed');
        this.loadDb();
    },

    async loadDs() {
        try {
            this.ds = await (await fetch('/api/status/distributed')).json();
            const d = this.ds;
            document.getElementById('ds-summary').innerHTML = `<p>Source: <strong>${this.esc(d.source)}</strong>`
                + (d.error ? ` <span class="node-warning">${this.esc(d.error)}</span>` : '') + ` · ${d.healthy_collectors} healthy collector(s)</p>`
                + `<table class="info-table diag-table"><thead><tr><th>Collector</th><th>Site</th><th>Last heartbeat (UTC)</th><th>State</th></tr></thead>
                <tbody>${d.collectors.map(c => `<tr><td>${this.esc(c.collector_id)}${c.local ? ' <small>(this PC)</small>' : ''}</td>
                    <td>${this.esc(c.site || '')}</td><td>${this.esc(c.last_heartbeat)}</td>
                    <td>${c.healthy ? '<span class="diag-badge infra-badge-ok">healthy</span>' : c.aborted ? 'lost its starting node' : 'stale'}</td></tr>`).join('')}</tbody></table>`
                + `<p>${Object.entries(d.counts).map(([k, v]) => `${this.esc(k)}: <strong>${v}</strong>`).join(' · ')}</p>`
                + (d.note ? `<p class="form-hint">${this.esc(d.note)}</p>` : '');
            this.renderDs();
        } catch (error) {
            document.getElementById('ds-summary').innerHTML = '<p class="log-empty">Failed to load.</p>';
        }
    },

    renderDs() {
        if (!this.ds) return;
        const f = document.getElementById('ds-filter').value;
        const rows = this.ds.nodes.filter(n => !f || (f === 'problem' ? n.status !== 'UP' : n.status === f));
        document.getElementById('ds-nodes').innerHTML = rows.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Node</th><th>Status</th><th>Why</th><th>Last seen (UTC)</th></tr></thead>
            <tbody>${rows.map(n => `<tr><td><a href="/nodes/${encodeURIComponent(n.node)}">${this.esc(n.node)}</a></td>
                <td>${this.esc(n.status)}</td><td class="form-hint">${this.esc(n.why)}</td><td>${this.esc(n.last_seen || '-')}</td></tr>`).join('')}
            </tbody></table></div>` : '<p class="log-empty">No nodes in this state.</p>';
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => Admin.init());
