/**
 * Starting Node section of the Reports tab (collector_health.py via
 * /api/collector/health): is a problem on this PC / its LAN / the starting
 * node, or out on the mesh? Follows the Reports window; ReportsModule.load() calls
 * CollectorHealth.load() after each load.
 */
const CollectorHealth = {
    initialized: false,
    data: null,
    charts: {},
    seq: 0,

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('ch-check')?.addEventListener('click', () => this.checkNow());
    },

    async load(start, end) {
        this.init();
        const seq = ++this.seq;
        try {
            const resp = await fetch(`/api/collector/health?${new URLSearchParams({ start, end })}`);
            const data = await resp.json();
            if (seq !== this.seq) return;
            if (!resp.ok) {
                document.getElementById('ch-status').innerHTML = `<p class="log-empty">${this.esc(data.error || 'Failed to load')}</p>`;
                return;
            }
            this.data = data;
            this.render();
        } catch (error) {
            console.error('Starting node health error:', error);
        }
    },

    async checkNow() {
        const button = document.getElementById('ch-check');
        if (button) button.disabled = true;
        try {
            const resp = await fetch('/api/collector/health/check', { method: 'POST' });
            const data = await resp.json();
            const c = data.check || {};
            const hint = document.getElementById('ch-check-result');
            if (hint) {
                hint.textContent = !resp.ok ? (data.error || 'Check failed')
                    : c.ok ? `Answered: ${this.ms(c.http_ms)} HTTP` + (c.connect_ms != null ? `, ${this.ms(c.connect_ms)} connect` : '') +
                        (c.dns_ms != null ? `, ${this.ms(c.dns_ms)} DNS` : '') + (c.node_name ? ` (${c.node_name})` : '')
                    : `No answer: ${this.cause(c.lan_ok === 0 ? 'lan_down' : c.error_class)}` +
                        (c.lan_gateway ? ` · LAN gateway ${c.lan_gateway} ${c.lan_ok ? 'answers' : 'not answering'}` : '');
            }
            if (typeof ReportsModule !== 'undefined') ReportsModule.load();
        } catch (error) {
            console.error('Starting node check error:', error);
        } finally {
            if (button) button.disabled = false;
        }
    },

    render() {
        this.renderStatus();
        this.renderTimeline();
        this.renderCharts();
        this.renderOutages();
    },

    renderStatus() {
        const s = this.data.summary;
        const stateText = s.state === 'up' ? 'Up' : s.state === 'down' ? 'Down' : 'No checks yet';
        const tiles = [
            ['Starting Node', `${s.host || '-'}${s.node_name ? ' · ' + s.node_name : ''}`],
            ['State', stateText, s.state === 'down' ? 'rpt-down' : s.state === 'up' ? 'rpt-up' : ''],
            ['Last Answer', this.when(s.last_success)],
            ['HTTP (now / p50 / p95)', `${this.ms(s.latest_http_ms)} / ${this.ms(s.http_p50)} / ${this.ms(s.http_p95)}`],
            ['Connect (p50 / p95)', `${this.ms(s.connect_p50)} / ${this.ms(s.connect_p95)}`],
            ['DNS (p50 / p95)', s.dns_applicable ? `${this.ms(s.dns_p50)} / ${this.ms(s.dns_p95)}` : 'n/a (IP address)'],
            ['Outages', `${s.outages} · ${this.dur(s.outage_minutes)}`],
            ['Longest', this.dur(s.longest_outage_min)],
            ['Lost Scans', s.lost_scans],
            ['Availability', s.availability_pct == null ? '-' : `${s.availability_pct}%`],
            ['LAN Failures', s.lan_failures]
        ];
        const cause = s.state === 'down' && s.state_cause
            ? `<p class="node-warning">Starting node not answering since the last check: ${this.esc(s.state_cause)}.</p>` : '';
        const since = s.recording_since
            ? `Detailed checks (DNS/connect/HTTP, LAN gateway) recorded since ${this.esc(s.recording_since)}; earlier outages come from
               gateway events and failed starting-node polls.`
            : 'No detailed checks recorded yet (they start with the next scan); outages shown come from gateway events and failed starting-node polls.';
        document.getElementById('ch-status').innerHTML = `
            <div class="rpt-summary ch-tiles">${tiles.map(([label, value, cls]) =>
                `<div class="rpt-tile"><span class="rpt-tile-label">${this.esc(label)}</span>` +
                `<span class="rpt-tile-value ${cls || ''}">${this.esc(value)}</span></div>`).join('')}</div>
            ${cause}
            <p class="form-hint">While the starting node is unreachable, scans and probes are aborted and their failures are
                discarded, not blamed on mesh nodes, so other pages show a gap rather than "down". ${since}</p>`;
    },

    /** Outage bars across the window; purple = this PC lost its LAN. */
    renderTimeline() {
        const el = document.getElementById('ch-timeline');
        const win = this.data.window;
        const t0 = this.date(win.start).getTime(), t1 = this.date(win.end).getTime();
        const W = 900, H = 46, padL = 4, padR = 4, barY = 6, barH = 18;
        const x = t => padL + Math.max(0, Math.min(1, (t - t0) / (t1 - t0))) * (W - padL - padR);
        const now = Date.now();
        const bars = this.data.outages.map(o => {
            const a = this.date(o.start).getTime();
            const b = o.end ? this.date(o.end).getTime() : Math.min(now, t1);
            const color = o.cause === 'lan_down' ? '#8e44ad' : '#c0392b';
            return `<rect x="${x(a)}" y="${barY}" width="${Math.max(2, x(b) - x(a))}" height="${barH}" fill="${color}" rx="2">
                <title>${this.esc(o.start)} – ${this.esc(o.end || 'ongoing')} (${this.dur(o.duration_min)}): ${this.esc(o.cause_label)}</title></rect>`;
        }).join('');
        let ticks = '';
        const span = t1 - t0;
        const step = [3600e3, 3 * 3600e3, 6 * 3600e3, 12 * 3600e3, 24 * 3600e3, 7 * 24 * 3600e3].find(s => span / s <= 12) || 7 * 24 * 3600e3;
        const first = Math.ceil(t0 / step) * step;
        for (let t = first; t <= t1; t += step) {
            const d = new Date(t);
            const label = step >= 24 * 3600e3 ? `${d.getMonth() + 1}/${d.getDate()}`
                : `${String(d.getHours()).padStart(2, '0')}:00`;
            ticks += `<line x1="${x(t)}" x2="${x(t)}" y1="${barY}" y2="${barY + barH}" class="spec-grid"/>
                <text x="${x(t)}" y="${H - 4}" class="spec-tick">${label}</text>`;
        }
        el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="spec-chart" role="img" aria-label="Starting node outages in the window">
            <rect x="${padL}" y="${barY}" width="${W - padL - padR}" height="${barH}" fill="#27ae60" fill-opacity="0.18" rx="2"/>
            ${ticks}${bars}</svg>
            <p class="form-hint">Green = starting node answering, red = unreachable, purple = this PC lost its LAN. Hover a bar for details.</p>`;
    },

    renderCharts() {
        if (typeof Chart === 'undefined') return;
        const d = this.data;
        const win = d.window;
        const timeScale = typeof ReportsModule !== 'undefined' ? ReportsModule.timeScale(win)
            : { type: 'time', min: this.date(win.start), max: this.date(win.end) };
        const pts = key => d.series.filter(r => r[key] != null).map(r => ({ x: this.date(r.t), y: r[key] }));
        const line = (label, color, key) => ({
            label, data: pts(key), borderColor: color, backgroundColor: color, borderWidth: 1.5,
            pointRadius: d.series.length > 60 ? 0 : 2, spanGaps: false, tension: 0.2
        });
        this.chart('latency', 'ch-latency-chart', {
            type: 'line',
            data: { datasets: [line('HTTP sysinfo (ms)', '#3498db', 'http_ms'), line('TCP connect (ms)', '#16a085', 'connect_ms'),
                line('DNS (ms)', '#e67e22', 'dns_ms')] },
            options: { scales: { x: timeScale, y: { beginAtZero: true, title: { display: true, text: 'ms' } } } }
        });
        const bar = (label, color, key) => ({ label, data: pts(key), backgroundColor: color, stack: 'polls' });
        this.chart('polls', 'ch-polls-chart', {
            type: 'bar',
            data: { datasets: [
                bar('Polls answered', 'rgba(39, 174, 96, 0.7)', 'polls_ok'),
                { label: 'Polls failed', data: d.series.map(r => ({ x: this.date(r.t), y: r.polls - r.polls_ok })),
                    backgroundColor: 'rgba(192, 57, 43, 0.7)', stack: 'polls' },
                bar('Starting-node checks failed', 'rgba(142, 68, 173, 0.8)', 'failed')
            ] },
            options: { scales: { x: { ...timeScale, stacked: true, offset: false }, y: { beginAtZero: true, stacked: true } } }
        });
        this.chart('hours', 'ch-hours-chart', {
            type: 'bar',
            data: {
                labels: [...Array(24).keys()].map(h => String(h).padStart(2, '0')),
                datasets: [{ label: 'Outage minutes', data: d.hour_profile, backgroundColor: 'rgba(192, 57, 43, 0.7)' }]
            },
            options: { scales: { y: { beginAtZero: true, title: { display: true, text: 'minutes' } },
                x: { title: { display: true, text: 'hour of day' } } } }
        });
    },

    chart(key, canvasId, config) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        if (this.charts[key]) this.charts[key].destroy();
        config.options = {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { intersect: false, mode: 'index' },
            plugins: { legend: { display: true, position: 'top' } },
            ...config.options
        };
        this.charts[key] = new Chart(ctx, config);
    },

    renderOutages() {
        const el = document.getElementById('ch-outages');
        const rows = this.data.outages;
        if (!rows.length) {
            el.innerHTML = '<p class="log-empty">No starting-node outages in this window.</p>';
            return;
        }
        const sourceLabel = { collector_health: 'checks', gateway_events: 'events', legacy_polls: 'older polls' };
        el.innerHTML = `<table class="info-table diag-table rpt-table">
            <thead><tr><th>Start</th><th>End</th><th>Duration</th><th>Cause</th><th>Lost Scans</th><th>From</th></tr></thead>
            <tbody>${rows.map(o => `<tr>
                <td>${this.esc(o.start)}</td>
                <td>${o.end ? this.esc(o.end) : '<span class="rpt-down">ongoing</span>'}</td>
                <td>${this.dur(o.duration_min)}</td>
                <td class="${o.cause === 'lan_down' ? 'rpt-scanner_unreachable' : 'rpt-down'}">${this.esc(o.cause_label)}</td>
                <td>${o.lost_scans}</td>
                <td>${this.esc(o.sources.map(s => sourceLabel[s] || s).join(', '))}</td></tr>`).join('')}</tbody></table>`;
    },

    cause(key) {
        return (this.data?.cause_labels || {})[key] || key || 'unknown';
    },

    date(ts) {
        return new Date(String(ts).replace(' ', 'T'));
    },

    when(ts) {
        return ts ? ts.slice(5, 16) : '-';
    },

    ms(v) {
        return v == null ? '-' : `${Math.round(v)} ms`;
    },

    dur(min) {
        if (!min) return '0 min';
        return min >= 60 ? `${Math.floor(min / 60)} h ${Math.round(min % 60)} min` : `${Math.round(min)} min`;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
