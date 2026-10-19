/**
 * Reports tab: scrub through everything collected in a start/end window.
 *
 * The window is a start time plus a duration (default 24 hours); editing the
 * end directly makes it a custom window. Prev/Next step by the window length
 * and the scrub slider slides the window across the retained history.
 * Filters mirror the Nodes / RF Stats / event-log filters and are applied
 * server-side by /api/reports/window.
 */
const ReportsModule = {
    initialized: false,
    data: null,
    detail: null,          // {kind: 'node'|'link', name|source/target}
    charts: {},
    detailCharts: [],
    sort: {},              // table id -> {key, dir}
    loadTimer: null,
    requestSeq: 0,
    HISTORY_DAYS: 31,

    init() {
        if (this.initialized) return;
        this.initialized = true;

        const now = new Date();
        now.setSeconds(0, 0);
        this.setWindow(new Date(now.getTime() - 24 * 3600 * 1000), 24);

        const $ = id => document.getElementById(id);
        $('rpt-start')?.addEventListener('change', () => this.onStartChange());
        $('rpt-end')?.addEventListener('change', () => this.onEndChange());
        $('rpt-duration')?.addEventListener('change', () => this.onDurationChange());
        $('rpt-prev')?.addEventListener('click', () => this.step(-1));
        $('rpt-next')?.addEventListener('click', () => this.step(1));
        $('rpt-latest')?.addEventListener('click', () => this.latest());
        $('rpt-load')?.addEventListener('click', () => this.load());
        $('rpt-scrub')?.addEventListener('input', () => this.onScrub());
        $('rpt-detail-close')?.addEventListener('click', () => this.closeDetail());
        $('rpt-search')?.addEventListener('input', () => this.scheduleLoad(350));
        for (const id of ['rpt-scope', 'rpt-link-type', 'rpt-sample-type', 'rpt-severity']) {
            $(id)?.addEventListener('change', () => this.load());
        }
        document.querySelectorAll('.rpt-event-cat').forEach(cb =>
            cb.addEventListener('change', () => this.load()));
        $('rpt-show-explained')?.addEventListener('change', () => this.render());

        // Sorting: click any header in a report table.
        document.getElementById('reports-view')?.addEventListener('click', (e) => {
            const exclude = e.target.closest('button[data-exclude-node]');
            if (exclude) {
                this.excludeNode(exclude.dataset.excludeNode, exclude);
                return;
            }
            const th = e.target.closest('th[data-sort]');
            if (th) {
                const tableId = th.closest('[data-table]').dataset.table;
                const cur = this.sort[tableId] || {};
                this.sort[tableId] = {
                    key: th.dataset.sort,
                    dir: cur.key === th.dataset.sort && cur.dir === 'asc' ? 'desc' : 'asc'
                };
                this.render();
                return;
            }
            const row = e.target.closest('tr[data-node], tr[data-source]');
            if (row) {
                if (row.dataset.node) this.openNode(row.dataset.node);
                else this.openLink(row.dataset.source, row.dataset.target);
            }
        });

        this.initTimeline();
    },

    // ------------------------------------------------------------ window --

    toLocalInput(date) {
        const pad = n => String(n).padStart(2, '0');
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
            `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    },

    fromLocalInput(value) {
        const d = value ? new Date(value) : null;
        return d && !isNaN(d.getTime()) ? d : null;
    },

    getStart() { return this.fromLocalInput(document.getElementById('rpt-start').value); },
    getEnd() { return this.fromLocalInput(document.getElementById('rpt-end').value); },

    /** Set start and, for a preset duration (hours), the matching end. */
    setWindow(start, hours, end = null) {
        const durationSel = document.getElementById('rpt-duration');
        if (hours !== null && hours !== undefined) {
            end = new Date(start.getTime() + hours * 3600 * 1000);
            durationSel.value = String(hours);
        } else {
            const spanHours = (end - start) / 3600000;
            const match = [...durationSel.options].find(o => Number(o.value) === spanHours);
            durationSel.value = match ? match.value : 'custom';
        }
        document.getElementById('rpt-start').value = this.toLocalInput(start);
        document.getElementById('rpt-end').value = this.toLocalInput(end);
        this.syncScrub();
    },

    durationHours() {
        const v = document.getElementById('rpt-duration').value;
        return v === 'custom' ? null : Number(v);
    },

    onStartChange() {
        const start = this.getStart();
        if (!start) return;
        const hours = this.durationHours();
        if (hours) {
            this.setWindow(start, hours);
        } else {
            // Custom window: keep its length.
            const span = this.getEnd() - this.lastStart;
            this.setWindow(start, null, new Date(start.getTime() + (span > 0 ? span : 24 * 3600 * 1000)));
        }
        this.load();
    },

    onEndChange() {
        const start = this.getStart();
        const end = this.getEnd();
        if (!start || !end) return;
        if (end <= start) {
            this.setStatus('End must be after start');
            return;
        }
        this.setWindow(start, null, end);
        this.load();
    },

    onDurationChange() {
        const start = this.getStart();
        const hours = this.durationHours();
        if (start && hours) {
            this.setWindow(start, hours);
            this.load();
        }
    },

    step(direction) {
        const start = this.getStart();
        const end = this.getEnd();
        if (!start || !end) return;
        const span = end - start;
        this.setWindow(new Date(start.getTime() + direction * span), this.durationHours(),
            new Date(end.getTime() + direction * span));
        this.load();
    },

    latest() {
        const start = this.getStart();
        const end = this.getEnd();
        const span = (start && end) ? end - start : 24 * 3600 * 1000;
        const now = new Date();
        now.setSeconds(0, 0);
        this.setWindow(new Date(now.getTime() - span), this.durationHours(), now);
        this.load();
    },

    /** Scrub range: from the oldest retained data to "window ends now". */
    scrubBounds() {
        const now = new Date();
        const start = this.getStart();
        const end = this.getEnd();
        const span = (start && end) ? end - start : 24 * 3600 * 1000;
        const min = now.getTime() - this.HISTORY_DAYS * 24 * 3600 * 1000;
        const max = Math.max(min, now.getTime() - span);
        return { min, max, span };
    },

    syncScrub() {
        const scrub = document.getElementById('rpt-scrub');
        const start = this.getStart();
        if (!scrub || !start) return;
        this.lastStart = start;
        const { min, max } = this.scrubBounds();
        const frac = max > min ? (start.getTime() - min) / (max - min) : 1;
        scrub.value = String(Math.round(Math.min(1, Math.max(0, frac)) * 1000));
        const fmt = d => d.toLocaleDateString([], { month: 'short', day: 'numeric' });
        document.getElementById('rpt-scrub-min').textContent = fmt(new Date(min));
        document.getElementById('rpt-scrub-max').textContent = 'now';
    },

    onScrub() {
        const scrub = document.getElementById('rpt-scrub');
        const { min, max, span } = this.scrubBounds();
        const t = min + (Number(scrub.value) / 1000) * (max - min);
        const start = new Date(Math.round(t / 60000) * 60000);
        document.getElementById('rpt-start').value = this.toLocalInput(start);
        document.getElementById('rpt-end').value = this.toLocalInput(new Date(start.getTime() + span));
        this.lastStart = start;
        this.scheduleLoad(400);
    },

    // -------------------------------------------------------------- load --

    scheduleLoad(delay) {
        clearTimeout(this.loadTimer);
        this.loadTimer = setTimeout(() => this.load(), delay);
    },

    queryString() {
        const params = new URLSearchParams({
            start: document.getElementById('rpt-start').value,
            end: document.getElementById('rpt-end').value,
            scope: document.getElementById('rpt-scope').value,
            link_type: document.getElementById('rpt-link-type').value,
            sample_type: document.getElementById('rpt-sample-type').value,
            severity: document.getElementById('rpt-severity').value,
            search: document.getElementById('rpt-search').value.trim()
        });
        const cats = [...document.querySelectorAll('.rpt-event-cat')]
            .filter(cb => cb.checked).map(cb => cb.value);
        params.set('events', cats.length ? cats.join(',') : 'none');
        return params.toString();
    },

    async load() {
        clearTimeout(this.loadTimer);
        const seq = ++this.requestSeq;
        this.setStatus('Loading...');
        try {
            const resp = await fetch(`/api/reports/window?${this.queryString()}`);
            const data = await resp.json();
            if (seq !== this.requestSeq) return;  // a newer request superseded this one
            if (!resp.ok) {
                this.setStatus(data.error || 'Failed to load');
                return;
            }
            this.data = data;
            this.render();
            this.setStatus(`Loaded ${new Date().toLocaleTimeString()}`);
            if (typeof CollectorHealth !== 'undefined') {
                CollectorHealth.load(document.getElementById('rpt-start').value, document.getElementById('rpt-end').value);
            }
            if (this.detail) this.reloadDetail();
        } catch (error) {
            if (seq === this.requestSeq) this.setStatus('Failed to load');
            console.error('Reports load error:', error);
        }
    },

    // ------------------------------------------------------------ render --

    render() {
        if (!this.data) return;
        this.renderSummary();
        this.renderTimeline();
        const d = this.data;
        const bucketMin = d.window.bucket_seconds / 60;
        document.getElementById('rpt-bucket-hint').textContent =
            `${bucketMin >= 60 ? (bucketMin / 60) + ' h' : bucketMin + ' min'} buckets`;
        document.getElementById('rpt-transitions-hint').textContent =
            `${d.transitions.length}${d.transitions_truncated ? '+ (newest shown)' : ''} changes`;
        document.getElementById('rpt-events-hint').textContent =
            `${d.events.length}${d.events_truncated ? '+ (newest shown)' : ''} events`;

        this.renderUnreachable();
        const showExplained = document.getElementById('rpt-show-explained')?.checked;
        const nodeRows = showExplained ? d.nodes : d.nodes.filter(n => !n.explained);
        this.renderTable('rpt-nodes', nodeRows, [
            ['name', 'Node'], ['ip', 'IP'], ['model', 'Model'],
            ['polls', 'Polls'], ['reachable_pct', 'Reachable %', 1], ['unreachable', 'Unreachable'],
            ['cause_label', 'Unreachable Cause'],
            ['degraded', 'Degraded'], ['avg_response_ms', 'Avg Resp (ms)', 0],
            ['max_response_ms', 'Max Resp (ms)', 0], ['avg_load1', 'Avg Load', 2],
            ['max_load1', 'Max Load', 2], ['reboots', 'Reboots'], ['warnings', 'Warnings'],
            ['mesh_probes', 'Mesh Probes'], ['last_sample', 'Last Sample']
        ], row => `data-node="${this.esc(row.name)}"`, { key: 'name', dir: 'asc' });

        this.renderTable('rpt-links', d.links, [
            ['source_node', 'Source'], ['target_node', 'Target'], ['link_type', 'Type'],
            ['samples', 'Samples'], ['avg_quality', 'Avg Q %', 0], ['min_quality', 'Min Q %', 0],
            ['avg_snr', 'Avg SNR', 1], ['min_snr', 'Min SNR', 1], ['avg_rev_snr', 'Rev SNR', 1],
            ['avg_ping', 'Avg Ping (ms)', 1], ['max_ping', 'Max Ping', 1], ['avg_loss', 'Avg Loss %', 1],
            ['avg_jitter', 'Jitter', 1], ['iperf_tests', 'iPerf'], ['avg_throughput_tx', 'TX Mbps', 2],
            ['avg_throughput_rx', 'RX Mbps', 2], ['blocked_samples', 'Blocked'],
            ['trouble_transitions', 'Drops/Blocks']
        ], row => `data-source="${this.esc(row.source_node)}" data-target="${this.esc(row.target_node)}"`,
            { key: 'source_node', dir: 'asc' });

        this.renderTable('rpt-transitions', d.transitions, [
            ['timestamp', 'Time'], ['source_node', 'Source'], ['target_node', 'Target'],
            ['link_type', 'Type'], ['state', 'State'], ['quality', 'Q %', 0], ['snr', 'SNR', 1],
            ['rev_snr', 'Rev SNR', 1], ['blocked_reason', 'Block Reason'], ['detail', 'Detail'],
            ['origin', 'Origin']
        ], row => `data-source="${this.esc(row.source_node)}" data-target="${this.esc(row.target_node)}"`,
            { key: 'timestamp', dir: 'desc' });

        this.renderTable('rpt-events', d.events, [
            ['timestamp', 'Time'], ['severity', 'Severity'], ['event_type', 'Type'],
            ['node_name', 'Node'], ['details', 'Details']
        ], row => row.node_name ? `data-node="${this.esc(row.node_name)}"` : '',
            { key: 'timestamp', dir: 'desc' });

        const voipRows = [
            ...d.voip_tests.map(t => ({
                when: t.ran_at, kind: `Call test (${t.codec})`, source: t.source_node,
                target: t.target_node,
                result: `MOS ${this.fmt(t.mos, 2)} ${t.mos_label || ''}; ${this.fmt(t.latency_ms, 0)} ms, ` +
                    `jitter ${this.fmt(t.jitter_ms, 1)} ms, loss ${this.fmt(t.loss_pct, 1)}%`
            })),
            ...d.voip_sessions.map(s => ({
                when: (s.started_at || '').replace('T', ' ').slice(0, 19),
                kind: `Session #${s.id} (${s.mode})`, source: s.source, target: s.target,
                result: s.ended_at ? `ended ${(s.ended_at || '').replace('T', ' ').slice(0, 19)}` : 'running'
            }))
        ];
        this.renderTable('rpt-voip', voipRows, [
            ['when', 'Time'], ['kind', 'Kind'], ['source', 'Source'], ['target', 'Target'],
            ['result', 'Result']
        ], () => '', { key: 'when', dir: 'desc' });
    },

    // Why nodes were unreachable (unreachable.py); explained causes hidden by default.
    renderUnreachable() {
        const el = document.getElementById('rpt-unreachable');
        if (!el) return;
        const all = this.data.unreachable || [];
        const showExplained = document.getElementById('rpt-show-explained')?.checked;
        const rows = showExplained ? all : all.filter(r => !r.explained);
        const hidden = all.length - rows.length;
        const hint = document.getElementById('rpt-unreach-hint');
        if (hint) {
            hint.textContent = `${all.filter(r => !r.explained).length} real, ` +
                `${all.filter(r => r.explained).length} explained` +
                (hidden ? ` (${hidden} hidden)` : '');
        }
        if (!rows.length) {
            el.innerHTML = `<p class="log-empty">${all.length
                ? 'Every unreachable node in this window has an explanation. Tick "Show explained" to list them.'
                : 'No failed polls in this window.'}</p>`;
            return;
        }
        const body = rows.map(r => {
            const counts = [`${r.failed_polls} failed poll(s)`];
            if (r.answered_polls) counts.push(`${r.answered_polls} answered`);
            if (r.probe_ok || r.probe_fail) counts.push(`probes ${r.probe_ok} ok / ${r.probe_fail} failed`);
            const action = r.can_exclude
                ? `<button class="btn btn-small btn-secondary" data-exclude-node="${this.esc(r.name)}"
                     title="Never poll this node (scan boundary: exclude)">Exclude from scanning</button>`
                : (r.boundary ? `<small>boundary: ${this.esc(r.boundary)}</small>` : '');
            return `<tr class="rpt-clickable rpt-cause-${this.esc(r.severity)}" data-node="${this.esc(r.name)}">
                <td><strong>${this.esc(r.name)}</strong></td>
                <td><span class="rpt-cause-badge rpt-cause-${this.esc(r.severity)}">${this.esc(r.cause_label)}</span></td>
                <td class="rpt-cause-detail">${this.esc(r.detail)}
                    ${r.evidence.length ? `<ul>${r.evidence.map(x => `<li>${this.esc(x)}</li>`).join('')}</ul>` : ''}</td>
                <td>${this.esc(counts.join(', '))}</td>
                <td>${this.esc(r.last_success || 'never')}</td>
                <td>${action}</td></tr>`;
        }).join('');
        el.innerHTML = `<div class="table-scroll-wrapper rpt-table-wrap"><table class="info-table rpt-table rpt-unreach-table">
            <thead><tr><th>Node</th><th>Cause</th><th>Why</th><th>Polls</th><th>Last Answered</th><th></th></tr></thead>
            <tbody>${body}</tbody></table></div>`;
    },

    async excludeNode(name, button) {
        if (!confirm(`Stop scanning ${name}? It will never be polled (scan boundary "exclude"). ` +
                     'You can clear this on the Diagnostics tab.')) return;
        button.disabled = true;
        try {
            const resp = await fetch(`/api/scan-boundaries/${encodeURIComponent(name)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: 'exclude' })
            });
            const result = await resp.json();
            if (!resp.ok || !result.success) throw new Error(result.error || 'Failed to exclude node');
            for (const r of this.data.unreachable || []) {
                if (r.name === name) { r.can_exclude = false; r.boundary = 'exclude'; }
            }
            this.renderUnreachable();
            this.setStatus(`${name} excluded from scanning (next scan)`);
        } catch (error) {
            button.disabled = false;
            this.setStatus(`Exclude failed: ${error.message}`);
        }
    },

    renderSummary() {
        const s = this.data.summary;
        const tiles = [
            ['Nodes', s.nodes],
            ['Links', s.links],
            ['Node Polls', s.polls],
            ['Reachable', s.reachable_pct === null ? '-' : `${s.reachable_pct}%`],
            ['Down / Intermittent', `${s.really_down_nodes ?? 0} / ${s.intermittent_nodes ?? 0}`],
            ['Explained Unreachable', `${s.explained_unreachable_nodes ?? 0} nodes`],
            ['Link Samples', s.link_samples],
            ['Avg Quality', s.avg_quality === null ? '-' : `${s.avg_quality}%`],
            ['iPerf Tests', s.iperf_tests],
            ['State Changes', s.transitions],
            ['Events', s.events],
            ['Warnings', s.warnings],
            ['Starting Node Lost', s.gateway_outages],
            ['VoIP Tests / Sessions', `${s.voip_tests} / ${s.voip_sessions}`]
        ];
        document.getElementById('rpt-summary').innerHTML = tiles.map(([label, value]) =>
            `<div class="rpt-tile"><span class="rpt-tile-label">${this.esc(label)}</span>` +
            `<span class="rpt-tile-value">${this.esc(value)}</span></div>`).join('');
    },

    renderTable(id, rows, columns, rowAttrs, defaultSort) {
        const el = document.getElementById(id);
        if (!el) return;
        el.dataset.table = id;
        if (!rows || !rows.length) {
            el.innerHTML = '<p class="log-empty">No data in this window.</p>';
            return;
        }
        const sort = this.sort[id] || defaultSort;
        const sorted = [...rows].sort((a, b) => {
            const av = a[sort.key], bv = b[sort.key];
            if (av === bv) return 0;
            if (av === null || av === undefined) return 1;
            if (bv === null || bv === undefined) return -1;
            const cmp = (typeof av === 'number' && typeof bv === 'number')
                ? av - bv : String(av).localeCompare(String(bv));
            return sort.dir === 'desc' ? -cmp : cmp;
        });
        const head = columns.map(([key, label]) => {
            const arrow = sort.key === key ? (sort.dir === 'desc' ? ' &#9660;' : ' &#9650;') : '';
            return `<th data-sort="${key}">${this.esc(label)}${arrow}</th>`;
        }).join('');
        const body = sorted.map(row => {
            const attrs = rowAttrs(row);
            const cells = columns.map(([key, , digits]) => {
                const v = row[key];
                const text = digits !== undefined ? this.fmt(v, digits) : (v ?? '-');
                const cls = key === 'severity' || key === 'state' ? ` class="rpt-${this.esc(v)}"` : '';
                return `<td${cls}>${this.esc(text)}</td>`;
            }).join('');
            return `<tr ${attrs}${attrs ? ' class="rpt-clickable"' : ''}>${cells}</tr>`;
        }).join('');
        el.innerHTML = `<table class="info-table rpt-table"><thead><tr>${head}</tr></thead>` +
            `<tbody>${body}</tbody></table>`;
    },

    // ------------------------------------------------------------ charts --

    timeScale(win) {
        return {
            type: 'time',
            min: new Date(win.start.replace(' ', 'T')),
            max: new Date(win.end.replace(' ', 'T')),
            time: { displayFormats: { minute: 'HH:mm', hour: 'MMM d HH:mm', day: 'MMM d' } }
        };
    },

    initTimeline() {
        const ctx = document.getElementById('rpt-timeline-chart');
        if (!ctx || typeof Chart === 'undefined') return;
        this.charts.timeline = new Chart(ctx, {
            type: 'bar',
            data: {
                datasets: [
                    { label: 'Warnings', backgroundColor: '#f39c12', data: [], stack: 'c' },
                    { label: 'Info events', backgroundColor: '#95a5a6', data: [], stack: 'c' },
                    { label: 'Link drops/blocks', backgroundColor: '#e74c3c', data: [], stack: 'c' },
                    { label: 'Unreachable polls', backgroundColor: '#8e44ad', data: [], stack: 'c' },
                    { label: 'Explained unreachable', backgroundColor: '#d7bde2', data: [], stack: 'c', hidden: true },
                    {
                        label: 'Avg link quality %', type: 'line', borderColor: '#27ae60',
                        backgroundColor: 'transparent', data: [], yAxisID: 'q', tension: 0.3,
                        pointRadius: 0, spanGaps: true
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { intersect: false, mode: 'index' },
                scales: {
                    x: { type: 'time', stacked: true, offset: false },
                    y: { stacked: true, beginAtZero: true, title: { display: true, text: 'count' } },
                    q: { position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false },
                         title: { display: true, text: 'quality %' } }
                },
                onClick: (evt, elements) => {
                    // Zoom into the clicked bucket's surroundings.
                    if (!elements.length || !this.data) return;
                    const t = this.data.timeline[elements[0].index];
                    if (!t) return;
                    const center = new Date(t.t.replace(' ', 'T'));
                    const hours = Math.max(1, (this.data.window.bucket_seconds * 12) / 3600);
                    const preset = [1, 3, 6, 12, 24, 72, 168, 720].find(h => h >= hours) || 720;
                    this.setWindow(new Date(center.getTime() - preset * 1800 * 1000), preset);
                    this.load();
                }
            }
        });
    },

    renderTimeline() {
        const chart = this.charts.timeline;
        if (!chart) return;
        const rows = this.data.timeline;
        const pts = key => rows.map(r => ({ x: new Date(r.t.replace(' ', 'T')), y: r[key] }));
        chart.data.datasets[0].data = pts('warnings');
        chart.data.datasets[1].data = pts('info');
        chart.data.datasets[2].data = pts('link_trouble');
        chart.data.datasets[3].data = pts('unreachable');
        chart.data.datasets[4].data = pts('explained_unreachable');
        chart.data.datasets[5].data = pts('avg_quality');
        chart.options.scales.x = { ...this.timeScale(this.data.window), stacked: true, offset: false };
        chart.update('none');
    },

    destroyDetailCharts() {
        this.detailCharts.forEach(c => c.destroy());
        this.detailCharts = [];
    },

    addDetailChart(title, datasets, win, extraScales = {}) {
        const grid = document.getElementById('rpt-detail-charts');
        const card = document.createElement('div');
        card.className = 'chart-card';
        card.innerHTML = `<h4>${this.esc(title)}</h4><canvas></canvas>`;
        grid.appendChild(card);
        this.detailCharts.push(new Chart(card.querySelector('canvas'), {
            type: 'line',
            data: { datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { intersect: false, mode: 'index' },
                scales: { x: this.timeScale(win), y: { beginAtZero: true }, ...extraScales },
                plugins: { legend: { display: true, position: 'top' } }
            }
        }));
    },

    line(label, color, rows, key, opts = {}) {
        return {
            label, borderColor: color, backgroundColor: 'transparent', tension: 0.3,
            pointRadius: rows.length > 60 ? 0 : 2, spanGaps: false,
            data: rows.filter(r => r[key] !== null && r[key] !== undefined)
                .map(r => ({ x: new Date(r.t.replace(' ', 'T')), y: r[key] })),
            ...opts
        };
    },

    windowParams() {
        return new URLSearchParams({
            start: document.getElementById('rpt-start').value,
            end: document.getElementById('rpt-end').value,
            sample_type: document.getElementById('rpt-sample-type').value
        }).toString();
    },

    openNode(name) {
        this.detail = { kind: 'node', name };
        this.reloadDetail(true);
    },

    openLink(source, target) {
        this.detail = { kind: 'link', source, target };
        this.reloadDetail(true);
    },

    closeDetail() {
        this.detail = null;
        this.destroyDetailCharts();
        document.getElementById('rpt-detail').classList.add('hidden');
    },

    async reloadDetail(scroll = false) {
        const d = this.detail;
        if (!d) return;
        const section = document.getElementById('rpt-detail');
        const url = d.kind === 'node'
            ? `/api/reports/window/node/${encodeURIComponent(d.name)}?${this.windowParams()}`
            : `/api/reports/window/link/${encodeURIComponent(d.source)}/${encodeURIComponent(d.target)}?${this.windowParams()}`;
        try {
            const resp = await fetch(url);
            const data = await resp.json();
            if (!resp.ok || this.detail !== d) return;
            this.destroyDetailCharts();
            document.getElementById('rpt-detail-charts').innerHTML = '';
            section.classList.remove('hidden');
            if (d.kind === 'node') this.renderNodeDetail(data);
            else this.renderLinkDetail(data);
            if (scroll) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } catch (error) {
            console.error('Report detail error:', error);
        }
    },

    renderNodeDetail(data) {
        const rows = data.series;
        const win = data.window;
        document.getElementById('rpt-detail-title').textContent = `Node: ${data.node}`;
        this.addDetailChart('Poll Response Time (ms)', [
            this.line('Avg', '#3498db', rows, 'avg_response_ms'),
            this.line('Max', '#e74c3c', rows, 'max_response_ms')
        ], win);
        this.addDetailChart('Polls', [
            this.line('Polls', '#27ae60', rows, 'polls', { type: 'bar', backgroundColor: 'rgba(39,174,96,0.5)' }),
            this.line('Unreachable', '#8e44ad', rows, 'unreachable', { type: 'bar', backgroundColor: 'rgba(142,68,173,0.8)' }),
            this.line('Degraded', '#f39c12', rows, 'degraded', { type: 'bar', backgroundColor: 'rgba(243,156,18,0.8)' })
        ], win);
        this.addDetailChart('Load (1 min)', [this.line('Load', '#34495e', rows, 'load1')], win);
        this.addDetailChart('Mesh Probes (via neighbors)', [
            this.line('Reached', '#27ae60', rows, 'mesh_ok', { type: 'bar', backgroundColor: 'rgba(39,174,96,0.6)' }),
            this.line('Failed', '#e74c3c', rows, 'mesh_fail', { type: 'bar', backgroundColor: 'rgba(231,76,60,0.8)' })
        ], win);
        this.renderDetailList(data.events, [['timestamp', 'Time'], ['severity', 'Severity'],
            ['event_type', 'Type'], ['details', 'Details']], 'Events');
    },

    renderLinkDetail(data) {
        const rows = data.series;
        const win = data.window;
        document.getElementById('rpt-detail-title').textContent = `Link: ${data.source} → ${data.target}`;
        this.addDetailChart('Link Quality (%)', [
            this.line('Avg', '#3498db', rows, 'quality'),
            this.line('Min', '#e74c3c', rows, 'min_quality')
        ], win, { y: { min: 0, max: 100 } });
        this.addDetailChart('SNR (dB)', [
            this.line('Avg', '#27ae60', rows, 'snr'),
            this.line('Min', '#e67e22', rows, 'min_snr'),
            this.line('Reverse', '#9b59b6', rows, 'rev_snr')
        ], win);
        this.addDetailChart('Ping (ms) / Loss (%)', [
            this.line('Min', '#27ae60', rows, 'ping_min'),
            this.line('Avg', '#f39c12', rows, 'ping_avg'),
            this.line('Max', '#e74c3c', rows, 'ping_max'),
            this.line('Loss %', '#8e44ad', rows, 'ping_loss', { yAxisID: 'loss', borderDash: [4, 3] })
        ], win, { loss: { position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false } } });
        const tp = data.throughput;
        this.addDetailChart(`Throughput (Mbps) — ${tp.length} iPerf test(s)`, [
            this.line('TX', '#3498db', tp, 'throughput_tx', { showLine: tp.length > 1, pointRadius: 4 }),
            this.line('RX', '#9b59b6', tp, 'throughput_rx', { showLine: tp.length > 1, pointRadius: 4 })
        ], win);
        this.renderDetailList(data.transitions, [['timestamp', 'Time'], ['state', 'State'],
            ['quality', 'Q %'], ['snr', 'SNR'], ['blocked_reason', 'Block Reason'],
            ['detail', 'Detail'], ['origin', 'Origin']], 'State Changes');
    },

    renderDetailList(rows, columns, title) {
        const el = document.getElementById('rpt-detail-extra');
        if (!rows.length) {
            el.innerHTML = `<p class="log-empty">No ${this.esc(title.toLowerCase())} in this window.</p>`;
            return;
        }
        const head = columns.map(([, label]) => `<th>${this.esc(label)}</th>`).join('');
        const body = rows.map(r => '<tr>' + columns.map(([k]) => {
            const cls = k === 'severity' || k === 'state' ? ` class="rpt-${this.esc(r[k])}"` : '';
            return `<td${cls}>${this.esc(r[k] ?? '-')}</td>`;
        }).join('') + '</tr>').join('');
        el.innerHTML = `<h4 class="rpt-subhead">${this.esc(title)} (${rows.length})</h4>` +
            `<div class="table-scroll-wrapper rpt-table-wrap"><table class="info-table rpt-table">` +
            `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
    },

    // ----------------------------------------------------------- helpers --

    fmt(value, digits = 1) {
        if (value === null || value === undefined || value === '') return '-';
        const n = Number(value);
        return isNaN(n) ? String(value) : n.toFixed(digits);
    },

    setStatus(text) {
        const el = document.getElementById('rpt-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
