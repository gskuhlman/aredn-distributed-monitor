/**
 * WiFi Survey section on the Diagnostics tab (wifi_survey.py): non-mesh
 * access points heard by each radio, per channel. "Scan now" asks the node
 * for its WiFi scan (briefly takes its radio off channel: confirmed first);
 * "Import" parses pasted `iw dev wlan0 scan` output or a scan page. The
 * channel planner uses the newest survey per radio instead of fixed WiFi
 * guesses.
 */
const WifiSurvey = {
    initialized: false,

    init() {
        if (this.initialized || !document.getElementById('ws-report')) return;
        this.initialized = true;
        document.getElementById('ws-refresh')?.addEventListener('click', () => this.load());
        document.getElementById('ws-scan')?.addEventListener('click', () => this.scan());
        document.getElementById('ws-import')?.addEventListener('click', () => this.importText());
    },

    async load() {
        this.init();
        try {
            const d = await (await fetch('/api/wifi-survey')).json();
            const el = document.getElementById('ws-report');
            el.innerHTML = d.nodes.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Radio</th><th>Taken</th><th>Source</th><th>Non-mesh APs</th><th>By channel</th><th>Strongest</th></tr></thead>
                <tbody>${d.nodes.map(n => {
                    const top = n.aps.filter(a => !a.is_mesh).slice(0, 3);
                    return `<tr><td>${this.esc(n.node)}${n.site ? ` <small>${this.esc(n.site)}</small>` : ''}</td>
                        <td>${this.esc(n.taken_at)}</td><td>${this.esc(n.source)}</td><td>${n.non_mesh}</td>
                        <td>${Object.entries(n.by_channel).map(([c, k]) => `ch ${this.esc(c)}: ${k}`).join(', ') || '-'}</td>
                        <td>${top.map(a => `${this.esc(a.ssid || a.bssid || '?')} ch ${a.channel ?? '?'} ${a.signal ?? '?'} dBm`).join('<br>') || '-'}</td></tr>`;
                }).join('')}</tbody></table></div>`
                : '<p class="log-empty">No surveys yet. Scan a radio, or import scan text.</p>';
            this.setStatus(`Planner uses surveys up to ${d.max_age_days} days old` + (d.scheduled ? ' · nightly scan of infrastructure radios on' : ''));
        } catch (error) {
            this.setStatus('Failed to load.');
        }
    },

    node() {
        return (document.getElementById('ws-node')?.value || '').trim().toLowerCase();
    },

    async scan() {
        const node = this.node();
        if (!node) return this.setStatus('Enter a radio name first.');
        if (!window.confirm(`Ask ${node} for a WiFi scan? Its radio leaves its channel for a few seconds, ` +
                'so its links may blip.')) return;
        await this.post('/api/wifi-survey/scan', { node }, 'Scanning…');
    },

    async importText() {
        const node = this.node();
        const text = document.getElementById('ws-text')?.value || '';
        if (!node || !text.trim()) return this.setStatus('Enter the radio name and paste the scan text.');
        await this.post('/api/wifi-survey/import', { node, text }, 'Importing…');
    },

    async post(url, body, busy) {
        this.setStatus(busy);
        try {
            const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            const d = await r.json();
            if (!r.ok) throw new Error(d.error || 'failed');
            await this.load();
            this.setStatus(`${d.node}: ${d.aps} AP(s), ${d.non_mesh} non-mesh, ${d.source}`);
        } catch (error) {
            this.setStatus(error.message);
        }
    },

    setStatus(text) {
        const el = document.getElementById('ws-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => WifiSurvey.init());
