/**
 * AREDN Network Monitor - Nodes Database Module
 * Displays all nodes ever scanned with detailed history and management
 */

const NodesModule = {
    nodes: [],
    filteredNodes: [],
    networkData: { nodes: [], edges: [] },
    selectedNode: null,
    charts: {},
    timeRangeHours: 24,
    initialized: false,

    init() {
        if (this.initialized) return;
        this.initEventListeners();
        this.initialized = true;
        console.log('Nodes module initialized');
    },

    initEventListeners() {
        const searchInput = document.getElementById('nodes-search');
        const statusFilter = document.getElementById('nodes-status-filter');
        const typeFilter = document.getElementById('nodes-type-filter');
        const sortSelect = document.getElementById('nodes-sort');
        const closeDetail = document.getElementById('close-node-detail');

        if (searchInput) {
            searchInput.addEventListener('input', () => this.filterNodes());
        }
        if (statusFilter) {
            statusFilter.addEventListener('change', () => this.filterNodes());
        }
        if (typeFilter) {
            typeFilter.addEventListener('change', () => this.filterNodes());
        }
        if (sortSelect) {
            sortSelect.addEventListener('change', () => this.filterNodes());
        }
        if (closeDetail) {
            closeDetail.addEventListener('click', () => this.closeDetailPanel());
        }

        // Node detail tabs
        document.querySelectorAll('.node-tab-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const tab = e.target.dataset.nodeTab;
                this.switchNodeTab(tab);
            });
        });

        // Time range buttons in node detail
        document.querySelectorAll('#node-detail-panel .time-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const hours = parseInt(e.target.dataset.hours);
                this.timeRangeHours = hours;

                // Update active state
                const parent = e.target.parentElement;
                parent.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                e.target.classList.add('active');

                // Reload active tab's data
                const activeTab = document.querySelector('.node-tab-content.active');
                if (activeTab) {
                    if (activeTab.id === 'node-tab-quality-history') {
                        this.loadQualityHistory();
                    } else if (activeTab.id === 'node-tab-ping-history') {
                        this.loadPingHistory();
                    }
                }
            });
        });
    },

    async loadNodes() {
        try {
            const response = await fetch('/api/nodes/all');
            this.nodes = await response.json();
            await this.loadNetworkData();
            this.filterNodes();
        } catch (error) {
            console.error('Error loading nodes:', error);
        }
    },

    async loadNetworkData() {
        try {
            const response = await fetch('/api/network');
            this.networkData = response.ok ? await response.json() : { nodes: [], edges: [] };
        } catch (error) {
            console.error('Error loading network data for node filters:', error);
            this.networkData = { nodes: [], edges: [] };
        }
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

    getSelectedNodeNames() {
        const selected = new Set(
            this.nodes
                .filter(node => node.is_selected)
                .map(node => node.name)
                .filter(Boolean)
        );

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

    // Map an edge link_type to one of our coarse filter buckets.
    linkTypeBucket(linkType) {
        const t = String(linkType || '').toUpperCase();
        if (t === 'RF') return 'rf';
        if (t === 'WIREGUARD' || t === 'WG') return 'wireguard';
        if (t === 'TUN' || t === 'TUNNEL' || t === 'VTUN') return 'tunnel';
        return null;
    },

    // Node names that participate in at least one link of the given bucket.
    nodesWithLinkType(bucket) {
        const names = new Set();
        for (const edge of this.networkData.edges || []) {
            if (this.linkTypeBucket(edge.link_type) === bucket) {
                if (edge.from) names.add(edge.from);
                if (edge.to) names.add(edge.to);
            }
        }
        return names;
    },

    filterNodes() {
        const search = (document.getElementById('nodes-search')?.value || '').toLowerCase();
        const status = document.getElementById('nodes-status-filter')?.value || 'all';
        const typeFilter = document.getElementById('nodes-type-filter')?.value || 'all';
        const sortBy = document.getElementById('nodes-sort')?.value || 'alpha';

        const now = Date.now();
        const ms24h = 24 * 60 * 60 * 1000;
        const ms7d = 7 * ms24h;
        const selectedNames = status === 'selected' ? this.getSelectedNodeNames() : null;
        const selectedConnectedNames = status === 'selected-connected' ? this.getSelectedConnectedNodeNames() : null;
        const typeNames = typeFilter !== 'all' ? this.nodesWithLinkType(typeFilter) : null;

        this.filteredNodes = this.nodes.filter(node => {
            const name = (node.name || '').toLowerCase();
            const ip = (node.ip || '').toLowerCase();
            const model = (node.model || '').toLowerCase();
            const firmware = (node.firmware_version || '').toLowerCase();
            const description = (node.description || '').toLowerCase();
            const rfFreq = (node.rf_frequency || '').toLowerCase();
            const rfChannel = (node.rf_channel || '').toLowerCase();
            const services = (node.services_list || []).map(s => (s.name || '').toLowerCase()).join(' ');
            const reporters = (node.reporters || []).join(' ').toLowerCase();
            const observedStatus = (node.observed_status || '').toLowerCase();

            const matchesSearch = !search ||
                name.includes(search) ||
                ip.includes(search) ||
                model.includes(search) ||
                firmware.includes(search) ||
                description.includes(search) ||
                rfFreq.includes(search) ||
                rfChannel.includes(search) ||
                services.includes(search) ||
                reporters.includes(search) ||
                observedStatus.includes(search);

            let matchesStatus = true;
            if (status === 'selected') {
                matchesStatus = selectedNames.has(node.name);
            } else if (status === 'selected-connected') {
                matchesStatus = selectedConnectedNames.has(node.name);
            } else if (status === 'active') {
                matchesStatus = node.is_active === 1 && !node.is_link_only;
            } else if (status === 'link-only') {
                matchesStatus = !!node.is_link_only;
            } else if (status === 'inactive') {
                matchesStatus = node.is_active !== 1;
            } else if (status === '24h') {
                const lastSeen = node.last_seen ? new Date(node.last_seen).getTime() : 0;
                matchesStatus = (now - lastSeen) < ms24h;
            } else if (status === '7d') {
                const lastSeen = node.last_seen ? new Date(node.last_seen).getTime() : 0;
                matchesStatus = (now - lastSeen) < ms7d;
            }

            const matchesType = !typeNames || typeNames.has(node.name);

            return matchesSearch && matchesStatus && matchesType;
        });

        // Sort
        this.filteredNodes.sort((a, b) => {
            if (sortBy === 'alpha') {
                return (a.name || '').localeCompare(b.name || '');
            } else if (sortBy === 'last-seen') {
                const aTime = a.last_seen ? new Date(a.last_seen).getTime() : 0;
                const bTime = b.last_seen ? new Date(b.last_seen).getTime() : 0;
                return bTime - aTime; // Most recent first
            } else if (sortBy === 'firmware') {
                return (a.firmware_version || '').localeCompare(b.firmware_version || '');
            } else if (sortBy === 'model') {
                return (a.model || '').localeCompare(b.model || '');
            }
            return 0;
        });

        this.renderTable();
        const countEl = document.getElementById('nodes-total-count');
        if (countEl) countEl.textContent = this.filteredNodes.length;
    },

    getQRZLink(name) {
        if (!name) return name;
        const dashIndex = name.indexOf('-');
        const callsign = dashIndex > 0 ? name.substring(0, dashIndex) : name;
        const escapedName = this.escapeHtml(name);
        if (callsign.length >= 4 && callsign.length <= 7) {
            const escapedCallsign = this.escapeHtml(callsign);
            const escapedSuffix = dashIndex > 0 ? this.escapeHtml(name.substring(dashIndex)) : '';
            return `<a href="https://www.qrz.com/db/${encodeURIComponent(callsign)}" target="_blank" title="Lookup ${escapedCallsign} on QRZ">${escapedCallsign}</a>${escapedSuffix}`;
        }
        return escapedName;
    },

    renderTable() {
        const tbody = document.getElementById('nodes-table-body');
        if (!tbody) return;

        if (this.filteredNodes.length === 0) {
            tbody.innerHTML = '<tr><td colspan="9" class="nodes-empty">No nodes found</td></tr>';
            return;
        }

        let html = '';
        for (const node of this.filteredNodes) {
            const statusClass = node.is_link_only ? 'status-link-only' : (node.is_active === 1 ? 'status-active' : 'status-inactive');
            const statusText = node.is_link_only
                ? (node.observed_status === 'removed' ? 'Link-only Removed' : 'Link-only')
                : (node.is_active === 1 ? 'Active' : 'Inactive');
            const lastSeen = node.last_seen ? new Date(node.last_seen).toLocaleString() : 'Never';
            const servicesCount = (node.services_list || []).length;
            const nodeName = node.name || '';
            const encodedName = encodeURIComponent(nodeName);

            const ipLink = this.renderNodeIpLink(node.ip);
            const nameLink = this.getQRZLink(nodeName);
            const modelText = node.is_link_only ? 'Not polled' : (node.model || 'Unknown');
            const firmwareText = node.is_link_only ? 'Not polled' : (node.firmware_version || 'Unknown');
            const deleteButton = node.is_link_only
                ? ''
                : `<button class="btn btn-small btn-danger" data-node-action="delete" data-node-name="${encodedName}">Delete</button>`;

            html += `
                <tr>
                    <td><strong>${nameLink}</strong></td>
                    <td>${ipLink}</td>
                    <td>${this.escapeHtml(modelText)}</td>
                    <td>${this.escapeHtml(firmwareText)}</td>
                    <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                    <td>${this.escapeHtml(lastSeen)}</td>
                    <td>${node.links_count || 0}</td>
                    <td>${servicesCount}</td>
                    <td>
                        <button class="btn btn-small btn-secondary" data-node-action="details" data-node-name="${encodedName}">Details</button>
                        <a class="btn btn-small btn-secondary" href="/nodes/${encodedName}">Full</a>
                        ${deleteButton}
                    </td>
                </tr>
            `;
        }
        tbody.innerHTML = html;

        tbody.querySelectorAll('[data-node-action]').forEach(button => {
            button.addEventListener('click', () => {
                const name = decodeURIComponent(button.dataset.nodeName || '');
                if (button.dataset.nodeAction === 'details') {
                    this.showNodeDetail(name);
                } else if (button.dataset.nodeAction === 'delete') {
                    this.deleteNode(name);
                }
            });
        });
    },

    async showNodeDetail(name) {
        this.selectedNode = name;
        const panel = document.getElementById('node-detail-panel');
        const title = document.getElementById('node-detail-title');

        if (title) title.textContent = name;
        if (panel) panel.classList.remove('hidden');

        // Scroll to detail panel
        panel?.scrollIntoView({ behavior: 'smooth', block: 'start' });

        // Load overview first
        this.switchNodeTab('overview');
    },

    closeDetailPanel() {
        const panel = document.getElementById('node-detail-panel');
        if (panel) panel.classList.add('hidden');
        this.selectedNode = null;
    },

    switchNodeTab(tab) {
        // Update tab buttons
        document.querySelectorAll('.node-tab-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.nodeTab === tab);
        });

        // Update tab content
        document.querySelectorAll('.node-tab-content').forEach(content => {
            content.classList.toggle('active', content.id === `node-tab-${tab}`);
        });

        if (!this.selectedNode) return;

        if (tab === 'overview') {
            this.loadOverview();
        } else if (tab === 'quality-history') {
            this.loadQualityHistory();
        } else if (tab === 'ping-history') {
            this.loadPingHistory();
        } else if (tab === 'connectivity') {
            this.loadConnectivityLog();
        }
    },

    async loadOverview() {
        const container = document.getElementById('node-overview-content');
        if (!container || !this.selectedNode) return;

        container.innerHTML = '<p class="loading">Loading...</p>';

        try {
            const [response, flapsResp] = await Promise.all([
                fetch(`/api/nodes/detail/${encodeURIComponent(this.selectedNode)}`),
                fetch(`/api/reports/flaps?node=${encodeURIComponent(this.selectedNode)}&hours=24`)
            ]);
            const data = await response.json();
            const flaps = flapsResp.ok ? ((await flapsResp.json()).links || []) : [];
            const flapMap = this.buildFlapMap(flaps, this.selectedNode);

            const node = data.node;
            const services = data.services || [];
            const links = data.links || [];

            const nameQRZ = this.getQRZLink(node.name);

            let html = `
                <div class="node-detail-section">
                    <h3>Node Information</h3>
                    <table class="info-table">
                        <tr><td>Name:</td><td>${nameQRZ}</td></tr>
                        <tr><td>IP:</td><td>${this.renderNodeIpLink(node.ip)}</td></tr>
                        <tr><td>Model:</td><td>${this.escapeHtml(node.model || 'Unknown')}</td></tr>
                        <tr><td>Firmware:</td><td>${this.escapeHtml(node.firmware_version || 'Unknown')}</td></tr>
                        <tr><td>Description:</td><td>${this.escapeHtml(node.description || 'N/A')}</td></tr>
                        <tr><td>RF Frequency:</td><td>${this.escapeHtml(node.rf_frequency || 'N/A')}</td></tr>
                        <tr><td>RF Channel:</td><td>${this.escapeHtml(node.rf_channel || 'N/A')}</td></tr>
                        <tr><td>First Seen:</td><td>${node.first_seen ? new Date(node.first_seen).toLocaleString() : 'N/A'}</td></tr>
                        <tr><td>Last Seen:</td><td>${node.last_seen ? new Date(node.last_seen).toLocaleString() : 'N/A'}</td></tr>
                        <tr><td>Status:</td><td>${this.getNodeStatusText(node)}</td></tr>
                        <tr><td>Supernode:</td><td>${node.is_supernode === 1 ? 'Yes' : 'No'}</td></tr>
                        ${node.is_link_only ? `<tr><td>Reported By:</td><td>${this.escapeHtml((node.reporters || []).join(', ') || 'N/A')}</td></tr>` : ''}
                    </table>
                </div>
            `;

            if (node.is_link_only) {
                html += `
                    <div class="node-detail-section">
                        <h3>Diagnostic Notes</h3>
                        <p class="node-warning">${this.escapeHtml(node.lqm_status_message || 'LQM-only neighbor')}</p>
                        <table class="info-table">
                            <tr><td>Identity Status:</td><td>${this.escapeHtml(node.identity_status || 'lqm_only')}</td></tr>
                            <tr><td>Routability Status:</td><td>${this.escapeHtml(node.routability_status || 'unknown')}</td></tr>
                            <tr><td>MAC Address:</td><td>${this.escapeHtml((node.mac_addresses || []).join(', ') || 'N/A')}</td></tr>
                            <tr><td>Canonical IP:</td><td>${this.escapeHtml((node.canonical_ips || []).join(', ') || 'N/A')}</td></tr>
                            <tr><td>Likely causes:</td><td>No routable canonical IP, DNS failure, discovery depth limit, supernode boundary, or node HTTP/sysinfo unreachable</td></tr>
                            <tr><td>Useful check:</td><td>Inspect the reporter's LQM tracker entry for routable and canonical_ip</td></tr>
                        </table>
                    </div>
                `;
            }

            if (node.lat && node.lon) {
                html += `
                    <div class="node-detail-section">
                        <h3>Location</h3>
                        <p>Latitude: ${this.escapeHtml(node.lat)}, Longitude: ${this.escapeHtml(node.lon)}</p>
                    </div>
                `;
            }

            if (links.length > 0) {
                html += `
                    <div class="node-detail-section">
                        <h3>Connected Nodes (${links.length})</h3>
                        <table class="info-table">
                            <tr><th>Node</th><th>Type</th><th>Quality</th><th>SNR</th><th>Signal</th><th>Noise</th><th>MAC</th><th>Routability</th><th>Status</th><th>Node-reported flaps</th><th>Scanner-to-node</th><th>Last Seen</th></tr>
                `;
                for (const link of links) {
                    const otherNode = link.source_node === node.name ? link.target_node : link.source_node;
                    const qualityClass = this.getQualityClass(link.quality);
                    const fm = flapMap[otherNode] || { node_reported: 0, scanner: 0 };
                    html += `
                        <tr>
                            <td><strong>${this.escapeHtml(otherNode)}</strong></td>
                            <td>${this.escapeHtml(link.link_type)}</td>
                            <td class="${qualityClass}">${link.quality || 0}%</td>
                            <td>${this.escapeHtml(link.snr || 'N/A')}</td>
                            <td>${this.escapeHtml(link.signal || 'N/A')}</td>
                            <td>${this.escapeHtml(link.noise || 'N/A')}</td>
                            <td>${this.escapeHtml(link.mac_address || 'N/A')}</td>
                            <td>${this.escapeHtml(link.routability_status || 'unknown')}</td>
                            <td>${this.escapeHtml(link.status || 'good')}</td>
                            <td>${this.flapCountCell(fm.node_reported, 'node-reported peer down(s)')}</td>
                            <td>${this.flapCountCell(fm.scanner, 'time(s) the scanner could not reach this node (not a peer flap)', true)}</td>
                            <td>${link.last_seen ? new Date(link.last_seen).toLocaleString() : 'N/A'}</td>
                        </tr>
                    `;
                }
                html += '</table></div>';
            }

            if (services.length > 0) {
                html += `
                    <div class="node-detail-section">
                        <h3>Services (${services.length})</h3>
                        <ul class="services-list">
                `;
                for (const service of services) {
                    const icon = this.getServiceIcon(service.name);
                    if (service.link) {
                        html += `<li>${icon} <a href="${this.escapeHtml(service.link)}" target="_blank">${this.escapeHtml(service.name)}</a></li>`;
                    } else {
                        html += `<li>${icon} ${this.escapeHtml(service.name)}</li>`;
                    }
                }
                html += '</ul></div>';
            }

            container.innerHTML = html;

        } catch (error) {
            console.error('Error loading node overview:', error);
            container.innerHTML = '<p class="error">Failed to load node details</p>';
        }
    },

    async loadQualityHistory() {
        if (!this.selectedNode) return;

        // Initialize charts if needed
        this.initQualityCharts();

        try {
            const response = await fetch(
                `/api/nodes/history/${encodeURIComponent(this.selectedNode)}?hours=${this.timeRangeHours}`
            );
            const history = await response.json();

            // Process data - group by link pair
            const qualityData = [];
            const snrData = [];

            for (const h of history) {
                const ts = new Date(h.timestamp);
                const otherNode = h.source_node === this.selectedNode ? h.target_node : h.source_node;
                const label = `${otherNode}`;

                if (h.quality !== null) {
                    qualityData.push({ x: ts, y: h.quality, label });
                }
                if (h.snr !== null) {
                    snrData.push({ x: ts, y: h.snr, label });
                }
            }

            // Update quality chart
            if (this.charts.quality) {
                this.charts.quality.data.datasets = [{
                    label: 'Quality %',
                    borderColor: '#27ae60',
                    backgroundColor: 'rgba(39, 174, 96, 0.1)',
                    fill: true,
                    tension: 0.3,
                    data: qualityData,
                    pointRadius: 2
                }];
                this.charts.quality.update('none');
            }

            // Update SNR chart
            if (this.charts.snr) {
                this.charts.snr.data.datasets = [{
                    label: 'SNR (dB)',
                    borderColor: '#3498db',
                    backgroundColor: 'rgba(52, 152, 219, 0.1)',
                    fill: true,
                    tension: 0.3,
                    data: snrData,
                    pointRadius: 2
                }];
                this.charts.snr.update('none');
            }

        } catch (error) {
            console.error('Error loading quality history:', error);
        }
    },

    async loadPingHistory() {
        if (!this.selectedNode) return;

        this.initPingChart();

        try {
            const response = await fetch(
                `/api/nodes/ping-history/${encodeURIComponent(this.selectedNode)}?hours=${this.timeRangeHours}`
            );
            const history = await response.json();

            const avgData = [];
            const minData = [];
            const maxData = [];

            for (const h of history) {
                const ts = new Date(h.timestamp);
                if (h.ping_avg !== null) avgData.push({ x: ts, y: h.ping_avg });
                if (h.ping_min !== null) minData.push({ x: ts, y: h.ping_min });
                if (h.ping_max !== null) maxData.push({ x: ts, y: h.ping_max });
            }

            if (this.charts.ping) {
                this.charts.ping.data.datasets = [
                    {
                        label: 'Min',
                        borderColor: '#27ae60',
                        backgroundColor: 'transparent',
                        tension: 0.3,
                        data: minData,
                        pointRadius: 2
                    },
                    {
                        label: 'Avg',
                        borderColor: '#f39c12',
                        backgroundColor: 'rgba(243, 156, 18, 0.1)',
                        fill: true,
                        tension: 0.3,
                        data: avgData,
                        pointRadius: 2
                    },
                    {
                        label: 'Max',
                        borderColor: '#e74c3c',
                        backgroundColor: 'transparent',
                        tension: 0.3,
                        data: maxData,
                        pointRadius: 2
                    }
                ];
                this.charts.ping.update('none');
            }

        } catch (error) {
            console.error('Error loading ping history:', error);
        }
    },

    async loadConnectivityLog() {
        const container = document.getElementById('node-connectivity-content');
        if (!container || !this.selectedNode) return;

        container.innerHTML = '<p class="loading">Loading...</p>';

        try {
            const response = await fetch(`/api/nodes/connectivity/${encodeURIComponent(this.selectedNode)}`);
            const events = await response.json();

            if (events.length === 0) {
                container.innerHTML = '<p class="log-empty">No connectivity events recorded</p>';
                return;
            }

            const eventIcons = {
                'node_discovered': { icon: '&#10133;', label: 'Discovered', class: 'event-info' },
                'node_offline': { icon: '&#10060;', label: 'Offline', class: 'event-warning' },
                'node_online': { icon: '&#10004;', label: 'Reconnected', class: 'event-success' },
                'link_new': { icon: '&#128279;', label: 'New Link', class: 'event-info' },
                'link_dropped': { icon: '&#128280;', label: 'Link Dropped', class: 'event-warning' },
                'link_removed': { icon: '&#128465;', label: 'Link Removed', class: 'event-info' },
                'link_restored': { icon: '&#128279;', label: 'Link Restored', class: 'event-success' },
                'frequency_change': { icon: '&#128246;', label: 'Freq Change', class: 'event-warning' }
            };

            let html = '';
            for (const event of events) {
                const info = eventIcons[event.event_type] || { icon: '&#8226;', label: event.event_type, class: 'event-info' };
                const time = event.timestamp ? new Date(event.timestamp).toLocaleString() : '';

                html += `
                    <div class="log-entry ${info.class}">
                        <span class="log-icon">${info.icon}</span>
                        <div class="log-details">
                            <div class="log-header">
                                <span class="log-type">${info.label}</span>
                                <span class="log-time">${time}</span>
                            </div>
                    <div class="log-message">${this.escapeHtml(event.details || '')}</div>
                        </div>
                    </div>
                `;
            }
            container.innerHTML = html;

        } catch (error) {
            console.error('Error loading connectivity log:', error);
            container.innerHTML = '<p class="error">Failed to load connectivity log</p>';
        }
    },

    initQualityCharts() {
        if (this.charts.quality && this.charts.snr) return;

        const commonOptions = {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'index' },
            scales: {
                x: {
                    type: 'time',
                    time: { unit: 'minute', displayFormats: { minute: 'HH:mm', hour: 'HH:mm' } },
                    title: { display: false }
                },
                y: { beginAtZero: true }
            },
            plugins: { legend: { display: true, position: 'top' } }
        };

        const qualityCtx = document.getElementById('node-quality-chart');
        if (qualityCtx && !this.charts.quality) {
            this.charts.quality = new Chart(qualityCtx, {
                type: 'line',
                data: { datasets: [] },
                options: {
                    ...commonOptions,
                    scales: { ...commonOptions.scales, y: { beginAtZero: true, max: 100 } }
                }
            });
        }

        const snrCtx = document.getElementById('node-snr-chart');
        if (snrCtx && !this.charts.snr) {
            this.charts.snr = new Chart(snrCtx, {
                type: 'line',
                data: { datasets: [] },
                options: commonOptions
            });
        }
    },

    initPingChart() {
        if (this.charts.ping) return;

        const pingCtx = document.getElementById('node-ping-chart');
        if (!pingCtx) return;

        const commonOptions = {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'index' },
            scales: {
                x: {
                    type: 'time',
                    time: { unit: 'minute', displayFormats: { minute: 'HH:mm', hour: 'HH:mm' } },
                    title: { display: false }
                },
                y: { beginAtZero: true }
            },
            plugins: { legend: { display: true, position: 'top' } }
        };

        this.charts.ping = new Chart(pingCtx, {
            type: 'line',
            data: { datasets: [] },
            options: commonOptions
        });
    },

    async deleteNode(name) {
        if (!confirm(`Are you sure you want to delete node "${name}" and all its related data (links, services, history)?`)) {
            return;
        }

        try {
            const response = await fetch(`/api/nodes/delete/${encodeURIComponent(name)}`, {
                method: 'POST'
            });
            const result = await response.json();

            if (result.success) {
                showToast('success', 'Node Deleted', `${name} has been removed`);
                // Close detail panel if it was open for this node
                if (this.selectedNode === name) {
                    this.closeDetailPanel();
                }
                // Reload the table
                this.loadNodes();
            } else {
                showToast('error', 'Delete Failed', result.error || 'Unknown error');
            }
        } catch (error) {
            console.error('Error deleting node:', error);
            showToast('error', 'Delete Failed', 'Network error');
        }
    },

    // Aggregate the flap report (directional rows) into per-peer totals for the
    // selected node, separating node-reported peer flaps from scanner-to-node loss.
    buildFlapMap(flaps, nodeName) {
        const map = {};
        for (const l of flaps) {
            let peer = null;
            if (l.source_node === nodeName) peer = l.target_node;
            else if (l.target_node === nodeName) peer = l.source_node;
            else continue;
            const e = map[peer] || { node_reported: 0, scanner: 0 };
            e.node_reported += l.node_reported_downs || 0;
            e.scanner += (l.scanner_unreachable || 0) + (l.inferred_downs || 0);
            map[peer] = e;
        }
        return map;
    },

    flapCountCell(count, title, marginalOnly = false) {
        if (!count) return '<span class="health-val health-good">0</span>';
        const rating = marginalOnly ? 'marginal' : (count >= 8 ? 'poor' : count >= 3 ? 'marginal' : 'good');
        return `<span class="health-val health-${rating}" title="${this.escapeAttr(count + ' ' + title)}">${count}</span>`;
    },

    getQualityClass(quality) {
        if (quality > 70) return 'quality-good';
        if (quality > 40) return 'quality-poor';
        return 'quality-bad';
    },

    getNodeStatusText(node) {
        if (node.is_link_only) {
            return node.observed_status === 'removed' ? 'Link-only / removed' : 'Link-only / not pollable';
        }
        return node.is_active === 1 ? 'Active' : 'Inactive';
    },

    getServiceIcon(serviceName) {
        const name = (serviceName || '').toLowerCase();
        if (name.includes('phone') || name.includes('voip') || name.includes('sip')) return '&#128222;';
        if (name.includes('meshchat') || name.includes('chat')) return '&#128172;';
        if (name.includes('pbx') || name.includes('asterisk')) return '&#9742;';
        if (name.includes('camera') || name.includes('cam') || name.includes('video')) return '&#127909;';
        if (name.includes('weather') || name.includes('weewx')) return '&#127780;';
        if (name.includes('winlink')) return '&#9993;';
        if (name.includes('iperf')) return '&#128200;';
        return '&#8226;';
    }
};
