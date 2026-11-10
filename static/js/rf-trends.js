/**
 * Radio & RF → Slow Degradation (degradation.py: 30-day SNR / LQ trend per
 * RF link direction from the hourly rollups, and the gap to the link budget)
 * and → Noise Sources (noise_source.py: radios whose noise jumped together,
 * with the area their beams point at).
 */
const RfTrends = {
    init() {
        if (document.getElementById('dg-report')) {
            document.getElementById('dg-run').addEventListener('click', () => this.loadDegradation());
            document.getElementById('dg-all').addEventListener('change', () => this.renderDegradation());
            R7.onShow('radio', 'degradation', () => { if (!this.deg) this.loadDegradation(); });
        }
        if (document.getElementById('ns-report')) {
            document.getElementById('ns-refresh').addEventListener('click', () => this.loadNoise());
            R7.onShow('radio', 'noise', () => this.loadNoise());
        }
    },

    async loadDegradation() {
        const status = document.getElementById('dg-status');
        status.textContent = 'Computing trends…';
        try {
            this.deg = await R7.json(`/api/reports/degradation?days=${document.getElementById('dg-days').value}`);
            status.textContent = `${this.deg.flagged.length} of ${this.deg.links.length} link directions slipping over ${this.deg.days} days`;
            this.renderDegradation();
        } catch (e) {
            status.textContent = '';
            document.getElementById('dg-report').innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`;
        }
    },

    spark(series, key) {
        const pts = series.filter(p => p[key] != null);
        if (pts.length < 2) return '';
        const vals = pts.map(p => p[key]), lo = Math.min(...vals), hi = Math.max(...vals), span = Math.max(hi - lo, 1);
        const d = pts.map((p, i) => `${(i * 100 / (pts.length - 1)).toFixed(1)},${(22 - (p[key] - lo) * 20 / span).toFixed(1)}`).join(' ');
        return `<svg viewBox="0 0 100 24" width="110" height="24" preserveAspectRatio="none"><polyline points="${d}" fill="none" stroke="#2471a3" stroke-width="1.5"/></svg>`;
    },

    renderDegradation() {
        const d = this.deg;
        if (!d) return;
        const rows = document.getElementById('dg-all').checked ? d.links : d.flagged;
        const t = d.thresholds;
        document.getElementById('dg-report').innerHTML = rows.length ? `<table class="info-table"><thead><tr>
            <th>Direction (hears)</th><th>SNR trend</th><th>LQ trend</th><th>SNR now</th><th>vs budget</th><th>Daily SNR</th><th>Why</th></tr></thead><tbody>
            ${rows.map(l => `<tr><td>${R7.node(l.source)} hears ${R7.node(l.target)}${l.infrastructure ? ' ◆' : ''}</td>
                <td>${l.snr ? `${R7.num(l.snr.slope_month, 1)} dB/mo <small>r² ${R7.num(l.snr.r2, 2)}, ${l.snr.days} d</small>` : '—'}</td>
                <td>${l.lq ? `${R7.num(l.lq.slope_month, 1)} %/mo` : '—'}</td><td>${R7.num(l.snr_now, 0, ' dB')}</td>
                <td>${l.budget_gap_db == null ? '—' : R7.num(l.budget_gap_db, 1, ' dB')}</td>
                <td>${this.spark(l.series || [], 'snr')}</td>
                <td>${(l.reasons || []).map(R7.esc).join('<br>')}${l.since_change ? `<br><small>trend since the change on ${R7.esc(l.since_change.slice(0, 10))}</small>` : ''}</td></tr>`).join('')}
            </tbody></table>` : `<p class="log-empty">No link direction is slipping more than ${t.db_per_month} dB or ${t.lq_per_month}% LQ a month (fit r² ≥ ${t.min_r2}, ${t.min_days}+ days).</p>`;
        document.getElementById('dg-report').innerHTML += (d.notes || []).map(n => `<p class="form-hint">${R7.esc(n)}</p>`).join('');
    },

    async loadNoise() {
        const el = document.getElementById('ns-report');
        try {
            const d = await R7.json(`/api/reports/noise-sources?hours=${Math.max(24, R7.hours())}`);
            el.innerHTML = d.clusters.length ? d.clusters.map(c => `<div class="diag-section"><h3>${R7.esc(R7.hm(c.start))} – ${R7.esc(R7.hm(c.end))}
                ${c.current ? R7.badge('now', 'warning') : ''}</h3><p>${R7.esc(c.text)}</p>
                ${c.estimate ? `<p>Likely area: <a href="https://www.openstreetmap.org/?mlat=${c.estimate.lat}&mlon=${c.estimate.lon}#map=13/${c.estimate.lat}/${c.estimate.lon}"
                    target="_blank" rel="noopener">${R7.num(c.estimate.lat, 4)}, ${R7.num(c.estimate.lon, 4)}</a>
                    ${c.estimate.radius_km != null ? ` (± ${R7.num(c.estimate.radius_km, 1)} km)` : ''}</p>` : ''}
                <table class="info-table"><thead><tr><th>Radio</th><th>Jump</th><th>Aim</th><th>Channel</th></tr></thead><tbody>
                ${c.radios.map(r => `<tr><td>${R7.node(r.name)}</td><td>+${R7.num(r.delta, 1)} dB</td>
                    <td>${r.azimuth != null ? `${R7.num(r.azimuth, 0)}° / ${R7.num(r.beamwidth, 0)}°` : 'omni'}</td>
                    <td>${R7.num(r.frequency, 0)} MHz / ${R7.num(r.chanbw, 0)}</td></tr>`).join('')}</tbody></table>
                ${c.quiet.length ? `<p class="form-hint">Quiet radios nearby (rule areas out): ${c.quiet.map(q => R7.esc(q.name || q)).join(', ')}</p>` : ''}</div>`).join('')
                : `<p class="log-empty">No radios had noise jumps together in the last ${d.hours} h (within ${d.window_minutes} min of each other).</p>`;
            el.innerHTML += (d.notes || []).map(n => `<p class="form-hint">${R7.esc(n)}</p>`).join('');
        } catch (e) {
            el.innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`;
        }
    },
};

RfTrends.init();
