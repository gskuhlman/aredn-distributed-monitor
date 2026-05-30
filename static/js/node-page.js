const NodePage = {
    nodeName: window.NODE_NAME,
    data: null,
    charts: {},
    testResults: [],

    async init() {
        document.getElementById('node-scan-now')?.addEventListener('click', () => this.scanNow());
        document.getElementById('node-log-search')?.addEventListener('input', () => this.renderLog());
        await this.load();
    },

    async load() {
        this.setStatus('Loading...');
        try {
            const response = await fetch(`/api/nodes/full/${encodeURIComponent(this.nodeName)}?hours=24&event_limit=500`);
            if (!response.ok) throw new Error('Node not found');
            this.data = await response.json();
            this.render();
            this.setStatus('Loaded');
        } catch (error) {
            this.setStatus('Failed to load');
            document.getElementById('node-summary').innerHTML = `<p class="error">${this.escapeHtml(error.message)}</p>`;
        }
    },

    async scanNow() {
        const button = document.getElementById('node-scan-now');
        const original = button.textContent;
        button.disabled = true;
        button.textContent = 'Scanning...';
        this.setStatus('Scanning...');
        try {
            const response = await fetch(`/api/nodes/scan/${encodeURIComponent(this.nodeName)}`, { method: 'POST' });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || 'Scan failed');
            this.showToast('success', 'Scan Complete', `${this.nodeName} was scanned`);
            await this.load();
        } catch (error) {
            this.showToast('error', 'Scan Failed', error.message);
            this.setStatus('Scan failed');
        } finally {
            button.disabled = false;
            button.textContent = original;
        }
    },

    render() {
        const node = this.data.node;
        document.getElementById('node-page-title').textContent = node.name;
        const scanButton = document.getElementById('node-scan-now');
        if (scanButton && node.is_link_only) {
            scanButton.disabled = true;
            scanButton.title = 'No polled IP address is available for this link-only endpoint';
        }
        this.renderSummary();
        this.renderLinks();
        this.renderServices();
        this.renderCharts();
        this.renderLinkHealth();
        this.renderLog();
    },

    renderSummary() {
        const node = this.data.node;
        document.getElementById('node-summary').innerHTML = `
            <table class="info-table">
                <tr><td>Name:</td><td>${this.escapeHtml(node.name)}</td></tr>
                <tr><td>IP:</td><td>${node.ip ? `<a href="http://${encodeURIComponent(node.ip)}" target="_blank">${this.escapeHtml(node.ip)}</a>` : 'N/A'}</td></tr>
                <tr><td>Model:</td><td>${this.escapeHtml(node.model || 'Unknown')}</td></tr>
                <tr><td>Firmware:</td><td>${this.escapeHtml(node.firmware_version || 'Unknown')}</td></tr>
                <tr><td>Description:</td><td>${this.escapeHtml(node.description || 'N/A')}</td></tr>
                <tr><td>RF Frequency:</td><td>${this.escapeHtml(node.rf_frequency || 'N/A')}</td></tr>
                <tr><td>RF Channel:</td><td>${this.escapeHtml(node.rf_channel || 'N/A')}</td></tr>
                <tr><td>First Seen:</td><td>${this.formatDate(node.first_seen)}</td></tr>
                <tr><td>Last Seen:</td><td>${this.formatDate(node.last_seen)}</td></tr>
                <tr><td>Status:</td><td>${this.getNodeStatusText(node)}</td></tr>
                <tr><td>Supernode:</td><td>${node.is_supernode === 1 ? 'Yes' : 'No'}</td></tr>
                <tr><td>Latitude:</td><td>${this.escapeHtml(node.lat || 'N/A')}</td></tr>
                <tr><td>Longitude:</td><td>${this.escapeHtml(node.lon || 'N/A')}</td></tr>
                ${node.is_link_only ? `<tr><td>Reported By:</td><td>${this.escapeHtml((node.reporters || []).join(', ') || 'N/A')}</td></tr>` : ''}
            </table>
            ${node.is_link_only ? `
                <p class="node-warning">${this.escapeHtml(node.lqm_status_message || 'LQM-only neighbor')}</p>
                <table class="info-table">
                    <tr><td>Identity Status:</td><td>${this.escapeHtml(node.identity_status || 'lqm_only')}</td></tr>
                    <tr><td>Routability Status:</td><td>${this.escapeHtml(node.routability_status || 'unknown')}</td></tr>
                    <tr><td>MAC Address:</td><td>${this.escapeHtml((node.mac_addresses || []).join(', ') || 'N/A')}</td></tr>
                    <tr><td>Canonical IP:</td><td>${this.escapeHtml((node.canonical_ips || []).join(', ') || 'N/A')}</td></tr>
                    <tr><td>Likely causes:</td><td>No routable canonical IP, DNS failure, discovery depth limit, supernode boundary, or node HTTP/sysinfo unreachable</td></tr>
                    <tr><td>Useful check:</td><td>Inspect the reporter's LQM tracker entry for routable and canonical_ip</td></tr>
                </table>
            ` : ''}
        `;
    },

    deduplicateLinks(links) {
        // Links are directional (A→B and B→A are separate rows).
        // Merge into one row per peer, preferring the row where this node is
        // the source (local LQM perspective), falling back to the reverse.
        const byPeer = {};
        const me = this.data.node.name;
        for (const link of links) {
            const peer = link.source_node === me ? link.target_node : link.source_node;
            const isLocal = link.source_node === me;
            const existing = byPeer[peer];
            if (!existing) {
                byPeer[peer] = { peer, local: isLocal ? link : null, remote: isLocal ? null : link };
            } else {
                if (isLocal) existing.local = link; else existing.remote = link;
            }
        }
        return Object.values(byPeer).map(({ peer, local, remote }) => {
            // Prefer local (this node's LQM report), fill gaps from remote
            const primary = local || remote;
            const secondary = local ? remote : null;
            if (!secondary) return { ...primary, _peer: peer };
            // Merge: take primary values, fill nulls from secondary
            const merged = { ...primary, _peer: peer };
            for (const key of ['snr', 'signal', 'noise', 'tx_rate', 'rx_rate', 'distance',
                               'mac_address', 'canonical_ip', 'identity_status',
                               'routability_status', 'lqm_status_message']) {
                if (merged[key] == null) merged[key] = secondary[key];
            }
            // Use earliest first_seen, latest last_seen, sum drop_count
            if (secondary.first_seen && (!merged.first_seen || secondary.first_seen < merged.first_seen))
                merged.first_seen = secondary.first_seen;
            if (secondary.last_seen && (!merged.last_seen || secondary.last_seen > merged.last_seen))
                merged.last_seen = secondary.last_seen;
            merged.drop_count = (merged.drop_count || 0) + (secondary.drop_count || 0);
            // Use worse status
            const statusRank = { removed: 0, dropped: 1, good: 2 };
            if ((statusRank[secondary.status] ?? 2) < (statusRank[merged.status] ?? 2))
                merged.status = secondary.status;
            return merged;
        });
    },

    renderLinks() {
        const rawLinks = this.data.all_links || [];
        if (!rawLinks.length) {
            document.getElementById('node-links').innerHTML = '<p>No links recorded for this node.</p>';
            return;
        }
        const links = this.deduplicateLinks(rawLinks);

        const rows = links.map(link => {
            const other = link._peer;
            const actions = this.data.node.is_link_only
                ? 'N/A'
                : `
                    <button class="btn btn-small btn-secondary" data-action="ping" data-source="${this.escapeAttr(this.data.node.name)}" data-target="${this.escapeAttr(other)}">Ping</button>
                    <button class="btn btn-small btn-secondary" data-action="iperf" data-source="${this.escapeAttr(this.data.node.name)}" data-target="${this.escapeAttr(other)}">iPerf3</button>
                `;
            return `
                <tr>
                    <td><a href="/nodes/${encodeURIComponent(other)}">${this.escapeHtml(other)}</a></td>
                    <td>${this.escapeHtml(link.link_type || '')}</td>
                    <td>${this.escapeHtml(link.quality ?? '')}</td>
                    <td>${this.escapeHtml(link.snr ?? 'N/A')}</td>
                    <td>${this.escapeHtml(link.distance ?? 'N/A')}</td>
                    <td>${this.escapeHtml(link.signal || 'N/A')}</td>
                    <td>${this.escapeHtml(link.noise || 'N/A')}</td>
                    <td>${this.escapeHtml(link.tx_rate || 'N/A')}</td>
                    <td>${this.escapeHtml(link.rx_rate || 'N/A')}</td>
                    <td>${this.escapeHtml(link.mac_address || 'N/A')}</td>
                    <td>${this.escapeHtml(link.canonical_ip || 'N/A')}</td>
                    <td>${this.escapeHtml(link.identity_status || 'N/A')}</td>
                    <td>${this.escapeHtml(link.routability_status || 'unknown')}</td>
                    <td>${this.escapeHtml(link.lqm_status_message || 'N/A')}</td>
                    <td>${this.escapeHtml(link.status || '')}</td>
                    <td>${this.escapeHtml(link.drop_count ?? 0)}</td>
                    <td>${this.formatDate(link.first_seen)}</td>
                    <td>${this.formatDate(link.stable_since)}</td>
                    <td>${this.formatDate(link.last_seen)}</td>
                    <td>${actions}</td>
                </tr>
            `;
        }).join('');

        const container = document.getElementById('node-links');
        container.innerHTML = `
            <div class="table-scroll-wrapper">
                <table class="info-table">
                    <tr><th>Node</th><th>Type</th><th>Quality</th><th>SNR</th><th>Distance</th><th>Signal</th><th>Noise</th><th>TX Rate</th><th>RX Rate</th><th>MAC</th><th>Canonical IP</th><th>Identity</th><th>Routability</th><th>LQM Status</th><th>Status</th><th>Drops</th><th>First Seen</th><th>Stable Since</th><th>Last Seen</th><th>Actions</th></tr>
                    ${rows}
                </table>
            </div>
        `;
        container.querySelectorAll('[data-action]').forEach(button => {
            button.addEventListener('click', () => this.runLinkTest(button.dataset.source, button.dataset.target, button.dataset.action, button));
        });
    },

    renderServices() {
        const services = this.data.services || [];
        if (!services.length) {
            document.getElementById('node-services').innerHTML = '<p>No services recorded for this node.</p>';
            return;
        }
        const rows = services.map(service => `
            <tr>
                <td>${service.link ? `<a href="${this.escapeAttr(service.link)}" target="_blank">${this.escapeHtml(service.name)}</a>` : this.escapeHtml(service.name)}</td>
                <td>${this.escapeHtml(service.protocol || 'N/A')}</td>
                <td>${this.escapeHtml(service.ip || 'N/A')}</td>
            </tr>
        `).join('');
        document.getElementById('node-services').innerHTML = `
            <table class="info-table">
                <tr><th>Service</th><th>Protocol</th><th>IP</th></tr>
                ${rows}
            </table>
        `;
    },

    renderCharts() {
        this.initCharts();
        const qualityHistory = this.data.quality_history || [];
        const pingHistory = this.data.ping_history || [];

        const quality = qualityHistory
            .filter(row => row.quality !== null)
            .map(row => ({ x: new Date(row.timestamp), y: row.quality }));
        const snr = qualityHistory
            .filter(row => row.snr !== null && row.snr !== undefined)
            .map(row => ({ x: new Date(row.timestamp), y: row.snr }));
        const throughputTx = qualityHistory
            .filter(row => row.throughput_tx !== null && row.throughput_tx !== undefined)
            .map(row => ({ x: new Date(row.timestamp), y: row.throughput_tx }));
        const throughputRx = qualityHistory
            .filter(row => row.throughput_rx !== null && row.throughput_rx !== undefined)
            .map(row => ({ x: new Date(row.timestamp), y: row.throughput_rx }));

        const pingAvg = pingHistory
            .filter(row => row.ping_avg !== null)
            .map(row => ({ x: new Date(row.timestamp), y: row.ping_avg }));
        const pingMin = pingHistory
            .filter(row => row.ping_min !== null && row.ping_min !== undefined)
            .map(row => ({ x: new Date(row.timestamp), y: row.ping_min }));
        const pingMax = pingHistory
            .filter(row => row.ping_max !== null && row.ping_max !== undefined)
            .map(row => ({ x: new Date(row.timestamp), y: row.ping_max }));
        const pingLoss = pingHistory
            .filter(row => row.ping_loss !== null && row.ping_loss !== undefined)
            .map(row => ({ x: new Date(row.timestamp), y: row.ping_loss }));
        const jitter = pingHistory
            .filter(row => row.jitter !== null && row.jitter !== undefined)
            .map(row => ({ x: new Date(row.timestamp), y: row.jitter }));

        this.charts.quality.data.datasets[0].data = quality;
        this.charts.quality.update('none');

        this.charts.snr.data.datasets[0].data = snr;
        this.charts.snr.update('none');

        this.charts.ping.data.datasets[0].data = pingAvg;
        this.charts.ping.data.datasets[1].data = pingMin;
        this.charts.ping.data.datasets[2].data = pingMax;
        this.charts.ping.data.datasets[3].data = jitter;
        this.charts.ping.data.datasets[4].data = pingLoss;
        this.charts.ping.update('none');

        this.charts.throughput.data.datasets[0].data = throughputTx;
        this.charts.throughput.data.datasets[1].data = throughputRx;
        this.charts.throughput.update('none');
    },

    renderLog() {
        const container = document.getElementById('node-log');
        const search = (document.getElementById('node-log-search')?.value || '').toLowerCase();
        const events = (this.data?.connectivity_log || []).filter(event => {
            const haystack = `${event.timestamp || ''} ${event.event_type || ''} ${event.node_name || ''} ${event.details || ''}`.toLowerCase();
            return !search || haystack.includes(search);
        });

        if (!events.length) {
            container.innerHTML = '<p class="log-empty">No matching log entries.</p>';
            return;
        }

        container.innerHTML = events.map(event => `
            <div class="log-entry ${event.severity === 'warning' ? 'event-warning' : 'event-info'}">
                <div class="log-details">
                    <div class="log-header">
                        <span class="log-type">${this.escapeHtml(event.event_type)}</span>
                        <span class="log-time">${this.formatDate(event.timestamp)}</span>
                    </div>
                    <div class="log-message">${this.escapeHtml(event.details || '')}</div>
                </div>
            </div>
        `).join('');
    },

    renderLinkHealth() {
        const container = document.getElementById('node-link-health');
        const health = this.data.link_health || [];
        if (!health.length) {
            container.innerHTML = '<p class="log-empty">No health data available. Link metrics are computed from scan and ping history.</p>';
            return;
        }

        const rows = health.map(h => {
            const grade = h.overall || {};
            const ping = h.ping || {};
            const mos = h.mos || {};
            const snr = h.snr || {};
            const noise = h.noise || {};
            const stability = h.stability || {};
            const flapping = h.flapping || {};
            const asymmetry = h.rate_asymmetry || {};

            return `<tr>
                <td><a href="/nodes/${encodeURIComponent(h.peer)}">${this.escapeHtml(h.peer)}</a></td>
                <td>${this.escapeHtml(h.link_type)}</td>
                <td><span class="health-badge health-grade-${grade.rating || 'unknown'}">${this.escapeHtml(grade.grade || '?')}</span></td>
                <td>${this.healthCell(ping.latency_avg, 'ms', ping.latency_rating)}</td>
                <td>${this.healthCell(ping.jitter_avg, 'ms', ping.jitter_rating)}</td>
                <td>${this.healthCell(ping.loss_avg, '%', ping.loss_rating)}</td>
                <td>${this.healthCell(mos.score, mos.label ? ' ' + mos.label : '', mos.rating)}</td>
                <td>${this.healthCell(snr.snr, ' dB', snr.rating)}</td>
                <td>${this.healthCell(noise.noise_floor, ' dBm', noise.noise_rating)}${noise.interference_likely ? ' <span class="health-flag health-poor" title="Elevated noise floor suggests interference">!</span>' : ''}</td>
                <td>${this.healthCell(stability.quality_stddev, '%', stability.rating)}</td>
                <td>${this.trendCell(stability.quality_trend)}</td>
                <td>${this.trendCell(stability.snr_trend)}</td>
                <td>${this.flapCell(flapping)}</td>
                <td>${this.healthCell(asymmetry.ratio, '', asymmetry.rating)}</td>
            </tr>`;
        }).join('');

        container.innerHTML = `
            <div class="table-scroll-wrapper">
                <table class="info-table health-table">
                    <thead>
                        <tr>
                            <th>Peer</th>
                            <th>Type</th>
                            <th>Grade</th>
                            <th>Latency</th>
                            <th>Jitter</th>
                            <th>Loss</th>
                            <th>VoIP MOS</th>
                            <th>SNR</th>
                            <th>Noise</th>
                            <th>RF Stability</th>
                            <th>Quality Trend</th>
                            <th>SNR Trend</th>
                            <th>Flapping/24h</th>
                            <th>TX/RX Ratio</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <div class="health-footnote">
                <p><strong>Flapping/24h</strong> counts link drop/restore events in the last 24 hours and classifies the cause:</p>
                <ul>
                    <li><strong>Node outage</strong> &mdash; link drops coincided with the source or peer node going offline (reboot, power loss, unreachable). DTD and Xlink (wired) drops are always attributed to the node since physical cables do not independently flap.</li>
                    <li><strong>RF instability</strong> &mdash; the RF link dropped with no corresponding node outage, indicating actual wireless signal issues such as interference, marginal SNR, or environmental factors.</li>
                    <li><strong>Mixed</strong> (e.g. "3 link / 2 node") &mdash; some drops were caused by node outages and others by independent link instability.</li>
                </ul>
                <p><strong>VoIP MOS</strong> is an estimated Mean Opinion Score (ITU-T E-model) from 1.0&ndash;4.5 computed from average latency, jitter, and packet loss. Scores above 4.0 are good for voice; below 3.5 most users will notice degradation.</p>
                <p><strong>RF Stability</strong> is the standard deviation of link quality over the measurement window. Low stddev with no dips below 50% is rated good; high variance or frequent quality dips indicates an unstable RF environment.</p>
            </div>
        `;
    },

    healthCell(value, suffix, rating) {
        if (value === null || value === undefined) return '<span class="health-val health-unknown">-</span>';
        return `<span class="health-val health-${rating || 'unknown'}">${this.escapeHtml(String(value))}${suffix}</span>`;
    },

    trendCell(trend) {
        if (!trend) return '<span class="health-val health-unknown">-</span>';
        const arrow = trend === 'improving' ? '&#9650;' : trend === 'degrading' ? '&#9660;' : '&#9654;';
        const rating = trend === 'degrading' ? 'poor' : trend === 'improving' ? 'good' : 'marginal';
        return `<span class="health-val health-${rating}">${arrow} ${this.escapeHtml(trend)}</span>`;
    },

    flapCell(flapping) {
        if (!flapping || flapping.flap_count === undefined) return '<span class="health-val health-unknown">-</span>';
        if (flapping.flap_count === 0) return '<span class="health-val health-good">0</span>';
        const cause = flapping.cause_label || '';
        const title = `${flapping.flap_count} events: ${flapping.node_flaps || 0} node, ${flapping.link_flaps || 0} link`;
        return `<span class="health-val health-${flapping.rating || 'unknown'}" title="${this.escapeAttr(title)}">${flapping.flap_count} <span class="flap-cause flap-${flapping.cause || 'unknown'}">${this.escapeHtml(cause)}</span></span>`;
    },

    initCharts() {
        if (this.charts.quality) return;
        const baseOptions = {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'index' },
            scales: { x: { type: 'time' }, y: { beginAtZero: true } },
            plugins: { legend: { display: true, position: 'top' } }
        };
        const thinLine = { borderWidth: 1, pointRadius: 0 };

        this.charts.quality = new Chart(document.getElementById('node-page-quality-chart'), {
            type: 'line',
            data: { datasets: [{ label: 'Quality %', borderColor: '#27ae60', data: [], ...thinLine, borderWidth: 2 }] },
            options: { ...baseOptions, scales: { ...baseOptions.scales, y: { beginAtZero: true, max: 100 } } }
        });

        this.charts.snr = new Chart(document.getElementById('node-page-snr-chart'), {
            type: 'line',
            data: { datasets: [{ label: 'SNR dB', borderColor: '#8e44ad', data: [], ...thinLine, borderWidth: 2 }] },
            options: baseOptions
        });

        this.charts.ping = new Chart(document.getElementById('node-page-ping-chart'), {
            type: 'line',
            data: {
                datasets: [
                    { label: 'Avg', borderColor: '#f39c12', data: [], ...thinLine, borderWidth: 2 },
                    { label: 'Min', borderColor: '#27ae60', data: [], ...thinLine, borderDash: [4, 2] },
                    { label: 'Max', borderColor: '#e74c3c', data: [], ...thinLine, borderDash: [4, 2] },
                    { label: 'Jitter', borderColor: '#8e44ad', data: [], ...thinLine, borderDash: [6, 3] },
                    { label: 'Loss %', borderColor: '#95a5a6', data: [], ...thinLine, borderDash: [2, 2], yAxisID: 'yLoss' }
                ]
            },
            options: {
                ...baseOptions,
                scales: {
                    x: { type: 'time' },
                    y: { beginAtZero: true, position: 'left', title: { display: true, text: 'ms' } },
                    yLoss: { beginAtZero: true, max: 100, position: 'right', grid: { drawOnChartArea: false }, title: { display: true, text: 'Loss %' } }
                }
            }
        });

        this.charts.throughput = new Chart(document.getElementById('node-page-throughput-chart'), {
            type: 'line',
            data: {
                datasets: [
                    { label: 'TX Mbps', borderColor: '#3498db', data: [], ...thinLine, borderWidth: 2 },
                    { label: 'RX Mbps', borderColor: '#e67e22', data: [], ...thinLine, borderWidth: 2 }
                ]
            },
            options: baseOptions
        });
    },

    async runLinkTest(source, target, type, button) {
        const original = button.textContent;
        button.disabled = true;
        button.textContent = type === 'ping' ? 'Pinging...' : 'Testing...';
        try {
            const response = await fetch(`/api/rf-stats/test/${encodeURIComponent(source)}/${encodeURIComponent(target)}?type=${type}`, { method: 'POST' });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || 'Test failed');
            this.addTestResult(type, source, target, result.result || result);
            this.showToast('success', type === 'ping' ? 'Ping Complete' : 'iPerf Complete', `${source} -> ${target}`);
            await this.load();
        } catch (error) {
            this.addTestResult(type, source, target, null, error.message);
            this.showToast('error', 'Test Failed', error.message);
        } finally {
            button.disabled = false;
            button.textContent = original;
        }
    },

    addTestResult(type, source, target, result, error = null) {
        this.testResults.unshift({
            type,
            source,
            target,
            result,
            error,
            timestamp: new Date()
        });
        this.testResults = this.testResults.slice(0, 20);
        this.renderTestResults();
    },

    renderTestResults() {
        const pingContainer = document.getElementById('node-ping-results');
        const iperfContainer = document.getElementById('node-iperf-results');
        if (!pingContainer || !iperfContainer) return;

        const pings = this.testResults.filter(r => r.type === 'ping');
        const iperfs = this.testResults.filter(r => r.type === 'iperf');

        if (!pings.length) {
            pingContainer.innerHTML = '<p class="log-empty">No ping tests run yet.</p>';
        } else {
            const rows = pings.map(item => {
                const r = item.result;
                const err = item.error;
                return `<tr class="${err ? 'test-row-failed' : ''}">
                    <td>${this.escapeHtml(item.source)}</td>
                    <td>${this.escapeHtml(item.target)}</td>
                    <td>${err ? '-' : this.formatNumber(r.min)}</td>
                    <td>${err ? '-' : this.formatNumber(r.avg)}</td>
                    <td>${err ? '-' : this.formatNumber(r.max)}</td>
                    <td>${err ? '-' : this.formatNumber(r.jitter)}</td>
                    <td>${err ? '-' : this.formatNumber(r.loss)}</td>
                    <td>${err ? `<span class="test-result-error">${this.escapeHtml(err)}</span>` : ''}</td>
                    <td>${this.formatDate(item.timestamp)}</td>
                </tr>`;
            }).join('');
            pingContainer.innerHTML = `
                <table class="info-table">
                    <thead><tr><th>Source</th><th>Target</th><th>Min (ms)</th><th>Avg (ms)</th><th>Max (ms)</th><th>Jitter (ms)</th><th>Loss (%)</th><th>Error</th><th>Time</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>`;
        }

        if (!iperfs.length) {
            iperfContainer.innerHTML = '<p class="log-empty">No iPerf3 tests run yet.</p>';
        } else {
            const rows = iperfs.map(item => {
                const r = item.result;
                const err = item.error;
                return `<tr class="${err ? 'test-row-failed' : ''}">
                    <td>${this.escapeHtml(item.source)}</td>
                    <td>${this.escapeHtml(item.target)}</td>
                    <td>${err ? '-' : this.formatNumber(r.tx_mbps)}</td>
                    <td>${err ? '-' : this.formatNumber(r.rx_mbps)}</td>
                    <td>${err ? `<span class="test-result-error">${this.escapeHtml(err)}</span>` : ''}</td>
                    <td>${this.formatDate(item.timestamp)}</td>
                </tr>`;
            }).join('');
            iperfContainer.innerHTML = `
                <table class="info-table">
                    <thead><tr><th>Source</th><th>Target</th><th>TX (Mbps)</th><th>RX (Mbps)</th><th>Error</th><th>Time</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>`;
        }
    },

    formatNumber(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) return 'N/A';
        return Number(value).toFixed(2).replace(/\\.00$/, '');
    },

    setStatus(text) {
        document.getElementById('node-page-status').textContent = text;
    },

    formatDate(value) {
        return value ? new Date(value).toLocaleString() : 'N/A';
    },

    escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },

    escapeAttr(value) {
        return this.escapeHtml(value).replace(/`/g, '&#96;');
    },

    getNodeStatusText(node) {
        if (node.is_link_only) {
            return node.observed_status === 'removed' ? 'Link-only / removed' : 'Link-only / not pollable';
        }
        return node.is_active === 1 ? 'Active' : 'Inactive';
    },

    showToast(type, title, message) {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `
            <div class="toast-content">
                <div class="toast-title">${this.escapeHtml(title)}</div>
                <div class="toast-message">${this.escapeHtml(message)}</div>
            </div>
            <button class="toast-close" type="button">&times;</button>
        `;
        toast.querySelector('button').addEventListener('click', () => toast.remove());
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 7000);
    }
};

document.addEventListener('DOMContentLoaded', () => NodePage.init());
