/**
 * "MCS, rate & LQM ping" block of the RF Stats pair diagnosis (TODO #66, #67,
 * #73): per direction the transmit MCS and rate and signal from Prometheus
 * station samples (prom_source.py), LQM ping quality / RTT from our own scans
 * and from Prometheus, and the per-link alert rules for this pair
 * (link_alerts.py) with a small form to add one.
 */
const LinkProm = {
    charts: [],

    async renderInto(el, a, b, hours) {
        if (!el) return;
        this.charts.forEach(c => c.destroy());
        this.charts = [];
        try {
            const [d, rules] = await Promise.all([
                fetch(`/api/prometheus/link/${encodeURIComponent(a)}/${encodeURIComponent(b)}?hours=${hours || 24}`).then(r => r.json()),
                fetch('/api/link-alerts').then(r => r.json())
            ]);
            const mine = rules.rules.filter(r => (r.source === a && r.target === b) || (r.source === b && r.target === a));
            const dirs = [d.forward, d.reverse];
            const has = dirs.some(x => x.stations.length || x.lqm.length || x.polled.length);
            el.innerHTML = `
                ${has ? '' : `<p class="log-empty">No MCS or LQM ping samples for this pair yet${d.enabled ? '' : ' (Prometheus is not configured: PROMETHEUS_URL)'}.</p>`}
                ${has ? `<div class="lp-summary">${dirs.map(x => this.summary(x)).join('')}</div>
                <div class="node-page-charts">
                    <div class="chart-card"><h4>Transmit MCS (Prometheus)</h4><canvas id="lp-mcs"></canvas></div>
                    <div class="chart-card"><h4>Transmit rate, Mbps (Prometheus)</h4><canvas id="lp-rate"></canvas></div>
                    <div class="chart-card"><h4>LQM ping quality, %</h4><canvas id="lp-pingq"></canvas></div>
                    <div class="chart-card"><h4>LQM ping time, ms</h4><canvas id="lp-pingms"></canvas></div>
                </div>` : ''}
                <h4 class="diag-subhead">Alerts on this link</h4>
                ${mine.length ? `<ul class="lp-rules">${mine.map(r => `<li>${this.esc(r.source)} hearing ${this.esc(r.target)}: ${this.esc(this.describe(r))}
                    ${r.active ? `<strong class="sc-down">alerting</strong> (${this.esc(r.detail || '')})`
                        : r.last_check ? '<span class="sc-up">ok</span>' : '<span class="form-hint">checked after the next scan</span>'}
                    <button class="btn btn-small btn-secondary" data-lp-del="${r.id}">Remove</button></li>`).join('')}</ul>`
                    : '<p class="form-hint">No alert rules for this pair. Rules fire link_alert events (notification rule "Per-link alerts").</p>'}
                <div class="lp-form mh-controls">
                    <select id="lp-dir" class="form-input">
                        <option value="${this.esc(a)}|${this.esc(b)}">${this.esc(a)} hearing ${this.esc(b)}</option>
                        <option value="${this.esc(b)}|${this.esc(a)}">${this.esc(b)} hearing ${this.esc(a)}</option>
                    </select>
                    <label>SNR below <input id="lp-snr" type="number" class="form-input lp-num" placeholder="dB"></label>
                    <label>LQ below <input id="lp-lq" type="number" class="form-input lp-num" placeholder="%"></label>
                    <label>Rate below <input id="lp-rate-min" type="number" class="form-input lp-num" placeholder="Mbps"></label>
                    <label>More than <input id="lp-flaps" type="number" class="form-input lp-num" placeholder="n"> drops/h</label>
                    <label><input id="lp-missing" type="checkbox" checked> when missing</label>
                    <button id="lp-add" class="btn btn-small btn-primary">Add alert</button>
                    <span id="lp-msg" class="form-hint"></span>
                </div>`;
            if (has) this.drawCharts(d);
            el.querySelector('#lp-add')?.addEventListener('click', () => this.add(el, a, b, hours));
            el.querySelectorAll('[data-lp-del]').forEach(btn => btn.addEventListener('click', async () => {
                await fetch(`/api/link-alerts/${btn.dataset.lpDel}`, { method: 'DELETE' });
                this.renderInto(el, a, b, hours);
            }));
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Failed to load.</p>';
            console.error('LinkProm error:', error);
        }
    },

    last(rows, key) {
        for (let i = rows.length - 1; i >= 0; i--) if (rows[i][key] != null) return rows[i][key];
        return null;
    },

    summary(x) {
        const st = x.stations, q = x.lqm, p = x.polled;
        const mcs = this.last(st, 'tx_mcs'), rate = this.last(st, 'tx_rate'), sig = this.last(st, 'signal');
        const pq = this.last(p, 'ping_quality') ?? this.last(q, 'ping_quality');
        const pms = this.last(p, 'lqm_rtt') ?? this.last(q, 'ping_ms');
        return `<div class="lp-dir"><strong>${this.esc(x.node)} → ${this.esc(x.peer)}</strong>
            <span>MCS ${mcs ?? '-'}</span><span>${rate != null ? (rate / 1e6).toFixed(1) + ' Mbps' : '- Mbps'}</span>
            <span>${sig != null ? sig + ' dBm' : ''}</span>
            <span>LQM ping ${pq != null ? Math.round(pq) + '%' : '-'}${pms != null ? ' · ' + Number(pms).toFixed(1) + ' ms' : ''}</span></div>`;
    },

    drawCharts(d) {
        const colors = ['#2980b9', '#c0392b'];
        const pts = (rows, key, scale = 1) => rows.filter(r => r[key] != null)
            .map(r => ({ x: new Date(r.timestamp.replace(' ', 'T')), y: r[key] * scale }));
        const ds = (key, src, scale, extra = {}) => [d.forward, d.reverse].map((x, i) => ({
            label: `${x.node} → ${x.peer}`, data: pts(x[src], key, scale), borderColor: colors[i],
            backgroundColor: colors[i], pointRadius: 0, borderWidth: 1.5, stepped: key === 'tx_mcs', ...extra
        }));
        const pollDs = (key, promKey) => [d.forward, d.reverse].flatMap((x, i) => [
            { label: `${x.node} → ${x.peer} (our scans)`, data: pts(x.polled, key), borderColor: colors[i],
              backgroundColor: colors[i], pointRadius: 0, borderWidth: 1.5 },
            { label: `${x.node} → ${x.peer} (Prometheus)`, data: pts(x.lqm, promKey), borderColor: colors[i],
              borderDash: [4, 3], pointRadius: 0, borderWidth: 1 }
        ]).filter(s => s.data.length);
        const opts = (y) => ({ responsive: true, maintainAspectRatio: false, animation: false, parsing: true,
            scales: { x: { type: 'time' }, y: { title: { display: !!y, text: y || '' }, beginAtZero: true } },
            plugins: { legend: { labels: { boxWidth: 10, font: { size: 10 } } } } });
        const mk = (id, datasets, y) => {
            const c = document.getElementById(id);
            if (c && typeof Chart !== 'undefined') this.charts.push(new Chart(c, { type: 'line', data: { datasets }, options: opts(y) }));
        };
        mk('lp-mcs', ds('tx_mcs', 'stations', 1), 'MCS');
        mk('lp-rate', ds('tx_rate', 'stations', 1e-6), 'Mbps');
        mk('lp-pingq', pollDs('ping_quality', 'ping_quality'), '%');
        mk('lp-pingms', pollDs('lqm_rtt', 'ping_ms'), 'ms');
    },

    describe(r) {
        const parts = [];
        if (r.min_snr != null) parts.push(`SNR < ${r.min_snr} dB`);
        if (r.min_quality != null) parts.push(`LQ < ${r.min_quality}%`);
        if (r.min_rate_mbps != null) parts.push(`rate < ${r.min_rate_mbps} Mbps`);
        if (r.min_mcs != null) parts.push(`MCS < ${r.min_mcs}`);
        if (r.max_flaps != null) parts.push(`> ${r.max_flaps} drops in ${r.flap_hours} h`);
        if (r.alert_missing) parts.push('missing');
        return parts.join(', ') || 'no limits';
    },

    async add(el, a, b, hours) {
        const [source, target] = el.querySelector('#lp-dir').value.split('|');
        const num = id => { const v = el.querySelector(id).value; return v === '' ? null : Number(v); };
        const body = { source, target, min_snr: num('#lp-snr'), min_quality: num('#lp-lq'),
                       min_rate_mbps: num('#lp-rate-min'), max_flaps: num('#lp-flaps'),
                       alert_missing: el.querySelector('#lp-missing').checked };
        const resp = await fetch('/api/link-alerts', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                       body: JSON.stringify(body) });
        const d = await resp.json();
        if (!resp.ok) {
            el.querySelector('#lp-msg').textContent = d.error || 'Failed';
            return;
        }
        this.renderInto(el, a, b, hours);
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
