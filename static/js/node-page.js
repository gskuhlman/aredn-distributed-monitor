const NodePage = {
    nodeName: window.NODE_NAME,
    data: null,
    charts: {},
    testResults: [],

    async init() {
        document.getElementById('node-scan-now')?.addEventListener('click', () => this.scanNow());
        document.getElementById('node-incident-report-btn')?.addEventListener('click', () => this.generateIncidentReport());
        document.getElementById('node-troubleshoot-btn')?.addEventListener('click', () => this.runTroubleshoot());
        const traceSource = document.getElementById('trace-source');
        if (traceSource && !traceSource.value) traceSource.value = this.nodeName;
        document.getElementById('trace-run')?.addEventListener('click', () => {
            const src = (document.getElementById('trace-source')?.value || '').trim() || this.nodeName;
            const tgt = (document.getElementById('trace-target')?.value || '').trim();
            if (!tgt) { this.showToast('error', 'Traceroute', 'Enter a target node or IP'); return; }
            this.runTraceroute(src, tgt, document.getElementById('trace-run'));
        });
        document.getElementById('node-log-search')?.addEventListener('input', () => this.renderLog());
        document.getElementById('node-selected-toggle')?.addEventListener('change', (event) => {
            this.setSelected(event.target.checked);
        });
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
        const selectedToggle = document.getElementById('node-selected-toggle');
        if (selectedToggle) {
            selectedToggle.checked = Boolean(node.is_selected);
        }
        this.renderSummary();
        this.renderNodeHealth();
        this.renderLinks();
        this.renderServices();
        this.renderCharts();
        this.renderLinkHealth();
        this.renderIncidents();
        this.renderLog();
    },

    async setSelected(selected) {
        const toggle = document.getElementById('node-selected-toggle');
        if (toggle) toggle.disabled = true;
        try {
            const response = await fetch(`/api/nodes/selected/${encodeURIComponent(this.nodeName)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ selected })
            });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || 'Failed to update node');
            if (this.data?.node) {
                this.data.node.is_selected = result.is_selected;
            }
            if (toggle) toggle.checked = result.is_selected;
            this.showToast('success', result.is_selected ? 'Node Selected' : 'Node Excluded', this.nodeName);
        } catch (error) {
            if (toggle) toggle.checked = !selected;
            this.showToast('error', 'Selection Failed', error.message);
        } finally {
            if (toggle) toggle.disabled = false;
        }
    },

    renderSummary() {
        const node = this.data.node;
        document.getElementById('node-summary').innerHTML = `
            <table class="info-table">
                <tr><td>Name:</td><td>${this.escapeHtml(node.name)}</td></tr>
                <tr><td>IP:</td><td>${this.renderNodeIpLink(node.ip)}</td></tr>
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

    renderNodeHealth() {
        const container = document.getElementById('node-health-summary');
        if (!container) return;
        this.initCharts();
        const health = this.data.node_health || [];

        if (!health.length) {
            container.innerHTML = '<p class="log-empty">No node health samples yet. Uptime, load, and memory are captured each poll.</p>';
            if (this.charts.load) { this.charts.load.data.datasets.forEach(d => d.data = []); this.charts.load.update('none'); }
            if (this.charts.mem) { this.charts.mem.data.datasets[0].data = []; this.charts.mem.update('none'); }
            return;
        }

        // Latest reachable sample for current values.
        const latest = [...health].reverse().find(h => h.reachable) || health[health.length - 1];

        // Reboots = uptime going backwards; degraded/unreachable counts over window.
        let reboots = 0;
        let prevUptime = null;
        for (const h of health) {
            if (h.uptime_seconds != null) {
                if (prevUptime != null && h.uptime_seconds + 30 < prevUptime) reboots++;
                prevUptime = h.uptime_seconds;
            }
        }
        const degraded = health.filter(h => h.degraded).length;
        const unreachable = health.filter(h => !h.reachable).length;

        const memPct = (latest.mem_free != null && latest.mem_total)
            ? ` (${Math.round(100 * latest.mem_free / latest.mem_total)}% free)` : '';

        container.innerHTML = `
            <table class="info-table">
                <tr><td>Uptime:</td><td>${this.formatUptime(latest.uptime_seconds)}</td></tr>
                <tr><td>Load (1/5/15):</td><td>${this.fmt(latest.load1)} / ${this.fmt(latest.load5)} / ${this.fmt(latest.load15)}</td></tr>
                <tr><td>Free Memory:</td><td>${latest.mem_free != null ? latest.mem_free + ' KB' + memPct : 'N/A'}</td></tr>
                <tr><td>Channel Busy:</td><td>${latest.channel_busy != null ? this.fmt(latest.channel_busy) + '%' : 'N/A'}</td></tr>
                <tr><td>Reboots (window):</td><td>${reboots > 0 ? `<span class="health-val health-poor">${reboots}</span>` : '0'}</td></tr>
                <tr><td>Degraded polls:</td><td>${degraded > 0 ? `<span class="health-val health-marginal">${degraded}</span>` : '0'}</td></tr>
                <tr><td>Scanner unreachable polls:</td><td>${unreachable > 0 ? `<span class="health-val health-poor">${unreachable}</span>` : '0'}</td></tr>
                <tr><td>Samples:</td><td>${health.length}</td></tr>
            </table>`;

        const load1 = health.filter(h => h.load1 != null).map(h => ({ x: new Date(h.timestamp), y: h.load1 }));
        const load5 = health.filter(h => h.load5 != null).map(h => ({ x: new Date(h.timestamp), y: h.load5 }));
        const load15 = health.filter(h => h.load15 != null).map(h => ({ x: new Date(h.timestamp), y: h.load15 }));
        const mem = health.filter(h => h.mem_free != null).map(h => ({ x: new Date(h.timestamp), y: h.mem_free }));

        if (this.charts.load) {
            this.charts.load.data.datasets[0].data = load1;
            this.charts.load.data.datasets[1].data = load5;
            this.charts.load.data.datasets[2].data = load15;
            this.charts.load.update('none');
        }
        if (this.charts.mem) {
            this.charts.mem.data.datasets[0].data = mem;
            this.charts.mem.update('none');
        }
    },

    renderIncidents() {
        const container = document.getElementById('node-incident-probes');
        if (!container) return;
        const samples = this.data.incident_samples || [];
        if (!samples.length) {
            container.innerHTML = '<p class="log-empty">No incident probes recorded. These run automatically when a watched link drops, is LQM-blocked, or goes marginal.</p>';
            return;
        }

        // Show most recent first, capped for readability.
        const rows = [...samples].reverse().slice(0, 60).map(s => {
            const lossClass = s.ping_loss == null ? 'unknown' : (s.ping_loss >= 50 ? 'poor' : s.ping_loss > 0 ? 'marginal' : 'good');
            return `<tr>
                <td>${this.formatDate(s.timestamp)}</td>
                <td>${this.escapeHtml(s.source_node)} &rarr; ${this.escapeHtml(s.target_node)}</td>
                <td>${s.ping_avg != null ? this.fmt(s.ping_avg) + ' ms' : '-'}</td>
                <td><span class="health-val health-${lossClass}">${s.ping_loss != null ? this.fmt(s.ping_loss) + '%' : '-'}</span></td>
                <td>${s.jitter != null ? this.fmt(s.jitter) + ' ms' : '-'}</td>
            </tr>`;
        }).join('');

        container.innerHTML = `
            <div class="table-scroll-wrapper">
                <table class="info-table">
                    <thead><tr><th>Time</th><th>Direction</th><th>Avg</th><th>Loss</th><th>Jitter</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    },

    async generateIncidentReport() {
        const btn = document.getElementById('node-incident-report-btn');
        const container = document.getElementById('node-incident-report');
        if (!container) return;
        const original = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = 'Generating...'; }
        container.innerHTML = '<p class="log-empty">Analyzing collected data...</p>';
        try {
            const resp = await fetch(`/api/reports/incident/${encodeURIComponent(this.nodeName)}?hours=24`);
            if (!resp.ok) throw new Error('Report failed');
            const report = await resp.json();

            const sevClass = { high: 'poor', medium: 'marginal', low: 'unknown', info: 'good' };
            const findings = (report.findings || []).map(f => {
                const rec = f.recommendation
                    ? `<div class="ts-rec"><strong>Suggested:</strong> ${this.escapeHtml(f.recommendation)}</div>` : '';
                return `<li><span class="health-val health-${sevClass[f.severity] || 'unknown'}">${this.escapeHtml(f.severity)}</span>
                 <strong>${this.escapeHtml(f.cause)}</strong> &mdash; ${this.escapeHtml(f.evidence)}${rec}</li>`;
            }).join('');

            const narrative = report.narrative
                ? `<div class="incident-narrative"><h4>AI Summary</h4><p>${this.escapeHtml(report.narrative)}</p></div>`
                : `<p class="health-footnote">AI summary ${report.ai_enabled ? 'unavailable (check API key / SDK)' : 'disabled'}; showing deterministic findings only.</p>`;

            container.innerHTML = `
                ${narrative}
                <h4>Problems detected</h4>
                <ul class="incident-findings">${findings || '<li>No problems detected.</li>'}</ul>
                <details><summary>Full report (markdown)</summary><pre class="incident-md">${this.escapeHtml(report.markdown || '')}</pre></details>
            `;
        } catch (error) {
            container.innerHTML = `<p class="error">${this.escapeHtml(error.message)}</p>`;
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = original; }
        }
    },

    async runTroubleshoot() {
        const btn = document.getElementById('node-troubleshoot-btn');
        const container = document.getElementById('node-troubleshoot');
        if (!container) return;
        const original = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = 'Troubleshooting...'; }
        container.innerHTML = '<p class="loading">Running live probes (ping, traceroute, neighbor relay)…</p>';
        try {
            const resp = await fetch(`/api/troubleshoot/${encodeURIComponent(this.nodeName)}`, { method: 'POST' });
            const data = await resp.json();
            if (!resp.ok || !data.success) throw new Error(data.error || 'Troubleshoot failed');
            this.renderTroubleshoot(data.result);
        } catch (error) {
            container.innerHTML = `<p class="error">${this.escapeHtml(error.message)}</p>`;
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = original; }
        }
    },

    renderTroubleshoot(result) {
        const container = document.getElementById('node-troubleshoot');
        if (!container) return;
        const sevClass = { high: 'poor', medium: 'marginal', low: 'unknown', info: 'good' };
        const steps = (result.steps || []).map(s => {
            const checks = (s.checks || []).map(c =>
                `<li><strong>${this.escapeHtml(c.action)}:</strong> ${this.escapeHtml(c.result)}</li>`
            ).join('');
            const checksHtml = checks ? `<ul class="ts-checks">${checks}</ul>` : '';
            const rec = s.recommendation
                ? `<p class="ts-rec"><strong>Next step:</strong> ${this.escapeHtml(s.recommendation)}</p>` : '';
            return `
                <div class="ts-step">
                    <div class="ts-step-head">
                        <span class="health-val health-${sevClass[s.severity] || 'unknown'}">${this.escapeHtml(s.severity)}</span>
                        <strong>${this.escapeHtml(s.problem)}</strong>
                    </div>
                    <p class="ts-detail">${this.escapeHtml(s.detail || '')}</p>
                    ${checksHtml}
                    ${rec}
                </div>`;
        }).join('');
        const reach = result.reachable_from_collector ? 'reachable from collector' : 'NOT reachable from collector';
        container.innerHTML = `
            <p class="trace-head">${this.escapeHtml(result.node)} &mdash; ${result.problem_count} problem(s); ${reach}</p>
            ${steps || '<p class="log-empty">No problems to troubleshoot.</p>'}`;
    },

    fmt(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) return 'N/A';
        return String(Number(Number(value).toFixed(2)));
    },

    formatUptime(seconds) {
        if (seconds === null || seconds === undefined) return 'N/A';
        const d = Math.floor(seconds / 86400);
        const h = Math.floor((seconds % 86400) / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return `${d}d ${h}h ${m}m`;
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
                    <button class="btn btn-small btn-secondary" data-action="traceroute" data-source="${this.escapeAttr(this.data.node.name)}" data-target="${this.escapeAttr(other)}">Trace</button>
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
            button.addEventListener('click', () => {
                const action = button.dataset.action;
                if (action === 'traceroute') {
                    this.runTraceroute(button.dataset.source, button.dataset.target, button);
                } else {
                    this.runLinkTest(button.dataset.source, button.dataset.target, action, button);
                }
            });
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
            const block = h.lqm_block || {};
            const snrAsym = h.snr_asymmetry || {};

            return `<tr>
                <td><a href="/nodes/${encodeURIComponent(h.peer)}">${this.escapeHtml(h.peer)}</a></td>
                <td>${this.escapeHtml(h.link_type)}</td>
                <td>${this.reachCell((this.data.peer_reach || {})[h.peer])}</td>
                <td><span class="health-badge health-grade-${grade.rating || 'unknown'}">${this.escapeHtml(grade.grade || '?')}</span></td>
                <td>${this.blockCell(block)}</td>
                <td>${this.flapCell(flapping)}</td>
                <td>${this.scannerCell(flapping)}</td>
                <td>${this.snrAsymCell(snrAsym)}</td>
                <td>${this.healthCell(ping.latency_avg, 'ms', ping.latency_rating)}</td>
                <td>${this.healthCell(ping.jitter_avg, 'ms', ping.jitter_rating)}</td>
                <td>${this.healthCell(ping.loss_avg, '%', ping.loss_rating)}</td>
                <td>${this.healthCell(mos.score, mos.label ? ' ' + mos.label : '', mos.rating)}</td>
                <td>${this.healthCell(snr.snr, ' dB', snr.rating)}</td>
                <td>${this.healthCell(noise.noise_floor, ' dBm', noise.noise_rating)}${noise.interference_likely ? ' <span class="health-flag health-poor" title="Elevated noise floor suggests interference">!</span>' : ''}</td>
                <td>${this.healthCell(stability.quality_stddev, '%', stability.rating)}</td>
                <td>${this.trendCell(stability.quality_trend)}</td>
                <td>${this.trendCell(stability.snr_trend)}</td>
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
                            <th>Reachability</th>
                            <th>Grade</th>
                            <th>LQM Blocked</th>
                            <th>Node-reported flaps</th>
                            <th>Scanner-to-node</th>
                            <th>SNR Asym</th>
                            <th>Latency</th>
                            <th>Jitter</th>
                            <th>Loss</th>
                            <th>VoIP MOS</th>
                            <th>SNR</th>
                            <th>Noise</th>
                            <th>RF Stability</th>
                            <th>Quality Trend</th>
                            <th>SNR Trend</th>
                            <th>TX/RX Ratio</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <div class="health-footnote">
                <p><strong>LQM Blocked</strong> shows whether AREDN's Link Quality Manager is currently blocking the link and why (signal, distance, dtd, dup, quality, user). A blocked link is the most direct explanation for flapping &mdash; the node itself is tearing the link down.</p>
                <p><strong>SNR Asym</strong> is the gap between the SNR you hear from the peer and the SNR the peer hears from you (rev_snr). A large gap ("you hear them, they can't hear you") commonly drives one-directional flapping and is invisible in a single SNR figure.</p>
                <p><strong>Node-reported flaps</strong> counts peer link drops that a node we successfully polled reported about itself (from its LQM data) in the window. This is the real flap signal. The chip classifies the heuristic cause:</p>
                <ul>
                    <li><strong>Node outage</strong> &mdash; link drops coincided with the source or peer node going offline (reboot, power loss, unreachable). DTD and Xlink (wired) drops are always attributed to the node since physical cables do not independently flap.</li>
                    <li><strong>RF instability</strong> &mdash; the RF link dropped with no corresponding node outage, indicating actual wireless signal issues such as interference, marginal SNR, or environmental factors.</li>
                    <li><strong>Mixed</strong> (e.g. "3 link / 2 node") &mdash; some drops were caused by node outages and others by independent link instability.</li>
                </ul>
                <p><strong>Scanner-to-node</strong> counts times <em>this scanner</em> could not reach the node, so a link looked down only because we lost our path to it &mdash; not because the peer link flapped. A high number here with low node-reported flaps means the problem is the collector's route to this node, not the node's RF links. (See also "Scanner unreachable polls" under Node Health.)</p>
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
        if (!flapping) return '<span class="health-val health-unknown">-</span>';
        // Headline = node-reported peer downs (de-conflated). Fall back to the
        // event-based flap_count only when the pair summary is unavailable.
        const count = (flapping.logged_downs !== undefined && flapping.logged_downs !== null)
            ? flapping.logged_downs : flapping.flap_count;
        if (count === undefined || count === null) return '<span class="health-val health-unknown">-</span>';
        const reason = flapping.top_block_reason
            ? ` <span class="flap-cause flap-link" title="Dominant LQM block reason">${this.escapeHtml(flapping.top_block_reason)}</span>`
            : '';
        const cause = flapping.cause_label
            ? ` <span class="flap-cause flap-${flapping.cause || 'unknown'}" title="Cause heuristic from connectivity events">${this.escapeHtml(flapping.cause_label)}</span>`
            : '';
        if (!count && !reason) return `<span class="health-val health-good">0</span>${cause}`;
        const rating = count >= 8 ? 'poor' : count >= 3 ? 'marginal' : (flapping.rating || 'good');
        const title = `${count} node-reported peer down(s) in window`;
        return `<span class="health-val health-${rating}" title="${this.escapeAttr(title)}">${count}${cause}${reason}</span>`;
    },

    scannerCell(flapping) {
        if (!flapping) return '<span class="health-val health-unknown">-</span>';
        // Times this scanner could not reach the node — NOT a peer flap.
        const lost = (flapping.scanner_unreachable || 0) + (flapping.inferred_downs || 0);
        if (!lost) return '<span class="health-val health-good">0</span>';
        const title = `${flapping.scanner_unreachable || 0} scanner-unreachable + ${flapping.inferred_downs || 0} inferred down — the scanner lost its path to this node (not a peer flap)`;
        return `<span class="health-val health-marginal" title="${this.escapeAttr(title)}">${lost}</span>`;
    },

    reachCell(reach) {
        if (!reach) return '<span class="health-val health-unknown">-</span>';
        let rating = 'unknown', label = reach.reach_status || '?';
        if (reach.reach_status === 'polled') { rating = 'good'; label = 'polled'; }
        else if (reach.reach_status === 'via_mesh') {
            rating = reach.mesh_probe_status === 'confirmed' ? 'good' : 'marginal';
            label = reach.mesh_probe_status === 'confirmed' ? 'via mesh ✓' : 'via mesh';
        }
        else if (reach.reach_status === 'down') {
            rating = 'poor';
            label = reach.mesh_probe_status === 'failed' ? 'down (no route)' : 'down';
        }
        else if (reach.reach_status === 'link_only') { rating = 'unknown'; label = 'link-only'; }
        let title = '';
        if (reach.mesh_prober) {
            const verb = reach.mesh_probe_status === 'confirmed' ? 'can reach it'
                : reach.mesh_probe_status === 'failed' ? 'hears RF but cannot route to it' : '';
            if (verb) title = `mesh probe: ${reach.mesh_prober} ${verb}`;
        }
        return `<span class="health-val health-${rating}" title="${this.escapeAttr(title)}">${this.escapeHtml(label)}</span>`;
    },

    blockCell(block) {
        if (!block || block.blocked === null || block.blocked === undefined) {
            return '<span class="health-val health-unknown">-</span>';
        }
        if (!block.blocked) return '<span class="health-val health-good">No</span>';
        return `<span class="health-val health-poor" title="LQM is blocking this link">BLOCKED: ${this.escapeHtml(block.reason || 'unspecified')}</span>`;
    },

    snrAsymCell(asym) {
        if (!asym || asym.delta === null || asym.delta === undefined) {
            return '<span class="health-val health-unknown">-</span>';
        }
        const title = asym.details || '';
        return `<span class="health-val health-${asym.rating || 'unknown'}" title="${this.escapeAttr(title)}">${this.escapeHtml(String(asym.delta))} dB</span>`;
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

        const loadCanvas = document.getElementById('node-page-load-chart');
        if (loadCanvas) {
            this.charts.load = new Chart(loadCanvas, {
                type: 'line',
                data: {
                    datasets: [
                        { label: '1 min', borderColor: '#e74c3c', data: [], ...thinLine, borderWidth: 2 },
                        { label: '5 min', borderColor: '#f39c12', data: [], ...thinLine },
                        { label: '15 min', borderColor: '#27ae60', data: [], ...thinLine }
                    ]
                },
                options: baseOptions
            });
        }

        const memCanvas = document.getElementById('node-page-mem-chart');
        if (memCanvas) {
            this.charts.mem = new Chart(memCanvas, {
                type: 'line',
                data: { datasets: [{ label: 'Free KB', borderColor: '#16a085', data: [], ...thinLine, borderWidth: 2 }] },
                options: baseOptions
            });
        }
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

    async runTraceroute(source, target, button) {
        const original = button ? button.textContent : '';
        if (button) { button.disabled = true; button.textContent = 'Tracing...'; }
        const container = document.getElementById('node-traceroute-results');
        if (container) {
            container.innerHTML = `<p class="loading">Tracing ${this.escapeHtml(source)} &rarr; ${this.escapeHtml(target)}&hellip;</p>`;
        }
        try {
            const response = await fetch(
                `/api/rf-stats/test/${encodeURIComponent(source)}/${encodeURIComponent(target)}?type=traceroute`,
                { method: 'POST' }
            );
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || 'Traceroute failed');
            this.renderTraceroute(source, target, data.result);
            this.showToast('success', 'Traceroute Complete', `${source} → ${target}`);
        } catch (error) {
            if (container) container.innerHTML = `<p class="error">${this.escapeHtml(error.message)}</p>`;
            this.showToast('error', 'Traceroute Failed', error.message);
        } finally {
            if (button) { button.disabled = false; button.textContent = original; }
        }
    },

    renderTraceroute(source, target, result) {
        const container = document.getElementById('node-traceroute-results');
        if (!container) return;
        const hops = (result && result.hops) || [];
        const rows = hops.map(h => `
            <tr>
                <td>${h.hop}</td>
                <td>${h.timeout ? '<span class="health-val health-poor">* * *</span>' : this.escapeHtml(h.host || h.ip || '?')}</td>
                <td>${this.escapeHtml(h.ip || '')}</td>
                <td>${h.ms !== null && h.ms !== undefined ? this.fmt(h.ms) + ' ms' : '-'}</td>
            </tr>`).join('');
        container.innerHTML = `
            <p class="trace-head">From <strong>${this.escapeHtml(result.source || source)}</strong>
               to <strong>${this.escapeHtml(result.target || target)}</strong> &mdash; ${hops.length} hop(s)</p>
            <table class="info-table">
                <thead><tr><th>#</th><th>Host</th><th>IP</th><th>RTT</th></tr></thead>
                <tbody>${rows || '<tr><td colspan="4" class="log-empty">No hops returned</td></tr>'}</tbody>
            </table>`;
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

    renderNodeIpLink(ip) {
        if (!ip) return 'N/A';
        const address = String(ip).trim();
        return `<a href="${this.escapeAttr(`http://${address}`)}" target="_blank" rel="noopener noreferrer">${this.escapeHtml(address)}</a>`;
    },

    getNodeStatusText(node) {
        const reach = this.data && this.data.reach;
        if (node.is_link_only || (reach && reach.reach_status === 'link_only')) {
            return node.observed_status === 'removed' ? 'Link-only / removed' : 'Link-only / not pollable';
        }
        if (reach) {
            const prober = reach.mesh_prober;
            if (reach.reach_status === 'polled') return 'Polled (scanner reached it)';
            if (reach.reach_status === 'via_mesh') {
                return reach.mesh_probe_status === 'confirmed'
                    ? `Reachable via mesh — confirmed by ${prober}`
                    : "Reachable via mesh (a neighbor reports it; scanner can't poll it)";
            }
            if (reach.reach_status === 'down') {
                return reach.mesh_probe_status === 'failed'
                    ? `Down — ${prober} hears it on RF but can't route to it`
                    : 'Down / unseen (no reachable node reports a live link)';
            }
        }
        // Fallback before reachability data is available.
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
