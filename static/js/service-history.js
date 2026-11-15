/**
 * Service check history (TODO #94; service_check.service_probes): a
 * Kuma-style heartbeat bar of the last 50 checks, uptime over 24 h / 30 d /
 * 1 y, and a detail panel with a response-time chart (Chart.js) over 24 h,
 * 7 d or 30 d. Used by the node page Services table and the Status page
 * Service Checks list.
 *
 *   ServiceHistory.bar(beats)             beats = [[ts, state, ms, error], ...] oldest first;
 *                                         state 1 answered, 0 failed, -1 failed while the node was down
 *   ServiceHistory.uptime(u)              "24 h 99.9% · 30 d 99.8% · 1 y 99.7%"
 *   ServiceHistory.open(el, node, service) detail panel into el (toggles when it is already open)
 */
const ServiceHistory = {
    SLOTS: 50,
    chart: null,
    current: null,

    esc(v) {
        return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    bar(beats, attrs = '') {
        beats = beats || [];
        const pad = Math.max(0, this.SLOTS - beats.length);
        const cls = { 1: 'sh-up', 0: 'sh-down', '-1': 'sh-nodedown' };
        const tip = b => `${b[0].slice(5, 16)} ` + (b[1] === 1 ? `answered${b[2] != null ? ` in ${b[2]} ms` : ''}`
            : b[1] === 0 ? `failed: ${b[3] || 'no answer'}` : `failed while the node was down: ${b[3] || ''}`);
        return `<span class="sh-bar" ${attrs} role="img" aria-label="Last ${beats.length} checks">${
            '<i class="sh-empty"></i>'.repeat(pad)}${beats.map(b =>
            `<i class="${cls[b[1]]}" title="${this.esc(tip(b))}"></i>`).join('')}</span>`;
    },

    pct(v) {
        return v == null ? '–' : `${v >= 99.995 ? 100 : v.toFixed(v >= 99 ? 2 : 1)}%`;
    },

    pctClass(v) {
        return v == null ? '' : v >= 99 ? 'sh-pct-good' : v >= 95 ? 'sh-pct-warn' : 'sh-pct-bad';
    },

    uptime(u) {
        u = u || {};
        return [['24h', '24 h'], ['30d', '30 d'], ['1y', '1 y']].map(([k, l]) =>
            `<span class="sh-up-item">${l} <b class="${this.pctClass(u[k])}">${this.pct(u[k])}</b></span>`).join(' ');
    },

    /** Bar + uptime in one cell; data-sh-node/service make it open the detail on click. */
    cell(node, service, beats, uptime) {
        if (!beats || !beats.length) return '<small class="form-hint">no checks yet</small>';
        return `<button type="button" class="sh-cell" data-sh-node="${this.esc(node)}" data-sh-service="${this.esc(service)}"
            title="Response-time chart and history">${this.bar(beats)}<span class="sh-uptime">${this.uptime(uptime)}</span></button>`;
    },

    /** Wire clicks on .sh-cell buttons inside `container`, opening the detail into `target`. */
    bind(container, target) {
        if (!container || container.dataset.shBound) return;
        container.dataset.shBound = '1';
        container.addEventListener('click', (e) => {
            const b = e.target.closest('.sh-cell');
            if (!b || !container.contains(b)) return;
            e.preventDefault();
            this.open(typeof target === 'function' ? target() : target, b.dataset.shNode, b.dataset.shService);
        });
    },

    async open(el, node, service, hours) {
        if (!el) return;
        const key = `${node}\u0000${service}`;
        if (!hours && this.current === key && el.innerHTML) {      // second click closes it
            this.close(el);
            return;
        }
        this.current = key;
        hours = hours || 24;
        el.innerHTML = `<div class="sh-detail"><p class="loading">Loading ${this.esc(service)}…</p></div>`;
        let d;
        try {
            const r = await fetch(`/api/services/history?node=${encodeURIComponent(node)}&service=${encodeURIComponent(service)}&hours=${hours}`);
            d = await r.json();
            if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        } catch (e) {
            el.innerHTML = `<div class="sh-detail"><p class="log-empty">Could not load the history: ${this.esc(e.message)}</p></div>`;
            return;
        }
        const w = d.window;
        const fails = d.probes.filter(p => !p.ok);
        el.innerHTML = `<div class="sh-detail">
            <div class="sh-detail-head">
                <h4>${this.esc(service)} <small class="form-hint">on ${this.esc(node)}</small></h4>
                <select class="form-input sh-hours" aria-label="Chart window">
                    ${[[24, '24 hours'], [168, '7 days'], [720, '30 days']].map(([h, l]) =>
                        `<option value="${h}"${h === d.hours ? ' selected' : ''}>${l}</option>`).join('')}
                </select>
                <button type="button" class="btn btn-secondary btn-small sh-close">Close</button>
            </div>
            <p>${this.bar(d.beats)} <span class="sh-uptime">${this.uptime(d.uptime)}</span></p>
            <p class="form-hint">In this window: ${w.answered} of ${w.checks} checks answered (${this.pct(w.uptime_pct)})${
                w.ms_median != null ? ` · response median ${w.ms_median} ms, 95% under ${w.ms_p95} ms` : ''}.
                Checks while the node itself was down don't count against the service.${
                d.history_since ? ` History kept since ${this.esc(d.history_since.slice(0, 16))}.` : ''}</p>
            <div class="sh-chart-wrap"><canvas></canvas></div>
            ${fails.length ? `<details class="diag-details"><summary>${fails.length} failed check(s)</summary>
                <table class="info-table diag-table"><thead><tr><th>Time</th><th>Error</th><th>Node</th></tr></thead><tbody>${
                fails.slice(-100).reverse().map(p => `<tr><td>${this.esc(p.ts)}</td><td>${this.esc(p.error || 'no answer')}</td>
                    <td>${p.node_up ? 'up' : 'down (not counted)'}</td></tr>`).join('')}</tbody></table></details>` : ''}
        </div>`;
        el.querySelector('.sh-close').addEventListener('click', () => this.close(el));
        el.querySelector('.sh-hours').addEventListener('change', (e) => this.open(el, node, service, Number(e.target.value)));
        this.draw(el.querySelector('canvas'), d);
        el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    },

    close(el) {
        if (this.chart) { this.chart.destroy(); this.chart = null; }
        this.current = null;
        el.innerHTML = '';
    },

    draw(canvas, d) {
        if (this.chart) { this.chart.destroy(); this.chart = null; }
        if (!canvas || typeof Chart === 'undefined') return;
        const t = s => new Date(s.replace(' ', 'T'));
        const okPts = d.probes.filter(p => p.ok && p.ms != null).map(p => ({ x: t(p.ts), y: p.ms }));
        const top = Math.max(10, ...okPts.map(p => p.y)) * 1.05;
        const failPts = (up) => d.probes.filter(p => !p.ok && p.node_up === up)
            .map(p => ({ x: t(p.ts), y: top, error: p.error }));
        const end = new Date();
        this.chart = new Chart(canvas, {
            type: 'line',
            data: { datasets: [
                { label: 'Response (ms)', data: okPts, borderColor: '#2e86c1', backgroundColor: '#2e86c1',
                  borderWidth: 1.5, pointRadius: okPts.length > 150 ? 0 : 2, tension: 0.2, spanGaps: false },
                { label: 'Failed', type: 'scatter', data: failPts(true), backgroundColor: '#c0392b', pointRadius: 4, pointStyle: 'rectRot' },
                { label: 'Failed, node down', type: 'scatter', data: failPts(false), backgroundColor: '#95a5a6', pointRadius: 3 },
            ] },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                interaction: { intersect: false, mode: 'nearest' },
                plugins: { tooltip: { callbacks: { label: (c) => c.raw.error != null
                    ? `${c.dataset.label}: ${c.raw.error || 'no answer'}` : `${c.raw.y} ms` } } },
                scales: {
                    x: { type: 'time', min: new Date(end - d.hours * 3600e3), max: end },
                    y: { beginAtZero: true, suggestedMax: top, title: { display: true, text: 'ms' } }
                }
            }
        });
    }
};
