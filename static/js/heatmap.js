/**
 * Heatmap section of the Reports tab (heatmap.py): node x hour, coloured by
 * reachability or RF link quality, rows grouped by site / callsign; optional
 * hour-of-day fold for nightly patterns. Drawn on one canvas; hover shows the
 * cell, click opens the node's charts in the Reports detail panel.
 */
const HeatmapPanel = {
    initialized: false,
    data: null,
    query: '',
    requestSeq: 0,
    LABEL_W: 200,
    ROW_H: 13,
    HEAD_H: 30,

    init() {
        if (this.initialized) return;
        if (!document.getElementById('hm-canvas')) return;
        this.initialized = true;
        ['hm-metric', 'hm-fold'].forEach(id =>
            document.getElementById(id)?.addEventListener('change', () => this.fetch()));
        const canvas = document.getElementById('hm-canvas');
        canvas.addEventListener('mousemove', (e) => this.hover(e));
        canvas.addEventListener('mouseleave', () => document.getElementById('hm-tip').classList.add('hidden'));
        canvas.addEventListener('click', (e) => {
            const hit = this.hitTest(e);
            if (hit && typeof ReportsModule !== 'undefined') {
                ReportsModule.openNode(hit.row.name);
                document.getElementById('rpt-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });
        window.addEventListener('resize', () => { if (this.data) this.draw(); });
    },

    load(query) {
        this.init();
        if (!this.initialized) return;
        this.query = query;
        this.fetch();
    },

    async fetch() {
        const seq = ++this.requestSeq;
        const metric = document.getElementById('hm-metric').value;
        const fold = document.getElementById('hm-fold').checked ? 1 : 0;
        const hint = document.getElementById('hm-hint');
        hint.textContent = 'Loading...';
        try {
            const resp = await fetch(`/api/reports/heatmap?${this.query}&metric=${metric}&fold=${fold}`);
            const data = await resp.json();
            if (seq !== this.requestSeq) return;
            if (!resp.ok) {
                hint.textContent = data.error || 'Failed to load';
                return;
            }
            this.data = data;
            const together = data.together.length
                ? ` · ${new Set(data.together.map(t => t.group + t.column)).size} hour(s) where a whole group was below 50 together` : '';
            hint.textContent = `${data.rows.length} node(s) in ${data.groups} group(s) · ${data.columns.length} column(s)${together}`;
            this.draw();
        } catch (error) {
            if (seq === this.requestSeq) hint.textContent = 'Failed to load heatmap.';
            console.error('Heatmap error:', error);
        }
    },

    color(v) {
        if (v == null) return '#f2f3f4';
        const reach = this.data.metric === 'reach';
        const t = reach ? [99, 90, 50] : [80, 60, 40];
        if (v >= t[0]) return '#27ae60';
        if (v >= t[1]) return '#f1c40f';
        if (v >= t[2]) return '#e67e22';
        return '#c0392b';
    },

    layout() {
        const canvas = document.getElementById('hm-canvas');
        const wrap = canvas.parentElement;
        const cols = this.data.columns.length;
        const avail = Math.max(300, wrap.clientWidth - this.LABEL_W - 8);
        const cw = Math.max(2, Math.min(24, avail / cols));
        // Group header rows take one row each.
        const lines = [];
        let last = null;
        for (const row of this.data.rows) {
            if (row.group !== last) {
                lines.push({ group: row.group });
                last = row.group;
            }
            lines.push({ row });
        }
        return { canvas, cols, cw, lines };
    },

    draw() {
        const { canvas, cols, cw, lines } = this.layout();
        this.geom = { cw, lines };
        const dpr = window.devicePixelRatio || 1;
        const w = this.LABEL_W + cols * cw + 4;
        const h = this.HEAD_H + lines.length * this.ROW_H + 4;
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        canvas.style.width = `${w}px`;
        canvas.style.height = `${h}px`;
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);
        ctx.font = '11px sans-serif';
        ctx.textBaseline = 'middle';

        // Column labels: day boundaries / every few hours.
        ctx.fillStyle = '#566573';
        const fold = this.data.fold;
        const every = Math.max(1, Math.ceil(40 / cw));
        this.data.columns.forEach((c, i) => {
            const hour = fold ? i : Number(c.slice(11, 13));
            const x = this.LABEL_W + i * cw;
            if (!fold && hour === 0) {
                ctx.fillStyle = '#2c3e50';
                ctx.fillText(c.slice(5, 10), x + 2, 8);
                ctx.fillStyle = '#566573';
                ctx.fillRect(x, 14, 1, h - 14);
            }
            if (i % every === 0) ctx.fillText(fold ? c.slice(0, 2) : c.slice(11, 13), x + 1, 22);
        });

        lines.forEach((line, li) => {
            const y = this.HEAD_H + li * this.ROW_H;
            if (line.group !== undefined) {
                ctx.fillStyle = '#eaf2f8';
                ctx.fillRect(0, y, w, this.ROW_H);
                ctx.fillStyle = '#1b4f72';
                ctx.font = 'bold 11px sans-serif';
                ctx.fillText(line.group, 4, y + this.ROW_H / 2);
                ctx.font = '11px sans-serif';
                return;
            }
            const row = line.row;
            ctx.fillStyle = row.infrastructure ? '#1b4f72' : '#2c3e50';
            const label = row.name.length > 30 ? row.name.slice(0, 29) + '…' : row.name;
            ctx.fillText(label, 12, y + this.ROW_H / 2);
            row.cells.forEach((v, i) => {
                ctx.fillStyle = this.color(v);
                ctx.fillRect(this.LABEL_W + i * cw, y + 1, Math.max(1, cw - (cw > 4 ? 1 : 0)), this.ROW_H - 2);
                if (row.maint[i]) {
                    ctx.fillStyle = 'rgba(52, 152, 219, 0.45)';
                    ctx.fillRect(this.LABEL_W + i * cw, y + this.ROW_H - 4, Math.max(1, cw - 1), 3);
                }
            });
        });
    },

    hitTest(e) {
        if (!this.geom) return null;
        const rect = e.target.getBoundingClientRect();
        const x = e.clientX - rect.left, y = e.clientY - rect.top;
        const li = Math.floor((y - this.HEAD_H) / this.ROW_H);
        const line = this.geom.lines[li];
        if (!line || !line.row) return null;
        const ci = Math.floor((x - this.LABEL_W) / this.geom.cw);
        return { row: line.row, col: ci >= 0 && ci < this.data.columns.length ? ci : null, x, y };
    },

    hover(e) {
        const tip = document.getElementById('hm-tip');
        const hit = this.hitTest(e);
        if (!hit) {
            tip.classList.add('hidden');
            return;
        }
        const r = hit.row;
        const unit = this.data.metric === 'reach' ? '% of polls answered' : '% RF link quality';
        let text = `${r.name} (${r.group}) · avg ${r.avg ?? '-'}`;
        if (hit.col != null) {
            const v = r.cells[hit.col];
            const label = this.data.fold ? `${this.data.columns[hit.col]} (every day)` : this.data.columns[hit.col].slice(0, 16);
            text = `${r.name} · ${label}: ${v == null ? 'no data' : v + unit} (${r.samples[hit.col]} sample(s))`
                + (r.maint[hit.col] ? ' · maintenance' : '');
        }
        tip.textContent = text;
        tip.style.left = `${hit.x + 14}px`;
        tip.style.top = `${hit.y + 10}px`;
        tip.classList.remove('hidden');
    }
};

window.ReportPanels = window.ReportPanels || [];
window.ReportPanels.push(HeatmapPanel);
