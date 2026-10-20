/**
 * Weather (weather.py, NWS station observations):
 * - Weather: optional overlay on time charts (RF Stats link charts, node page
 *   charts): precipitation as bars and temperature / wind gust as lines on
 *   their own right-hand axis. Added as extra datasets flagged $weather, so
 *   the charts' own datasets[0..] are untouched and the overlay is removed
 *   cleanly when the toggle is off.
 * - WeatherPanel: Weather section of the Reports tab (station, latest
 *   observation, links most affected by rain / wind / temperature).
 * No weather data (offline, disabled, not fetched yet) just means no overlay.
 */
const Weather = {
    STORE_KEY: 'wxOverlay',
    last: null,
    cache: {},

    enabled() {
        try { return localStorage.getItem(this.STORE_KEY) === '1'; } catch (e) { return false; }
    },

    setEnabled(on) {
        try { localStorage.setItem(this.STORE_KEY, on ? '1' : '0'); } catch (e) { /* per-viewer only */ }
        document.querySelectorAll('.wx-toggle').forEach(el => { el.checked = on; });
        if (this.last) this.markCharts(this.last);
    },

    /** Wire a checkbox (class wx-toggle) to the overlay. */
    bindToggle(id) {
        const el = document.getElementById(id);
        if (!el) return;
        el.classList.add('wx-toggle');
        el.checked = this.enabled();
        el.addEventListener('change', () => this.setEnabled(el.checked));
    },

    localValue(ms) {
        const d = new Date(ms);
        const p = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
    },

    /** Time range covered by the charts' own data. */
    range(charts) {
        let min = Infinity, max = -Infinity;
        for (const ch of charts) {
            for (const ds of ch.data.datasets) {
                if (ds.$weather) continue;
                for (const pt of ds.data || []) {
                    const t = pt && pt.x != null ? new Date(pt.x).getTime() : NaN;
                    if (!Number.isNaN(t)) { min = Math.min(min, t); max = Math.max(max, t); }
                }
            }
        }
        return Number.isFinite(min) ? [min - 3600e3, max + 3600e3] : null;
    },

    async fetchObs(start, end) {
        const key = `${start}|${end}`;
        if (!this.cache[key]) {
            this.cache[key] = fetch(`/api/weather?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`)
                .then(r => r.ok ? r.json() : { observations: [] })
                .catch(() => ({ observations: [] }));
        }
        return this.cache[key];
    },

    strip(chart) {
        chart.data.datasets = chart.data.datasets.filter(ds => !ds.$weather);
        delete chart.options.scales.wxRain;
        delete chart.options.scales.wxLine;
    },

    /** Add (or remove, when the toggle is off) the weather overlay. */
    async markCharts(charts) {
        charts = (charts || []).filter(Boolean);
        this.last = charts;
        if (!charts.length) return;
        if (!this.enabled()) {
            charts.forEach(ch => { this.strip(ch); ch.update('none'); });
            return;
        }
        const r = this.range(charts);
        if (!r) return;
        // Round to the hour so neighbouring charts share one request.
        const start = this.localValue(Math.floor(r[0] / 3600e3) * 3600e3);
        const end = this.localValue(Math.ceil(r[1] / 3600e3) * 3600e3);
        const data = await this.fetchObs(start, end);
        if (!this.enabled()) return;
        const obs = data.observations || [];
        const t = o => new Date(o.ts_local.replace(' ', 'T'));
        const rain = obs.filter(o => o.wet || (o.precip_1h_mm || 0) > 0)
            .map(o => ({ x: t(o), y: o.precip_1h_mm != null && o.precip_1h_mm > 0 ? o.precip_1h_mm : 0.2 }));
        const temp = obs.filter(o => o.temp_c != null).map(o => ({ x: t(o), y: o.temp_c }));
        const wind = obs.map(o => ({ x: t(o), y: Math.max(o.wind_kmh || 0, o.gust_kmh || 0) }));
        for (const ch of charts) {
            this.strip(ch);
            if (!obs.length) { ch.update('none'); continue; }
            ch.options.scales.wxRain = { position: 'right', beginAtZero: true, suggestedMax: 5, display: rain.length > 0,
                grid: { drawOnChartArea: false }, title: { display: true, text: 'rain mm/h' } };
            ch.options.scales.wxLine = { position: 'right', grid: { drawOnChartArea: false },
                title: { display: true, text: '°C · wind km/h' } };
            ch.data.datasets.push(
                { $weather: true, type: 'bar', label: 'Rain mm/h', data: rain, yAxisID: 'wxRain',
                  backgroundColor: 'rgba(41, 128, 185, 0.35)', borderWidth: 0, barThickness: 6, order: 10 },
                { $weather: true, type: 'line', label: 'Temp °C', data: temp, yAxisID: 'wxLine',
                  borderColor: '#e67e22', borderDash: [5, 3], borderWidth: 1, pointRadius: 0, fill: false, tension: 0.2 },
                { $weather: true, type: 'line', label: 'Wind/gust km/h', data: wind, yAxisID: 'wxLine',
                  borderColor: '#7f8c8d', borderDash: [2, 2], borderWidth: 1, pointRadius: 0, fill: false, stepped: true });
            ch.update('none');
        }
    }
};

const WeatherPanel = {
    initialized: false,

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('wx-refresh')?.addEventListener('click', () => this.refresh());
    },

    esc(v) {
        return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    num(v, d = 1) {
        return v == null ? '-' : Number(v).toFixed(d).replace(/\.0+$/, '');
    },

    async load(start, end) {
        this.init();
        const el = document.getElementById('wx-panel');
        if (!el) return;
        this.range = [start, end];
        const qs = start && end ? `start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}` : 'hours=168';
        try {
            const [obs, sum] = await Promise.all([
                fetch(`/api/weather?${qs}`).then(r => r.json()),
                fetch(`/api/weather/summary?${qs}`).then(r => r.json())
            ]);
            this.render(obs, sum);
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Weather unavailable.</p>';
            console.error('Weather panel error:', error);
        }
    },

    async refresh() {
        const btn = document.getElementById('wx-refresh');
        const msg = document.getElementById('wx-refresh-result');
        if (btn) btn.disabled = true;
        if (msg) msg.textContent = 'Fetching from NWS...';
        try {
            const resp = await fetch('/api/weather/refresh', { method: 'POST' });
            const data = await resp.json();
            if (msg) msg.textContent = resp.ok
                ? `Fetched ${data.fetched} observation(s) from ${data.station?.id || '?'}`
                : `Not available: ${data.error || resp.status}`;
            Weather.cache = {};
            if (resp.ok) this.load(...(this.range || []));
        } catch (error) {
            if (msg) msg.textContent = 'Not available (offline?)';
        } finally {
            if (btn) btn.disabled = false;
        }
    },

    linkRows(rows, cols) {
        if (!rows.length) return '<p class="form-hint">Nothing to rank in this window.</p>';
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Radio → hears</th>${cols.map(c => `<th>${c[0]}</th>`).join('')}</tr></thead>
            <tbody>${rows.slice(0, 8).map(r => `<tr class="${r.verdicts.length ? 'rel-row rel-row-adjacent' : ''}">
                <td>${this.esc(r.source)} → ${this.esc(r.target)}</td>
                ${cols.map(c => `<td>${c[1](r)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    },

    render(obs, sum) {
        const el = document.getElementById('wx-panel');
        const st = obs.status || {};
        const station = st.station;
        const latest = st.latest;
        const head = station
            ? `<strong>${this.esc(station.id)}</strong> ${this.esc(station.name || '')}${station.distance_km != null ? ` · ${station.distance_km} km from the mesh centre` : ''}${station.source === 'config' ? ' (WEATHER_STATION)' : ''}`
            : 'No station yet (first fetch picks the nearest NWS station).';
        const now = latest ? `Latest ${this.esc(latest.ts_local)}: ${this.esc(latest.description || '-')},
            ${this.num(latest.temp_c)} °C, wind ${this.num(latest.wind_kmh, 0)} km/h${latest.gust_kmh != null ? ` gust ${this.num(latest.gust_kmh, 0)}` : ''},
            humidity ${this.num(latest.humidity, 0)}%${latest.precip_1h_mm ? `, rain ${this.num(latest.precip_1h_mm)} mm/h` : ''}` : '';
        const lf = st.last_fetch || {};
        const fetchNote = lf.error ? `<p class="form-hint">Last fetch failed: ${this.esc(lf.error)}</p>` : '';
        const uaNote = st.user_agent_default ? `<p class="form-hint">NWS asks for a contact in the User-Agent: set
            WEATHER_USER_AGENT, e.g. "aredn-monitor (you@example.com)".</p>` : '';
        const notes = (sum.notes || []).map(n => `<li>${this.esc(n)}</li>`).join('');
        const flagged = (sum.flagged || []).map(r => `<li><strong>${this.esc(r.source)} → ${this.esc(r.target)}</strong>:
            ${r.verdicts.map(v => this.esc(v)).join('; ')}</li>`).join('');
        el.innerHTML = `
            <p>${head}${st.enabled === false ? ' · <em>disabled (WEATHER_ENABLED=0)</em>' : ''}</p>
            ${now ? `<p class="form-hint">${now}</p>` : ''}${fetchNote}${uaNote}
            <p class="form-hint">${sum.observations} observation(s) in the window · ${sum.wet_observations} wet ·
                ${sum.windy_observations} windy (gust or wind ≥ ${sum.thresholds?.wind_high_kmh} km/h) ·
                ${sum.directions} RF link direction(s) compared</p>
            ${notes ? `<ul class="diag-evidence">${notes}</ul>` : ''}
            ${flagged ? `<h4 class="rpt-subhead">Flagged</h4><ul class="diag-evidence">${flagged}</ul>`
                      : '<p class="form-hint">No link shows a clear rain, wind or temperature effect in this window.</p>'}
            <details class="diag-details"><summary>Rain: SNR dry vs wet</summary>
                ${this.linkRows(sum.rain || [], [
                    ['Dry dB', r => this.num(r.snr_dry)], ['Wet dB', r => this.num(r.snr_wet)],
                    ['Drop dB', r => this.num(r.rain_drop_db)], ['Wet samples', r => r.wet_samples]])}</details>
            <details class="diag-details"><summary>Wind: SNR spread windy vs calm</summary>
                ${this.linkRows(sum.wind || [], [
                    ['Spread windy', r => this.num(r.spread_windy_db, 2)], ['Spread calm', r => this.num(r.spread_calm_db, 2)],
                    ['Median drop dB', r => this.num(r.wind_drop_db)], ['Windy h', r => r.windy_hours], ['r(wind)', r => this.num(r.wind_r, 2)]])}</details>
            <details class="diag-details"><summary>Temperature: SNR correlation</summary>
                ${this.linkRows(sum.temperature || [], [
                    ['r(temp)', r => this.num(r.temp_r, 2)], ['Range °C', r => this.num(r.temp_range_c)],
                    ['Below 0 °C drop', r => this.num(r.freezing_drop_db)]])}</details>
            <p class="form-hint">Turn on "Weather" above the RF Stats or node page charts to overlay rain, temperature and wind.</p>`;
    }
};

document.addEventListener('DOMContentLoaded', () => {
    Weather.bindToggle('rf-weather-toggle');
    Weather.bindToggle('np-weather-toggle');
});
