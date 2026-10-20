/**
 * Link budget + terrain (link_budget.py): predicted vs measured signal both
 * ways, assumptions, findings and a terrain profile chart (ground with earth
 * bulge, line of sight, first Fresnel zone). Used by the RF Stats pair
 * diagnosis ("Link budget & terrain") and the Diagnostics "Link Budget"
 * section, which lists every RF link with an infrastructure end.
 */
const LinkBudget = {
    charts: new Map(),
    initialized: false,

    init() {
        if (this.initialized) return;
        if (!document.getElementById('lb-report')) return;
        this.initialized = true;
        document.getElementById('lb-run')?.addEventListener('click', () => this.loadAll());
        document.getElementById('lb-report').addEventListener('click', (event) => {
            const row = event.target.closest('tr[data-lb-a]');
            if (!row) return;
            const box = document.getElementById('lb-detail');
            box.classList.remove('hidden');
            this.renderInto(document.getElementById('lb-detail-body'), row.dataset.lbA, row.dataset.lbB, 24);
            box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        });
    },

    sev(s) {
        return { critical: 'critical', warning: 'warning', info: 'info', ok: 'ok' }[s] || 'info';
    },

    async loadAll() {
        const el = document.getElementById('lb-report');
        const status = document.getElementById('lb-status');
        status.textContent = 'Computing (first run looks up ground elevations)...';
        el.innerHTML = '<p class="log-empty">Loading...</p>';
        try {
            const d = await fetch('/api/reports/link-budget?hours=24').then(r => r.json());
            status.textContent = `${d.links.length} RF link(s) with an infrastructure end`;
            if (!d.links.length) {
                el.innerHTML = '<p class="log-empty">No current RF links touch infrastructure.</p>';
                return;
            }
            const delta = v => v == null ? '-' : `<span class="${Math.abs(v) >= 10 ? 'lb-bad' : ''}">${v > 0 ? '+' : ''}${v}</span>`;
            el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table cap-table">
                <thead><tr><th></th><th>Link</th><th>Distance</th><th>Fresnel clear</th><th>A → B predicted / measured</th>
                <th>B → A predicted / measured</th><th>Confidence</th><th>Findings</th></tr></thead>
                <tbody>${d.links.map(l => {
                    if (l.error) return `<tr><td></td><td>${this.esc(l.a)} ↔ ${this.esc(l.b)}</td><td colspan="6" class="form-hint">${this.esc(l.error)}</td></tr>`;
                    const dir = x => x ? `${x.predicted_signal} / ${x.measured_signal ?? '?'} dBm (${delta(x.delta_db)})` : '-';
                    return `<tr data-lb-a="${this.esc(l.a)}" data-lb-b="${this.esc(l.b)}">
                        <td><span class="diag-badge infra-badge-${this.sev(l.worst)}">${this.esc(l.worst)}</span></td>
                        <td>${this.esc(l.a)} ↔ ${this.esc(l.b)}</td><td>${(l.distance_m / 1000).toFixed(2)} km</td>
                        <td>${l.fresnel != null ? Math.round(l.fresnel * 100) + '%' : `<small>${this.esc(l.terrain_error || '-')}</small>`}</td>
                        <td>${dir(l.directions[0])}</td><td>${dir(l.directions[1])}</td><td>${this.esc(l.confidence)}</td>
                        <td class="spof-behind">${l.findings.map(f => this.esc(f.text)).join('<br>')}</td></tr>`;
                }).join('')}</tbody></table></div>
                <p class="form-hint">Δ = measured − predicted. Click a row for the full budget and terrain profile.</p>`;
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Failed to compute.</p>';
            status.textContent = '';
        }
    },

    async renderInto(el, a, b, hours = 24) {
        if (!el) return;
        el.innerHTML = '<p class="log-empty">Computing link budget...</p>';
        let d;
        try {
            const resp = await fetch(`/api/link-budget/${encodeURIComponent(a)}/${encodeURIComponent(b)}?hours=${hours}`);
            d = await resp.json();
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(d.error || 'Not available')}</p>`;
                return;
            }
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Failed to compute the link budget.</p>';
            return;
        }
        const rows = d.directions.map(x => `<tr><td>${this.esc(x.from)} → ${this.esc(x.to)}</td>
            <td>${x.fspl_db}</td><td>${x.tx_off_axis_db}${x.tx_angle != null ? ` <small>(${x.tx_angle}°)</small>` : ''}</td>
            <td>${x.rx_off_axis_db}${x.rx_angle != null ? ` <small>(${x.rx_angle}°)</small>` : ''}</td>
            <td><strong>${x.predicted_signal}</strong></td><td><strong>${x.measured_signal ?? '?'}</strong></td>
            <td class="${x.delta_db != null && Math.abs(x.delta_db) >= 10 ? 'lb-bad' : ''}">${x.delta_db ?? '-'}</td>
            <td>${x.predicted_snr} / ${x.measured_snr ?? '?'}</td>
            <td>${x.noise}${x.noise_measured ? '' : ' <small>(thermal)</small>'}</td></tr>`).join('');
        const radios = d.radios.map(r => `<li><strong>${this.esc(r.name)}</strong>: ${r.gain} dBi${r.gain_assumed ? ' (assumed)' : ''},
            ${r.height} m up, ${r.azimuth != null ? `aimed ${r.azimuth}°` : 'no azimuth'}${r.beamwidth ? `, ${r.beamwidth}° beam` : ''},
            TX ${r.tx_dbm} dBm${r.tx_assumed ? ' (assumed)' : ''}</li>`).join('');
        const id = `lb-chart-${Math.random().toString(36).slice(2)}`;
        el.innerHTML = `<p class="form-hint">${(d.distance_m / 1000).toFixed(2)} km at ${d.frequency} MHz / ${d.chanbw} MHz ·
                confidence ${this.esc(d.confidence)} · ${d.assumptions.cable_loss_db} dB cable loss each end</p>
            <div class="diag-findings">${d.findings.map(f => `<div class="diag-finding diag-${this.sev(f.severity)}">
                <div class="diag-finding-body">${this.esc(f.text)}</div></div>`).join('')}</div>
            <div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Direction</th><th>FSPL dB</th><th>TX off-axis dB</th><th>RX off-axis dB</th>
                <th>Predicted dBm</th><th>Measured dBm</th><th>Δ dB</th><th>SNR pred / meas</th><th>Noise dBm</th></tr></thead>
                <tbody>${rows}</tbody></table></div>
            <ul class="diag-evidence">${radios}</ul>
            ${d.terrain.available ? `<div class="lb-chart-wrap"><canvas id="${id}"></canvas></div>
                <p class="form-hint">Ground includes earth curvature (k = 4/3). Shaded: first Fresnel zone around the line of sight;
                keep 60% of it clear. Trees and buildings are not in the elevation data.</p>`
                : `<p class="form-hint">No terrain profile: ${this.esc(d.terrain.error)}</p>`}`;
        if (d.terrain.available && typeof Chart !== 'undefined') {
            const pts = d.terrain.points;
            const km = p => p.d / 1000;
            const old = this.charts.get(el);
            if (old) old.destroy();
            this.charts.set(el, new Chart(document.getElementById(id), {
                type: 'line',
                data: { datasets: [
                    { label: 'Fresnel top', data: pts.map(p => ({ x: km(p), y: p.los + p.fresnel })), borderColor: 'rgba(46,134,193,0.3)', pointRadius: 0, borderWidth: 1, fill: false },
                    { label: 'First Fresnel zone', data: pts.map(p => ({ x: km(p), y: p.los - p.fresnel })), borderColor: 'rgba(46,134,193,0.3)', backgroundColor: 'rgba(46,134,193,0.12)', pointRadius: 0, borderWidth: 1, fill: '-1' },
                    { label: 'Line of sight', data: pts.map(p => ({ x: km(p), y: p.los })), borderColor: '#1b4f72', pointRadius: 0, borderWidth: 2 },
                    { label: 'Ground (with curvature)', data: pts.map(p => ({ x: km(p), y: p.ground })), borderColor: '#7e5109', backgroundColor: 'rgba(126,81,9,0.25)', pointRadius: 0, fill: 'origin' }
                ] },
                options: {
                    responsive: true, maintainAspectRatio: false, animation: false,
                    plugins: { legend: { labels: { filter: i => i.text !== 'Fresnel top' } } },
                    scales: {
                        x: { type: 'linear', title: { display: true, text: `km from ${d.a}` } },
                        y: { title: { display: true, text: 'm above sea level' },
                             min: Math.floor(Math.min(...pts.map(p => p.terrain)) / 10) * 10 - 10 }
                    }
                }
            }));
        }
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => LinkBudget.init());
