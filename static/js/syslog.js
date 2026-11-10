/**
 * Node Logs page (menu History → Node Logs, syslog_rx.py): messages from the
 * syslog receiver (when enabled) and the SSH collector's logread snapshots.
 * Filters: node, worst level, program, text, WireGuard / Network / Auth /
 * Kernel / Radio chips, source, time; collapse repeats; saved views (this
 * browser); event-rate chart (errors / warnings / other); CSV export.
 */
const NodeLogs = {
    VIEWS: 'syslog-views',
    chart: null,

    init() {
        if (!document.getElementById('sl-list')) return;
        ['sl-node', 'sl-program', 'sl-q'].forEach(id => document.getElementById(id).addEventListener('keydown', e => {
            if (e.key === 'Enter') this.load();
        }));
        ['sl-severity', 'sl-source', 'sl-hours', 'sl-collapse'].forEach(id =>
            document.getElementById(id).addEventListener('change', () => this.load()));
        document.getElementById('sl-run').addEventListener('click', () => this.load());
        document.getElementById('sl-csv').addEventListener('click', () => { location.href = '/api/syslog/export.csv?' + this.query(5000); });
        document.getElementById('sl-import').addEventListener('click', () => this.importSnapshots());
        document.getElementById('sl-save-view').addEventListener('click', () => this.saveView());
        document.getElementById('sl-views').addEventListener('change', e => this.applyView(e.target.value));
        R7.onShow('syslog', null, () => { this.renderViews(); this.loadStatus(); this.load(); });
    },

    chips() {
        return [...document.querySelectorAll('#sl-chips input:checked')].map(i => i.value);
    },

    filters() {
        return {
            node: document.getElementById('sl-node').value.trim(),
            severity: document.getElementById('sl-severity').value,
            program: document.getElementById('sl-program').value.trim(),
            q: document.getElementById('sl-q').value.trim(),
            source: document.getElementById('sl-source').value,
            hours: document.getElementById('sl-hours').value,
            collapse: document.getElementById('sl-collapse').checked,
            chips: this.chips(),
        };
    },

    query(limit = 500) {
        const f = this.filters();
        const since = new Date(Date.now() - Number(f.hours) * 3600e3);
        const pad = n => String(n).padStart(2, '0');
        const sinceS = `${since.getFullYear()}-${pad(since.getMonth() + 1)}-${pad(since.getDate())} ${pad(since.getHours())}:${pad(since.getMinutes())}:00`;
        const p = new URLSearchParams({ limit, since: sinceS });
        ['node', 'severity', 'program', 'q', 'source'].forEach(k => { if (f[k]) p.set(k, f[k]); });
        if (f.chips.length) p.set('chips', f.chips.join(','));
        if (f.collapse) p.set('collapse', '1');
        return p.toString();
    },

    async loadStatus() {
        const el = document.getElementById('sl-receiver');
        try {
            const s = await R7.json('/api/syslog/status');
            const chipBox = document.getElementById('sl-chips');
            if (!chipBox.children.length) {
                chipBox.innerHTML = Object.entries(s.chips).map(([k, label]) =>
                    `<label><input type="checkbox" value="${R7.esc(k)}">${R7.esc(label)}</label>`).join('');
                chipBox.querySelectorAll('input').forEach(i => i.addEventListener('change', () => this.load()));
            }
            el.innerHTML = s.enabled
                ? (s.running ? `Receiver listening on UDP ${R7.esc(s.port)} · ${s.received} received` : `Receiver not running: ${R7.esc(s.error || 'starting')}`)
                : `Syslog receiver off (SYSLOG_ENABLED=0): showing the SSH collector's logread snapshots of infrastructure radios.`;
            el.innerHTML += ` · ${s.total} messages stored, kept ${s.retention_days} days`;
        } catch (e) { el.textContent = e.message; }
    },

    async load() {
        const list = document.getElementById('sl-list');
        const status = document.getElementById('sl-status');
        status.textContent = 'Loading…';
        try {
            const data = await R7.json('/api/syslog?' + this.query());
            const f = this.filters();
            status.textContent = `${data.shown} shown of ${data.total} matching${data.collapsed ? ' (repeats collapsed)' : ''}`;
            list.innerHTML = data.messages.length ? `<table class="info-table syslog-table"><thead><tr><th>Time</th><th>Node</th>
                <th>Level</th><th>Program</th><th>Message</th>${data.collapsed ? '<th>×</th>' : ''}</tr></thead><tbody>${data.messages.map(m => `
                <tr><td>${R7.esc((m.node_time || m.received_at).slice(5, 19))}</td><td>${R7.node(m.node_name)}</td>
                    <td>${m.severity_name ? R7.badge(m.severity_name) : ''}</td><td>${R7.esc(m.program || '')}</td>
                    <td class="msg">${R7.esc(m.message)}</td>${data.collapsed ? `<td>${m.count > 1 ? m.count : ''}</td>` : ''}</tr>`).join('')}</tbody></table>`
                : '<p class="log-empty">No messages match.</p>';
            this.loadRate(f);
        } catch (e) {
            status.textContent = '';
            list.innerHTML = `<p class="log-empty">Could not load: ${R7.esc(e.message)}</p>`;
        }
    },

    async loadRate(f) {
        const canvas = document.getElementById('sl-rate');
        if (!canvas || typeof Chart === 'undefined') return;
        const p = new URLSearchParams({ hours: f.hours });
        if (f.node) p.set('node', f.node);
        const data = await R7.json('/api/syslog/rate?' + p);
        const labels = data.series.map(b => b.t.slice(5, 16));
        const ds = [['errors', 'Errors', '#c0392b'], ['warnings', 'Warnings', '#d68910'], ['other', 'Other', '#95a5a6']]
            .map(([k, label, color]) => ({ label, data: data.series.map(b => b[k]), backgroundColor: color, stack: 's' }));
        if (this.chart) this.chart.destroy();
        this.chart = new Chart(canvas, { type: 'bar', data: { labels, datasets: ds },
            options: { responsive: true, maintainAspectRatio: false, animation: false,
                scales: { x: { stacked: true, ticks: { maxTicksLimit: 12 } }, y: { stacked: true, beginAtZero: true } },
                plugins: { legend: { position: 'bottom' } } } });
    },

    async importSnapshots() {
        const status = document.getElementById('sl-status');
        try {
            const r = await R7.json('/api/syslog/import', { method: 'POST', body: { hours: 72 } });
            status.textContent = `Imported ${r.added} new lines from the SSH snapshots.`;
            this.load();
        } catch (e) { status.textContent = e.message; }
    },

    views() {
        try { return JSON.parse(localStorage.getItem(this.VIEWS) || '{}'); } catch (e) { return {}; }
    },

    renderViews() {
        const sel = document.getElementById('sl-views');
        const v = this.views();
        sel.innerHTML = '<option value="">Saved views…</option>' + Object.keys(v).sort()
            .map(k => `<option value="${R7.esc(k)}">${R7.esc(k)}</option>`).join('');
    },

    saveView() {
        const input = document.getElementById('sl-view-name');
        const name = input.value.trim();
        if (!name) { input.focus(); return; }
        input.value = '';
        const v = this.views();
        v[name] = this.filters();
        try { localStorage.setItem(this.VIEWS, JSON.stringify(v)); } catch (e) { /* storage off */ }
        this.renderViews();
    },

    applyView(name) {
        const f = this.views()[name];
        if (!f) return;
        ['node', 'severity', 'program', 'q', 'source', 'hours'].forEach(k => { document.getElementById('sl-' + k).value = f[k] ?? ''; });
        document.getElementById('sl-collapse').checked = !!f.collapse;
        document.querySelectorAll('#sl-chips input').forEach(i => { i.checked = (f.chips || []).includes(i.value); });
        this.load();
    },
};

NodeLogs.init();
