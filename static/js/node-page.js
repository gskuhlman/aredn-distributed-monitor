const NodePage = {
    nodeName: window.NODE_NAME,
    data: null,
    charts: {},

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
        this.renderSummary();
        this.renderLinks();
        this.renderServices();
        this.renderCharts();
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
                <tr><td>Status:</td><td>${node.is_active === 1 ? 'Active' : 'Inactive'}</td></tr>
                <tr><td>Supernode:</td><td>${node.is_supernode === 1 ? 'Yes' : 'No'}</td></tr>
                <tr><td>Latitude:</td><td>${this.escapeHtml(node.lat || 'N/A')}</td></tr>
                <tr><td>Longitude:</td><td>${this.escapeHtml(node.lon || 'N/A')}</td></tr>
            </table>
        `;
    },

    renderLinks() {
        const links = this.data.all_links || [];
        if (!links.length) {
            document.getElementById('node-links').innerHTML = '<p>No links recorded for this node.</p>';
            return;
        }

        const rows = links.map(link => {
            const other = link.source_node === this.data.node.name ? link.target_node : link.source_node;
            return `
                <tr>
                    <td><a href="/nodes/${encodeURIComponent(other)}">${this.escapeHtml(other)}</a></td>
                    <td>${this.escapeHtml(link.link_type || '')}</td>
                    <td>${this.escapeHtml(link.quality ?? '')}</td>
                    <td>${this.escapeHtml(link.snr ?? 'N/A')}</td>
                    <td>${this.escapeHtml(link.status || '')}</td>
                    <td>${this.formatDate(link.last_seen)}</td>
                    <td>
                        <button class="btn btn-small btn-secondary" data-action="ping" data-source="${this.escapeAttr(this.data.node.name)}" data-target="${this.escapeAttr(other)}">Ping</button>
                        <button class="btn btn-small btn-secondary" data-action="iperf" data-source="${this.escapeAttr(this.data.node.name)}" data-target="${this.escapeAttr(other)}">iPerf3</button>
                    </td>
                </tr>
            `;
        }).join('');

        const container = document.getElementById('node-links');
        container.innerHTML = `
            <table class="info-table">
                <tr><th>Node</th><th>Type</th><th>Quality</th><th>SNR</th><th>Status</th><th>Last Seen</th><th>Actions</th></tr>
                ${rows}
            </table>
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
        document.getElementById('node-services').innerHTML = `
            <ul class="services-list">
                ${services.map(service => `
                    <li>${service.link ? `<a href="${this.escapeAttr(service.link)}" target="_blank">${this.escapeHtml(service.name)}</a>` : this.escapeHtml(service.name)}</li>
                `).join('')}
            </ul>
        `;
    },

    renderCharts() {
        this.initCharts();
        const quality = (this.data.quality_history || [])
            .filter(row => row.quality !== null)
            .map(row => ({ x: new Date(row.timestamp), y: row.quality }));
        const ping = (this.data.ping_history || [])
            .filter(row => row.ping_avg !== null)
            .map(row => ({ x: new Date(row.timestamp), y: row.ping_avg }));

        this.charts.quality.data.datasets[0].data = quality;
        this.charts.ping.data.datasets[0].data = ping;
        this.charts.quality.update('none');
        this.charts.ping.update('none');
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

    initCharts() {
        if (this.charts.quality && this.charts.ping) return;
        const options = {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'index' },
            scales: { x: { type: 'time' }, y: { beginAtZero: true } },
            plugins: { legend: { display: true, position: 'top' } }
        };
        this.charts.quality = new Chart(document.getElementById('node-page-quality-chart'), {
            type: 'line',
            data: { datasets: [{ label: 'Quality %', borderColor: '#27ae60', data: [] }] },
            options: { ...options, scales: { ...options.scales, y: { beginAtZero: true, max: 100 } } }
        });
        this.charts.ping = new Chart(document.getElementById('node-page-ping-chart'), {
            type: 'line',
            data: { datasets: [{ label: 'Ping avg ms', borderColor: '#f39c12', data: [] }] },
            options
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
            this.showToast('success', type === 'ping' ? 'Ping Complete' : 'iPerf Complete', `${source} -> ${target}`);
            await this.load();
        } catch (error) {
            this.showToast('error', 'Test Failed', error.message);
        } finally {
            button.disabled = false;
            button.textContent = original;
        }
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
