/**
 * Link Potential section on the Diagnostics tab (link_potential.py, TODO #69):
 * every RF link direction as a dot, distance across and measured SNR (or
 * rate) up, coloured by measured / theoretical rate, with the free-space
 * reference curve. Links well below the curve are listed; click a dot or a
 * row to diagnose the pair in RF Stats.
 */
const LinkPotential = {
    initialized: false,
    chart: null,
    data: null,
    COLORS: { good: '#1e8449', fair: '#d68910', poor: '#c0392b', none: '#95a5a6' },

    init() {
        if (this.initialized || !document.getElementById('lpot-report')) return;
        this.initialized = true;
        document.getElementById('lpot-run')?.addEventListener('click', () => this.load());
        document.getElementById('lpot-metric')?.addEventListener('change', () => this.draw());
        document.getElementById('lpot-report').addEventListener('click', (event) => {
            const btn = event.target.closest('[data-a]');
            if (btn) this.diagnose(btn.dataset.a, btn.dataset.b);
        });
    },

    diagnose(a, b) {
        document.querySelector('.tab-btn[data-tab="rf-stats"]')?.click();
        setTimeout(() => { if (typeof RFStats !== 'undefined') RFStats.selectLink(a, b); }, 300);
    },

    async load() {
        this.init();
        this.setStatus('Loading...');
        try {
            this.data = await (await fetch(`/api/reports/link-potential?hours=${((typeof GlobalFilter !== 'undefined') ? GlobalFilter.hours() : 24)}`)).json();
            this.draw();
            const d = this.data;
            this.setStatus(`${d.points.length} link direction(s), ${d.below.length} well below potential`
                + (d.skipped ? `, ${d.skipped} without a location` : '') + ` · ${d.generated_at}`);
        } catch (error) {
            this.setStatus('Failed to load.');
        }
    },

    draw() {
        const d = this.data;
        if (!d) return;
        const metric = document.getElementById('lpot-metric')?.value || 'snr';
        const yKey = metric === 'rate' ? 'rate_mbps' : 'snr';
        const groups = {};
        d.points.forEach(p => {
            if (p[yKey] == null) return;
            (groups[p.grade || 'none'] = groups[p.grade || 'none'] || []).push({ x: p.distance_km, y: p[yKey], p });
        });
        const labels = { good: 'Near potential (≥50%)', fair: 'Below (25-50%)', poor: 'Far below (<25%)', none: 'No rate' };
        const datasets = Object.entries(groups).map(([g, pts]) => ({
            label: labels[g], data: pts, backgroundColor: this.COLORS[g], pointRadius: pts.map(x => x.p.infrastructure ? 6 : 4),
            pointStyle: pts.map(x => x.p.infrastructure ? 'rectRot' : 'circle')
        }));
        datasets.push({ label: 'Free-space potential (typical pair)', type: 'line',
                        data: d.curve.map(c => ({ x: c.distance_km, y: metric === 'rate' ? c.rate_mbps : c.snr })),
                        borderColor: 'rgba(52,73,94,0.6)', borderDash: [5, 4], pointRadius: 0, borderWidth: 1.5 });
        if (metric === 'snr') {
            datasets.push({ label: 'Predicted (each link)', data: d.points.map(p => ({ x: p.distance_km, y: p.predicted_snr, p })),
                            backgroundColor: 'rgba(0,0,0,0)', borderColor: 'rgba(52,73,94,0.5)', pointRadius: 3, pointStyle: 'crossRot' });
        }
        if (this.chart) this.chart.destroy();
        const canvas = document.getElementById('lpot-chart');
        if (!canvas || typeof Chart === 'undefined') return;
        this.chart = new Chart(canvas, {
            type: 'scatter', data: { datasets },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                scales: { x: { title: { display: true, text: 'Distance (km)' }, beginAtZero: true },
                          y: { title: { display: true, text: metric === 'rate' ? 'Measured rate (Mbps)' : 'Measured SNR (dB)' }, beginAtZero: true } },
                plugins: { tooltip: { callbacks: { label: (ctx) => {
                    const p = ctx.raw.p;
                    if (!p) return `${ctx.raw.y}`;
                    return `${p.from} → ${p.to}: ${p.distance_km} km, SNR ${p.snr ?? '-'} (predicted ${p.predicted_snr}), `
                        + `${p.rate_mbps ?? '-'} Mbps of ~${p.expected_mbps} (${p.rate_basis || 'no rate'})`;
                } } } },
                onClick: (evt, items) => {
                    const p = items.length && items[0].element.$context.raw.p;
                    if (p) this.diagnose(p.to, p.from);
                }
            }
        });
        const rows = d.below.slice(0, 25);
        document.getElementById('lpot-report').innerHTML = rows.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Link (sender → receiver)</th><th>Distance</th><th>SNR / predicted</th><th>Rate / expected</th><th>Of potential</th><th></th></tr></thead>
            <tbody>${rows.map(p => `<tr><td>${this.esc(p.from)} → ${this.esc(p.to)}${p.infrastructure ? ' <small class="hs-infra">infra</small>' : ''}</td>
                <td>${p.distance_km} km</td><td>${p.snr ?? '-'} / ${p.predicted_snr} dB${p.assumed ? ' <small class="form-hint" title="A gain or TX power was assumed">≈</small>' : ''}</td>
                <td>${p.rate_mbps ?? '-'} / ${p.expected_mbps} Mbps <small class="form-hint">${this.esc(p.rate_basis || '')}</small></td>
                <td><strong style="color:${this.COLORS[p.grade || 'none']}">${p.ratio != null ? Math.round(p.ratio * 100) + '%' : '-'}</strong></td>
                <td><button class="btn btn-small btn-secondary" data-a="${this.esc(p.to)}" data-b="${this.esc(p.from)}">Diagnose</button></td></tr>`).join('')}
            </tbody></table></div><p class="form-hint">${d.notes.map(n => this.esc(n)).join(' ')}</p>`
            : `<p class="log-empty">No link is far below its potential.</p><p class="form-hint">${d.notes.map(n => this.esc(n)).join(' ')}</p>`;
    },

    setStatus(text) {
        const el = document.getElementById('lpot-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => LinkPotential.init());
