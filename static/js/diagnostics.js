/**
 * Diagnostics tab: mesh-wide flap and SNR-asymmetry reports.
 * Reads the deterministic /api/reports/* endpoints (no AI).
 *
 * Filters/sorts mirror the Nodes page. Because diagnostics rows are *links*,
 * a node-status filter keeps a link when EITHER endpoint matches, and node-based
 * sorts (alpha/last-seen/firmware/model) key off the link's source node.
 */
const DiagnosticsModule = {
    initialized: false,
    flaps: [],
    asym: [],
    nodeMap: {},
    networkData: { nodes: [], edges: [] },

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('diag-refresh')?.addEventListener('click', () => this.load());
        document.getElementById('diag-hours')?.addEventListener('change', () => this.load());
        document.getElementById('diag-status-filter')?.addEventListener('change', () => this.applyView());
        document.getElementById('diag-type-filter')?.addEventListener('change', () => this.applyView());
        document.getElementById('diag-sort')?.addEventListener('change', () => this.applyView());
        const search = document.getElementById('diag-node-filter');
        if (search) {
            search.addEventListener('input', () => this.applyView());
        }
    },

    async load() {
        const hours = document.getElementById('diag-hours')?.value || 24;
        this.setStatus('Loading...');
        try {
            const [flapsResp, asymResp, nodesResp, netResp] = await Promise.all([
                fetch(`/api/reports/flaps?hours=${encodeURIComponent(hours)}`),
                fetch('/api/reports/asymmetry?min_delta=3'),
                fetch('/api/nodes/all'),
                fetch('/api/network')
            ]);
            this.flaps = (await flapsResp.json()).links || [];
            this.asym = (await asymResp.json()).links || [];
            const nodes = nodesResp.ok ? await nodesResp.json() : [];
            this.networkData = netResp.ok ? await netResp.json() : { nodes: [], edges: [] };

            this.nodeMap = {};
            for (const n of nodes) {
                if (n.name) this.nodeMap[n.name] = n;
            }
            this.applyView();
            this.setStatus(`Updated ${new Date().toLocaleTimeString()}`);
        } catch (error) {
            this.setStatus('Failed to load');
            console.error('Diagnostics load error:', error);
        }
    },

    // ---- selected-node sets (same logic as the Nodes page) ----
    selectedNodeNames() {
        const selected = new Set();
        for (const name in this.nodeMap) {
            if (this.nodeMap[name].is_selected) selected.add(name);
        }
        for (const node of this.networkData.nodes || []) {
            if (node.is_selected && node.id) selected.add(node.id);
        }
        return selected;
    },

    selectedConnectedNames() {
        const selected = this.selectedNodeNames();
        const visible = new Set(selected);
        for (const edge of this.networkData.edges || []) {
            const t = String(edge.link_type || '').toUpperCase();
            if (t !== 'RF' && t !== 'DTD') continue;
            if (selected.has(edge.from) || selected.has(edge.to)) {
                visible.add(edge.from);
                visible.add(edge.to);
            }
        }
        return visible;
    },

    nodeMatchesStatus(name, status, ctx) {
        if (status === 'all') return true;
        const node = this.nodeMap[name];
        if (status === 'selected') return ctx.selected.has(name);
        if (status === 'selected-connected') return ctx.selectedConnected.has(name);
        if (!node) return false;  // link-only endpoints have no nodes row
        if (status === 'active') return node.is_active === 1 && !node.is_link_only;
        if (status === 'link-only') return !!node.is_link_only;
        if (status === 'inactive') return node.is_active !== 1;
        if (status === '24h' || status === '7d') {
            const span = (status === '24h' ? 1 : 7) * 24 * 60 * 60 * 1000;
            const seen = node.last_seen ? new Date(node.last_seen).getTime() : 0;
            return (Date.now() - seen) < span;
        }
        return true;
    },

    linkPasses(source, target, search, status, ctx) {
        const matchesSearch = !search ||
            source.toLowerCase().includes(search) || target.toLowerCase().includes(search);
        if (!matchesSearch) return false;
        if (status === 'all') return true;
        // Keep the link if either endpoint matches the node status.
        return this.nodeMatchesStatus(source, status, ctx) || this.nodeMatchesStatus(target, status, ctx);
    },

    sortRows(rows, sortBy) {
        if (sortBy === 'relevance') return rows;  // keep backend ranking
        const node = (n) => this.nodeMap[n] || {};
        const sorted = [...rows];
        sorted.sort((a, b) => {
            if (sortBy === 'alpha') {
                return (a.source_node || '').localeCompare(b.source_node || '');
            } else if (sortBy === 'last-seen') {
                const t = (r) => Math.max(
                    node(r.source_node).last_seen ? new Date(node(r.source_node).last_seen).getTime() : 0,
                    node(r.target_node).last_seen ? new Date(node(r.target_node).last_seen).getTime() : 0
                );
                return t(b) - t(a);
            } else if (sortBy === 'firmware') {
                return (node(a.source_node).firmware_version || '').localeCompare(node(b.source_node).firmware_version || '');
            } else if (sortBy === 'model') {
                return (node(a.source_node).model || '').localeCompare(node(b.source_node).model || '');
            }
            return 0;
        });
        return sorted;
    },

    linkTypeBucket(linkType) {
        const t = String(linkType || '').toUpperCase();
        if (t === 'RF') return 'rf';
        if (t === 'WIREGUARD' || t === 'WG') return 'wireguard';
        if (t === 'TUN' || t === 'TUNNEL' || t === 'VTUN') return 'tunnel';
        return null;
    },

    applyView() {
        const search = (document.getElementById('diag-node-filter')?.value || '').trim().toLowerCase();
        const status = document.getElementById('diag-status-filter')?.value || 'all';
        const typeFilter = document.getElementById('diag-type-filter')?.value || 'all';
        const sortBy = document.getElementById('diag-sort')?.value || 'relevance';
        const ctx = { selected: this.selectedNodeNames(), selectedConnected: this.selectedConnectedNames() };

        const flaps = this.sortRows(
            this.flaps.filter(l =>
                this.linkPasses(l.source_node, l.target_node, search, status, ctx)
                && (typeFilter === 'all' || this.linkTypeBucket(l.link_type) === typeFilter)),
            sortBy
        );
        // Asymmetry is computed for RF links only, so it is empty unless the
        // type filter is "all" or "rf".
        const asym = this.sortRows(
            this.asym.filter(l =>
                this.linkPasses(l.source_node, l.target_node, search, status, ctx)
                && (typeFilter === 'all' || typeFilter === 'rf')),
            sortBy
        );
        this.renderFlaps(flaps);
        this.renderAsymmetry(asym);
    },

    renderFlaps(links) {
        const body = document.getElementById('diag-flaps-body');
        if (!body) return;
        if (!links.length) {
            body.innerHTML = '<tr><td colspan="9" class="log-empty">No link transitions match.</td></tr>';
            return;
        }
        body.innerHTML = links.map(l => {
            const reason = l.top_block_reason
                ? `<span class="flap-cause flap-link">${this.esc(l.top_block_reason)}</span>` : '-';
            const rate = l.flaps_per_hour ?? 0;
            const rateClass = rate >= 1 ? 'poor' : rate > 0 ? 'marginal' : 'good';
            // Scanner-to-node trouble: source unreachable, or downs we only
            // inferred because we couldn't reach the source node.
            const scannerLost = (l.scanner_unreachable || 0) + (l.inferred_downs || 0);
            const scannerCell = scannerLost > 0
                ? `<span class="health-val health-marginal" title="Times the scanner could not reach the source node (not a peer flap)">${scannerLost}</span>`
                : '<span class="health-val health-good">0</span>';
            return `<tr>
                <td><a href="/nodes/${encodeURIComponent(l.source_node)}">${this.esc(l.source_node)}</a></td>
                <td><a href="/nodes/${encodeURIComponent(l.target_node)}">${this.esc(l.target_node)}</a></td>
                <td>${this.esc(l.link_type || '')}</td>
                <td>${l.transitions}</td>
                <td><span class="health-val health-${rateClass}" title="${l.node_reported_downs || 0} node-reported peer down(s)">${rate}</span></td>
                <td>${scannerCell}</td>
                <td>${l.blocks}</td>
                <td>${reason}</td>
                <td>${this.esc(l.last_change || '')}</td>
            </tr>`;
        }).join('');
    },

    renderAsymmetry(links) {
        const body = document.getElementById('diag-asym-body');
        if (!body) return;
        if (!links.length) {
            body.innerHTML = '<tr><td colspan="7" class="log-empty">No asymmetric links match.</td></tr>';
            return;
        }
        body.innerHTML = links.map(l => {
            const cls = l.max_delta >= 6 ? 'poor' : 'marginal';
            return `<tr>
                <td><a href="/nodes/${encodeURIComponent(l.source_node)}">${this.esc(l.source_node)}</a></td>
                <td><a href="/nodes/${encodeURIComponent(l.target_node)}">${this.esc(l.target_node)}</a></td>
                <td>${this.fmt(l.snr)}</td>
                <td>${this.fmt(l.rev_snr)}</td>
                <td>${this.fmt(l.within_row_delta)}</td>
                <td>${this.fmt(l.cross_direction_delta)}</td>
                <td><span class="health-val health-${cls}">${this.fmt(l.max_delta)} dB</span></td>
            </tr>`;
        }).join('');
    },

    fmt(value) {
        if (value === null || value === undefined) return '-';
        return String(Number(Number(value).toFixed(1)));
    },

    setStatus(text) {
        const el = document.getElementById('diag-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
