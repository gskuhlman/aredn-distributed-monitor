/**
 * Link Watch section on the Diagnostics tab: one-way link alarms and
 * noise-floor jumps from /api/reports/one-way and /api/reports/noise.
 * All detection rules live in link_watch.py; this only renders them.
 */
const LinkWatch = {
    initialized: false,
    charts: {},
    selectedRadio: null,

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('lw-refresh')?.addEventListener('click', () => this.load());
        document.getElementById('lw-all-radios')?.addEventListener('change', () => this.renderNoise());
        this.load();
    },

    async load() {
        this.setStatus('Loading...');
        try {
            const [oneResp, noiseResp] = await Promise.all([
                fetch('/api/reports/one-way?hours=24'),
                fetch('/api/reports/noise')
            ]);
            this.oneWay = await oneResp.json();
            this.noise = await noiseResp.json();
            this.renderOneWay();
            this.renderNoise();
            this.setStatus(`Checked ${this.oneWay.evaluated_pairs} RF pair(s) and ` +
                `${this.noise.radios.length} radio(s) at ${this.oneWay.generated_at}`);
            if (this.selectedRadio) this.loadRadio(this.selectedRadio);
        } catch (error) {
            console.error('Link watch error:', error);
            this.setStatus('Failed to load link watch.');
        }
    },

    renderOneWay() {
        const d = this.oneWay;
        const el = document.getElementById('lw-one-way');
        const rows = [
            ...d.active.map(r => ({ ...r, state: r.escalated ? 'critical' : 'warning', label: 'alarm' })),
            ...d.pending.map(r => ({ ...r, state: 'info', label: 'pending' })),
            ...d.cleared.map(r => ({ ...r, state: 'ok', label: 'cleared' }))
        ];
        if (!rows.length) {
            el.innerHTML = '<p class="log-empty">No one-way RF links. Every evaluated pair hears each other both ways.</p>';
            return;
        }
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Status</th><th>Link</th><th>Deaf end</th><th>Why</th><th>Since</th><th></th></tr></thead>
            <tbody>${rows.map(r => `<tr>
                <td><span class="diag-badge infra-badge-${r.state}">${r.label}</span></td>
                <td>${this.esc(r.pair[0])} ↔ ${this.esc(r.pair[1])}${r.touches_infrastructure ? ' <small>(infra)</small>' : ''}</td>
                <td>${this.esc((r.deaf || []).join(', ') || '-')}</td>
                <td class="lw-why">${(r.reasons || []).map(x => this.esc(x)).join('<br>') || '-'}</td>
                <td>${this.esc(r.label === 'cleared' ? `cleared ${r.cleared_at}` : (r.since || 'this scan'))}</td>
                <td><button class="btn btn-small btn-secondary" data-a="${this.esc(r.pair[0])}" data-b="${this.esc(r.pair[1])}">Diagnose</button></td>
            </tr>`).join('')}</tbody></table></div>
            <p class="form-hint">Alarm = one-way on ${d.thresholds.scans}+ consecutive scans (one end hears
            ≥ ${d.thresholds.high_lq}% of hellos while the other hears ≤ ${d.thresholds.low_lq}%, infinite Babel
            cost with SNR ≥ ${d.thresholds.snr_ok} dB, or MAC-only identity). Pending = one-way right now, not yet
            confirmed.</p>`;
        el.querySelectorAll('button[data-a]').forEach(b =>
            b.addEventListener('click', () => this.openPair(b.dataset.a, b.dataset.b)));
    },

    renderNoise() {
        const d = this.noise;
        const el = document.getElementById('lw-noise');
        const all = document.getElementById('lw-all-radios')?.checked;
        const radios = d.radios.filter(r => all || r.alarm || (r.delta != null && r.delta > d.clear_db));
        if (!d.radios.length) {
            el.innerHTML = '<p class="log-empty">No noise samples yet (needs RF trackers with signal and SNR).</p>';
            return;
        }
        const body = radios.length ? radios.map(r => {
            const cls = r.alarm ? 'warning' : (r.delta != null && r.delta > d.clear_db ? 'info' : 'ok');
            return `<tr class="lw-radio${r.node === this.selectedRadio ? ' lw-selected' : ''}" data-node="${this.esc(r.node)}">
                <td>${this.esc(r.node)}${r.is_infrastructure ? ' <small>(infra)</small>' : ''}</td>
                <td>${r.current != null ? r.current + ' dBm' : '-'}</td>
                <td>${r.baseline != null ? r.baseline + ' dBm' : '-'} <small>${this.esc((r.baseline_method || '').replace(/_/g, ' '))}</small></td>
                <td><span class="diag-badge infra-badge-${cls}">${r.delta != null ? (r.delta > 0 ? '+' : '') + r.delta + ' dB' : 'n/a'}</span></td>
                <td>${r.alarm ? 'since ' + this.esc(r.since) : (r.cleared_at ? 'cleared ' + this.esc(r.cleared_at) : '-')}</td>
            </tr>`;
        }).join('') : `<tr><td colspan="5" class="log-empty">No radio is above its baseline. Tick "show all radios" to browse.</td></tr>`;
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Radio</th><th>Noise now</th><th>Baseline</th><th>Change</th><th>Alarm</th></tr></thead>
            <tbody>${body}</tbody></table></div>
            <p class="form-hint">Noise = signal − SNR from each radio's own neighbor trackers. Alarm at
            ≥ +${d.jump_db} dB for 2+ scans, clears within +${d.clear_db} dB. Click a radio for its history and
            time-of-day pattern (WiFi noise follows when people are home).</p>`;
        el.querySelectorAll('tr.lw-radio').forEach(tr =>
            tr.addEventListener('click', () => this.loadRadio(tr.dataset.node)));
    },

    async loadRadio(node) {
        this.selectedRadio = node;
        document.querySelectorAll('#lw-noise tr.lw-radio').forEach(tr =>
            tr.classList.toggle('lw-selected', tr.dataset.node === node));
        const wrap = document.getElementById('lw-noise-detail');
        wrap.classList.remove('hidden');
        document.getElementById('lw-noise-title').textContent = `${node}: noise floor`;
        try {
            const resp = await fetch(`/api/reports/noise?node=${encodeURIComponent(node)}&hours=72&days=7`);
            const d = await resp.json();
            this.drawSeries(d);
            this.drawProfile(d);
        } catch (error) {
            console.error('Noise detail error:', error);
        }
    },

    drawSeries(d) {
        const ctx = document.getElementById('lw-noise-series');
        const points = d.series.map(p => ({ x: new Date(p.t.replace(' ', 'T')), y: p.noise }));
        const base = d.status.baseline;
        const datasets = [{ label: 'Noise (dBm)', data: points, borderColor: '#c0392b', borderWidth: 1.5,
            pointRadius: 0, tension: 0.1 }];
        if (base != null && points.length) {
            datasets.push({ label: `Baseline (${base} dBm)`, borderColor: '#7f8c8d', borderDash: [6, 4],
                borderWidth: 1, pointRadius: 0,
                data: [{ x: points[0].x, y: base }, { x: points[points.length - 1].x, y: base }] });
            datasets.push({ label: `Alarm (+${d.jump_db} dB)`, borderColor: '#e67e22', borderDash: [2, 3],
                borderWidth: 1, pointRadius: 0,
                data: [{ x: points[0].x, y: base + d.jump_db }, { x: points[points.length - 1].x, y: base + d.jump_db }] });
        }
        this.charts.series?.destroy();
        this.charts.series = new Chart(ctx, {
            type: 'line', data: { datasets },
            options: { responsive: true, maintainAspectRatio: false, animation: false,
                scales: { x: { type: 'time', time: { tooltipFormat: 'MMM d HH:mm' } },
                          y: { title: { display: true, text: 'dBm' } } },
                plugins: { legend: { position: 'bottom' } } }
        });
    },

    drawProfile(d) {
        const ctx = document.getElementById('lw-noise-profile');
        const labels = d.hour_profile.map(h => String(h.hour).padStart(2, '0'));
        this.charts.profile?.destroy();
        this.charts.profile = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets: [
                { label: 'Average', data: d.hour_profile.map(h => h.avg), borderColor: '#c0392b', borderWidth: 2,
                  spanGaps: true, pointRadius: 2 },
                { label: 'Max', data: d.hour_profile.map(h => h.max), borderColor: '#e67e22', borderWidth: 1,
                  borderDash: [4, 3], spanGaps: true, pointRadius: 0 },
                { label: 'Min', data: d.hour_profile.map(h => h.min), borderColor: '#3498db', borderWidth: 1,
                  borderDash: [4, 3], spanGaps: true, pointRadius: 0 }
            ] },
            options: { responsive: true, maintainAspectRatio: false, animation: false,
                scales: { x: { title: { display: true, text: `Hour of day (${d.profile_days} day(s) of data)` } },
                          y: { title: { display: true, text: 'dBm' } } },
                plugins: { legend: { position: 'bottom' } } }
        });
    },

    openPair(a, b) {
        document.querySelector('.tab-btn[data-tab="rf-stats"]')?.click();
        setTimeout(() => { if (typeof RFStats !== 'undefined') RFStats.selectLink(a, b); }, 300);
    },

    setStatus(text) {
        const el = document.getElementById('lw-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
