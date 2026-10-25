/**
 * Babel Costs section on the Diagnostics tab (babel_view.py): good RF links
 * Babel won't use, detours, nodes whose tunnels cost less than their RF
 * links, and Route Watch paths that go over a tunnel while an RF route
 * exists. Plus a per-node neighbour table (costs, metric, route count).
 */
const BabelView = {
    initialized: false,
    data: null,

    init() {
        if (this.initialized || !document.getElementById('bv-report')) return;
        this.initialized = true;
        document.getElementById('bv-run')?.addEventListener('click', () => this.load());
        document.getElementById('bv-node')?.addEventListener('change', () => this.renderNode());
    },

    async load() {
        this.init();
        this.setStatus('Loading...');
        try {
            this.data = await (await fetch('/api/reports/babel')).json();
            const f = this.data.findings;
            document.getElementById('bv-report').innerHTML = f.length ? `<ul class="diag-findings">${f.map(x => `
                <li class="diag-finding"><span class="diag-badge infra-badge-${this.esc(x.severity)}">${this.esc(x.severity)}</span>
                <strong>${this.esc(x.title)}</strong><div class="form-hint">${this.esc(x.detail)}</div></li>`).join('')}</ul>`
                : '<p class="log-empty">Routing follows the RF links: nothing to report.</p>';
            const sel = document.getElementById('bv-node');
            sel.innerHTML = '<option value="">Neighbour costs for a node…</option>' + this.data.nodes.map(n =>
                `<option value="${this.esc(n.node)}">${this.esc(n.node)} (${n.rf} RF, ${n.tunnels} tunnel)</option>`).join('');
            this.renderNode();
            this.setStatus(`${f.length} finding(s) · ${this.data.nodes.length} polled node(s)`);
        } catch (error) {
            this.setStatus('Failed to load.');
        }
    },

    renderNode() {
        const el = document.getElementById('bv-detail');
        const name = document.getElementById('bv-node')?.value;
        const n = this.data?.nodes.find(x => x.node === name);
        if (!n) {
            el.innerHTML = '';
            return;
        }
        const v = x => x == null ? '-' : Math.round(x);
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Neighbour</th><th>Type</th><th>LQ</th><th>Quality</th><th>SNR</th>
            <th title="Babel cost of hearing the neighbour">rxcost</th><th>txcost</th>
            <th title="Metric of the selected route to the neighbour">metric</th><th>Routes via</th><th>Routable</th></tr></thead>
            <tbody>${n.neighbours.map(x => `<tr>
                <td>${this.esc(x.node)}</td><td>${this.esc(x.link_type)}</td><td>${v(x.lq)}</td><td>${v(x.quality)}</td>
                <td>${v(x.snr)}</td><td>${v(x.rxcost)}</td><td>${v(x.txcost)}</td><td>${v(x.metric)}</td>
                <td>${x.routes ?? '-'}</td><td>${x.routable === false ? '<strong>no</strong>' : x.routable ? 'yes' : '-'}</td>
            </tr>`).join('')}</tbody></table></div>`;
    },

    setStatus(text) {
        const el = document.getElementById('bv-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => BabelView.init());
