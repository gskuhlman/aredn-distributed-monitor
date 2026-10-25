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
            <thead><tr><th>Score</th><th>Node</th><th>Owner</th><th>Firmware</th><th>Fix these</th></tr></thead>
            <tbody>${rows.map(n => `<tr>
                <td><span class="diag-badge infra-badge-${this.scoreClass(n.score)}">${n.score ?? '-'}</span></td>
                <td><a href="/nodes/${encodeURIComponent(n.name)}">${this.esc(n.name)}</a></td>
                <td>${this.esc(n.owner)}</td><td>${this.esc(n.firmware || '-')}</td>
                <td>${n.fixes.length ? `<ul class="hy-fixes">${n.fixes.map(f => `<li>${this.esc(f)}</li>`).join('')}</ul>` : 'Nothing to fix'}</td>
            </tr>`).join('')}</tbody></table></div>`;
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
