/**
 * Link Swimlanes section of the Reports tab (swimlanes.py): one bar per link
 * over the window, each bucket good / degraded / flapping / down / unknown,
 * with reboot and config-change markers and maintenance shading. Clicking a
 * lane opens the link's charts in the Reports detail panel.
 */
const SwimlanesPanel = {
    initialized: false,
    data: null,
    query: '',
    requestSeq: 0,
    COLORS: {
        good: '#27ae60', degraded: '#f1c40f', flapping: '#8e44ad', down: '#c0392b',
        unknown: '#bdc3c7', none: '#f2f3f4'
    },
    MARKS: { reboot: '#c0392b', config: '#2874a6', change: '#117864' },

    init() {
        if (this.initialized) return;
        if (!document.getElementById('sl-lanes')) return;
        this.initialized = true;
        document.getElementById('sl-never')?.addEventListener('change', () => this.render());
        document.getElementById('sl-lanes').addEventListener('click', (event) => {
            const lane = event.target.closest('[data-sl-a]');
            if (lane && typeof ReportsModule !== 'undefined') {
                ReportsModule.openLink(lane.dataset.slA, lane.dataset.slB);
                document.getElementById('rpt-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });
        document.getElementById('sl-legend').innerHTML = Object.entries(this.COLORS)
            .map(([k, c]) => `<span class="sl-key"><i style="background:${c}"></i>${k}</span>`).join('')
            + Object.entries(this.MARKS).map(([k, c]) => `<span class="sl-key"><b style="color:${c}">▼</b>${k}</span>`).join('')
            + '<span class="sl-key"><i class="sl-maint-key"></i>maintenance</span>';
    },

    load(query) {
        this.init();
        if (!this.initialized) return;
        this.query = query;
        this.fetch();
    },

    async fetch() {
        const seq = ++this.requestSeq;
        const el = document.getElementById('sl-lanes');
        el.innerHTML = '<p class="log-empty">Loading...</p>';
        try {
            const resp = await fetch(`/api/reports/swimlanes?${this.query}`);
            const data = await resp.json();
            if (seq !== this.requestSeq) return;
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Failed to load')}</p>`;
                return;
            }
            this.data = data;
            this.render();
        } catch (error) {
            if (seq === this.requestSeq) el.innerHTML = '<p class="log-empty">Failed to load swimlanes.</p>';
            console.error('Swimlanes error:', error);
        }
    },

    lane(l) {
        const n = l.cells.length;
        const w = 1000, h = 16;
        const step = w / n;
        let rects = '';
        let i = 0;
        // Merge runs of the same state into one rect.
        while (i < n) {
            let j = i;
            while (j + 1 < n && l.cells[j + 1] === l.cells[i]) j++;
            rects += `<rect x="${(i * step).toFixed(2)}" y="4" width="${((j - i + 1) * step).toFixed(2)}" height="${h - 4}" fill="${this.COLORS[l.cells[i]]}">
                <title>${this.esc(this.data.window.buckets[i].slice(5, 16))}${j > i ? ' – ' + this.esc(this.data.window.buckets[j].slice(5, 16)) : ''}: ${l.cells[i]}</title></rect>`;
            i = j + 1;
        }
        let maint = '';
        l.maint.forEach((m, k) => {
            if (m) maint += `<rect x="${(k * step).toFixed(2)}" y="${h - 3}" width="${step.toFixed(2)}" height="3" class="sl-maint"/>`;
        });
        const marks = l.marks.map(m => {
            const x = (m.bucket + 0.5) * step;
            return `<path d="M${(x - 4).toFixed(1)},0 L${(x + 4).toFixed(1)},0 L${x.toFixed(1)},5 Z" fill="${this.MARKS[m.kind] || '#555'}">
                <title>${this.esc(m.time.slice(5, 16))} ${this.esc(m.title)}${m.detail ? ': ' + this.esc(m.detail) : ''}</title></path>`;
        }).join('');
        return `<svg class="sl-bar" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${rects}${maint}${marks}</svg>`;
    },

    render() {
        const el = document.getElementById('sl-lanes');
        const d = this.data;
        if (!d) return;
        const showNever = document.getElementById('sl-never')?.checked;
        const lanes = d.lanes.filter(l => showNever || !l.never_up);
        document.getElementById('sl-hint').textContent = `${d.total} link(s)${d.total > d.lanes.length ? `, worst ${d.lanes.length} shown` : ''} · `
            + `${d.window.bucket_seconds / 60}-minute buckets${!showNever && d.never_up ? ` · ${d.never_up} never up (hidden)` : ''}`;
        if (!lanes.length) {
            el.innerHTML = '<p class="log-empty">No links in this window.</p>';
            return;
        }
        const b = d.window.buckets;
        el.innerHTML = `<div class="sl-axis"><span>${this.esc(b[0].slice(5, 16))}</span><span>${this.esc(b[Math.floor(b.length / 2)].slice(5, 16))}</span>
            <span>${this.esc(d.window.end.slice(5, 16))}</span></div>
            ${lanes.map(l => {
                const c = l.counts;
                const summary = ['down', 'flapping', 'degraded', 'unknown'].filter(k => c[k])
                    .map(k => `${k} ${Math.round(c[k] * d.window.bucket_seconds / 60)} min`).join(', ') || 'good throughout';
                return `<div class="sl-lane" data-sl-a="${this.esc(l.a)}" data-sl-b="${this.esc(l.b)}"
                    title="${this.esc(l.a)} ↔ ${this.esc(l.b)} (${this.esc(l.type)}): ${this.esc(summary)}${l.flaps ? ` · ${l.flaps} drop(s)` : ''}">
                    <div class="sl-label"><span class="sl-type">${this.esc(l.type)}</span> ${this.esc(l.a)} ↔ ${this.esc(l.b)}</div>
                    ${this.lane(l)}</div>`;
            }).join('')}`;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

window.ReportPanels = window.ReportPanels || [];
window.ReportPanels.push(SwimlanesPanel);
