/**
 * AREDN Network Monitor - RF Statistics Module
 * Uses Chart.js for time-series visualization of RF link metrics
 */

// RF Stats Module
const RFStats = {
    selectedLink: null,
    charts: {},
    timeRangeHours: 24,
    rfLinks: [],
    filteredLinks: [],
    networkData: { nodes: [], edges: [] },
    initialized: false,

    /**
     * Initialize the RF Stats module
     */
    init() {
        if (this.initialized) return;

        this.initCharts();
        this.initEventListeners();
        this.initialized = true;

        console.log('RF Stats module initialized');
    },

    /**
     * Initialize Chart.js chart instances
     */
    initCharts() {
        const commonOptions = {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'index'
            },
            scales: {
                x: {
                    type: 'time',
                    time: {
                        unit: 'minute',
                        displayFormats: {
                            minute: 'HH:mm',
                            hour: 'HH:mm'
                        }
                    },
                    title: {
                        display: false
                    }
                },
                y: {
                    beginAtZero: true
                }
            },
            plugins: {
                legend: {
                    display: true,
                    position: 'top'
                }
            }
        };

        // Quality chart (0-100%)
        const qualityCtx = document.getElementById('quality-chart');
        if (qualityCtx) {
            this.charts.quality = new Chart(qualityCtx, {
                type: 'line',
                data: {
                    datasets: [{
                        label: 'Quality %',
                        borderColor: '#27ae60',
                        backgroundColor: 'rgba(39, 174, 96, 0.1)',
                        fill: true,
                        tension: 0.3,
                        data: []
                    }]
                },
                options: {
                    ...commonOptions,
                    scales: {
                        ...commonOptions.scales,
                        y: { beginAtZero: true, max: 100 }
                    }
                }
            });
        }

        // SNR chart
        const snrCtx = document.getElementById('snr-chart');
        if (snrCtx) {
            this.charts.snr = new Chart(snrCtx, {
                type: 'line',
                data: {
                    datasets: [{
                        label: 'SNR (dB)',
                        borderColor: '#3498db',
                        backgroundColor: 'rgba(52, 152, 219, 0.1)',
                        fill: true,
                        tension: 0.3,
                        data: []
                    }]
                },
                options: commonOptions
            });
        }

        // Ping chart (min/avg/max)
        const pingCtx = document.getElementById('ping-chart');
        if (pingCtx) {
            this.charts.ping = new Chart(pingCtx, {
                type: 'line',
                data: {
                    datasets: [
                        {
                            label: 'Min',
                            borderColor: '#27ae60',
                            backgroundColor: 'transparent',
                            tension: 0.3,
                            data: []
                        },
                        {
                            label: 'Avg',
                            borderColor: '#f39c12',
                            backgroundColor: 'rgba(243, 156, 18, 0.1)',
                            fill: true,
                            tension: 0.3,
                            data: []
                        },
                        {
                            label: 'Max',
                            borderColor: '#e74c3c',
                            backgroundColor: 'transparent',
                            tension: 0.3,
                            data: []
                        }
                    ]
                },
                options: commonOptions
            });
        }

        // Throughput chart (TX/RX)
        const throughputCtx = document.getElementById('throughput-chart');
        if (throughputCtx) {
            this.charts.throughput = new Chart(throughputCtx, {
                type: 'line',
                data: {
                    datasets: [
                        {
                            label: 'TX (Mbps)',
                            borderColor: '#3498db',
                            backgroundColor: 'rgba(52, 152, 219, 0.1)',
                            fill: true,
                            tension: 0.3,
                            data: []
                        },
                        {
                            label: 'RX (Mbps)',
                            borderColor: '#9b59b6',
                            backgroundColor: 'rgba(155, 89, 182, 0.1)',
                            fill: true,
                            tension: 0.3,
                            data: []
                        }
                    ]
                },
                options: commonOptions
            });
        }
    },

    /**
     * Initialize event listeners
     */
    initEventListeners() {
        // Time range buttons
        document.querySelectorAll('.time-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                e.target.classList.add('active');
                this.timeRangeHours = parseInt(e.target.dataset.hours);

                if (this.selectedLink) {
                    this.loadLinkHistory(this.selectedLink.source, this.selectedLink.target);
                }
            });
        });

        // Ping All button
        const pingAllBtn = document.getElementById('rf-ping-all');
        if (pingAllBtn) {
            pingAllBtn.addEventListener('click', () => this.pingAll());
        }

        // Clear Ping Times button
        const clearPingBtn = document.getElementById('rf-clear-ping');
        if (clearPingBtn) {
            clearPingBtn.addEventListener('click', () => this.clearPingTimes());
        }

        // Back button
        const backBtn = document.getElementById('rf-back-btn');
        if (backBtn) {
            backBtn.addEventListener('click', () => {
                this.deselectLink();
            });
        }

        // Chart view test buttons
        const chartPingBtn = document.getElementById('chart-ping-btn');
        if (chartPingBtn) {
            chartPingBtn.addEventListener('click', (e) => {
                if (this.selectedLink) {
                    this.triggerTest(this.selectedLink.source, this.selectedLink.target, 'ping', e.target);
                }
            });
        }

        const chartIperfBtn = document.getElementById('chart-iperf-btn');
        if (chartIperfBtn) {
            chartIperfBtn.addEventListener('click', (e) => {
                if (this.selectedLink) {
                    this.triggerTest(this.selectedLink.source, this.selectedLink.target, 'iperf', e.target);
                }
            });
        }

        // Filter / sort controls (overview table)
        const search = document.getElementById('rf-search');
        const statusFilter = document.getElementById('rf-status-filter');
        const sortSelect = document.getElementById('rf-sort');
        if (search) {
            search.addEventListener('input', () => this.filterLinks());
        }
        if (statusFilter) {
            statusFilter.addEventListener('change', () => this.filterLinks());
        }
        if (sortSelect) {
            sortSelect.addEventListener('change', () => this.filterLinks());
        }
    },

    /**
     * Load RF links from server
     */
    async loadRFLinks() {
        try {
            const [linksResp, netResp] = await Promise.all([
                fetch('/api/rf-stats/links'),
                fetch('/api/network')
            ]);
            this.rfLinks = await linksResp.json();
            this.networkData = netResp.ok ? await netResp.json() : { nodes: [], edges: [] };

            this.filterLinks();

        } catch (error) {
            console.error('Error loading RF links:', error);
        }
    },

    // Selected node names, gathered from the network graph (mirrors NodesModule).
    getSelectedNodeNames() {
        const selected = new Set();
        for (const node of this.networkData.nodes || []) {
            if (node.is_selected && node.id) {
                selected.add(node.id);
            }
        }
        return selected;
    },

    getSelectedConnectedNodeNames() {
        const selected = this.getSelectedNodeNames();
        const visible = new Set(selected);

        for (const edge of this.networkData.edges || []) {
            const linkType = String(edge.link_type || '').toUpperCase();
            if (linkType !== 'RF' && linkType !== 'DTD') continue;
            if (selected.has(edge.from) || selected.has(edge.to)) {
                visible.add(edge.from);
                visible.add(edge.to);
            }
        }

        return visible;
    },

    /**
     * Apply the search / status / sort filters to the loaded RF links and
     * repaint the overview table. Mirrors the nodes-page filter logic.
     */
    filterLinks() {
        const search = (document.getElementById('rf-search')?.value || '').toLowerCase();
        const status = document.getElementById('rf-status-filter')?.value || 'all';
        const sortBy = document.getElementById('rf-sort')?.value || 'alpha';

        const selectedNames = status === 'selected' ? this.getSelectedNodeNames() : null;
        const selectedConnectedNames = status === 'selected-connected' ? this.getSelectedConnectedNodeNames() : null;

        this.filteredLinks = (this.rfLinks || []).filter(link => {
            const source = (link.source_node || '').toLowerCase();
            const target = (link.target_node || '').toLowerCase();
            const matchesSearch = !search ||
                source.includes(search) || target.includes(search);

            let matchesStatus = true;
            if (status === 'selected') {
                matchesStatus = selectedNames.has(link.source_node) || selectedNames.has(link.target_node);
            } else if (status === 'selected-connected') {
                matchesStatus = selectedConnectedNames.has(link.source_node) || selectedConnectedNames.has(link.target_node);
            }

            return matchesSearch && matchesStatus;
        });

        this.filteredLinks.sort((a, b) => {
            if (sortBy === 'alpha') {
                const aKey = `${a.source_node || ''}-${a.target_node || ''}`;
                const bKey = `${b.source_node || ''}-${b.target_node || ''}`;
                return aKey.localeCompare(bKey);
            } else if (sortBy === 'last-seen') {
                const aTime = a.last_seen ? new Date(a.last_seen).getTime() : 0;
                const bTime = b.last_seen ? new Date(b.last_seen).getTime() : 0;
                return bTime - aTime;
            } else if (sortBy === 'quality') {
                return (a.quality || 0) - (b.quality || 0);
            } else if (sortBy === 'snr') {
                return (a.snr || -999) - (b.snr || -999);
            } else if (sortBy === 'ping') {
                const aPing = a.ping_avg == null ? Infinity : a.ping_avg;
                const bPing = b.ping_avg == null ? Infinity : b.ping_avg;
                return aPing - bPing;
            }
            return 0;
        });

        this.renderOverviewTable(this.filteredLinks);

        const countEl = document.getElementById('rf-stats-link-count');
        if (countEl) {
            const total = (this.rfLinks || []).length;
            const shown = this.filteredLinks.length;
            countEl.textContent = (shown === total) ? `${shown}` : `${shown}/${total}`;
        }
    },

    /**
     * Render the overview table
     */
    renderOverviewTable(links) {
        const tbody = document.getElementById('rf-overview-body');
        if (!tbody) return;

        if (links.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6">No RF links found</td></tr>';
            return;
        }

        let html = '';
        for (const link of links) {
            const qualityClass = this.getQualityClass(link.quality);
            const pingDisplay = link.ping_avg != null
                ? `${link.ping_avg.toFixed(1)} ms`
                : '--';
            const pingTime = link.last_ping_time
                ? this.formatRelTime(link.last_ping_time)
                : '';
            const throughputDisplay = (link.throughput_tx !== null && link.throughput_tx !== undefined)
                ? `${this.formatThroughput(link.throughput_tx)} / ${this.formatThroughput(link.throughput_rx)}`
                : '--';
            const throughputTime = link.last_throughput_time
                ? this.formatRelTime(link.last_throughput_time)
                : '';

            html += `
                <tr class="rf-link-row"
                    data-source="${link.source_node}"
                    data-target="${link.target_node}"
                    onclick="RFStats.selectLink('${link.source_node}', '${link.target_node}')">
                    <td><strong>${link.source_node}</strong> &harr; <strong>${link.target_node}</strong></td>
                    <td class="${qualityClass}">${link.quality || 0}%</td>
                    <td>${link.snr || 'N/A'}</td>
                    <td>${pingDisplay}${pingTime ? ` <small class="rf-last-time">(${pingTime})</small>` : ''}</td>
                    <td>${throughputDisplay}${throughputTime ? ` <small class="rf-last-time">(${throughputTime})</small>` : ''}</td>
                    <td>
                        <button class="btn btn-small btn-secondary"
                                onclick="event.stopPropagation(); RFStats.triggerTest('${link.source_node}', '${link.target_node}', 'ping', this)">
                            Ping
                        </button>
                        <button class="btn btn-small btn-secondary"
                                onclick="event.stopPropagation(); RFStats.triggerTest('${link.source_node}', '${link.target_node}', 'iperf', this)">
                            iPerf
                        </button>
                    </td>
                </tr>
            `;
        }

        tbody.innerHTML = html;
    },

    /**
     * Render a relative-time label like "2m ago" / "3h ago" / "just now".
     */
    formatRelTime(ts) {
        const t = new Date(ts).getTime();
        if (!t) return '';
        const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
        if (sec < 60) return 'just now';
        const min = Math.floor(sec / 60);
        if (min < 60) return `${min}m ago`;
        const hr = Math.floor(min / 60);
        if (hr < 24) return `${hr}h ago`;
        const d = Math.floor(hr / 24);
        return `${d}d ago`;
    },

    /**
     * Format an iperf throughput value (stored in Mbps) with a unit label.
     * Sub-1 Mbps values are shown in Kbps so slow links are still legible.
     */
    formatThroughput(mbps) {
        if (mbps === null || mbps === undefined) return '--';
        if (mbps >= 1) return `${mbps.toFixed(1)} Mbps`;
        return `${(mbps * 1000).toFixed(0)} Kbps`;
    },

    /**
     * Ping the currently filtered RF links from the collector and repaint the
     * overview table. Previous ping/throughput results for those links are
     * cleared first so the table shows only the fresh values.
     */
    async pingAll() {
        const btn = document.getElementById('rf-ping-all');
        if (btn && btn.disabled) return;  // a sweep is already running
        const status = document.getElementById('rf-ping-status');
        const original = btn ? btn.textContent : '';
        // Ping only the links currently passing the filter/sort bar.
        const links = (this.filteredLinks || this.rfLinks || []).map(l => [l.source_node, l.target_node]);
        const n = links.length;
        if (btn) { btn.disabled = true; btn.textContent = 'Pinging...'; }
        if (status) status.textContent = `Pinging ${n} link(s)... `;
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 90000);
        try {
            const resp = await fetch('/api/rf-stats/ping-all', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ links, clear: true }),
                signal: controller.signal
            });
            const data = await resp.json();
            const results = data.results || [];
            const reached = results.filter(r => r.reachable).length;
            // The server recorded the pings to link history; reload to repaint.
            await this.loadRFLinks();
            if (status) status.textContent = `Pinged ${results.length}; ${reached} reachable. `;
        } catch (e) {
            if (status) status.textContent = (e.name === 'AbortError')
                ? 'Ping all timed out (still running on the server?). '
                : 'Ping all failed. ';
        } finally {
            clearTimeout(timer);
            if (btn) { btn.disabled = false; btn.textContent = original; }
        }
    },

    /**
     * Null out stored ping and throughput results so the overview stops showing
     * stale values, then repaint. Clears only the currently filtered links.
     */
    async clearPingTimes() {
        const btn = document.getElementById('rf-clear-ping');
        if (btn && btn.disabled) return;
        const status = document.getElementById('rf-ping-status');
        const original = btn ? btn.textContent : '';
        const links = (this.filteredLinks || this.rfLinks || []).map(l => [l.source_node, l.target_node]);
        if (btn) { btn.disabled = true; btn.textContent = 'Clearing...'; }
        try {
            await fetch('/api/rf-stats/clear-ping', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ links })
            });
            await this.loadRFLinks();
            if (status) status.textContent = 'Cleared ping/throughput. ';
        } catch (e) {
            if (status) status.textContent = 'Clear failed. ';
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = original; }
        }
    },

    /**
     * Select a link and show its charts
     */
    async selectLink(source, target) {
        this.selectedLink = { source, target };

        // Show back button
        const backBtn = document.getElementById('rf-back-btn');
        if (backBtn) {
            backBtn.classList.remove('hidden');
        }

        // Update selected link info
        const linkInfo = document.getElementById('selected-link-info');
        if (linkInfo) {
            linkInfo.classList.remove('hidden');
            document.getElementById('selected-link-title').textContent = `${source} ↔ ${target}`;
        }

        // Show charts, hide overview table
        document.getElementById('rf-charts-grid').classList.remove('hidden');
        document.getElementById('rf-overview-table').classList.add('hidden');
        const rfFilters = document.getElementById('rf-filters');
        if (rfFilters) rfFilters.classList.add('hidden');

        // Load history data
        await this.loadLinkHistory(source, target);
    },

    /**
     * Deselect link and show overview
     */
    deselectLink() {
        this.selectedLink = null;

        // Hide back button
        const backBtn = document.getElementById('rf-back-btn');
        if (backBtn) {
            backBtn.classList.add('hidden');
        }

        // Hide link info
        const linkInfo = document.getElementById('selected-link-info');
        if (linkInfo) {
            linkInfo.classList.add('hidden');
        }

        // Hide charts, show overview table
        document.getElementById('rf-charts-grid').classList.add('hidden');
        document.getElementById('rf-overview-table').classList.remove('hidden');
        const rfFilters = document.getElementById('rf-filters');
        if (rfFilters) rfFilters.classList.remove('hidden');
    },

    /**
     * Load historical data for a link
     */
    async loadLinkHistory(source, target) {
        try {
            const response = await fetch(
                `/api/rf-stats/history/${encodeURIComponent(source)}/${encodeURIComponent(target)}?hours=${this.timeRangeHours}`
            );
            const history = await response.json();

            this.updateCharts(history);

        } catch (error) {
            console.error('Error loading link history:', error);
        }
    },

    /**
     * Update all charts with new data
     */
    updateCharts(history) {
        // Quality chart
        if (this.charts.quality) {
            const qualityData = history
                .filter(h => h.quality !== null)
                .map(h => ({ x: new Date(h.timestamp), y: h.quality }));

            this.charts.quality.data.datasets[0].data = qualityData;
            this.charts.quality.update('none');
        }

        // SNR chart
        if (this.charts.snr) {
            const snrData = history
                .filter(h => h.snr !== null)
                .map(h => ({ x: new Date(h.timestamp), y: h.snr }));

            this.charts.snr.data.datasets[0].data = snrData;
            this.charts.snr.update('none');
        }

        // Ping chart
        if (this.charts.ping) {
            const pingMin = history
                .filter(h => h.ping_min !== null)
                .map(h => ({ x: new Date(h.timestamp), y: h.ping_min }));
            const pingAvg = history
                .filter(h => h.ping_avg !== null)
                .map(h => ({ x: new Date(h.timestamp), y: h.ping_avg }));
            const pingMax = history
                .filter(h => h.ping_max !== null)
                .map(h => ({ x: new Date(h.timestamp), y: h.ping_max }));

            this.charts.ping.data.datasets[0].data = pingMin;
            this.charts.ping.data.datasets[1].data = pingAvg;
            this.charts.ping.data.datasets[2].data = pingMax;
            this.charts.ping.update('none');
        }

        // Throughput chart
        if (this.charts.throughput) {
            const txData = history
                .filter(h => h.throughput_tx !== null)
                .map(h => ({ x: new Date(h.timestamp), y: h.throughput_tx }));
            const rxData = history
                .filter(h => h.throughput_rx !== null)
                .map(h => ({ x: new Date(h.timestamp), y: h.throughput_rx }));

            this.charts.throughput.data.datasets[0].data = txData;
            this.charts.throughput.data.datasets[1].data = rxData;
            this.charts.throughput.update('none');
        }
    },

    /**
     * Trigger a manual test (ping or iperf)
     * @param {string} source - Source node name
     * @param {string} target - Target node name
     * @param {string} testType - 'ping' or 'iperf'
     * @param {HTMLElement} clickedButton - Optional button element that was clicked
     */
    async triggerTest(source, target, testType, clickedButton = null) {
        // Find the button - either passed in or find chart button
        let button = clickedButton;
        if (!button) {
            const buttonSelector = testType === 'ping' ? '#chart-ping-btn' : '#chart-iperf-btn';
            button = document.querySelector(buttonSelector);
        }
        const originalText = button ? button.textContent : '';

        // Show loading state
        if (button) {
            button.disabled = true;
            button.textContent = testType === 'ping' ? 'Pinging...' : 'Testing...';
        }

        // Show immediate feedback toast
        showToast('info', testType === 'ping' ? 'Running Ping' : 'Running iPerf',
            `Testing ${source} -> ${target}...`);

        try {
            const response = await fetch(
                `/api/rf-stats/test/${encodeURIComponent(source)}/${encodeURIComponent(target)}?type=${testType}`,
                { method: 'POST' }
            );
            const result = await response.json();

            if (result.success) {
                if (testType === 'ping' && result.result) {
                    showToast('success', 'Ping Complete',
                        `${source} -> ${target}: ${result.result.avg?.toFixed(1) || 'N/A'} ms avg, ${result.result.loss || 0}% loss`);
                } else if (testType === 'iperf' && result.result) {
                    showToast('success', 'iPerf Complete',
                        `${source} -> ${target}: ${result.result.tx_mbps?.toFixed(1) || 'N/A'} Mbps`);
                }

                // Reload data
                this.loadRFLinks();
                if (this.selectedLink &&
                    this.selectedLink.source === source &&
                    this.selectedLink.target === target) {
                    this.loadLinkHistory(source, target);
                }
            } else {
                showToast('error', 'Test Failed', result.error || 'Unknown error');
            }

        } catch (error) {
            console.error('Error triggering test:', error);
            showToast('error', 'Test Failed', 'Network error');
        } finally {
            // Restore button state
            if (button) {
                button.disabled = false;
                button.textContent = originalText;
            }
        }
    },

    /**
     * Handle real-time stats update from WebSocket
     */
    handleRealTimeUpdate(data) {
        // Update overview table
        this.loadRFLinks();

        // Update charts if this link is selected
        if (this.selectedLink &&
            this.selectedLink.source === data.link.source &&
            this.selectedLink.target === data.link.target) {

            const timestamp = new Date(data.timestamp);

            // Add new data points to charts
            if (data.quality !== undefined && this.charts.quality) {
                this.charts.quality.data.datasets[0].data.push({
                    x: timestamp, y: data.quality
                });
                this.charts.quality.update('none');
            }

            if (data.snr !== undefined && this.charts.snr) {
                this.charts.snr.data.datasets[0].data.push({
                    x: timestamp, y: data.snr
                });
                this.charts.snr.update('none');
            }

            if (data.ping && this.charts.ping) {
                if (data.ping.min !== null) {
                    this.charts.ping.data.datasets[0].data.push({
                        x: timestamp, y: data.ping.min
                    });
                }
                if (data.ping.avg !== null) {
                    this.charts.ping.data.datasets[1].data.push({
                        x: timestamp, y: data.ping.avg
                    });
                }
                if (data.ping.max !== null) {
                    this.charts.ping.data.datasets[2].data.push({
                        x: timestamp, y: data.ping.max
                    });
                }
                this.charts.ping.update('none');
            }

            if (data.throughput && this.charts.throughput) {
                if (data.throughput.tx_mbps !== undefined) {
                    this.charts.throughput.data.datasets[0].data.push({
                        x: timestamp, y: data.throughput.tx_mbps
                    });
                }
                if (data.throughput.rx_mbps !== undefined) {
                    this.charts.throughput.data.datasets[1].data.push({
                        x: timestamp, y: data.throughput.rx_mbps
                    });
                }
                this.charts.throughput.update('none');
            }
        }
    },

    /**
     * Handle iperf test status update
     */
    handleIperfStatus(data) {
        if (data.status === 'complete' && data.result) {
            showToast('success', 'iPerf Complete',
                `${data.link.source} -> ${data.link.target}: TX=${data.result.tx_mbps}Mbps, RX=${data.result.rx_mbps}Mbps`);
        } else if (data.status === 'failed') {
            showToast('warning', 'iPerf Failed',
                `${data.link.source} -> ${data.link.target}: Test failed`);
        }
    },

    /**
     * Get CSS class for quality value
     */
    getQualityClass(quality) {
        if (quality > 70) return 'quality-good';
        if (quality > 40) return 'quality-poor';
        return 'quality-bad';
    }
};

/**
 * Tab switching functionality
 */
function initTabSwitching() {
    const tabBtns = document.querySelectorAll('.tab-btn');
    const networkView = document.getElementById('network-view');
    const rfStatsView = document.getElementById('rf-stats-view');
    const nodesView = document.getElementById('nodes-view');
    const diagnosticsView = document.getElementById('diagnostics-view');
    const voipView = document.getElementById('voip-view');
    const legend = document.querySelector('.legend');
    const allViews = [networkView, rfStatsView, nodesView, diagnosticsView, voipView];

    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;

            // Update button states
            tabBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            // Hide all views, then activate the selected one.
            allViews.forEach(v => v && v.classList.remove('active'));
            if (legend) legend.style.display = tab === 'network' ? 'block' : 'none';

            if (tab === 'network') {
                networkView.classList.add('active');
            } else if (tab === 'rf-stats') {
                rfStatsView.classList.add('active');
                RFStats.init();
                RFStats.loadRFLinks();
            } else if (tab === 'nodes') {
                nodesView.classList.add('active');
                if (typeof NodesModule !== 'undefined') {
                    NodesModule.init();
                    NodesModule.loadNodes();
                }
            } else if (tab === 'diagnostics') {
                if (diagnosticsView) diagnosticsView.classList.add('active');
                if (typeof DiagnosticsModule !== 'undefined') {
                    DiagnosticsModule.init();
                    DiagnosticsModule.load();
                }
            } else if (tab === 'voip') {
                if (voipView) voipView.classList.add('active');
                if (typeof VOIPModule !== 'undefined') {
                    VOIPModule.init();
                    VOIPModule.load();
                }
            }
        });
    });
}

// Initialize tab switching when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    initTabSwitching();
});

// Add WebSocket handlers for RF stats (called from network.js socket setup)
function setupRFStatsSocketHandlers(socket) {
    socket.on('rf_stats_update', (data) => {
        if (RFStats.initialized) {
            RFStats.handleRealTimeUpdate(data);
        }
    });

    socket.on('iperf_test_status', (data) => {
        if (RFStats.initialized) {
            RFStats.handleIperfStatus(data);
        }
    });
}
