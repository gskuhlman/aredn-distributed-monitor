/**
 * Field mode (/field/): phone-sized map and node list for site visits.
 * Uses the Channel Map's base map (offline Protomaps extracts from the
 * laptop, online tiles on top when there is internet) and /api/network.
 * The service worker (/field/sw.js) keeps the last answers, so the pages and
 * the last data still open when the phone loses its connection; a banner
 * says how old the data is then.
 */
const Field = {
    map: null,
    layers: null,
    data: null,
    fitted: false,

    async init() {
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/field/sw.js', { scope: '/field/' }).catch(e => console.warn('SW:', e));
        }
        document.querySelectorAll('.field-tab[data-fd-tab]').forEach(b => b.addEventListener('click', () => this.tab(b.dataset.fdTab)));
        document.getElementById('fd-search').addEventListener('input', () => this.renderList());
        document.getElementById('fd-list').addEventListener('click', (e) => {
            const row = e.target.closest('[data-node]');
            if (row) this.sheet(row.dataset.node);
        });
        document.getElementById('fd-sheet').addEventListener('click', (e) => {
            if (e.target.closest('[data-close]')) document.getElementById('fd-sheet').hidden = true;
        });
        window.addEventListener('online', () => this.load());
        window.addEventListener('offline', () => this.conn(false));
        await this.initMap();
        await this.load();
        setInterval(() => this.load(), 60000);
    },

    tab(name) {
        document.querySelectorAll('.field-tab[data-fd-tab]').forEach(b => b.classList.toggle('active', b.dataset.fdTab === name));
        document.querySelectorAll('.field-view').forEach(v => v.classList.toggle('active', v.id === `fd-${name}-view`));
        if (name === 'map' && this.map) setTimeout(() => this.map.invalidateSize(), 50);
    },

    conn(ok, cachedAt) {
        const el = document.getElementById('fd-conn');
        el.className = 'field-conn ' + (ok ? 'ok' : 'off');
        el.textContent = ok ? `live ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
            : `offline${cachedAt ? ' · data from ' + cachedAt : ''}`;
    },

    async initMap() {
        try {
            await ChannelMap.ensureBase();
        } catch (e) {
            document.getElementById('fd-map').innerHTML = '<p class="log-empty">Map library unavailable.</p>';
            return;
        }
        this.map = L.map('fd-map', { zoomControl: true }).setView([42.0, -91.67], 11);
        ChannelMap.addBaseLayers(this.map);
        this.layers = { links: L.layerGroup().addTo(this.map), nodes: L.layerGroup().addTo(this.map) };
    },

    async load() {
        try {
            const r = await fetch('/api/network');
            const d = await r.json();
            this.data = d;
            const cached = r.headers.get('X-Field-Cache');
            this.conn(!cached, cached);
            this.render();
            this.renderList();
        } catch (e) {
            this.conn(false);
        }
    },

    reach(n) {
        return n.reach_status || (n.is_link_only ? 'link_only' : (n.is_inactive ? 'down' : 'polled'));
    },

    color(n) {
        return { polled: '#27ae60', via_mesh: '#f39c12', down: '#c0392b', link_only: '#95a5a6' }[this.reach(n)] || '#2980b9';
    },

    render() {
        if (!this.map || !this.data) return;
        const nodes = (this.data.nodes || []).filter(n => n.lat != null && n.lon != null && (Number(n.lat) || Number(n.lon)));
        const byId = new Map(nodes.map(n => [n.id, n]));
        this.layers.links.clearLayers();
        this.layers.nodes.clearLayers();
        for (const e of this.data.edges || []) {
            const t = String(e.link_type || '').toUpperCase();
            if (t !== 'RF' && t !== 'DTD') continue;
            const a = byId.get(e.from), b = byId.get(e.to);
            if (!a || !b) continue;
            const q = e.quality;
            L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
                color: t === 'DTD' ? '#2874a6' : (q >= 80 ? '#27ae60' : q >= 50 ? '#d4ac0d' : '#c0392b'),
                weight: t === 'DTD' ? 1.5 : 3, opacity: 0.8
            }).addTo(this.layers.links);
        }
        for (const n of nodes) {
            const m = L.circleMarker([n.lat, n.lon], {
                radius: n.is_infrastructure ? 8 : 6, color: '#2c3e50', weight: 1, fillColor: this.color(n), fillOpacity: 0.9
            });
            m.on('click', () => this.sheet(n.id));
            m.addTo(this.layers.nodes);
        }
        if (!this.fitted && nodes.length) {
            this.fitted = true;
            // Densest 25 km cluster, like the Map page.
            const R = 6371, rad = Math.PI / 180;
            const km = (p, q) => 2 * R * Math.asin(Math.sqrt(Math.sin((q.lat - p.lat) * rad / 2) ** 2
                + Math.cos(p.lat * rad) * Math.cos(q.lat * rad) * Math.sin((q.lon - p.lon) * rad / 2) ** 2));
            let best = [];
            for (const p of nodes) {
                const w = nodes.filter(q => km(p, q) <= 25);
                if (w.length > best.length) best = w;
            }
            this.map.fitBounds(L.latLngBounds(best.map(p => [p.lat, p.lon])).pad(0.1));
        }
    },

    renderList() {
        if (!this.data) return;
        const q = (document.getElementById('fd-search').value || '').toLowerCase();
        const rank = { down: 0, via_mesh: 1, polled: 2, link_only: 3 };
        const nodes = (this.data.nodes || []).filter(n => !q || n.id.toLowerCase().includes(q))
            .sort((a, b) => (rank[this.reach(a)] ?? 9) - (rank[this.reach(b)] ?? 9) || a.id.localeCompare(b.id));
        document.getElementById('fd-list').innerHTML = nodes.map(n => `<div class="field-row" data-node="${this.esc(n.id)}">
            <span class="field-dot" style="background:${this.color(n)}"></span>
            <span class="field-name">${this.esc(n.id)}</span>
            <span class="form-hint">${this.esc(this.reach(n).replace('_', ' '))}</span></div>`).join('');
    },

    sheet(id) {
        const n = (this.data?.nodes || []).find(x => x.id === id);
        if (!n) return;
        const links = (this.data.edges || []).filter(e => e.from === id || e.to === id);
        const el = document.getElementById('fd-sheet');
        el.innerHTML = `<div class="field-sheet-head"><strong>${this.esc(id)}</strong>
                <button class="btn btn-small btn-secondary" data-close>Close</button></div>
            <p><span class="field-dot" style="background:${this.color(n)}"></span> ${this.esc(this.reach(n).replace('_', ' '))}
                ${n.is_infrastructure ? ' · infrastructure' : ''}</p>
            <div class="field-links">${links.map(e => {
                const peer = e.from === id ? e.to : e.from;
                return `<div>${this.esc(peer)} <small>${this.esc(e.link_type)}${e.quality != null ? ` ${Math.round(e.quality)}%` : ''}${e.snr != null ? ` · ${e.snr} dB` : ''}</small></div>`;
            }).join('') || '<p class="form-hint">No links.</p>'}</div>
            <div class="field-actions">
                <a class="btn btn-primary" href="/field/align?node=${encodeURIComponent(id)}">Align antenna</a>
                <a class="btn btn-secondary" href="/nodes/${encodeURIComponent(id)}">Full node page</a>
            </div>`;
        el.hidden = false;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => Field.init());
