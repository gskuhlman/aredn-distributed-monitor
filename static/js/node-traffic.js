/**
 * Node page "Traffic (SSH)" (ssh_traffic.py, POST /api/ssh/traffic/<node>):
 * on request, reads the node's interface counters, WireGuard tunnels and
 * connection table over SSH a few seconds apart and shows interface rates,
 * per-tunnel throughput, a summary of connections by kind (LAN → mesh,
 * mesh → internet through this node's WAN ...) and the connections with
 * both ends named. Nothing is stored; the classification lives in
 * ssh_traffic.py.
 */
const NodeTraffic = {
    data: null,
    showAll: false,
    CLASS: { node: 'this node', lan: 'LAN', wan: 'WAN network', mesh: 'mesh', dtdlink: 'DtD', tunnel: 'tunnel',
        internet: 'internet', private: 'private', broadcast: 'broadcast' },

    init() {
        const btn = document.getElementById('nt-run');
        if (!btn || !window.NODE_NAME) return;
        btn.addEventListener('click', () => this.run());
        document.getElementById('node-traffic').addEventListener('click', (event) => {
            if (event.target.closest('[data-nt-all]')) { this.showAll = true; this.render(); }
        });
        document.getElementById('node-traffic').addEventListener('input', (event) => {
            if (event.target.id === 'nt-filter') this.renderFlows();
        });
    },

    async run() {
        const btn = document.getElementById('nt-run');
        const seconds = Number(document.getElementById('nt-seconds')?.value || 5);
        btn.disabled = true;
        this.setStatus(`Reading counters over ${seconds} s…`);
        try {
            const resp = await fetch(`/api/ssh/traffic/${encodeURIComponent(window.NODE_NAME)}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ seconds })
            });
            const d = await resp.json();
            if (!resp.ok) throw new Error(d.error || `HTTP ${resp.status}`);
            this.data = d;
            this.data.at = new Date().toLocaleTimeString();
            this.showAll = false;
            this.render();
            this.setStatus(`Measured over ${d.seconds} s at ${this.data.at}.`);
        } catch (error) {
            this.setStatus(`Could not read traffic: ${error.message}`);
        } finally {
            btn.disabled = false;
        }
    },

    render() {
        const d = this.data;
        const el = document.getElementById('node-traffic');
        const ifaces = d.interfaces.filter(i => i.rx_bps || i.tx_bps || ['wan', 'lan', 'wifi', 'dtdlink', 'wireguard', 'tun'].includes(i.role));
        const ifTable = `<h3 class="subsection-heading">Interfaces</h3><div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Interface</th><th>Role</th><th>Address</th><th>Receiving</th><th>Sending</th><th>Total since boot (rx / tx)</th></tr></thead>
            <tbody>${ifaces.map(i => `<tr><td><strong>${this.esc(i.name)}</strong></td><td>${this.esc(i.role_label)}</td>
                <td><small>${this.esc(i.address || '')}</small></td>
                <td>${this.rate(i.rx_bps)}<br><small>${i.rx_pps} pkt/s</small></td>
                <td>${this.rate(i.tx_bps)}<br><small>${i.tx_pps} pkt/s</small></td>
                <td><small>${this.size(i.rx_total_bytes)} / ${this.size(i.tx_total_bytes)}</small></td></tr>`).join('')}</tbody></table></div>`;
        const wg = d.wireguard.length ? `<h3 class="subsection-heading">WireGuard tunnels</h3><div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Interface</th><th>Peer key</th><th>Far end</th><th>Receiving</th><th>Sending</th><th>Total (rx / tx)</th></tr></thead>
            <tbody>${d.wireguard.map(w => `<tr><td>${this.esc(w.iface)}</td><td><small>${this.esc(w.peer)}</small></td>
                <td>${this.esc(w.endpoint || '-')}</td><td>${this.rate(w.rx_bps)}</td><td>${this.rate(w.tx_bps)}</td>
                <td><small>${this.size(w.rx_total_bytes)} / ${this.size(w.tx_total_bytes)}</small></td></tr>`).join('')}</tbody></table></div>` : '';
        const ct = d.conntrack;
        let ctNote = '';
        if (!ct.available) ctNote = '<p class="form-hint">This node did not give its connection table (no /proc/net/nf_conntrack or conntrack tool).</p>';
        else if (!ct.accounting) ctNote = '<p class="form-hint">Per-connection byte counts are off on this node (nf_conntrack_acct = 0), so connections are listed without sizes; the interface rates above still show the volume.</p>';
        if (ct.truncated) ctNote += '<p class="form-hint">The connection table was longer than the first 4000 lines read.</p>';
        const summary = d.summary.length ? `<h3 class="subsection-heading">Connections by kind <small class="form-hint">${ct.count} tracked</small></h3>
            <div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>From → to</th><th>Connections</th>${ct.accounting ? '<th>Data</th>' : ''}<th>Services</th></tr></thead>
            <tbody>${d.summary.map(s => `<tr><td>${this.esc(s.kind)}</td><td>${s.flows}</td>
                ${ct.accounting ? `<td>${this.size(s.bytes)}</td>` : ''}<td><small>${this.esc(s.services.join(', '))}</small></td></tr>`).join('')}
            </tbody></table></div>` : '';
        const hosts = d.lan_hosts.length ? `<p class="form-hint">LAN hosts (DHCP): ${d.lan_hosts.map(h => `${this.esc(h.name)} ${this.esc(h.ip)}`).join(', ')}</p>` : '';
        const explain = (d.explain || []).length ? `<h3 class="subsection-heading">What this means</h3>
            <ul class="nt-explain">${d.explain.map(t => `<li>${this.esc(t)}</li>`).join('')}</ul>` : '';
        el.innerHTML = `${explain}${ifTable}${wg}${ctNote}${summary}
            ${d.flows.length ? `<h3 class="subsection-heading">Connections</h3>
            <input type="search" id="nt-filter" class="form-input" placeholder="Filter by name, address, port, service">
            <div id="nt-flows"></div>` : ''}${hosts}
            <p class="form-hint">Connections are the node's connection-tracking table: what passes through it (routed or NATed) and what
            it talks to itself. "NAT via WAN" = traffic from another host leaving through this node's internet connection.
            Bytes are totals since each connection started.</p>`;
        this.renderFlows();
    },

    renderFlows() {
        const el = document.getElementById('nt-flows');
        if (!el) return;
        const q = (document.getElementById('nt-filter')?.value || '').toLowerCase();
        const acct = this.data.conntrack.accounting;
        let rows = this.data.flows;
        if (q) rows = rows.filter(f => [f.src, f.dst, f.src_name, f.dst_name, f.sport, f.dport, f.service, f.proto,
            this.CLASS[f.src_class], this.CLASS[f.dst_class]].some(v => String(v ?? '').toLowerCase().includes(q)));
        const shown = this.showAll || q ? rows : rows.slice(0, 50);
        const end = (ip, port, name, cls) => `${name ? `<strong>${this.esc(name)}</strong> ` : ''}${this.esc(ip || '?')}${port != null ? `:${port}` : ''}
            <br><small class="form-hint">${this.esc(this.CLASS[cls] || cls || '')}</small>`;
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Proto</th><th>From</th><th>To</th><th>Service</th><th>State</th>${acct ? '<th>Sent / received</th>' : ''}</tr></thead>
            <tbody>${shown.map(f => `<tr>
                <td>${this.esc(f.proto)}</td>
                <td>${end(f.src, f.sport, f.src_name, f.src_class)}</td>
                <td>${end(f.dst, f.dport, f.dst_name, f.dst_class)}${f.via ? `<br><span class="diag-badge infra-badge-info">NAT via WAN</span>` : ''}</td>
                <td>${this.esc(f.service || '')}</td>
                <td><small>${this.esc(f.state || '')}${f.unreplied ? ' no reply' : ''}</small></td>
                ${acct ? `<td><small>${this.size(f.bytes_out)} / ${this.size(f.bytes_in)}</small></td>` : ''}
            </tr>`).join('')}</tbody></table></div>
            ${shown.length < rows.length ? `<button type="button" class="btn btn-small btn-secondary" data-nt-all="1">Show all ${rows.length}</button>` : ''}`;
    },

    rate(bps) {
        if (bps == null) return '-';
        if (bps >= 1e6) return `${(bps / 1e6).toFixed(2)} Mbps`;
        if (bps >= 1e3) return `${(bps / 1e3).toFixed(1)} kbps`;
        return `${bps} bps`;
    },

    size(b) {
        if (b == null) return '-';
        if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB`;
        if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
        if (b >= 1e3) return `${(b / 1e3).toFixed(1)} kB`;
        return `${b} B`;
    },

    setStatus(text) {
        const el = document.getElementById('nt-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
};

document.addEventListener('DOMContentLoaded', () => NodeTraffic.init());
