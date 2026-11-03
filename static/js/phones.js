/**
 * Phone Connectivity section on the VOIP tab (phones.py): every phone on the
 * PBX with its IP and the node it hangs off, pinged from this PC after each
 * scan. Phones come from advertised services, phone-like mesh host names,
 * PBX output pasted here, or are added by hand. Loads with the VOIP tab and
 * again when a scan's pings finish (`phones_update`).
 */
const Phones = {
    initialized: false,
    data: null,
    showHidden: false,
    STATE: {
        up: ['sc-up', 'answering'], down: ['sc-down', 'no answer'], alarm: ['sc-down', 'ALARM'],
        node_down: ['form-hint', 'node down'], unchecked: ['form-hint', 'not pinged yet'],
        off: ['form-hint', 'paused'], ignored: ['form-hint', 'hidden']
    },
    SOURCE: { service: 'advertised service', host: 'mesh host name', pbx: 'PBX import', manual: 'added by hand' },

    init() {
        if (this.initialized || !document.getElementById('ph-report')) return;
        this.initialized = true;
        document.getElementById('ph-run')?.addEventListener('click', () => this.runNow());
        document.getElementById('ph-hours')?.addEventListener('change', () => this.load());
        document.getElementById('ph-hidden')?.addEventListener('change', (e) => { this.showHidden = e.target.checked; this.render(); });
        document.getElementById('ph-add')?.addEventListener('click', () => this.add());
        document.getElementById('ph-import')?.addEventListener('click', () => this.importPbx());
        document.getElementById('ph-host-q')?.addEventListener('input', () => this.searchHosts());
        document.getElementById('ph-hosts')?.addEventListener('click', (e) => {
            const b = e.target.closest('[data-host-ip]');
            if (b) this.addHost(b.dataset.hostIp, b.dataset.hostName);
        });
        document.getElementById('ph-report').addEventListener('click', (e) => this.onTableClick(e));
        if (typeof socket !== 'undefined' && socket) socket.on('phones_update', () => this.load());
    },

    async load() {
        this.init();
        const hours = document.getElementById('ph-hours')?.value || 24;
        try {
            this.data = await (await fetch(`/api/voip/phones?hours=${hours}`)).json();
            this.render();
        } catch (error) {
            document.getElementById('ph-report').innerHTML = '<p class="log-empty">Failed to load.</p>';
        }
    },

    render() {
        const d = this.data;
        if (!d) return;
        const c = d.counts || {};
        const shown = d.phones.filter(p => this.showHidden || p.state !== 'ignored');
        const tile = (label, value, cls) => `<div class="np-tile${cls ? ' ' + cls : ''}"><div class="np-tile-label">${label}</div>
            <div class="np-tile-value">${value}</div></div>`;
        const listed = d.phones.filter(p => p.state !== 'ignored' && p.state !== 'off').length;
        document.getElementById('ph-tiles').innerHTML = [
            tile('Phones pinged', listed),
            tile('Answering', c.up || 0),
            tile('Not answering', (c.down || 0) + (c.alarm || 0), (c.down || c.alarm) ? 'np-tile-warn' : ''),
            tile('Node down', c.node_down || 0),
            tile('Last ping', d.last_check ? this.esc(d.last_check.slice(5, 16)) : '-')
        ].join('');
        this.setStatus(d.enabled ? `pinged ${d.ping_count}× from this PC after every scan · alarm after ${d.fails_to_alarm} failed scans in a row`
            : 'per-scan pings are off (PHONE_PING_ENABLED=0); Ping now still works');
        document.getElementById('ph-report').innerHTML = shown.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table ph-table">
            <thead><tr><th>Phone</th><th>Ext</th><th>IP</th><th>Attached node</th><th>Now</th><th>Ping</th>
                <th>Answered (${d.hours} h)</th><th>History</th><th></th></tr></thead>
            <tbody>${shown.map(p => this.row(p)).join('')}</tbody></table></div>`
            : `<p class="log-empty">No phones listed yet. They appear after the next scan when a node advertises a phone/PBX service
               or the mesh has a phone-like host name; otherwise paste the PBX's registrations or add one below.</p>`;
        const un = d.unresolved || [];
        document.getElementById('ph-unresolved').innerHTML = un.length ? `<details class="ph-more">
            <summary>${un.length} advertised phone service(s) without an IP address (not pinged)</summary>
            <p class="form-hint">These nodes advertise a phone but neither the service link nor its name gives the phone's address.
               Add the IP to ping it.</p>
            <ul class="ph-unresolved">${un.map((u, i) => `<li>${u.kind === 'pbx' ? '<strong>PBX</strong> ' : ''}${this.esc(u.name)}
                ${u.extension ? `<small class="form-hint">ext ${this.esc(u.extension)}</small>` : ''} on
                <a href="/nodes/${encodeURIComponent(u.node)}">${this.esc(u.node)}</a>
                <button class="btn btn-small btn-secondary" data-un="${i}">Add IP…</button></li>`).join('')}</ul></details>` : '';
        document.querySelectorAll('#ph-unresolved [data-un]').forEach(b => b.addEventListener('click', () => {
            const u = un[Number(b.dataset.un)];
            document.querySelector('#ph-add-box').open = true;
            document.getElementById('ph-name').value = u.name;
            document.getElementById('ph-ext').value = u.extension || '';
            document.getElementById('ph-node').value = u.node;
            document.getElementById('ph-ip').focus();
        }));
    },

    row(p) {
        const [cls, text] = this.STATE[p.state] || ['form-hint', p.state];
        const node = p.attached_node
            ? `<a href="/nodes/${encodeURIComponent(p.attached_node)}">${this.esc(p.attached_node)}</a>
               ${p.node_up === false ? ' <small class="sc-down">down</small>' : p.node_known ? '' : ' <small class="form-hint">unknown node</small>'}
               ${p.attached_by === 'lan' ? '<div class="form-hint">from its LAN</div>' : ''}`
            : '<span class="form-hint">unknown</span>';
        const ping = p.ok ? `${p.last_avg != null ? p.last_avg.toFixed(1) + ' ms' : '-'}${p.last_loss ? ` · ${Math.round(p.last_loss)}% loss` : ''}`
            : p.ok === 0 ? '<span class="sc-down">no reply</span>' : '-';
        const pct = p.window_pct == null ? '-' : `<strong class="${p.window_pct >= 99 ? 'sc-up' : p.window_pct < 90 ? 'sc-down' : ''}">${p.window_pct}%</strong>
            <small class="form-hint">of ${p.window_checks}${p.window_median_ms != null ? ` · ${p.window_median_ms} ms` : ''}</small>`;
        return `<tr data-id="${p.id}"${p.alarm ? ' class="rel-row rel-row-adjacent"' : ''}>
            <td>${p.kind === 'pbx' ? '<strong>PBX</strong> ' : ''}${this.esc(p.name || '')}
                <div class="form-hint">${p.host_name && p.host_name !== p.name ? this.esc(p.host_name) + ' · ' : ''}${this.esc(this.SOURCE[p.source] || p.source)}${p.note ? ' · ' + this.esc(p.note) : ''}</div></td>
            <td>${this.esc(p.extension || '')}</td>
            <td><code>${this.esc(p.ip)}</code></td>
            <td>${node}</td>
            <td><span class="${cls}">${text}</span>${p.alarm && p.since ? `<div class="form-hint">since ${this.esc(p.since.slice(5, 16))}</div>` : ''}</td>
            <td class="ph-ping">${ping}</td>
            <td>${pct}</td>
            <td>${this.sparkline(p.series)}</td>
            <td class="ph-actions">
                <button class="btn btn-small btn-secondary" data-act="ping" title="Ping this phone now (shown here, not stored)">Ping</button>
                <button class="btn btn-small btn-secondary" data-act="edit">Edit</button>
                ${p.state === 'ignored' ? '<button class="btn btn-small btn-secondary" data-act="unhide">Show</button>'
                  : `<button class="btn btn-small btn-secondary" data-act="toggle">${p.enabled ? 'Pause' : 'Resume'}</button>
                     <button class="btn btn-small btn-secondary" data-act="remove">${p.source === 'service' || p.source === 'host' ? 'Hide' : 'Remove'}</button>`}
            </td></tr>`;
    },

    /** Round-trip time per scan; failed pings as red dots on the baseline. */
    sparkline(series) {
        const pts = series || [];
        if (pts.length < 2) return '<span class="form-hint">-</span>';
        const vals = pts.filter(s => s.ok && s.ms != null).map(s => s.ms);
        const max = Math.max(...vals, 1), W = 120, H = 28;
        const x = i => (i / (pts.length - 1)) * (W - 4) + 2;
        const y = v => H - 3 - (v / max) * (H - 8);
        const line = pts.map((s, i) => s.ok && s.ms != null ? `${x(i).toFixed(1)},${y(s.ms).toFixed(1)}` : null)
            .filter(Boolean).join(' ');
        const fails = pts.map((s, i) => s.ok ? '' : `<circle cx="${x(i).toFixed(1)}" cy="${H - 3}" r="2" class="rw-spark-fail"/>`).join('');
        const lost = pts.filter(s => !s.ok).length;
        return `<svg class="rw-spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
            aria-label="Ping time per scan, up to ${Math.round(max)} ms, ${lost} without a reply">
            <polyline points="${line}" class="rw-spark-line"/>${fails}
            <title>up to ${Math.round(max)} ms · ${lost} of ${pts.length} without a reply</title></svg>`;
    },

    async onTableClick(e) {
        const btn = e.target.closest('[data-act]');
        if (!btn) return;
        const id = btn.closest('tr').dataset.id;
        const p = this.data.phones.find(x => String(x.id) === id);
        const patch = body => fetch(`/api/voip/phones/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                                                               body: JSON.stringify(body) });
        if (btn.dataset.act === 'toggle') await patch({ enabled: !p.enabled });
        else if (btn.dataset.act === 'unhide') await patch({ ignored: false });
        else if (btn.dataset.act === 'remove') await fetch(`/api/voip/phones/${id}`, { method: 'DELETE' });
        else if (btn.dataset.act === 'edit') return this.edit(btn.closest('tr'), p);
        else if (btn.dataset.act === 'ping') return this.pingOne(btn, id);
        this.load();
    },

    /** Ping one phone now; the answer replaces the row's Ping cell until the next reload. */
    async pingOne(btn, id) {
        const cell = btn.closest('tr').querySelector('.ph-ping');
        btn.disabled = true;
        cell.innerHTML = '<span class="form-hint">pinging…</span>';
        try {
            const resp = await fetch(`/api/voip/phones/${id}/ping`, { method: 'POST' });
            const d = await resp.json();
            cell.innerHTML = !resp.ok ? `<span class="sc-down">${this.esc(d.error || 'failed')}</span>`
                : d.ok ? `<span class="sc-up">${d.avg != null ? d.avg.toFixed(1) + ' ms' : 'answered'}</span>`
                    + `${d.loss ? ` · ${Math.round(d.loss)}% loss` : ''}<div class="form-hint">just now</div>`
                : '<span class="sc-down">no reply</span><div class="form-hint">just now</div>';
        } catch (error) {
            cell.innerHTML = '<span class="sc-down">failed</span>';
        } finally {
            btn.disabled = false;
        }
    },

    edit(tr, p) {
        const cell = tr.querySelector('.ph-actions');
        cell.innerHTML = `<div class="ph-edit">
            <input class="form-input" data-f="name" placeholder="name" value="${this.esc(p.name || '')}">
            <input class="form-input" data-f="extension" placeholder="ext" value="${this.esc(p.extension || '')}">
            <input class="form-input" data-f="node_name" placeholder="node (blank = from LAN)" value="${this.esc(p.node_name || '')}">
            <input class="form-input" data-f="note" placeholder="note" value="${this.esc(p.note || '')}">
            <select class="form-input" data-f="kind"><option value="phone">phone</option><option value="pbx"${p.kind === 'pbx' ? ' selected' : ''}>PBX</option></select>
            <button class="btn btn-small btn-primary" data-save>Save</button> <button class="btn btn-small btn-secondary" data-cancel>Cancel</button></div>`;
        cell.querySelector('[data-cancel]').addEventListener('click', (ev) => { ev.stopPropagation(); this.render(); });
        cell.querySelector('[data-save]').addEventListener('click', async (ev) => {
            ev.stopPropagation();
            const body = {};
            cell.querySelectorAll('[data-f]').forEach(i => { body[i.dataset.f] = i.value; });
            await fetch(`/api/voip/phones/${p.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            this.load();
        });
    },

    async runNow() {
        const btn = document.getElementById('ph-run');
        btn.disabled = true;
        this.setStatus('Pinging every phone...');
        try {
            const resp = await fetch('/api/voip/phones/run', { method: 'POST' });
            const d = await resp.json();
            if (!resp.ok) this.setStatus(d.error || 'Failed');
            await this.load();
            if (!resp.ok) this.setStatus(d.error || 'Failed');
        } finally {
            btn.disabled = false;
        }
    },

    async add() {
        const val = id => document.getElementById(id).value.trim();
        const resp = await fetch('/api/voip/phones', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ip: val('ph-ip'), name: val('ph-name'), extension: val('ph-ext'), node_name: val('ph-node') }) });
        const d = await resp.json();
        const msg = document.getElementById('ph-add-msg');
        if (!resp.ok) { msg.textContent = d.error || 'Failed'; return; }
        msg.textContent = 'Added; it is pinged after the next scan (or Ping now).';
        ['ph-ip', 'ph-name', 'ph-ext', 'ph-node'].forEach(id => { document.getElementById(id).value = ''; });
        this.load();
    },

    async addHost(ip, name) {
        await fetch('/api/voip/phones', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                          body: JSON.stringify({ ip, name }) });
        this.searchHosts();
        this.load();
    },

    async searchHosts() {
        clearTimeout(this.hostTimer);
        this.hostTimer = setTimeout(async () => {
            const q = document.getElementById('ph-host-q').value.trim();
            const box = document.getElementById('ph-hosts');
            if (!q) { box.innerHTML = ''; return; }
            const d = await (await fetch(`/api/voip/hosts?q=${encodeURIComponent(q)}`)).json();
            box.innerHTML = d.hosts.length ? d.hosts.map(h => `<div class="ph-host"><button class="btn btn-small btn-secondary"
                data-host-ip="${this.esc(h.ip)}" data-host-name="${this.esc(h.name)}">Add</button>
                ${this.esc(h.name)} <code>${this.esc(h.ip)}</code>${h.attached_node ? ` <small class="form-hint">on ${this.esc(h.attached_node)}</small>` : ''}</div>`).join('')
                : '<p class="form-hint">No mesh host matches (the host list fills after a scan).</p>';
        }, 250);
    },

    async importPbx() {
        const text = document.getElementById('ph-pbx-text').value;
        const msg = document.getElementById('ph-import-msg');
        const d = await (await fetch('/api/voip/phones/import', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                                  body: JSON.stringify({ text }) })).json();
        msg.textContent = d.phones.length ? `${d.added} added, ${d.updated} updated.` : 'No IP addresses found in that text.';
        if (d.phones.length) document.getElementById('ph-pbx-text').value = '';
        this.load();
    },

    setStatus(text) {
        const el = document.getElementById('ph-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => {
    Phones.init();
    // Load with the VOIP tab (rf-stats.js calls VOIPModule.load when it opens).
    if (typeof VOIPModule !== 'undefined') {
        const original = VOIPModule.load.bind(VOIPModule);
        VOIPModule.load = function (...args) {
            Phones.load();
            return original(...args);
        };
    }
});
