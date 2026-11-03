/**
 * Node page "Prometheus details" (TODO #63, #64, #68): read from Prometheus
 * (prom_source.py) when the page opens -- memory total/used, CPU, flash,
 * /tmp, processes, Babel routes and neighbours, traffic per interface,
 * per-station MCS and signal, LQM ping quality -- with 24 h charts and any
 * open resource / route / MCS alarms. Our own polling is untouched; when
 * Prometheus can't be reached or doesn't scrape this node, the section says
 * so and the rest of the page is unaffected.
 */
const NodeProm = {
    charts: [],

    async load(node, el) {
        el.innerHTML = '<p class="loading">Reading Prometheus...</p>';
        let d;
        try {
            d = await (await fetch(`/api/prometheus/node/${encodeURIComponent(node)}?hours=24`)).json();
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Could not ask the server for Prometheus details.</p>';
            return;
        }
        const grafana = d.grafana_url ? ` · <a href="${this.esc(d.grafana_url)}" target="_blank" rel="noopener">Grafana dashboard</a>` : '';
        if (!d.enabled || !d.available) {
            el.innerHTML = `<p class="log-empty">${this.esc(d.error || 'Prometheus is not available.')}${grafana}</p>
                ${d.stored ? `<p class="form-hint">Last stored sample: ${this.esc(d.stored[0].timestamp)}.</p>` : ''}`;
            return;
        }
        if (!d.scraped) {
            el.innerHTML = `<p class="log-empty">Prometheus (${this.esc(d.source)}) doesn't scrape this node.
                It scrapes ${d.scraped_nodes.length} node(s): ${d.scraped_nodes.map(n => this.esc(n)).join(', ')}.${grafana}</p>`;
            return;
        }
        const h = d.health || {};
        const mb = v => v == null ? '-' : (v / 1048576).toFixed(v < 10 * 1048576 ? 1 : 0) + ' MB';
        const tile = (label, value, sub, warn) => `<div class="np-tile${warn ? ' np-tile-warn' : ''}">
            <div class="np-tile-label">${label}</div><div class="np-tile-value">${value}</div>
            ${sub ? `<div class="np-tile-sub">${sub}</div>` : ''}</div>`;
        const days = s => s == null ? '-' : s >= 86400 ? `${Math.floor(s / 86400)} d ${Math.floor(s % 86400 / 3600)} h` : `${Math.floor(s / 3600)} h`;
        const memFree = h.mem_used_pct != null ? 100 - h.mem_used_pct : null;
        const flashFree = h.flash_used_pct != null ? 100 - h.flash_used_pct : null;
        const info = d.info || {};
        el.innerHTML = `
            <p class="health-footnote">Read now from Prometheus (${this.esc(d.source)}), which scrapes this node's
                metrics page; our own polling is unchanged.${grafana}
                ${info.model ? ` · ${this.esc(info.model)}` : ''}${info.firmware_version ? ` · ${this.esc(info.firmware_version)}` : ''}</p>
            ${(d.alarms || []).map(a => `<p class="node-warning"><strong>${this.esc(a.label)}</strong> since ${this.esc(a.since)}: ${this.esc(a.detail)}</p>`).join('')}
            <div class="np-tiles">
                ${tile('Memory used', h.mem_used_pct != null ? h.mem_used_pct + '%' : '-', `${mb(h.mem_avail)} free of ${mb(h.mem_total)}`, memFree != null && memFree < 10)}
                ${tile('CPU busy', h.cpu_busy != null ? Math.round(h.cpu_busy) + '%' : '-', h.load1 != null ? `load ${h.load1}` : '', h.cpu_busy >= 90)}
                ${tile('Flash (overlay)', h.flash_used_pct != null ? h.flash_used_pct + '% used' : '-', `${mb(h.flash_avail)} free of ${mb(h.flash_size)}`, flashFree != null && flashFree < 10)}
                ${tile('/tmp (RAM)', h.tmp_size ? Math.round(100 - 100 * h.tmp_avail / h.tmp_size) + '% used' : '-', `${mb(h.tmp_avail)} free`, h.tmp_size && h.tmp_avail / h.tmp_size < 0.1)}
                ${tile('Processes', h.procs ?? '-', h.procs_blocked ? `${h.procs_blocked} blocked` : '')}
                ${tile('Babel routes', h.routes ?? '-', `${h.neighbours ?? '-'} neighbours · ${h.aredn_routes ?? '-'} arednlink`)}
                ${tile('Uptime', days(h.uptime_s), '')}
            </div>
            <p class="form-hint">Not in AREDN's metrics page: ${d.missing.map(m => this.esc(m)).join(', ')}.</p>
            <div class="node-page-charts">
                <div class="chart-card"><h4>Available memory, MB (24 h)</h4><canvas id="np-prom-mem"></canvas></div>
                <div class="chart-card"><h4>CPU busy, % (24 h)</h4><canvas id="np-prom-cpu"></canvas></div>
                <div class="chart-card"><h4>Babel routes and neighbours (24 h)</h4><canvas id="np-prom-routes"></canvas></div>
                <div class="chart-card"><h4>RF traffic, Mbps (24 h)</h4><canvas id="np-prom-rf"></canvas></div>
            </div>
            ${this.ifaceTable(d.ifaces || [])}
            ${this.stationTable(d.stations || [])}
            ${this.lqmTable(d.lqm || [])}`;
        this.drawCharts(d.series || {});
    },

    ifaceTable(rows) {
        if (!rows.length) return '';
        const m = v => v == null ? '-' : (v / 1e6).toFixed(2);
        const n = v => v == null ? '-' : Number(v).toFixed(v >= 10 ? 0 : 1);
        return `<h3 class="subsection-heading">Traffic per interface (last 5 min)</h3>
            <div class="table-scroll-wrapper"><table class="info-table diag-table"><thead><tr><th>Interface</th><th>Kind</th>
            <th>Receive Mbps</th><th>Transmit Mbps</th><th>Packets/s in / out</th><th>Drops/s</th><th>Errors/s</th><th>FIFO / frame errors/s</th></tr></thead>
            <tbody>${rows.map(r => `<tr${(r.errors_ps || 0) > 0.1 ? ' class="rel-row rel-row-adjacent"' : ''}><td>${this.esc(r.iface)}</td><td>${this.esc(r.kind)}</td>
                <td>${m(r.rx_bps)}</td><td>${m(r.tx_bps)}</td><td>${n(r.rx_pps)} / ${n(r.tx_pps)}</td>
                <td>${n(r.drops_ps)}</td><td>${n(r.errors_ps)}</td><td>${n(r.fifo_ps)} / ${n(r.frame_ps)}</td></tr>`).join('')}</tbody></table></div>`;
    },

    stationTable(rows) {
        if (!rows.length) return '';
        return `<h3 class="subsection-heading">Wireless stations</h3>
            <div class="table-scroll-wrapper"><table class="info-table diag-table"><thead><tr><th>Station</th>
            <th>MCS out / in</th><th>Rate out / in, Mbps</th><th>Signal</th><th>Packets/s out / in</th></tr></thead>
            <tbody>${rows.map(r => `<tr><td>${r.peer ? `<a href="/nodes/${encodeURIComponent(r.peer)}">${this.esc(r.peer)}</a>` : this.esc(r.mac)}</td>
                <td>${r.tx_mcs ?? '-'} / ${r.rx_mcs ?? '-'}</td>
                <td>${r.tx_rate != null ? (r.tx_rate / 1e6).toFixed(1) : '-'} / ${r.rx_rate != null ? (r.rx_rate / 1e6).toFixed(1) : '-'}</td>
                <td>${r.signal != null ? r.signal + ' dBm' : '-'}</td>
                <td>${r.tx_pps != null ? r.tx_pps.toFixed(1) : '-'} / ${r.rx_pps != null ? r.rx_pps.toFixed(1) : '-'}</td></tr>`).join('')}</tbody></table></div>`;
    },

    lqmTable(rows) {
        if (!rows.length) return '';
        return `<h3 class="subsection-heading">LQM neighbours (ping)</h3>
            <div class="table-scroll-wrapper"><table class="info-table diag-table"><thead><tr><th>Neighbour</th><th>Type</th>
            <th>Ping quality</th><th>Ping time</th><th>SNR / reverse</th><th>Quality</th></tr></thead>
            <tbody>${rows.map(r => `<tr><td>${this.esc(r.peer)}</td><td>${this.esc(r.link_type || '')}</td>
                <td>${r.ping_quality != null ? Math.round(r.ping_quality) + '%' : '-'}</td>
                <td>${r.ping_ms != null ? r.ping_ms.toFixed(1) + ' ms' : '-'}</td>
                <td>${r.snr ?? '-'} / ${r.rev_snr ?? '-'}</td><td>${r.quality != null ? Math.round(r.quality) + '%' : '-'}</td></tr>`).join('')}</tbody></table></div>`;
    },

    drawCharts(series) {
        this.charts.forEach(c => c.destroy());
        this.charts = [];
        if (typeof Chart === 'undefined') return;
        const pts = s => (s || []).filter(p => p.v != null).map(p => ({ x: new Date(p.t.replace(' ', 'T')), y: p.v }));
        const line = (label, data, color, extra = {}) => ({ label, data: pts(data), borderColor: color, backgroundColor: color,
                                                             pointRadius: 0, borderWidth: 1.5, ...extra });
        const opts = (extra = {}) => ({ responsive: true, maintainAspectRatio: false, animation: false,
            scales: { x: { type: 'time' }, y: { beginAtZero: true }, ...extra },
            plugins: { legend: { labels: { boxWidth: 10, font: { size: 10 } } } } });
        const mk = (id, datasets, o) => {
            const c = document.getElementById(id);
            if (c) this.charts.push(new Chart(c, { type: 'line', data: { datasets }, options: o || opts() }));
        };
        mk('np-prom-mem', [line('Available', series.mem_avail_mb, '#2e86c1')]);
        mk('np-prom-cpu', [line('CPU busy', series.cpu_busy, '#c0392b')], opts({ y: { min: 0, max: 100 } }));
        mk('np-prom-routes', [line('Routes', series.routes, '#117864'),
                              line('Neighbours', series.neighbours, '#b9770e', { yAxisID: 'y2' })],
           opts({ y2: { position: 'right', beginAtZero: true, grid: { drawOnChartArea: false } } }));
        mk('np-prom-rf', [line('Receive', series.rf_rx_mbps, '#2e86c1'), line('Transmit', series.rf_tx_mbps, '#8e44ad')]);
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => {
    const el = document.getElementById('node-prom');
    if (el && window.NODE_NAME) NodeProm.load(window.NODE_NAME, el);
    const embed = document.getElementById('node-embed-links');
    if (embed && window.NODE_NAME) {
        const base = `${location.origin}/embed/node/${encodeURIComponent(window.NODE_NAME)}`;
        embed.innerHTML = `Embed: <a href="${base}" target="_blank" rel="noopener">card</a> ·
            <a href="${base}/badge.svg" target="_blank" rel="noopener">badge</a>
            <code class="np-embed-code">&lt;iframe src="${base}" width="420" height="260" frameborder="0"&gt;&lt;/iframe&gt;</code>`;
    }
});
