/**
 * Health Score section of the Reports tab (health_score.py): every node or RF
 * link scored 0-100 per bucket over the Reports window, biggest drops first.
 * Clicking a point on a score line (or "What changed?" on a drop) lists what
 * happened around that time from /api/reports/what-changed.
 * Scoring rules live in health_score.py; this only renders them.
 */
const HealthScorePanel = {
    initialized: false,
    data: null,
    kind: 'node',
    query: '',
    requestSeq: 0,

    init() {
        if (this.initialized) return;
        if (!document.getElementById('hs-table')) return;
        this.initialized = true;
        document.querySelectorAll('input[name="hs-kind"]').forEach(r =>
            r.addEventListener('change', () => { this.kind = r.value; this.fetch(); }));
        document.getElementById('hs-never')?.addEventListener('change', () => this.render());
        document.getElementById('hs-table').addEventListener('click', (event) => {
            const point = event.target.closest('[data-hs-bucket]');
            if (point) {
                const row = point.closest('tr');
                this.whatChanged(row.dataset.a, row.dataset.b || null, Number(point.dataset.hsBucket));
                return;
            }
            const btn = event.target.closest('button[data-hs-drop]');
            if (btn) {
                const row = btn.closest('tr');
                this.whatChanged(row.dataset.a, row.dataset.b || null, Number(btn.dataset.hsDrop));
            }
        });
        document.getElementById('hs-changes-close')?.addEventListener('click', () =>
            document.getElementById('hs-changes').classList.add('hidden'));
    },

    load(query) {
        this.init();
        if (!this.initialized) return;
        this.query = query;
        this.fetch();
    },

    async fetch() {
        const seq = ++this.requestSeq;
        const el = document.getElementById('hs-table');
        el.innerHTML = '<p class="log-empty">Loading...</p>';
        try {
            const resp = await fetch(`/api/reports/health-score?${this.query}&kind=${this.kind}`);
            const data = await resp.json();
            if (seq !== this.requestSeq) return;
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Failed to load')}</p>`;
                return;
            }
            this.data = data;
            this.render();
        } catch (error) {
            if (seq === this.requestSeq) el.innerHTML = '<p class="log-empty">Failed to load health scores.</p>';
            console.error('Health score error:', error);
        }
    },

    color(score) {
        if (score == null) return '#d5d8dc';
        if (score >= 80) return '#27ae60';
        if (score >= 60) return '#f1c40f';
        if (score >= 40) return '#e67e22';
        return '#c0392b';
    },

    spark(item) {
        const series = item.series;
        const n = series.length;
        const w = 240, h = 34, step = w / Math.max(1, n);
        let maint = '', bars = '';
        series.forEach((s, i) => {
            if (item.maint[i]) maint += `<rect x="${(i * step).toFixed(1)}" y="0" width="${step.toFixed(1)}" height="${h}" class="hs-maint"/>`;
            const v = s == null ? 0 : s;
            const bh = s == null ? 2 : Math.max(2, v / 100 * (h - 2));
            bars += `<rect data-hs-bucket="${i}" x="${(i * step).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${Math.max(1, step - 0.5).toFixed(1)}"
                height="${bh.toFixed(1)}" fill="${this.color(s)}"><title>${this.esc(this.data.window.buckets[i])}: ${s == null ? 'no data' : s}</title></rect>`;
        });
        return `<svg class="hs-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" width="${w}" height="${h}">${maint}${bars}</svg>`;
    },

    render() {
        const el = document.getElementById('hs-table');
        const d = this.data;
        if (!d) return;
        const showNever = document.getElementById('hs-never')?.checked;
        const items = d.items.filter(i => showNever || !i.never_ok);
        const hidden = d.items.length - items.length;
        const hint = document.getElementById('hs-hint');
        if (hint) hint.textContent = `${d.total} ${d.kind === 'node' ? 'node(s)' : 'RF link(s)'}, ${d.with_drop} with a drop of 15+ points · `
            + `${d.window.bucket_seconds / 60}-minute buckets${hidden ? ` · ${hidden} never answered (hidden)` : ''}`;
        if (!items.length) {
            el.innerHTML = '<p class="log-empty">No scores in this window.</p>';
            return;
        }
        const cell = v => v == null ? '-' : `<span class="hs-score" style="background:${this.color(v)}">${v}</span>`;
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table hs-table">
            <thead><tr><th>${d.kind === 'node' ? 'Node' : 'RF link'}</th><th>Now</th><th>Avg</th><th>Min</th>
            <th>Score over the window <small>(click a bar: what changed?)</small></th><th>Biggest drop</th></tr></thead>
            <tbody>${items.map(i => {
                const label = d.kind === 'node' ? this.esc(i.name) : `${this.esc(i.a)} ↔ ${this.esc(i.b)}`;
                const drop = i.drop ? `<span class="diag-badge infra-badge-warning">−${i.drop.points}</span>
                    ${i.drop.from_score} → ${i.drop.to_score} <small>${this.esc(i.drop.at.slice(5, 16))}</small>
                    ${this.dropDetail(i.drop.detail)}
                    <button class="btn btn-secondary btn-small" data-hs-drop="${i.drop.index}">What changed?</button>` : '';
                return `<tr data-a="${this.esc(d.kind === 'node' ? i.name : i.a)}" data-b="${this.esc(i.b || '')}">
                    <td>${label}${i.infrastructure ? ' <small class="hs-infra">infra</small>' : ''}</td>
                    <td>${cell(i.now)}</td><td>${cell(i.avg)}</td><td>${cell(i.min)}</td>
                    <td>${this.spark(i)}</td><td class="hs-drop">${drop}</td></tr>`;
            }).join('')}</tbody></table></div>`;
    },

    dropDetail(x) {
        if (!x) return '';
        const parts = [];
        if (x.reach != null) parts.push(`reach ${Math.round(x.reach)}%`);
        if (x.rf != null) parts.push(`RF quality ${Math.round(x.rf)}%`);
        if (x.quality != null) parts.push(`quality ${Math.round(x.quality)}%`);
        if (x.snr != null) parts.push(`SNR ${x.snr} dB`);
        if (x.loss) parts.push(`loss ${Math.round(x.loss)}%`);
        if (x.penalty) parts.push(`−${x.penalty} reboot/drops`);
        if (x.transitions) parts.push(`${x.transitions} down/blocked`);
        return parts.length ? `<div class="form-hint">${parts.join(' · ')}</div>` : '';
    },

    async whatChanged(a, b, bucket) {
        const d = this.data;
        const start = d.window.buckets[bucket];
        const endDate = new Date(new Date(start.replace(' ', 'T')).getTime() + d.window.bucket_seconds * 1000);
        const pad = n => String(n).padStart(2, '0');
        const end = `${endDate.getFullYear()}-${pad(endDate.getMonth() + 1)}-${pad(endDate.getDate())}T${pad(endDate.getHours())}:${pad(endDate.getMinutes())}`;
        const box = document.getElementById('hs-changes');
        const list = document.getElementById('hs-changes-list');
        document.getElementById('hs-changes-title').textContent =
            `What changed: ${a}${b ? ' ↔ ' + b : ''} around ${start.slice(0, 16)}`;
        box.classList.remove('hidden');
        list.innerHTML = '<p class="log-empty">Loading...</p>';
        const qs = new URLSearchParams({ node: a, start: start.replace(' ', 'T').slice(0, 16), end });
        if (b) qs.set('peer', b);
        try {
            const resp = await fetch(`/api/reports/what-changed?${qs}`);
            const r = await resp.json();
            if (!resp.ok) {
                list.innerHTML = `<p class="log-empty">${this.esc(r.error || 'Failed')}</p>`;
                return;
            }
            this.renderChanges(r);
        } catch (error) {
            list.innerHTML = '<p class="log-empty">Failed to load.</p>';
        }
        box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    },

    renderChanges(r) {
        const list = document.getElementById('hs-changes-list');
        const counts = Object.entries(r.counts).map(([k, v]) => `${this.esc(r.labels[k] || k)}: ${v}`).join(' · ');
        const head = `<p class="form-hint">Searched ${this.esc(r.searched.start.slice(5, 16))} – ${this.esc(r.searched.end.slice(5, 16))}
            (the bucket plus some time either side)${r.neighbours.length ? ` · RF neighbours checked too: ${r.neighbours.map(n => this.esc(n)).join(', ')}` : ''}</p>`;
        if (!r.items.length) {
            list.innerHTML = head + '<p class="log-empty">Nothing recorded: no reboots, config, noise, route or starting-node events. Look at the RF Stats charts for a gradual change (noise, interference).</p>';
            return;
        }
        list.innerHTML = head + `<p>${counts}</p>
            <div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Time</th><th>Kind</th><th>What</th><th>Detail</th></tr></thead>
            <tbody>${r.items.map(i => `<tr class="hs-kind-${this.esc(i.kind)}">
                <td>${this.esc(i.time.slice(5, 19))}</td>
                <td><span class="diag-badge infra-badge-${i.severity === 'critical' ? 'critical' : i.severity === 'warning' ? 'warning' : 'info'}">${this.esc(i.label)}</span></td>
                <td>${this.esc(i.title)}${i.node && !i.title.includes(i.node) ? ` <small>(${this.esc(i.node)})</small>` : ''}</td>
                <td>${this.esc(i.detail)}</td></tr>`).join('')}</tbody></table></div>`;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

window.ReportPanels = window.ReportPanels || [];
window.ReportPanels.push(HealthScorePanel);
