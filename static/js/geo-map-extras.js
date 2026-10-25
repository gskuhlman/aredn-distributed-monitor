/**
 * Map page overlays and playback (TODO #38/#39), plugged into geo-map.js
 * through its hooks (edgeStyle, nodeStyle, onNodeClick, afterRender).
 *
 * Overlays (one at a time): RF links coloured by quality or by the weaker
 * side's SNR; single points of failure (spof.py, internet down) drawn thick
 * by nodes behind; Route Watch paths (last trace, hop by hop); Channel Map
 * conflicts as dashed lines between radios.
 * "What if down": clicking a node shows which nodes lose their path to the
 * starting node (spof.node_down) instead of opening the side panel.
 * Playback: a time slider over playback.py frames; nodes coloured by poll
 * answers in that bucket and links by their swimlane state, with the
 * warning events of that moment listed.
 */
const GeoMapExtras = {
    overlay: '',
    data: {},
    down: null,          // {node, lost: Set}
    pb: null,            // playback frames
    pbIndex: 0,
    pbTimer: null,
    extraLayer: null,

    init() {
        if (typeof GeoMap === 'undefined' || !document.getElementById('gm-overlay')) return;
        GeoMap.edgeStyle = (e, type) => this.edgeStyle(e, type);
        GeoMap.nodeStyle = (id, n) => this.nodeStyle(id, n);
        GeoMap.onNodeClick = (id) => this.onNodeClick(id);
        GeoMap.afterRender = (shown) => this.afterRender(shown);
        document.getElementById('gm-overlay').addEventListener('change', (e) => this.setOverlay(e.target.value));
        document.getElementById('gm-whatif').addEventListener('change', (e) => {
            if (!e.target.checked) { this.down = null; GeoMap.render(); }
            this.legend();
        });
        document.getElementById('gm-playback-toggle').addEventListener('click', () => this.togglePlayback());
        document.getElementById('gm-pb-range').addEventListener('change', () => this.loadPlayback());
        document.getElementById('gm-pb-slider').addEventListener('input', (e) => this.showFrame(Number(e.target.value)));
        document.getElementById('gm-pb-play').addEventListener('click', () => this.play());
    },

    pair(a, b) {
        return a < b ? `${a}|${b}` : `${b}|${a}`;
    },

    async setOverlay(kind) {
        this.overlay = kind;
        try {
            if (kind === 'spof' && !this.data.spof) {
                const d = await (await fetch('/api/reports/spof?tunnels=0')).json();
                this.data.spof = new Map((d.links || []).map(l => [this.pair(l.a, l.b), l]));
            } else if (kind === 'routes' && !this.data.routes) {
                const d = await (await fetch('/api/routes')).json();
                this.data.routes = (d.paths || []).filter(p => p.enabled && p.last && p.last.hops?.length);
            } else if (kind === 'conflicts' && !this.data.conflicts) {
                const d = await (await fetch('/api/channel-map')).json();
                this.data.conflicts = d.conflicts || [];
            }
        } catch (error) {
            console.error('Map overlay load error:', error);
        }
        this.legend();
        GeoMap.render();
    },

    qColor(q) {
        if (q == null) return '#7f8c8d';
        return q >= 80 ? '#27ae60' : q >= 50 ? '#d4ac0d' : '#c0392b';
    },

    snrColor(s) {
        if (s == null) return '#7f8c8d';
        return s >= 25 ? '#27ae60' : s >= 15 ? '#d4ac0d' : '#c0392b';
    },

    edgeStyle(e, type) {
        if (this.pb) {
            const lane = this.pb.lanes.get(this.pair(e.from, e.to));
            const state = lane ? lane[this.pbIndex] : null;
            const colors = { good: '#27ae60', degraded: '#d4ac0d', flapping: '#e67e22', down: '#c0392b', unknown: '#95a5a6' };
            if (!state || state === 'none') return { color: '#bdc3c7', opacity: 0.25, dashArray: '2 6' };
            return { color: colors[state] || '#7f8c8d', opacity: 0.9, weight: state === 'down' ? 4 : undefined,
                     dashArray: state === 'unknown' ? '4 6' : null };
        }
        if (this.down) {
            const touches = this.down.lost.has(e.from) || this.down.lost.has(e.to);
            return touches ? { color: '#c0392b', opacity: 0.8 } : { opacity: 0.2 };
        }
        if (this.overlay === 'quality' && type === 'RF') return { color: this.qColor(e.quality), dashArray: null };
        if (this.overlay === 'snr' && type === 'RF') return { color: this.snrColor(e.snr), dashArray: null };
        if ((this.overlay === 'quality' || this.overlay === 'snr') && type !== 'RF') return { opacity: 0.15 };
        if (this.overlay === 'spof') {
            const l = this.data.spof?.get(this.pair(e.from, e.to));
            return l ? { color: this.qColor(l.quality), weight: Math.min(10, 3 + l.behind / 2), opacity: 1, dashArray: null }
                : { opacity: 0.15 };
        }
        if (this.overlay === 'routes' || this.overlay === 'conflicts') return { opacity: 0.2 };
        return null;
    },

    nodeStyle(id, n) {
        if (this.pb) {
            const s = this.pb.nodes[id]?.[this.pbIndex];
            const c = { up: ['#2ecc71', '#1e8449'], partial: ['#f4d03f', '#b7950b'], down: ['#e74c3c', '#922b21'] }[s];
            return c ? { background: c[0], border: c[1] } : { background: '#d5d8dc', border: '#99a3a4', opacity: 0.5 };
        }
        if (this.down) {
            if (id === this.down.node) return { background: '#2c3e50', border: '#000' };
            if (this.down.lost.has(id)) return { background: '#e74c3c', border: '#922b21' };
            return { opacity: 0.4, background: (n.color || {}).background, border: (n.color || {}).border };
        }
        return null;
    },

    onNodeClick(id) {
        if (!document.getElementById('gm-whatif')?.checked) return false;
        this.nodeDown(id);
        return true;
    },

    async nodeDown(id) {
        try {
            const d = await (await fetch(`/api/map/node-down/${encodeURIComponent(id)}?tunnels=1`)).json();
            const noTun = await (await fetch(`/api/map/node-down/${encodeURIComponent(id)}?tunnels=0`)).json();
            this.down = { node: id, lost: new Set(d.lost || []), lostNoTunnels: noTun.lost || [], seed: d.seed, isSeed: d.is_seed };
            GeoMap.render();
            this.legend();
        } catch (error) {
            console.error('Node down what-if error:', error);
        }
    },

    afterRender(shown) {
        if (!GeoMap.map) return;
        if (!this.extraLayer) this.extraLayer = L.layerGroup().addTo(GeoMap.map);
        this.extraLayer.clearLayers();
        if (this.pb) return;
        if (this.overlay === 'routes') {
            const palette = ['#8e44ad', '#2980b9', '#16a085', '#d35400', '#c0392b', '#2c3e50'];
            (this.data.routes || []).forEach((p, i) => {
                const names = [p.last.source || p.source, ...p.last.hops.map(h => h.name)].filter(Boolean);
                const pts = names.map(n => shown.get(n)).filter(Boolean);
                if (pts.length < 2) return;
                L.polyline(pts, { color: palette[i % palette.length], weight: 4, opacity: 0.85 })
                    .bindTooltip(GeoMap.esc(`${p.label || p.target}: ${names.join(' → ')}`), { sticky: true })
                    .addTo(this.extraLayer);
            });
        } else if (this.overlay === 'conflicts') {
            const col = { critical: '#c0392b', warning: '#e67e22', info: '#f1c40f' };
            for (const c of this.data.conflicts || []) {
                const a = shown.get(c.a), b = shown.get(c.b);
                if (!a || !b) continue;
                L.polyline([a, b], { color: col[c.severity] || '#7f8c8d', weight: 3, dashArray: '6 6', opacity: 0.9 })
                    .bindTooltip(GeoMap.esc(`${c.severity}: ${c.a} ↔ ${c.b} ${c.relation}` +
                        (c.gap_mhz != null ? ` (${c.gap_mhz} MHz gap)` : '') + `, ${(c.distance_m / 1000).toFixed(1)} km`), { sticky: true })
                    .addTo(this.extraLayer);
            }
        }
    },

    legend() {
        const el = document.getElementById('gm-legend');
        if (!el) return;
        let html = '';
        if (this.down) {
            html = this.down.isSeed ? `<strong>${GeoMap.esc(this.down.node)}</strong> is the starting node: everything is lost.`
                : `<strong>If ${GeoMap.esc(this.down.node)} goes down</strong>: ${this.down.lost.size} node(s) lose their path to ${GeoMap.esc(this.down.seed)}`
                + ` (red); with the internet down too: ${this.down.lostNoTunnels.length}.`
                + (this.down.lost.size ? `<div class="gm-legend-list">${[...this.down.lost].map(n => GeoMap.esc(n)).join(', ')}</div>` : '');
        } else if (document.getElementById('gm-whatif')?.checked) {
            html = 'Click a node to see what loses its connection if it goes down.';
        } else if (this.overlay === 'quality') {
            html = 'RF quality: <span style="color:#27ae60">≥ 80%</span> · <span style="color:#d4ac0d">50–79%</span> · <span style="color:#c0392b">&lt; 50%</span>';
        } else if (this.overlay === 'snr') {
            html = 'RF SNR: <span style="color:#27ae60">≥ 25 dB</span> · <span style="color:#d4ac0d">15–24</span> · <span style="color:#c0392b">&lt; 15</span>';
        } else if (this.overlay === 'spof') {
            html = `Single points of failure with the internet down: ${this.data.spof?.size ?? 0} link(s); thicker = more nodes behind.`;
        } else if (this.overlay === 'routes') {
            html = `Route Watch: ${this.data.routes?.length ?? 0} path(s), last trace.`;
        } else if (this.overlay === 'conflicts') {
            html = `Channel conflicts: ${this.data.conflicts?.length ?? 0} (<span style="color:#c0392b">critical</span>, <span style="color:#e67e22">warning</span>, <span style="color:#d4ac0d">info</span>).`;
        }
        el.innerHTML = html;
        el.hidden = !html;
    },

    // ---------------------------------------------------------- playback --
    togglePlayback() {
        const box = document.getElementById('gm-playback');
        box.hidden = !box.hidden;
        document.getElementById('gm-playback-toggle').classList.toggle('active', !box.hidden);
        if (box.hidden) {
            this.stop();
            this.pb = null;
            GeoMap.render();
        } else {
            this.loadPlayback();
        }
    },

    localInput(d) {
        const p = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
    },

    async loadPlayback() {
        this.stop();
        const hours = Number(document.getElementById('gm-pb-range').value) || 24;
        const end = new Date();
        const start = new Date(end.getTime() - hours * 3600e3);
        document.getElementById('gm-pb-time').textContent = 'Loading…';
        try {
            const d = await (await fetch(`/api/map/playback?start=${this.localInput(start)}&end=${this.localInput(end)}`)).json();
            if (d.error) throw new Error(d.error);
            const lanes = new Map(d.links.map(l => [this.pair(l.a, l.b), l.cells]));
            this.pb = { buckets: d.window.buckets, nodes: d.nodes, lanes, events: d.events };
            const slider = document.getElementById('gm-pb-slider');
            slider.max = String(d.window.buckets.length - 1);
            slider.value = slider.max;
            this.showFrame(d.window.buckets.length - 1);
        } catch (error) {
            document.getElementById('gm-pb-time').textContent = `Failed: ${error.message}`;
        }
    },

    showFrame(i) {
        if (!this.pb) return;
        this.pbIndex = i;
        document.getElementById('gm-pb-slider').value = String(i);
        const t = this.pb.buckets[i];
        const down = Object.values(this.pb.nodes).filter(c => c[i] === 'down').length;
        document.getElementById('gm-pb-time').textContent = `${t.slice(0, 16)} · ${down} node(s) not answering`;
        const evs = this.pb.events.filter(e => e.bucket === i);
        document.getElementById('gm-pb-events').innerHTML = evs.slice(0, 12).map(e =>
            `<div class="gm-pb-event sev-${GeoMap.esc(e.severity)}">${GeoMap.esc(e.time.slice(11, 16))} ${GeoMap.esc(e.type.replace(/_/g, ' '))}
             ${GeoMap.esc(e.node || '')}</div>`).join('') + (evs.length > 12 ? `<div class="form-hint">+${evs.length - 12} more</div>` : '');
        GeoMap.render();
    },

    play() {
        if (this.pbTimer) return this.stop();
        if (!this.pb) return;
        const btn = document.getElementById('gm-pb-play');
        btn.textContent = 'Pause';
        if (this.pbIndex >= this.pb.buckets.length - 1) this.pbIndex = 0;
        this.pbTimer = setInterval(() => {
            if (this.pbIndex >= this.pb.buckets.length - 1) return this.stop();
            this.showFrame(this.pbIndex + 1);
        }, 400);
    },

    stop() {
        if (this.pbTimer) clearInterval(this.pbTimer);
        this.pbTimer = null;
        const btn = document.getElementById('gm-pb-play');
        if (btn) btn.textContent = 'Play';
    }
};

document.addEventListener('DOMContentLoaded', () => GeoMapExtras.init());
