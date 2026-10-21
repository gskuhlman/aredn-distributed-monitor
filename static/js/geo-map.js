/**
 * Map tab: the Topology graph on the real-world map. Draws exactly what the
 * Topology page shows (network.js's filtered and colored nodesDataset /
 * edgesDataset, so the header filters apply here too) on the Channel Map's
 * base map (offline Protomaps extract and/or online tiles, via
 * ChannelMap.ensureBase / addBaseLayers). Radios on one mast are fanned out
 * in a small ring around their true position; nodes without a location are
 * drawn next to a located DtD neighbour when they have one, otherwise listed.
 * Clicking a node opens the same side panel as the Topology page. Redrawn
 * after every graph update while the tab is open.
 */
const GeoMap = {
    CO_SITE_M: 60,          // nodes this close share one ring
    LABEL_ZOOM: 14,         // names shown from this zoom up
    FIT_KM: 25,             // Fit / first view: the densest cluster of this radius
    map: null,
    layers: null,
    fitted: false,
    sized: false,           // set once Leaflet knows the container size
    dirty: true,
    initialized: false,

    init() {
        if (this.initialized) return;
        this.initialized = true;
        const rerender = () => this.render();
        ['gm-tunnels', 'gm-dtd', 'gm-labels'].forEach(id =>
            document.getElementById(id)?.addEventListener('change', rerender));
        document.getElementById('gm-fit')?.addEventListener('click', () => this.fit());
        document.getElementById('gm-unlocated')?.addEventListener('click', (event) => {
            const a = event.target.closest('a[data-node]');
            if (!a) return;
            event.preventDefault();
            if (typeof showNodeDetails === 'function') showNodeDetails(a.dataset.node);
        });
        // Redraw whenever the Topology graph is updated (scans, filters).
        if (typeof window.updateNetwork === 'function') {
            const original = window.updateNetwork;
            window.updateNetwork = function (...args) {
                const result = original.apply(this, args);
                GeoMap.dirty = true;
                if (GeoMap.isOpen()) GeoMap.render();
                return result;
            };
        }
    },

    isOpen() {
        return document.getElementById('geo-map-view')?.classList.contains('active');
    },

    async open() {
        this.init();
        try {
            await ChannelMap.ensureBase();
        } catch (error) {
            document.getElementById('gm-map').innerHTML =
                '<p class="log-empty">The map library (Leaflet) could not be loaded.</p>';
            return;
        }
        if (!this.map) {
            this.map = L.map('gm-map', { zoomControl: true }).setView([42.0, -91.67], 11);
            ChannelMap.addBaseLayers(this.map);
            this.layers = {
                links: L.layerGroup().addTo(this.map),
                leaders: L.layerGroup().addTo(this.map),
                nodes: L.layerGroup().addTo(this.map)
            };
            this.map.on('zoomend', () => this.render());
        }
        setTimeout(() => {
            this.map.invalidateSize();
            this.sized = true;
            this.render();
        }, 50);
    },

    /** Nodes and edges as the Topology page currently shows them. */
    graph() {
        if (typeof nodesDataset !== 'undefined' && nodesDataset) {
            return { nodes: nodesDataset.get(), edges: edgesDataset.get() };
        }
        const data = typeof filterNetworkData === 'function' ? filterNetworkData(fullNetworkData) : { nodes: [], edges: [] };
        return data;
    },

    distanceM(a, b) {
        const R = 6371000, rad = Math.PI / 180;
        const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
        const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
        return 2 * R * Math.asin(Math.sqrt(h));
    },

    /** Position for every node that has one (its own, or a DtD neighbour's). */
    positions(nodes, edges) {
        const pos = new Map();
        for (const n of nodes) {
            const lat = Number(n.lat), lon = Number(n.lon);
            if (n.lat != null && n.lon != null && Number.isFinite(lat) && Number.isFinite(lon) && (lat || lon)) {
                pos.set(n.id, { lat, lon, borrowed: null });
            }
        }
        for (let pass = 0; pass < 3; pass++) {
            for (const e of edges) {
                if (String(e.link_type || '').toUpperCase() !== 'DTD') continue;
                for (const [a, b] of [[e.from, e.to], [e.to, e.from]]) {
                    if (!pos.has(a) && pos.has(b)) {
                        const p = pos.get(b);
                        pos.set(a, { lat: p.lat, lon: p.lon, borrowed: p.borrowed || b });
                    }
                }
            }
        }
        return pos;
    },

    /** Groups of nodes within CO_SITE_M of each other (greedy). */
    groups(ids, pos) {
        const groups = [];
        for (const id of ids) {
            const p = pos.get(id);
            const g = groups.find(x => this.distanceM(x.center, p) <= this.CO_SITE_M);
            if (g) {
                g.ids.push(id);
            } else {
                groups.push({ center: { lat: p.lat, lon: p.lon }, ids: [id] });
            }
        }
        return groups;
    },

    render() {
        if (!this.map || !this.layers) return;
        const { nodes, edges } = this.graph();
        const byId = new Map(nodes.map(n => [n.id, n]));
        const pos = this.positions(nodes, edges);
        const located = nodes.filter(n => pos.has(n.id)).map(n => n.id).sort();
        const unlocated = nodes.filter(n => !pos.has(n.id));

        const tunnels = document.getElementById('gm-tunnels')?.checked ?? true;
        const dtd = document.getElementById('gm-dtd')?.checked ?? true;
        const labels = (document.getElementById('gm-labels')?.checked ?? true) && this.map.getZoom() >= this.LABEL_ZOOM;

        // Fan co-sited nodes out in a ring (pixel offsets at this zoom).
        const shown = new Map();
        const leftLabel = new Set();    // ring members on the left half: name on the left
        const leaders = [];
        for (const g of this.groups(located, pos)) {
            const c = this.map.latLngToLayerPoint([g.center.lat, g.center.lon]);
            if (g.ids.length === 1) {
                const p = pos.get(g.ids[0]);
                shown.set(g.ids[0], L.latLng(p.lat, p.lon));
                continue;
            }
            const radius = labels ? Math.max(30, g.ids.length * 9) : Math.max(16, g.ids.length * 5);
            g.ids.forEach((id, i) => {
                const angle = (2 * Math.PI * i) / g.ids.length - Math.PI / 2;
                const ll = this.map.layerPointToLatLng(L.point(c.x + radius * Math.cos(angle), c.y + radius * Math.sin(angle)));
                shown.set(id, ll);
                if (Math.cos(angle) < -0.01) leftLabel.add(id);
                leaders.push([[g.center.lat, g.center.lon], ll]);
            });
        }

        this.layers.links.clearLayers();
        this.layers.leaders.clearLayers();
        this.layers.nodes.clearLayers();

        let drawnLinks = 0, offMap = 0;
        for (const e of edges) {
            const type = String(e.link_type || '').toUpperCase();
            if (type === 'DTD' && !dtd) continue;
            if (type !== 'RF' && type !== 'DTD' && !tunnels) continue;
            const a = shown.get(e.from), b = shown.get(e.to);
            if (!a || !b) {
                offMap++;
                continue;
            }
            const color = (e.color && (e.color.color || e.color)) || '#7f8c8d';
            const dashes = Array.isArray(e.dashes) ? e.dashes.join(' ') : (e.dashes ? '6 4' : null);
            const line = L.polyline([a, b], {
                color, weight: type === 'DTD' ? 1.5 : Math.max(2, (e.width || 1) + 1), dashArray: dashes,
                opacity: type === 'RF' ? 0.9 : (type === 'DTD' ? 0.45 : 0.6)
            });
            if (e.title) line.bindTooltip(this.esc(e.title).replace(/\n/g, '<br>'), { sticky: true });
            line.on('click', () => {
                if (type === 'RF' && typeof RFStats !== 'undefined') {
                    document.querySelector('.tab-btn[data-tab="rf-stats"]')?.click();
                    setTimeout(() => RFStats.selectLink(e.from, e.to), 300);
                }
            });
            line.addTo(this.layers.links);
            drawnLinks++;
        }
        for (const ll of leaders) {
            L.polyline(ll, { color: '#566573', weight: 1, opacity: 0.5, interactive: false }).addTo(this.layers.leaders);
        }

        for (const id of located) {
            const n = byId.get(id);
            const color = n.color || {};
            const infra = n.shape === 'diamond' || n.is_infrastructure;
            const size = n.is_supernode ? 18 : (infra ? 15 : 13);
            const borrowed = pos.get(id).borrowed;
            const icon = L.divIcon({
                className: 'geo-node-icon',
                iconSize: [size, size],
                html: `<div class="geo-node${infra ? ' geo-node-infra' : ''}${borrowed ? ' geo-node-borrowed' : ''}"
                    style="width:${size}px;height:${size}px;background:${this.esc(color.background || '#97C2FC')};
                    border-color:${this.esc(color.border || '#2B7CE9')};opacity:${n.opacity ?? 1}"></div>`
                    + (labels ? `<span class="geo-label" style="${leftLabel.has(id) ? 'right' : 'left'}:${size + 3}px">${this.esc(id)}</span>` : '')
            });
            const marker = L.marker(shown.get(id), { icon, riseOnHover: true, zIndexOffset: infra ? 500 : 0 });
            const title = this.esc(n.title || n.id).replace(/\n/g, '<br>')
                + (borrowed ? `<br><i>No location stored: drawn at its DtD neighbour ${this.esc(borrowed)}</i>` : '');
            marker.bindTooltip(title, { direction: 'top', offset: [0, -size / 2] });
            marker.on('click', () => { if (typeof showNodeDetails === 'function') showNodeDetails(id); });
            marker.addTo(this.layers.nodes);
        }

        const status = document.getElementById('gm-status');
        if (status) {
            status.textContent = `${located.length} node(s) on the map · ${drawnLinks} link(s)`
                + (offMap ? ` · ${offMap} link(s) to nodes without a location not drawn` : '');
        }
        const box = document.getElementById('gm-unlocated');
        if (box) {
            box.hidden = !unlocated.length;
            box.innerHTML = unlocated.length ? `<summary>${unlocated.length} without a location</summary>
                <div class="gm-unlocated-pop"><div class="gm-unlocated-list">${unlocated.map(n =>
                    `<a href="#" data-node="${this.esc(n.id)}">${this.esc(n.id)}</a>`).join('')}</div>
                <p class="form-hint">Set the location on the node (Setup &gt; Location) to place it.</p></div>` : '';
        }
        if (!this.fitted && this.sized && located.length) this.fit();
        this.dirty = false;
    },

    /** Zoom to the located nodes, ignoring far-away tunnel ends. */
    fit() {
        if (!this.map) return;
        const { nodes, edges } = this.graph();
        const pos = this.positions(nodes, edges);
        const pts = [...pos.values()];
        if (!pts.length) return;
        // Densest cluster: the node with the most others within FIT_KM, so
        // far tunnel ends (and bad coordinates) don't zoom the map out.
        const within = p => pts.filter(q => this.distanceM(p, q) <= this.FIT_KM * 1000);
        let near = [];
        for (const p of pts) {
            const w = within(p);
            if (w.length > near.length) near = w;
        }
        const use = near.length ? near : pts;
        this.fitted = true;
        this.map.fitBounds(L.latLngBounds(use.map(p => [p.lat, p.lon])).pad(0.1));
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
