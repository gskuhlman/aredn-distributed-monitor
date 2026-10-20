/**
 * Capacity section of the Reports tab (capacity.py): per key link direction
 * the iPerf capacity (overnight scheduled runs when there are any), PHY rate,
 * efficiency, estimated traffic and spare capacity, with a nightly trend.
 * Click a row for its iPerf history chart. Also manages the key links of the
 * overnight schedule and can start a pass now.
 */
const CapacityPanel = {
    initialized: false,
    data: null,
    chart: null,
    loaded: false,

    init() {
        if (this.initialized) return;
        if (!document.getElementById('cap-table')) return;
        this.initialized = true;
        document.getElementById('cap-refresh')?.addEventListener('click', () => this.fetch());
        document.getElementById('cap-run')?.addEventListener('click', () => this.runNow());
        document.getElementById('cap-add')?.addEventListener('click', () => this.addLink());
        document.getElementById('cap-all')?.addEventListener('change', () => this.render());
        document.getElementById('cap-table').addEventListener('click', (event) => {
            const off = event.target.closest('button[data-cap-off]');
            if (off) {
                event.stopPropagation();
                this.setEnabled(off.dataset.a, off.dataset.b, false);
                return;
            }
            const row = event.target.closest('tr[data-a]');
            if (row) this.showLink(row.dataset.a, row.dataset.b);
        });
    },

    load() {
        this.init();
        if (!this.initialized || this.loaded) return;  // not window-based: load once, Refresh for more
        this.loaded = true;
        this.fetch();
    },

    async fetch() {
        const el = document.getElementById('cap-table');
        el.innerHTML = '<p class="log-empty">Loading...</p>';
        try {
            const resp = await fetch('/api/capacity?hours=168');
            this.data = await resp.json();
            this.render();
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Failed to load capacity.</p>';
        }
    },

    spark(nightly) {
        if (!nightly.length) return '<span class="form-hint">no overnight runs yet</span>';
        const max = Math.max(...nightly.map(n => n.median), 1);
        const w = 14 * nightly.length, h = 24;
        return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">${nightly.map((n, i) => {
            const bh = Math.max(2, n.median / max * (h - 2));
            return `<rect x="${i * 14}" y="${h - bh}" width="11" height="${bh}" fill="#2e86c1"><title>${this.esc(n.night)}: ${n.median} Mbps (${n.runs} run(s))</title></rect>`;
        }).join('')}</svg>`;
    },

    render() {
        const d = this.data;
        if (!d) return;
        const sch = d.schedule;
        const st = sch.state || {};
        const last = st.last_run;
        document.getElementById('cap-hint').textContent = (sch.enabled
            ? `Overnight iPerf ${sch.start}–${sch.end}, ${sch.rounds} pass(es) over ${sch.key_links} key link direction(s)`
            : 'Overnight iPerf is off (IPERF_SCHEDULE_ENABLED)')
            + (st.running ? ` · running: ${st.done}/${st.planned}` : '')
            + (last ? ` · last run ${last.started.slice(5, 16)}: ${last.ok} ok, ${last.failed} failed${last.aborted ? ` (stopped: ${last.aborted})` : ''}` : '');
        const all = document.getElementById('cap-all')?.checked;
        const rows = d.links.filter(l => all || l.key);
        if (!rows.length) {
            document.getElementById('cap-table').innerHTML = '<p class="log-empty">No iPerf results yet.</p>';
            return;
        }
        const n = v => v == null ? '-' : v;
        document.getElementById('cap-table').innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table cap-table">
            <thead><tr><th>Link (iPerf direction)</th><th title="Overnight median, else 90th percentile">Capacity</th><th>Latest</th>
            <th>Daytime median</th><th>PHY</th><th title="iPerf / PHY rate">Efficiency</th>
            <th title="Estimated from LQM packet counters (median / 90th percentile)">Traffic</th><th>Spare</th>
            <th>Nightly</th><th>Runs</th><th></th></tr></thead>
            <tbody>${rows.map(l => `<tr data-a="${this.esc(l.source)}" data-b="${this.esc(l.target)}" class="${l.flag ? 'rel-row rel-row-adjacent' : ''}">
                <td>${this.esc(l.source)} → ${this.esc(l.target)}${l.backbone ? ' <small class="hs-infra">backbone</small>' : ''}
                    ${l.flag ? `<div class="rw-off">${this.esc(l.flag)}</div>` : ''}</td>
                <td><strong>${n(l.capacity)}</strong>${l.capacity != null ? ' Mbps' : ''}<div class="form-hint">${this.esc(l.capacity_basis || '')}</div></td>
                <td>${n(l.latest)}<div class="form-hint">${l.latest_at ? this.esc(l.latest_at.slice(5, 16)) : ''}</div></td>
                <td>${n(l.daytime_median)}</td><td>${n(l.phy_mbps)}</td>
                <td>${l.efficiency_pct != null ? l.efficiency_pct + '%' : '-'}</td>
                <td>${l.traffic_mbps != null ? `${l.traffic_mbps} / ${l.traffic_peak_mbps}` : '-'}</td>
                <td>${l.spare_mbps != null ? `<strong>${l.spare_mbps}</strong>` : '-'}</td>
                <td>${this.spark(l.nightly)}</td>
                <td>${l.runs}${l.failed ? ` <small>(${l.failed} failed)</small>` : ''}</td>
                <td>${l.key ? `<button class="btn btn-small btn-secondary" data-cap-off="1" data-a="${this.esc(l.source)}" data-b="${this.esc(l.target)}"
                    title="Leave this pair out of the overnight schedule">Unschedule</button>` : ''}</td></tr>`).join('')}</tbody></table></div>`;
    },

    async showLink(a, b) {
        const box = document.getElementById('cap-detail');
        box.classList.remove('hidden');
        document.getElementById('cap-detail-title').textContent = `iPerf ${a} → ${b} (7 days)`;
        try {
            const d = await fetch(`/api/capacity/link?${new URLSearchParams({ source: a, target: b, hours: 168 })}`).then(r => r.json());
            const pts = kind => d.runs.filter(r => r.ok && r.kind === kind).map(r => ({ x: new Date(r.timestamp.replace(' ', 'T')), y: r.mbps }));
            const fails = d.runs.filter(r => !r.ok).map(r => ({ x: new Date(r.timestamp.replace(' ', 'T')), y: 0 }));
            const phy = d.runs.filter(r => r.tx_bitrate).map(r => ({ x: new Date(r.timestamp.replace(' ', 'T')), y: r.tx_bitrate }));
            if (this.chart) this.chart.destroy();
            this.chart = new Chart(document.getElementById('cap-chart'), {
                type: 'scatter',
                data: { datasets: [
                    { label: 'Overnight (scheduled)', data: pts('scheduled'), backgroundColor: '#1b4f72', pointRadius: 4 },
                    { label: 'Queue (every 5 min)', data: pts('queue'), backgroundColor: 'rgba(46,134,193,0.5)', pointRadius: 2 },
                    { label: 'Manual', data: pts('manual'), backgroundColor: '#117864', pointRadius: 4 },
                    { label: 'Failed', data: fails, backgroundColor: '#c0392b', pointStyle: 'crossRot', pointRadius: 5 },
                    { label: 'PHY rate', data: phy, type: 'line', borderColor: 'rgba(127,140,141,0.6)', pointRadius: 0, borderWidth: 1, showLine: true }
                ] },
                options: {
                    responsive: true, maintainAspectRatio: false, animation: false,
                    scales: { x: { type: 'time' }, y: { title: { display: true, text: 'Mbps' }, beginAtZero: true } }
                }
            });
            box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } catch (error) {
            console.error('Capacity chart error:', error);
        }
    },

    async runNow() {
        const btn = document.getElementById('cap-run');
        btn.disabled = true;
        try {
            const resp = await fetch('/api/capacity/run', { method: 'POST' });
            const d = await resp.json();
            document.getElementById('cap-hint').textContent = resp.ok
                ? `Started one pass over ${d.planned} link direction(s); press Refresh to follow it.` : (d.error || 'Could not start');
        } finally {
            btn.disabled = false;
        }
    },

    async addLink() {
        const source = document.getElementById('cap-source').value.trim();
        const target = document.getElementById('cap-target').value.trim();
        if (!source || !target) return;
        const resp = await fetch('/api/capacity/links', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source, target })
        });
        const d = await resp.json();
        document.getElementById('cap-hint').textContent = resp.ok ? `Added; ${d.key_links} key link direction(s) now.` : (d.error || 'Failed');
        if (resp.ok) this.fetch();
    },

    async setEnabled(a, b, enabled) {
        await fetch('/api/capacity/links', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: a, target: b, enabled })
        });
        this.fetch();
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

window.ReportPanels = window.ReportPanels || [];
window.ReportPanels.push(CapacityPanel);
