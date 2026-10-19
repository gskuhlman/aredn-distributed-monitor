/**
 * Channel Map tab: every located radio on a Leaflet map, colored by
 * frequency, with antenna beams as wedges, RF links as solid lines and
 * channel conflicts as dashed lines. All conflict rules live in
 * channel_map.py; the what-if plan is recomputed server-side and never saved.
 *
 * Self-contained: hooks its own tab button and lazy-loads the bundled Leaflet
 * the first time the tab opens (the rest of the app works without it). The
 * base map is the local Protomaps extract when /api/map/base finds one
 * (fully offline), otherwise online OpenStreetMap tiles.
 */
const ChannelMap = {
    LEAFLET_JS: '/static/vendor/leaflet-1.9.4/leaflet.js',
    LEAFLET_CSS: '/static/vendor/leaflet-1.9.4/leaflet.css',
    PROTOMAPS_JS: '/static/vendor/protomaps-leaflet-5.1.0.min.js',
    HAM_LOW: 5650,
    HAM_HIGH: 5925,
    SEVERITY_RANK: { critical: 0, warning: 1, info: 2 },
    COLLINS_PLAN: [
        '# Proposed Collins Tower / kf9mt plan',
        'w0gq-col-rkt-s 5865 20', 'w0gq-col-rkt-w 5865 20',
        'kf9mt-home-litebeam 5865 20', 'ka0ies-square-one 5865 20',
        'w0gq-col-rkt-n 5910 20', 'w0gq-col-rkt-e 5910 20',
        'kf0jcl-qth-litebeam5ac 5910 20', 'kj0dx-qth-ub 5910 20',
        'w0gq-col-sq-2mmc 5675 40', 'w0gq-mmc-sq-2col 5675 40',
        'w0gq-col-sq-2bldg112 5735 40', 'w0gq-col-sq-2hiwt 5795 40'
    ].join('\n'),

    initialized: false,
    map: null,
    layers: null,
    data: null,
    whatIf: null,
    selected: null,
    fitted: false,
    leafletPromise: null,
    baseMap: null,

    hookTab() {
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const view = document.getElementById('channel-map-view');
                if (!view) return;
                if (btn.dataset.tab === 'channel-map') {
                    view.classList.add('active');
                    this.open();
                } else {
                    view.classList.remove('active');
                }
            });
        });
    },

    async open() {
        if (!this.initialized) {
            this.initialized = true;
            this.bindControls();
        }
        try {
            await this.loadLeaflet();
            if (!this.baseMap) await this.loadBaseMap();
        } catch (error) {
            document.getElementById('cm-map').innerHTML =
                '<p class="log-empty">The map library (Leaflet) could not be loaded.</p>';
            return;
        }
        if (!this.map) this.createMap();
        setTimeout(() => this.map.invalidateSize(), 50);
        if (!this.data) this.load();
    },

    loadLeaflet() {
        if (window.L) return Promise.resolve();
        if (this.leafletPromise) return this.leafletPromise;
        this.leafletPromise = new Promise((resolve, reject) => {
            const css = document.createElement('link');
            css.rel = 'stylesheet';
            css.href = this.LEAFLET_CSS;
            document.head.appendChild(css);
            const js = document.createElement('script');
            js.src = this.LEAFLET_JS;
            js.onload = () => resolve();
            js.onerror = () => { this.leafletPromise = null; reject(new Error('leaflet load failed')); };
            document.head.appendChild(js);
        });
        return this.leafletPromise;
    },

    loadScript(src) {
        return new Promise((resolve, reject) => {
            const js = document.createElement('script');
            js.src = src;
            js.onload = () => resolve();
            js.onerror = () => reject(new Error(`${src} failed to load`));
            document.head.appendChild(js);
        });
    },

    /** Use the local .pmtiles extract when the server has one. */
    async loadBaseMap() {
        this.baseMap = { offline: false };
        try {
            const info = await (await fetch('/api/map/base')).json();
            if (info.offline) {
                if (!window.protomapsL) await this.loadScript(this.PROTOMAPS_JS);
                this.baseMap = info;
            }
        } catch (error) {
            console.warn('Offline map unavailable, using online tiles:', error);
        }
    },

    createMap() {
        this.map = L.map('cm-map', { zoomControl: true, preferCanvas: false }).setView([42.0, -91.67], 11);
        const online = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap contributors'
        });
        if (this.baseMap.offline) {
            // Least detailed layer first, drawn underneath and enlarged past
            // its zoom; each more detailed layer only draws inside its own
            // bounds, from the zoom where the layer below runs out.
            const layers = this.baseMap.layers.map((m, i, all) => {
                const [west, south, east, north] = m.bounds;
                return protomapsL.leafletLayer({
                    url: m.url, flavor: 'light', lang: 'en', maxZoom: 19, maxDataZoom: m.max_zoom,
                    minZoom: i ? all[i - 1].max_zoom + 1 : 0,
                    bounds: i ? [[south, west], [north, east]] : undefined,
                    attribution: i ? '' : '&copy; OpenStreetMap contributors · Protomaps (offline, ' +
                        `${this.esc(m.updated)})`
                });
            });
            const offline = L.layerGroup(layers).addTo(this.map);
            L.control.layers({ 'Offline map': offline, 'OpenStreetMap (online)': online },
                null, { position: 'topright' }).addTo(this.map);
        } else {
            online.addTo(this.map);
        }
        this.layers = {
            beams: L.layerGroup().addTo(this.map),
            links: L.layerGroup().addTo(this.map),
            conflicts: L.layerGroup().addTo(this.map),
            sites: L.layerGroup().addTo(this.map),
            radios: L.layerGroup().addTo(this.map)
        };
        this.map.on('zoomend', () => this.render());
    },

    bindControls() {
        const rerender = () => this.render();
        ['cm-band', 'cm-severity', 'cm-beams', 'cm-links', 'cm-conflicts', 'cm-conflicts-only']
            .forEach(id => document.getElementById(id)?.addEventListener('change', rerender));
        ['cm-scope', 'cm-inactive'].forEach(id =>
            document.getElementById(id)?.addEventListener('change', () => this.load()));
        document.getElementById('cm-reload')?.addEventListener('click', () => this.load());
        document.getElementById('cm-band')?.addEventListener('change', () => this.bandRange());

        const sync = (src, dst) => {
            const a = document.getElementById(src), b = document.getElementById(dst);
            a?.addEventListener('input', () => { b.value = a.value; this.render(); });
        };
        sync('cm-fmin-range', 'cm-fmin'); sync('cm-fmin', 'cm-fmin-range');
        sync('cm-fmax-range', 'cm-fmax'); sync('cm-fmax', 'cm-fmax-range');

        document.getElementById('cm-whatif-run')?.addEventListener('click', () => this.runWhatIf());
        document.getElementById('cm-whatif-example')?.addEventListener('click', () => {
            document.getElementById('cm-whatif-text').value = this.COLLINS_PLAN;
        });
        document.getElementById('cm-whatif-clear')?.addEventListener('click', () => {
            document.getElementById('cm-whatif-text').value = '';
            this.whatIf = null;
            document.getElementById('cm-whatif-result').innerHTML = '';
            this.render();
            this.renderPanel();
        });
        this.bandRange();
    },

    /** Set the frequency range inputs to suit the chosen band. */
    bandRange() {
        const band = document.getElementById('cm-band').value;
        const ranges = { '5-ham': [5600, 5950, 5650, 5950], '5': [5100, 5950, 5100, 5950],
                         '2.4': [2380, 2500, 2380, 2500], '3': [3300, 3800, 3300, 3800],
                         all: [0, 6000, 0, 6000] };
        const [min, max, lo, hi] = ranges[band] || ranges.all;
        for (const id of ['cm-fmin-range', 'cm-fmax-range']) {
            const el = document.getElementById(id);
            el.min = min; el.max = max;
        }
        document.getElementById('cm-fmin').value = document.getElementById('cm-fmin-range').value = lo;
        document.getElementById('cm-fmax').value = document.getElementById('cm-fmax-range').value = hi;
        if (this.map) this.render();
    },

    query() {
        return {
            scope: document.getElementById('cm-scope').value,
            include_inactive: document.getElementById('cm-inactive').checked
        };
    },

    async load() {
        this.setStatus('Loading...');
        const q = this.query();
        try {
            const resp = await fetch(`/api/channel-map?scope=${encodeURIComponent(q.scope)}` +
                (q.include_inactive ? '&include_inactive=1' : ''));
            this.data = await resp.json();
            if (this.whatIf) await this.runWhatIf(true);
            this.render();
            this.renderPanel();
            this.setStatus(`${this.data.radios.length} located radio(s) · ${this.data.generated_at}`);
        } catch (error) {
            console.error('Channel map load error:', error);
            this.setStatus('Load failed');
        }
    },

    async runWhatIf(silent) {
        const text = document.getElementById('cm-whatif-text').value;
        if (!text.trim()) {
            this.whatIf = null;
            if (!silent) this.render();
            return;
        }
        const button = document.getElementById('cm-whatif-run');
        if (button) button.disabled = true;
        try {
            const resp = await fetch('/api/channel-map/what-if', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...this.query(), overrides: text })
            });
            this.whatIf = await resp.json();
            this.renderWhatIf();
            if (!silent) { this.render(); this.renderPanel(); }
        } catch (error) {
            console.error('What-if error:', error);
            document.getElementById('cm-whatif-result').innerHTML = '<p class="log-empty">What-if failed.</p>';
        } finally {
            if (button) button.disabled = false;
        }
    },

    /** The dataset currently shown: the what-if result when one is applied. */
    current() {
        return this.whatIf || this.data;
    },

    // ---------------------------------------------------------- colors ----

    freqColor(f) {
        if (f == null) return '#9e9e9e';
        if (f >= 5600 && f <= 5950) {
            const t = Math.min(1, Math.max(0, (f - this.HAM_LOW) / (this.HAM_HIGH - this.HAM_LOW)));
            return `hsl(${Math.round(265 - 215 * t)}, 70%, ${Math.round(42 + 6 * t)}%)`;
        }
        if (f >= 5000 && f < 6000) return '#5c6bc0';
        if (f >= 2300 && f < 2500) return '#8d6e63';
        if (f >= 3300 && f < 3800) return '#ad1457';
        return '#607d8b';
    },

    severityColor(sev) {
        return { critical: '#c0392b', warning: '#e67e22', info: '#d4ac0d' }[sev] || '#7f8c8d';
    },

    qualityColor(q) {
        if (q == null) return '#7f8c8d';
        if (q >= 80) return '#27ae60';
        if (q >= 50) return '#f1c40f';
        return '#e74c3c';
    },

    // ---------------------------------------------------------- filters ---

    radioVisible(r, conflictNames) {
        const band = document.getElementById('cm-band').value;
        const fmin = Number(document.getElementById('cm-fmin').value);
        const fmax = Number(document.getElementById('cm-fmax').value);
        if (band !== 'all') {
            if (!r.band) return false;
            if (band === '5' ? !r.band.startsWith('5') : r.band !== band) return false;
        }
        if (r.frequency != null && (r.frequency < fmin || r.frequency > fmax)) return false;
        if (r.frequency == null && band !== 'all') return false;
        if (document.getElementById('cm-conflicts-only').checked && !conflictNames.has(r.name)) return false;
        return true;
    },

    visibleConflicts(data) {
        const max = this.SEVERITY_RANK[document.getElementById('cm-severity').value];
        return data.conflicts.filter(c => this.SEVERITY_RANK[c.severity] <= max);
    },

    // ---------------------------------------------------------- render ----

    render() {
        const data = this.current();
        if (!this.map || !data) return;
        Object.values(this.layers).forEach(l => l.clearLayers());

        const conflicts = this.visibleConflicts(data);
        const conflictNames = new Set(conflicts.flatMap(c => [c.a, c.b]));
        const radios = data.radios.filter(r => this.radioVisible(r, conflictNames));
        const byName = Object.fromEntries(radios.map(r => [r.name, r]));
        const worst = {};
        for (const c of conflicts) {
            for (const n of [c.a, c.b]) {
                if (!worst[n] || this.SEVERITY_RANK[c.severity] < this.SEVERITY_RANK[worst[n]]) worst[n] = c.severity;
            }
        }

        if (document.getElementById('cm-beams').checked) {
            radios.forEach(r => this.drawBeam(r));
        }
        if (document.getElementById('cm-links').checked) {
            for (const l of data.links) {
                const a = byName[l.a], b = byName[l.b];
                if (!a || !b) continue;
                L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
                    color: this.qualityColor(l.quality), weight: 3, opacity: l.status === 'good' ? 0.85 : 0.4
                }).bindTooltip(`RF link ${this.esc(l.a)} ↔ ${this.esc(l.b)}<br>quality ${l.quality ?? '?'}% · SNR ${l.snr ?? '?'} dB · ${(l.distance_m / 1000).toFixed(1)} km · ${this.esc(l.status)}`)
                    .addTo(this.layers.links);
            }
        }
        if (document.getElementById('cm-conflicts').checked) {
            const sitesWithConflict = {};
            for (const c of conflicts) {
                const a = byName[c.a], b = byName[c.b];
                if (!a || !b) continue;
                if (c.colocated) {
                    (sitesWithConflict[a.site_id] = sitesWithConflict[a.site_id] || []).push(c);
                    continue;
                }
                L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
                    color: this.severityColor(c.severity), weight: c.severity === 'critical' ? 3 : 2,
                    dashArray: '8 6', opacity: 0.9
                }).bindTooltip(`<strong>${this.esc(c.severity)}</strong>: ${this.esc(c.summary)}`)
                    .on('click', () => this.selectRadio(c.a))
                    .addTo(this.layers.conflicts);
            }
            const sites = Object.fromEntries((data.sites || []).map(s => [s.id, s]));
            for (const [sid, list] of Object.entries(sitesWithConflict)) {
                const s = sites[sid];
                if (!s) continue;
                const sev = list.reduce((w, c) => this.SEVERITY_RANK[c.severity] < this.SEVERITY_RANK[w] ? c.severity : w, 'info');
                L.circleMarker([s.lat, s.lon], {
                    radius: 30, color: this.severityColor(sev), weight: 3, dashArray: '6 5', fill: false
                }).bindTooltip(`<strong>${this.esc(s.name)}</strong>: ${list.length} channel conflict(s) on one mast<br>` +
                    list.map(c => this.esc(c.summary)).join('<br>'))
                    .addTo(this.layers.sites);
            }
        }
        radios.forEach(r => this.drawRadio(r, worst[r.name]));

        if (!this.fitted && radios.length) {
            this.fitted = true;
            const pts = radios.map(r => [r.lat, r.lon]);
            this.map.fitBounds(pts, { padding: [30, 30], maxZoom: 13 });
        }
        this.renderLegend();
        this.renderSummary(data, conflicts);
    },

    /** Wedge for a directional antenna, faint circle for an omni. */
    drawBeam(r) {
        if (r.frequency == null || r.rf_status === 'off') return;
        const color = this.freqColor(r.frequency);
        if (r.beamwidth >= 360 || r.azimuth == null) {
            L.circle([r.lat, r.lon], { radius: 700, color, weight: 1, opacity: 0.35, fillOpacity: 0.05, interactive: false })
                .addTo(this.layers.beams);
            return;
        }
        const g = r.gain || 0;
        const km = g >= 22 ? 5 : g >= 18 ? 3.5 : g >= 14 ? 2.5 : 1.5;
        const half = r.beamwidth / 2;
        const pts = [[r.lat, r.lon]];
        const steps = Math.max(4, Math.ceil(r.beamwidth / 5));
        for (let i = 0; i <= steps; i++) {
            pts.push(this.destination(r.lat, r.lon, r.azimuth - half + (r.beamwidth * i) / steps, km));
        }
        L.polygon(pts, {
            color, weight: 1, opacity: 0.6, fillColor: color, fillOpacity: 0.13,
            dashArray: r.beamwidth_assumed ? '4 4' : null, interactive: false
        }).addTo(this.layers.beams);
    },

    destination(lat, lon, bearingDeg, km) {
        const R = 6371, d = km / R, b = bearingDeg * Math.PI / 180;
        const p1 = lat * Math.PI / 180, l1 = lon * Math.PI / 180;
        const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
        const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
        return [p2 * 180 / Math.PI, l2 * 180 / Math.PI];
    },

    drawRadio(r, worstSeverity) {
        // Radios on one mast fan out in a small ring (pixels, so it works at every zoom).
        let dx = 0, dy = 0;
        if (r.site_size > 1) {
            const radius = 10 + Math.min(16, r.site_size * 2);
            const angle = (2 * Math.PI * r.site_index) / r.site_size - Math.PI / 2;
            dx = Math.round(radius * Math.cos(angle));
            dy = Math.round(radius * Math.sin(angle));
        }
        const fill = r.rf_status === 'off' ? '#ffffff' : this.freqColor(r.frequency);
        const ring = worstSeverity ? this.severityColor(worstSeverity) : '#263238';
        const classes = ['cm-dot'];
        if (r.is_infrastructure) classes.push('cm-infra');
        if (r.what_if) classes.push('cm-whatif');
        if (!r.recent) classes.push('cm-stale');
        if (this.selected === r.name) classes.push('cm-selected');
        const size = 14;
        const icon = L.divIcon({
            className: 'cm-icon',
            html: `<span class="${classes.join(' ')}" style="background:${fill};border-color:${ring}"></span>`,
            iconSize: [size, size],
            iconAnchor: [size / 2 - dx, size / 2 - dy]
        });
        const freq = r.frequency != null
            ? `${r.frequency} MHz${r.chanbw ? ' / ' + r.chanbw : ' / 20?'} MHz · ch ${this.esc(r.channel ?? '?')}`
            : (r.rf_status === 'off' ? 'radio off' : 'no radio data');
        L.marker([r.lat, r.lon], { icon, riseOnHover: true })
            .bindTooltip(`<strong>${this.esc(r.name)}</strong><br>${freq}` +
                (r.what_if ? '<br><em>what-if</em>' : '') +
                (r.conflict_count ? `<br>${r.conflict_count} conflict(s)` : ''))
            .on('click', () => this.selectRadio(r.name))
            .addTo(this.layers.radios);
    },

    selectRadio(name) {
        this.selected = name;
        this.render();
        this.renderPanel();
    },

    // ---------------------------------------------------------- side ------

    renderLegend() {
        const el = document.getElementById('cm-legend');
        if (!el) return;
        const lo = 5600, hi = 5950, pct = f => ((f - lo) / (hi - lo)) * 100;
        const stops = [];
        for (let f = lo; f <= hi; f += 25) stops.push(`${this.freqColor(f)} ${pct(f)}%`);
        const wifi = (this.current()?.wifi_bands || []).map(b => {
            const l = Math.max(lo, b.low), h = Math.min(hi, b.high);
            if (h <= l) return '';
            return `<span class="cm-wifi cm-wifi-${b.busy}" style="left:${pct(l)}%;width:${pct(h) - pct(l)}%"
                title="${this.esc(b.label)} (${this.esc(b.busy)} use)"></span>`;
        }).join('');
        const ticks = [5650, 5700, 5750, 5800, 5850, 5900, 5925]
            .map(f => `<span class="cm-tick" style="left:${pct(f)}%">${f}</span>`).join('');
        el.innerHTML = `
            <div class="cm-legend-title">Frequency (MHz)</div>
            <div class="cm-scale" style="background:linear-gradient(to right, ${stops.join(',')})">
                <span class="cm-ham" style="left:${pct(this.HAM_LOW)}%;width:${pct(this.HAM_HIGH) - pct(this.HAM_LOW)}%" title="US amateur 5650–5925 MHz"></span>
            </div>
            <div class="cm-wifi-row">${wifi}</div>
            <div class="cm-ticks">${ticks}</div>
            <div class="cm-legend-keys">
                <span><i class="cm-key-dot" style="background:#5c6bc0"></i>5 GHz non-ham</span>
                <span><i class="cm-key-dot" style="background:#8d6e63"></i>2.4 GHz</span>
                <span><i class="cm-key-dot cm-infra-key"></i>infrastructure</span>
                <span><i class="cm-key-line" style="border-color:#27ae60"></i>RF link</span>
                <span><i class="cm-key-line cm-dashed" style="border-color:#c0392b"></i>critical</span>
                <span><i class="cm-key-line cm-dashed" style="border-color:#e67e22"></i>warning</span>
                <span><i class="cm-key-line cm-dashed" style="border-color:#d4ac0d"></i>info</span>
            </div>
            <div class="form-hint">Shaded bars under the scale: busy WiFi ranges. Dashed ring = conflicts on one mast.</div>`;
    },

    renderSummary(data, conflicts) {
        const el = document.getElementById('cm-summary');
        const c = data.counts;
        const siteConf = (data.sites || []).filter(s => s.conflicts.length);
        const top = conflicts.slice(0, 12);
        el.innerHTML = `
            <div class="cm-counts">
                <span class="diag-badge infra-badge-critical">${c.critical} critical</span>
                <span class="diag-badge infra-badge-warning">${c.warning} warning</span>
                <span class="diag-badge infra-badge-info">${c.info} info</span>
                ${this.whatIf ? '<span class="cm-whatif-flag">what-if applied</span>' : ''}
            </div>
            ${siteConf.length ? `<h4>Conflicts on one mast</h4><ul class="cm-list">${siteConf.map(s =>
                `<li><strong>${this.esc(s.name)}</strong>: ${s.conflicts.length}</li>`).join('')}</ul>` : ''}
            <h4>Worst conflicts</h4>
            ${top.length ? `<ul class="cm-list">${top.map(x => `<li class="cm-conf cm-sev-${x.severity}"
                data-a="${this.esc(x.a)}" data-b="${this.esc(x.b)}">${this.esc(x.summary)}</li>`).join('')}</ul>`
                : '<p class="form-hint">No conflicts at this filter level.</p>'}`;
        el.querySelectorAll('.cm-conf').forEach(li => li.addEventListener('click', () => this.zoomTo([li.dataset.a, li.dataset.b])));
    },

    zoomTo(names) {
        const radios = this.current().radios.filter(r => names.includes(r.name));
        if (!radios.length) return;
        const pts = radios.map(r => [r.lat, r.lon]);
        if (pts.length === 1 || radios.every(r => r.site_id === radios[0].site_id)) {
            this.map.setView(pts[0], 15);
        } else {
            this.map.fitBounds(pts, { padding: [60, 60] });
        }
        this.selectRadio(names[0]);
    },

    renderPanel() {
        const el = document.getElementById('cm-panel');
        const data = this.current();
        const r = data?.radios.find(x => x.name === this.selected);
        if (!r) {
            el.innerHTML = '<p class="form-hint">Click a radio for its settings and conflicts.</p>';
            return;
        }
        const mine = data.conflicts.filter(c => c.a === r.name || c.b === r.name);
        const links = data.links.filter(l => l.a === r.name || l.b === r.name);
        const span = r.frequency != null
            ? `${r.low}–${r.high} MHz${r.low_channel != null ? ` (ch ${r.low_channel}–${r.high_channel})` : ''}` : '-';
        const orig = r.original ? `<tr><td>Stored:</td><td>${r.original.frequency} MHz / ${r.original.chanbw || '?'} MHz (what-if replaces it)</td></tr>` : '';
        el.innerHTML = `
            <h3 class="cm-panel-title"><a href="/nodes/${encodeURIComponent(r.name)}" target="_blank">${this.esc(r.name)}</a>
                ${r.is_infrastructure ? '<span class="diag-badge infra-badge-info">infra</span>' : ''}</h3>
            <table class="info-table cm-table">
                <tr><td>Channel:</td><td>${r.frequency != null ? `${r.frequency} MHz, ch ${this.esc(r.channel ?? '?')}, ${r.chanbw || '20?'} MHz wide` : (r.rf_status === 'off' ? 'radio off' : 'no radio data')}</td></tr>
                <tr><td>Occupies:</td><td>${span}${r.outside_ham ? ' <span class="cm-warn">outside 5650–5925 MHz ham band</span>' : ''}</td></tr>
                ${orig}
                <tr><td>Mode / SSID:</td><td>${this.esc([r.mode, r.role ? `(${r.role})` : null, r.ssid].filter(Boolean).join(' ') || '-')}</td></tr>
                <tr><td>Antenna:</td><td>${this.esc(r.antenna || '-')}</td></tr>
                <tr><td>Aim:</td><td>${r.azimuth != null ? `${r.azimuth}°` : 'not set'} · beam ${r.beamwidth}°${r.beamwidth_assumed ? ' (estimated)' : ''}${r.gain != null ? ` · ${r.gain} dBi` : ''}${r.height != null ? ` · ${r.height} m` : ''}</td></tr>
                <tr><td>Model:</td><td>${this.esc(r.model || '-')} · ${this.esc(r.firmware || '')}</td></tr>
                <tr><td>Status:</td><td>${r.active ? 'active' : `inactive, last seen ${this.esc(r.last_seen || '?')}`}</td></tr>
            </table>
            <h4>Conflicts (${mine.length})</h4>
            ${mine.length ? `<ul class="cm-list">${mine.map(c => `<li class="cm-conf cm-sev-${c.severity}"
                data-a="${this.esc(c.a)}" data-b="${this.esc(c.b)}"><strong>${this.esc(c.severity)}</strong> · ${this.esc(c.summary)}</li>`).join('')}</ul>`
                : '<p class="form-hint">None.</p>'}
            <h4>RF links (${links.length})</h4>
            ${links.length ? `<ul class="cm-list">${links.map(l => `<li>${this.esc(l.a === r.name ? l.b : l.a)} · quality ${l.quality ?? '?'}% · SNR ${l.snr ?? '?'} dB · ${(l.distance_m / 1000).toFixed(1)} km</li>`).join('')}</ul>`
                : '<p class="form-hint">None.</p>'}
            <div class="cm-quick-whatif">
                <label>Try <input type="number" id="cm-q-freq" class="form-input cm-num" step="5" value="${r.frequency ?? ''}"> MHz</label>
                <select id="cm-q-width" class="form-input">
                    ${[5, 10, 20, 40, 80].map(w => `<option value="${w}" ${Number(r.chanbw || 20) === w ? 'selected' : ''}>${w} MHz</option>`).join('')}
                </select>
                <button id="cm-q-apply" class="btn btn-secondary btn-small">What-if</button>
            </div>`;
        el.querySelectorAll('.cm-conf').forEach(li => li.addEventListener('click', () => {
            const other = li.dataset.a === r.name ? li.dataset.b : li.dataset.a;
            this.zoomTo([r.name, other]);
        }));
        document.getElementById('cm-q-apply')?.addEventListener('click', () => {
            const freq = document.getElementById('cm-q-freq').value;
            const width = document.getElementById('cm-q-width').value;
            if (!freq) return;
            const box = document.getElementById('cm-whatif-text');
            const lines = box.value.split('\n').filter(l => l.trim() && l.trim().split(/\s+/)[0].toLowerCase() !== r.name);
            lines.push(`${r.name} ${freq} ${width}`);
            box.value = lines.join('\n');
            document.getElementById('cm-whatif-box').open = true;
            this.runWhatIf();
        });
    },

    renderWhatIf() {
        const el = document.getElementById('cm-whatif-result');
        const w = this.whatIf?.what_if;
        if (!w) { el.innerHTML = ''; return; }
        const b = w.baseline_counts, a = this.whatIf.counts;
        const item = c => `<li class="cm-conf cm-sev-${c.severity}" data-a="${this.esc(c.a)}" data-b="${this.esc(c.b)}">${this.esc(c.summary)}</li>`;
        const filtered = list => list.filter(c => c.severity !== 'info');
        el.innerHTML = `
            <table class="info-table cm-table">
                <tr><th></th><th>Now</th><th>What-if</th></tr>
                <tr><td>Critical</td><td>${b.critical}</td><td>${a.critical}</td></tr>
                <tr><td>Warning</td><td>${b.warning}</td><td>${a.warning}</td></tr>
                <tr><td>Info</td><td>${b.info}</td><td>${a.info}</td></tr>
                <tr><td>On one mast</td><td>${b.colocated}</td><td>${a.colocated}</td></tr>
            </table>
            ${w.unknown.length ? `<p class="cm-warn">Not on the map (check the name or scope): ${w.unknown.map(n => this.esc(n)).join(', ')}</p>` : ''}
            <h4>New conflicts (${w.introduced.length})</h4>
            ${w.introduced.length ? `<ul class="cm-list">${w.introduced.map(item).join('')}</ul>` : '<p class="form-hint">None.</p>'}
            <h4>Changed severity (${w.changed.length})</h4>
            ${w.changed.length ? `<ul class="cm-list">${w.changed.map(x => `<li>${this.esc(x.before.severity)} → ${this.esc(x.after.severity)}: ${this.esc(x.after.summary)}</li>`).join('')}</ul>` : '<p class="form-hint">None.</p>'}
            <details><summary>Resolved (${w.resolved.length}, ${filtered(w.resolved).length} warning or worse)</summary>
                <ul class="cm-list">${w.resolved.map(item).join('')}</ul></details>`;
        el.querySelectorAll('.cm-conf').forEach(li => li.addEventListener('click', () => this.zoomTo([li.dataset.a, li.dataset.b])));
    },

    setStatus(text) {
        const el = document.getElementById('cm-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => ChannelMap.hookTab());
