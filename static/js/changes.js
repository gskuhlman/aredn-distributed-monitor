/**
 * Changes tab: change log (manual entries plus channel/config/firmware
 * changes imported from scanner events) and a before/after comparison of
 * every RF link of the changed node. All statistics come from
 * /api/changes/<id>/compare (change_log.py); this only renders them.
 */
const ChangesModule = {
    initialized: false,
    changes: [],
    selected: new Set(),
    infrastructure: new Set(),
    current: null,
    charts: [],

    init() {
        if (this.initialized) return;
        this.initialized = true;
        const on = (id, ev, fn) => document.getElementById(id)?.addEventListener(ev, fn);
        on('chg-search', 'input', () => this.renderList());
        on('chg-scope', 'change', () => this.renderList());
        on('chg-category', 'change', () => this.renderList());
        on('chg-hours', 'change', () => this.load());
        on('chg-add-toggle', 'click', () => this.toggleForm());
        on('chg-cancel', 'click', () => this.toggleForm(false));
        on('chg-save', 'click', () => this.save());
        on('chg-window', 'change', () => this.current && this.compare(this.current.id));
        document.getElementById('chg-list')?.addEventListener('click', e => {
            const btn = e.target.closest('[data-chg-action]');
            if (!btn) return;
            const id = Number(btn.dataset.id);
            if (btn.dataset.chgAction === 'compare') this.compare(id);
            else if (btn.dataset.chgAction === 'delete') this.remove(id);
        });
    },

    async load() {
        const hours = document.getElementById('chg-hours')?.value;
        this.setStatus('Loading...');
        try {
            const [chg, sel, infra] = await Promise.all([
                fetch('/api/changes' + (hours && hours !== 'all' ? `?hours=${hours}` : '')).then(r => r.json()),
                fetch('/api/nodes/selected').then(r => r.json()).catch(() => ({})),
                fetch('/api/nodes/infrastructure').then(r => r.json()).catch(() => ({}))
            ]);
            this.changes = chg.changes || [];
            this.selected = new Set(sel.selected_nodes || []);
            this.infrastructure = new Set(Object.keys(infra.infrastructure_nodes || {}));
            this.renderList();
            this.loadNodeNames();
        } catch (error) {
            console.error('Changes load error:', error);
            this.setStatus('Failed to load changes');
        }
    },

    async loadNodeNames() {
        const list = document.getElementById('chg-node-names');
        if (!list || list.childElementCount) return;
        try {
            const nodes = await fetch('/api/nodes/all').then(r => r.json());
            list.innerHTML = (nodes || []).map(n => `<option value="${this.esc(n.name || n)}">`).join('');
        } catch (e) { /* datalist is a convenience only */ }
    },

    filtered() {
        const search = (document.getElementById('chg-search')?.value || '').trim().toLowerCase();
        const scope = document.getElementById('chg-scope')?.value || 'all';
        const category = document.getElementById('chg-category')?.value || 'all';
        return this.changes.filter(c => {
            const names = [c.node_name, c.peer_node].filter(Boolean);
            if (scope === 'selected' && !names.some(n => this.selected.has(n))) return false;
            if (scope === 'infrastructure' && !names.some(n => this.infrastructure.has(n))) return false;
            if (category !== 'all' && c.category !== category) return false;
            if (search && !`${names.join(' ')} ${c.description || ''}`.toLowerCase().includes(search)) return false;
            return true;
        });
    },

    renderList() {
        const el = document.getElementById('chg-list');
        if (!el) return;
        const rows = this.filtered();
        this.setStatus(`${rows.length} of ${this.changes.length} change(s)`);
        if (!rows.length) {
            el.innerHTML = `<p class="log-empty">No changes recorded. Channel, radio-setting and firmware
                changes are added automatically when a scan sees them; use "Add change" for power,
                antenna or other work.</p>`;
            return;
        }
        el.innerHTML = `<table class="info-table diag-table chg-table">
            <thead><tr><th>Time</th><th>Node</th><th>Peer</th><th>Category</th><th>Description</th>
            <th>Source</th><th></th></tr></thead>
            <tbody>${rows.map(c => `<tr class="${this.current && this.current.id === c.id ? 'chg-active' : ''}">
                <td>${this.esc(c.timestamp)}</td>
                <td><a href="/nodes/${encodeURIComponent(c.node_name)}">${this.esc(c.node_name)}</a>
                    ${this.infrastructure.has(c.node_name) ? '<small class="chg-infra">infra</small>' : ''}</td>
                <td>${c.peer_node ? `<a href="/nodes/${encodeURIComponent(c.peer_node)}">${this.esc(c.peer_node)}</a>` : ''}</td>
                <td><span class="chg-cat chg-cat-${this.esc(c.category)}">${this.esc(c.category)}</span></td>
                <td class="chg-desc">${this.esc(c.description || '')}${c.author ? ` <small>(${this.esc(c.author)})</small>` : ''}</td>
                <td>${this.esc(c.source)}</td>
                <td class="chg-actions">
                    <button class="btn btn-small btn-primary" data-chg-action="compare" data-id="${c.id}">Before / after</button>
                    <button class="btn btn-small btn-secondary" data-chg-action="delete" data-id="${c.id}" title="Remove from the log">Delete</button>
                </td></tr>`).join('')}</tbody></table>`;
    },

    toggleForm(show) {
        const form = document.getElementById('chg-form');
        if (!form) return;
        const visible = show ?? form.hidden;
        form.hidden = !visible;
        if (visible) {
            const t = document.getElementById('chg-time');
            if (t) t.value = this.localInputValue(new Date());
            document.getElementById('chg-node')?.focus();
        }
    },

    /** Open the form prefilled (used by the node page link and RF Stats). */
    openForm(node, peer) {
        this.toggleForm(true);
        if (node) document.getElementById('chg-node').value = node;
        if (peer) document.getElementById('chg-peer').value = peer;
    },

    async save() {
        const val = id => (document.getElementById(id)?.value || '').trim();
        const body = {
            node_name: val('chg-node'), peer_node: val('chg-peer'), category: val('chg-cat'),
            description: val('chg-desc'), author: val('chg-author'),
            timestamp: val('chg-time').replace('T', ' ')
        };
        if (!body.node_name || !body.description) {
            this.setStatus('Node and description are required');
            return;
        }
        try {
            const resp = await fetch('/api/changes', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Save failed');
            ['chg-desc', 'chg-peer'].forEach(id => { const e = document.getElementById(id); if (e) e.value = ''; });
            this.toggleForm(false);
            await this.load();
            this.compare(data.change.id);
        } catch (error) {
            this.setStatus(error.message);
        }
    },

    async remove(id) {
        const c = this.changes.find(x => x.id === id);
        if (!c) return;
        try {
            const resp = await fetch(`/api/changes/${id}`, { method: 'DELETE' });
            if (!resp.ok) throw new Error('Delete failed');
            if (this.current && this.current.id === id) this.clearCompare();
            await this.load();
        } catch (error) {
            this.setStatus(error.message);
        }
    },

    clearCompare() {
        this.current = null;
        this.destroyCharts();
        const el = document.getElementById('chg-compare');
        if (el) el.innerHTML = '';
    },

    async compare(id) {
        const hours = document.getElementById('chg-window')?.value || 24;
        const el = document.getElementById('chg-compare');
        this.current = this.changes.find(c => c.id === id) || { id };
        this.renderList();
        el.innerHTML = '<p class="loading">Comparing...</p>';
        try {
            const resp = await fetch(`/api/changes/${id}/compare?hours=${hours}`);
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Compare failed');
            this.renderCompare(data);
            el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } catch (error) {
            el.innerHTML = `<p class="log-empty">${this.esc(error.message)}</p>`;
        }
    },

    renderCompare(d) {
        this.destroyCharts();
        const el = document.getElementById('chg-compare');
        const c = d.change || {};
        const s = d.summary || {};
        const reach = Object.entries(d.reachability || {}).map(([n, r]) =>
            `<tr><td>${this.esc(n)}</td><td>${this.fmt(r.before, '%')}</td><td>${this.fmt(r.after, '%')}</td></tr>`).join('');
        el.innerHTML = `
            <div class="diag-section">
                <h3>Before / after: ${this.esc(c.node_name || d.node)}${c.peer_node ? ' ↔ ' + this.esc(c.peer_node) : ''}
                    <small class="form-hint">${this.esc(c.category || '')} · ${this.esc(c.description || '')}</small></h3>
                <p class="form-hint">Change at ${this.esc(d.change_time)}. Before ${this.esc(d.before_start)} → change,
                    after change → ${this.esc(d.after_end)}${d.partial
                        ? ` (only ${d.after_hours_available} h have elapsed, so the before window is shortened to match)` : ''}.</p>
                <div class="chg-summary">
                    <span class="chg-verdict chg-v-better">${s.better || 0} better</span>
                    <span class="chg-verdict chg-v-worse">${s.worse || 0} worse</span>
                    <span class="chg-verdict chg-v-same">${s.same || 0} unchanged</span>
                    ${s['n/a'] ? `<span class="chg-verdict chg-v-na">${s['n/a']} no data</span>` : ''}
                </div>
                ${d.links.length ? '' : '<p class="log-empty">No RF link samples in either window.</p>'}
                ${d.links.map((l, i) => this.renderLink(l, i)).join('')}
                ${reach ? `<details class="diag-details"><summary>Node reachability (scanner polls)</summary>
                    <table class="info-table diag-table"><thead><tr><th>Node</th><th>Before</th><th>After</th></tr></thead>
                    <tbody>${reach}</tbody></table></details>` : ''}
            </div>`;
        el.querySelectorAll('details[data-link]').forEach(det => {
            const draw = () => { if (det.open && !det.dataset.drawn) { det.dataset.drawn = '1'; this.drawCharts(det, d.links[Number(det.dataset.link)]); } };
            det.addEventListener('toggle', draw);
            draw();
        });
    },

    renderLink(link, index) {
        const dirs = link.directions.map(dir => `
            <h4 class="chg-dir">${this.esc(dir.source)} hears ${this.esc(dir.target)}
                <small class="form-hint">${dir.before_samples} sample(s) before · ${dir.after_samples} after</small></h4>
            <div class="table-scroll-wrapper"><table class="info-table diag-table chg-metrics">
                <thead><tr><th>Metric</th><th>Before</th><th>After</th><th>Change</th><th></th></tr></thead>
                <tbody>${dir.metrics.map(m => `<tr class="chg-row-${m.verdict.replace('/', '')}">
                    <td>${this.esc(m.label)}</td><td>${this.fmt(m.before)}</td><td>${this.fmt(m.after)}</td>
                    <td>${m.delta != null ? (m.delta > 0 ? '+' : '') + this.fmt(m.delta) : ''}</td>
                    <td>${['better', 'worse', 'same'].includes(m.verdict) ? `<span class="chg-verdict chg-v-${m.verdict}">${m.verdict}</span>` : ''}</td>
                </tr>`).join('')}</tbody></table></div>
            <div class="chg-charts" data-dir="${this.esc(dir.source)}"></div>`).join('');
        return `<details class="diag-details" data-link="${index}" ${index < 3 ? 'open' : ''}>
            <summary>${this.esc(link.peer)} <span class="chg-verdict chg-v-${link.verdict.replace('/', '')}">${this.esc(link.verdict)}</span>
                <small class="form-hint">${link.better} better · ${link.worse} worse</small></summary>
            ${dirs}</details>`;
    },

    drawCharts(det, link) {
        if (typeof Chart === 'undefined') return;
        det.querySelectorAll('.chg-charts').forEach((box, i) => {
            const dir = link.directions[i];
            if (!dir) return;
            const make = (title, keys) => {
                const wrap = document.createElement('div');
                wrap.className = 'chg-chart';
                const canvas = document.createElement('canvas');
                wrap.appendChild(canvas);
                box.appendChild(wrap);
                const colors = ['#2e86c1', '#c0392b'];
                const datasets = [];
                keys.forEach((k, ki) => {
                    for (const side of ['before', 'after']) {
                        datasets.push({
                            label: `${k.label} ${side}`,
                            data: (dir.series[side] || []).filter(p => p[k.key] != null).map(p => ({ x: p.h, y: p[k.key] })),
                            borderColor: colors[ki], backgroundColor: colors[ki],
                            borderDash: side === 'before' ? [5, 4] : [],
                            borderWidth: side === 'before' ? 1.5 : 2, pointRadius: 0, tension: 0.2,
                            yAxisID: ki ? 'y1' : 'y'
                        });
                    }
                });
                this.charts.push(new Chart(canvas, {
                    type: 'line',
                    data: { datasets },
                    options: {
                        responsive: true, maintainAspectRatio: false, animation: false,
                        interaction: { mode: 'nearest', intersect: false },
                        plugins: { title: { display: true, text: title }, legend: { labels: { boxWidth: 12 } } },
                        scales: {
                            x: { type: 'linear', title: { display: true, text: 'Hours into window (before dashed, after solid)' } },
                            y: { position: 'left', title: { display: true, text: keys[0].label } },
                            y1: { position: 'right', grid: { drawOnChartArea: false }, title: { display: true, text: keys[1].label } }
                        }
                    }
                }));
            };
            make('LQ and quality', [{ key: 'lq', label: 'LQ %' }, { key: 'quality', label: 'Quality %' }]);
            make('SNR and noise', [{ key: 'snr', label: 'SNR dB' }, { key: 'noise', label: 'Noise dBm' }]);
        });
    },

    /** Mark recorded changes for a link (either end, or the pair) as dashed
     *  vertical lines on time-axis charts (RF Stats link charts), and
     *  maintenance windows covering either end as shaded bands. */
    async markCharts(charts, source, target) {
        try {
            const [a, b] = await Promise.all([source, target].map(n =>
                fetch(`/api/changes?node=${encodeURIComponent(n)}&limit=200`).then(r => r.json())));
            const maint = await fetch('/api/maintenance?days=31').then(r => r.json()).catch(() => ({}));
            const pair = [source, target];
            const seen = new Set();
            const markers = [...(a.changes || []), ...(b.changes || [])]
                .filter(c => !seen.has(c.id) && seen.add(c.id))
                .filter(c => !c.peer_node || (pair.includes(c.peer_node) && pair.includes(c.node_name)))
                .map(c => ({ t: new Date(String(c.timestamp).replace(' ', 'T')).getTime(),
                             label: `${c.category}: ${c.node_name}` }));
            for (const w of maint.windows || []) {
                if (w.scope !== 'all' && !pair.some(n => (w.covered_nodes || []).includes(n))) continue;
                markers.push({ t: new Date(w.start_time.replace(' ', 'T')).getTime(),
                               end: new Date(w.end_time.replace(' ', 'T')).getTime(),
                               label: `maintenance${w.note ? ': ' + w.note : ''}` });
            }
            charts.forEach(ch => {
                if (!ch) return;
                ch.$changeMarkers = markers;
                ch.update('none');
            });
        } catch (e) { /* markers are optional */ }
    },

    destroyCharts() {
        this.charts.forEach(c => c.destroy());
        this.charts = [];
    },

    localInputValue(date) {
        const p = n => String(n).padStart(2, '0');
        return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}T${p(date.getHours())}:${p(date.getMinutes())}`;
    },

    fmt(v, suffix = '') {
        if (v == null) return '–';
        const n = Number(v);
        return (Number.isInteger(n) ? n : n.toFixed(Math.abs(n) >= 100 ? 0 : 2)) + suffix;
    },

    setStatus(text) {
        const el = document.getElementById('chg-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

// Deep link from the node page: /#changes or /#changes/<id> opens the tab
// (and that change's before/after comparison).
document.addEventListener('DOMContentLoaded', () => {
    const m = /^#changes(?:\/(\d+))?$/.exec(location.hash || '');
    if (!m) return;
    document.querySelector('.tab-btn[data-tab="changes"]')?.click();
    if (m[1]) {
        const id = Number(m[1]);
        const wait = setInterval(() => {
            if (ChangesModule.changes.length) {
                clearInterval(wait);
                ChangesModule.compare(id);
            }
        }, 200);
        setTimeout(() => clearInterval(wait), 10000);
    }
});

// Draws chart.$changeMarkers ([{t: ms, label, end?}]) as dashed vertical lines
// (or shaded bands when `end` is set, e.g. maintenance windows) on charts with
// a time x axis. Charts without markers are untouched.
if (typeof Chart !== 'undefined') {
    Chart.register({
        id: 'changeMarkers',
        afterDatasetsDraw(chart) {
            const markers = chart.$changeMarkers;
            const x = chart.scales?.x;
            if (!markers || !markers.length || !x || x.type !== 'time') return;
            const { ctx, chartArea } = chart;
            ctx.save();
            ctx.strokeStyle = '#8e44ad';
            ctx.fillStyle = '#8e44ad';
            ctx.setLineDash([4, 3]);
            ctx.lineWidth = 1.5;
            ctx.font = '10px sans-serif';
            for (const m of markers) {
                if (m.end) {
                    if (m.end < x.min || m.t > x.max) continue;
                    const x0 = x.getPixelForValue(Math.max(m.t, x.min));
                    const x1 = x.getPixelForValue(Math.min(m.end, x.max));
                    ctx.save();
                    ctx.fillStyle = 'rgba(93, 173, 226, 0.15)';
                    ctx.fillRect(x0, chartArea.top, x1 - x0, chartArea.bottom - chartArea.top);
                    ctx.fillStyle = '#2e86c1';
                    ctx.fillText(m.label, Math.min(x0 + 3, chartArea.right - 80), chartArea.bottom - 4);
                    ctx.restore();
                    continue;
                }
                if (!(m.t >= x.min && m.t <= x.max)) continue;
                const px = x.getPixelForValue(m.t);
                ctx.beginPath();
                ctx.moveTo(px, chartArea.top);
                ctx.lineTo(px, chartArea.bottom);
                ctx.stroke();
                ctx.fillText(m.label, Math.min(px + 3, chartArea.right - 80), chartArea.top + 10);
            }
            ctx.restore();
        }
    });
}
