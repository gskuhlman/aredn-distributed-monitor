/**
 * Node Hygiene section on the Diagnostics tab (hygiene.py): a 0-100 setup
 * score per node with a "fix these" list, grouped by owner (callsign).
 * `Hygiene.block(node)` renders one node's score and fixes for the node page
 * and the new-node welcome.
 */
const Hygiene = {
    initialized: false,
    data: null,

    init() {
        if (this.initialized || !document.getElementById('hy-report')) return;
        this.initialized = true;
        document.getElementById('hy-run')?.addEventListener('click', () => this.load());
        document.getElementById('hy-owner')?.addEventListener('change', () => this.render());
        document.getElementById('hy-report')?.addEventListener('click', (event) => {
            const btn = event.target.closest('button[data-hy-recheck]');
            if (btn) this.recheck(btn.dataset.hyRecheck, btn);
        });
    },

    async load() {
        this.init();
        this.setStatus('Loading...');
        try {
            this.data = await (await fetch('/api/reports/hygiene')).json();
            const sel = document.getElementById('hy-owner');
            const keep = sel.value;
            sel.innerHTML = '<option value="">All owners</option>' + this.data.owners.map(o =>
                `<option value="${this.esc(o.owner)}">${this.esc(o.owner)} · ${o.nodes} node(s) · avg ${o.avg_score}</option>`).join('');
            sel.value = keep;
            this.render();
            this.setStatus(`${this.data.nodes.length} active node(s), average ${this.data.average}/100 · newest release seen ${this.data.latest_release || '?'}`);
        } catch (error) {
            this.setStatus('Failed to load.');
        }
    },

    scoreClass(s) {
        return s == null ? 'info' : s >= 85 ? 'ok' : s >= 60 ? 'warning' : 'critical';
    },

    render() {
        const owner = document.getElementById('hy-owner')?.value;
        const rows = this.data.nodes.filter(n => !owner || n.owner === owner);
        document.getElementById('hy-report').innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Score</th><th>Node</th><th>Owner</th><th>Firmware</th><th>Fix these</th><th></th></tr></thead>
            <tbody>${rows.map(n => `<tr>
                <td><span class="diag-badge infra-badge-${this.scoreClass(n.score)}">${n.score ?? '-'}</span></td>
                <td><a href="/nodes/${encodeURIComponent(n.name)}">${this.esc(n.name)}</a></td>
                <td>${this.esc(n.owner)}</td><td>${this.esc(n.firmware || '-')}</td>
                <td>${n.fixes.length ? `<ul class="hy-fixes">${n.fixes.map(f => `<li>${this.esc(f)}</li>`).join('')}</ul>` : 'Nothing to fix'}
                    ${n.rechecked ? `<br><small class="form-hint">re-checked ${this.esc(n.rechecked)}</small>` : ''}</td>
                <td><button type="button" class="btn btn-small btn-secondary" data-hy-recheck="${this.esc(n.name)}"
                    title="Read this node now (targeted scan) and score it again">Re-check</button></td>
            </tr>`).join('')}</tbody></table></div>`;
    },

    /** Targeted scan of one node, then score it again in place. */
    async recheck(name, button) {
        button.disabled = true;
        this.setStatus(`Reading ${name}…`);
        try {
            const resp = await fetch(`/api/nodes/scan/${encodeURIComponent(name)}`, { method: 'POST' });
            const scan = await resp.json().catch(() => ({}));
            await fetch('/api/node-clock/run', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify({ nodes: [name] }) }).catch(() => null);
            const d = await (await fetch(`/api/reports/hygiene?node=${encodeURIComponent(name)}`)).json();
            const row = d.nodes[0];
            if (row) {
                const i = this.data.nodes.findIndex(n => n.name === name);
                const old = i >= 0 ? this.data.nodes[i].score : null;
                row.rechecked = new Date().toLocaleTimeString();
                if (i >= 0) this.data.nodes[i] = row;
                this.render();
                this.setStatus(resp.ok
                    ? `${name}: ${old ?? '-'} → ${row.score}/100.`
                    : `${name}: could not read the node (${scan.error || resp.status}); scored from what was stored: ${row.score}/100.`);
            }
        } catch (error) {
            this.setStatus(`Re-check failed: ${error.message}`);
            button.disabled = false;
        }
    },

    /** One node's score + fixes (node page, welcome). */
    async block(node, el) {
        try {
            const d = await (await fetch(`/api/reports/hygiene?node=${encodeURIComponent(node)}`)).json();
            const n = d.nodes[0];
            if (!n) { el.innerHTML = ''; return; }
            el.innerHTML = `<div class="hy-block"><span class="diag-badge infra-badge-${this.scoreClass(n.score)}">Setup ${n.score}/100</span>
                ${n.fixes.length ? `<ul class="hy-fixes">${n.fixes.map(f => `<li>${this.esc(f)}</li>`).join('')}</ul>`
                    : '<span class="form-hint">Location, firmware, description and antenna settings are all set.</span>'}</div>`;
        } catch (e) { el.innerHTML = ''; }
    },

    setStatus(text) {
        const el = document.getElementById('hy-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => Hygiene.init());
