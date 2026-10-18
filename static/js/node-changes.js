/**
 * Node page "Changes" section: this node's change log (as node or link peer)
 * with an inline "Add change" form. Before/after comparisons open on the
 * main page's Changes tab (/#changes/<id>).
 */
const NodeChanges = {
    nodeName: window.NODE_NAME,

    init() {
        if (!document.getElementById('node-changes')) return;
        document.getElementById('node-chg-add')?.addEventListener('click', () => this.toggleForm());
        document.getElementById('node-chg-cancel')?.addEventListener('click', () => this.toggleForm(false));
        document.getElementById('node-chg-save')?.addEventListener('click', () => this.save());
        this.load();
        this.loadPeers();
    },

    async load() {
        const el = document.getElementById('node-changes');
        try {
            const data = await fetch(`/api/changes?node=${encodeURIComponent(this.nodeName)}&limit=50`).then(r => r.json());
            const rows = data.changes || [];
            if (!rows.length) {
                el.innerHTML = '<p class="log-empty">No changes recorded for this node.</p>';
                return;
            }
            el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table chg-table">
                <thead><tr><th>Time</th><th>Node</th><th>Peer</th><th>Category</th><th>Description</th><th>Source</th><th></th></tr></thead>
                <tbody>${rows.map(c => `<tr>
                    <td>${this.esc(c.timestamp)}</td><td>${this.esc(c.node_name)}</td><td>${this.esc(c.peer_node || '')}</td>
                    <td><span class="chg-cat chg-cat-${this.esc(c.category)}">${this.esc(c.category)}</span></td>
                    <td class="chg-desc">${this.esc(c.description || '')}</td><td>${this.esc(c.source)}</td>
                    <td><a class="btn btn-small btn-secondary" href="/#changes/${c.id}">Before / after</a></td>
                </tr>`).join('')}</tbody></table></div>`;
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Failed to load changes.</p>';
        }
    },

    async loadPeers() {
        const list = document.getElementById('node-chg-peers');
        if (!list) return;
        try {
            const data = await fetch(`/api/nodes/detail/${encodeURIComponent(this.nodeName)}`).then(r => r.json());
            const peers = new Set((data.links || [])
                .filter(l => l.link_type === 'RF')
                .map(l => l.source_node === this.nodeName ? l.target_node : l.source_node));
            list.innerHTML = [...peers].sort().map(p => `<option value="${this.esc(p)}">`).join('');
        } catch (e) { /* convenience only */ }
    },

    toggleForm(show) {
        const form = document.getElementById('node-chg-form');
        if (!form) return;
        const visible = show ?? form.hidden;
        form.hidden = !visible;
        if (visible) {
            const p = n => String(n).padStart(2, '0');
            const d = new Date();
            document.getElementById('node-chg-time').value =
                `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
            document.getElementById('node-chg-desc')?.focus();
        }
    },

    async save() {
        const val = id => (document.getElementById(id)?.value || '').trim();
        const body = {
            node_name: this.nodeName, peer_node: val('node-chg-peer'), category: val('node-chg-cat'),
            description: val('node-chg-desc'), timestamp: val('node-chg-time').replace('T', ' ')
        };
        const toast = (type, title, msg) => (typeof NodePage !== 'undefined' && NodePage.showToast)
            ? NodePage.showToast(type, title, msg) : console.log(title, msg);
        if (!body.description) { toast('error', 'Add Change', 'Enter a description'); return; }
        try {
            const resp = await fetch('/api/changes', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Save failed');
            document.getElementById('node-chg-desc').value = '';
            document.getElementById('node-chg-peer').value = '';
            this.toggleForm(false);
            toast('success', 'Change Recorded', body.description);
            this.load();
        } catch (error) {
            toast('error', 'Add Change Failed', error.message);
        }
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => NodeChanges.init());
