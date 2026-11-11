/**
 * Route Traffic page (/routes/<id>/traffic, route_traffic.py): every node on
 * a Route Watch path is read over SSH at once; this lists the connections
 * between the route's two ends, those passing along it, and (optionally) the
 * nodes' own, with service, source and destination.
 */
const RouteTraffic = {
    data: null,
    CAT: { ends: 'between ends', transit: 'passing along', own: "node's own" },

    init() {
        document.getElementById('rt-refresh').addEventListener('click', () => this.load());
        document.getElementById('rt-own').addEventListener('change', () => this.renderFlows());
        document.getElementById('rt-filter').addEventListener('input', () => this.renderFlows());
        this.load();
    },

    esc(v) {
        return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    size(b) {
        if (b == null) return '';
        return b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB`
            : b >= 1e3 ? `${(b / 1e3).toFixed(0)} kB` : `${b} B`;
    },

    nodeLink(n) {
        return n ? `<a href="/nodes/${encodeURIComponent(n)}">${this.esc(n)}</a>` : '';
    },

    endLabel(e) {
        return e.site ? `site ${this.esc(e.site)}` : this.esc(e.node || 'starting node');
    },

    async load() {
        const btn = document.getElementById('rt-refresh');
        btn.disabled = true;
        this.status('Reading the nodes…');
        try {
            const seconds = document.getElementById('rt-seconds').value;
            const resp = await fetch(`/api/routes/${window.PATH_ID}/traffic`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ seconds }) });
            const d = await resp.json();
            this.data = d;
            this.render();
            this.status(d.error ? '' : `Read at ${d.generated_at}`);
        } catch (err) {
            this.status('Failed to read the route.');
        } finally {
            btn.disabled = false;
        }
    },

    status(t) { document.getElementById('rt-status').textContent = t; },

    render() {
        const d = this.data;
        if (d.path) {
            document.getElementById('rt-title').textContent = `Traffic: ${d.path.label || `${d.path.source || 'starting node'} → ${d.path.target}`}`;
            document.title = `${d.path.label || 'Route'} traffic - AREDN Network Monitor`;
        }
        const routeEl = document.getElementById('rt-route');
        if (!d.path) {
            routeEl.innerHTML = `<p class="log-empty">${this.esc(d.error || 'Route not found.')}</p>`;
            return;
        }
        const hops = (d.hops || d.route.map(n => ({ node: n }))).map(h => {
            let note = '<span class="form-hint">no SSH key</span>';
            if (h.readable && h.error) note = `<span class="sc-down">${this.esc(h.error)}</span>`;
            else if (h.readable && h.available === false) note = '<span class="form-hint">no connection table</span>';
            else if (h.readable && h.connections != null) {
                note = `<span class="sc-up">${h.connections} connections</span>` +
                    (h.accounting ? '' : ' <span class="form-hint">(no byte counts)</span>') +
                    (h.truncated ? ' <span class="form-hint">(first part only)</span>' : '');
            } else if (h.readable) note = '<span class="form-hint">not read</span>';
            return `<li>${this.nodeLink(h.node)} · ${note}</li>`;
        }).join('');
        routeEl.innerHTML = `<p>From <strong>A: ${this.endLabel(d.ends.a)}</strong> to <strong>B: ${this.endLabel(d.ends.b)}</strong>
            <span class="form-hint">· route traced ${this.esc(d.traced_at || 'never')}</span></p>
            <ol class="rt-hops">${hops}</ol>
            ${d.error ? `<p class="sc-down">${this.esc(d.error)}</p>` : ''}`;
        document.getElementById('rt-ends-hint').textContent =
            `Connections from ${d.ends.a.nodes.join(', ') || 'the starting node'} (and hosts on their LANs) to ` +
            `${d.ends.b.nodes.join(', ')} (and hosts on their LANs), either way.`;
        if (d.error) {
            ['rt-ends', 'rt-transit', 'rt-flows'].forEach(id => { document.getElementById(id).innerHTML = ''; });
            return;
        }
        document.getElementById('rt-ends').innerHTML = this.summary(d.summary.ends, 'No connections between the two ends right now.');
        document.getElementById('rt-transit').innerHTML = this.summary(d.summary.transit, 'Nothing else is passing along this route right now.');
        this.renderFlows();
    },

    summary(s, empty) {
        if (!s.flows) return `<p class="log-empty">${empty}</p>`;
        const svc = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Type of traffic</th><th>Connections</th><th>Data</th><th>Packets</th></tr></thead>
            <tbody>${s.services.map(r => `<tr><td>${this.esc(r.service)}</td><td>${r.flows}</td><td>${this.size(r.bytes)}</td>
                <td>${r.packets ?? ''}</td></tr>`).join('')}</tbody></table></div>`;
        const pairs = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>From</th><th>To</th><th>Types</th><th>Connections</th><th>Data</th></tr></thead>
            <tbody>${s.pairs.map(p => `<tr><td>${this.esc(p.src)}${p.src_node ? ` <span class="form-hint">behind ${this.nodeLink(p.src_node)}</span>` : ''}</td>
                <td>${this.esc(p.dst)}${p.dst_node ? ` <span class="form-hint">behind ${this.nodeLink(p.dst_node)}</span>` : ''}</td>
                <td>${p.services.map(x => this.esc(x)).join(', ')}</td><td>${p.flows}</td><td>${this.size(p.bytes)}</td></tr>`).join('')}</tbody></table></div>`;
        return `<p>${s.flows} connection${s.flows === 1 ? '' : 's'}${s.bytes != null ? `, ${this.size(s.bytes)} so far` : ''}.</p>
            <h4>By type</h4>${svc}<h4>By source and destination</h4>${pairs}`;
    },

    renderFlows() {
        const d = this.data;
        if (!d || !d.flows) return;
        const own = document.getElementById('rt-own').checked;
        const q = document.getElementById('rt-filter').value.trim().toLowerCase();
        const rows = d.flows.filter(f => (own || f.category !== 'own') && (!q || [f.service, f.proto, f.src, f.dst, f.src_name,
            f.dst_name, f.src_node, f.dst_node, f.sport, f.dport, ...f.seen_at].some(v => String(v ?? '').toLowerCase().includes(q))));
        document.getElementById('rt-count').textContent = `${rows.length} of ${d.flows.length} connections`;
        const end = (ip, port, name, node) => `${this.esc(name || ip)}${port ? `<span class="form-hint">:${port}</span>` : ''}
            ${name ? `<br><span class="form-hint">${this.esc(ip)}</span>` : ''}${node ? `<br><span class="form-hint">behind ${this.nodeLink(node)}</span>` : ''}`;
        const dir = f => f.direction === 'a_to_b' ? ' (A → B)' : f.direction === 'b_to_a' ? ' (B → A)' : '';
        document.getElementById('rt-flows').innerHTML = rows.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table rt-table">
            <thead><tr><th>Type</th><th>Source</th><th>Destination</th><th>Where</th><th>Sent / received</th><th>State</th><th>Seen at</th></tr></thead>
            <tbody>${rows.map(f => `<tr class="rt-${f.category}">
                <td><strong>${this.esc(f.service)}</strong><br><span class="form-hint">${this.esc(f.proto)}</span></td>
                <td>${end(f.src, f.sport, f.src_name, f.src_node)}</td>
                <td>${end(f.dst, f.dport, f.dst_name, f.dst_node)}</td>
                <td>${this.CAT[f.category]}${dir(f)}${f.nat ? '<br><span class="form-hint">NAT</span>' : ''}</td>
                <td>${f.bytes != null ? `${this.size(f.bytes_out)} / ${this.size(f.bytes_in)}` : ''}
                    ${f.packets != null ? `<br><span class="form-hint">${f.packets_out ?? 0} / ${f.packets_in ?? 0} pkts</span>` : ''}</td>
                <td>${this.esc(f.state || '')}${f.unreplied ? '<br><span class="form-hint">no reply</span>' : ''}</td>
                <td>${f.seen_at.map(n => this.esc(n)).join('<br>')}</td></tr>`).join('')}</tbody></table></div>`
            : '<p class="log-empty">No connections match.</p>';
    },
};

document.addEventListener('DOMContentLoaded', () => RouteTraffic.init());
