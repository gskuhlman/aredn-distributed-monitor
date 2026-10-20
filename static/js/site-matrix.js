/**
 * Site Matrix (Diagnostics tab): every radio at a site (rows) against every
 * radio it hears (columns: site radios first, then remote radios) from
 * /api/reports/site-matrix. All verdicts live in site_matrix.py; this renders
 * a heatmap (cell = row's SNR / far end's SNR, color by the weaker one),
 * markers for one-way, lopsided, blocked, MAC-only and non-serving cells,
 * row/column summaries and the findings. Clicking a cell opens the pair in
 * RF Stats.
 */
const SiteMatrix = {
    initialized: false,
    data: null,

    FLAG_MARKS: {
        one_way: { mark: '⇥', label: 'one-way' },
        lopsided: { mark: '≠', label: 'lopsided SNR' },
        blocked: { mark: '⊘', label: 'LQM blocked' },
        mac_only: { mark: '#', label: 'known only by MAC' },
        also_heard: { mark: '◦', label: 'not its serving radio' },
        colocated: { mark: '▲', label: 'same mast' }
    },

    init() {
        if (this.initialized || !document.getElementById('sm-run')) return;
        this.initialized = true;
        document.getElementById('sm-run').addEventListener('click', () => this.run());
        document.getElementById('sm-report').addEventListener('click', (event) => {
            const target = event.target.closest('[data-a]');
            if (target) this.openPair(target.dataset.a, target.dataset.b);
        });
        this.loadSites();
    },

    async loadSites() {
        const select = document.getElementById('sm-site');
        try {
            const data = await (await fetch('/api/nodes/infrastructure')).json();
            const codes = new Set(Object.values(data.infrastructure_nodes || {}).filter(Boolean));
            Object.keys(data.sites || {}).forEach(c => codes.add(c));
            select.innerHTML = '<option value="">Site...</option>' + [...codes].sort().map(c =>
                `<option value="${this.esc(c)}">${this.esc(c)}${data.sites?.[c] ? ' – ' + this.esc(data.sites[c]) : ''}</option>`).join('');
            if (codes.has('COL')) select.value = 'COL';
        } catch (error) {
            console.error('Site matrix site list error:', error);
        }
    },

    async run() {
        const button = document.getElementById('sm-run');
        const el = document.getElementById('sm-report');
        const radios = document.getElementById('sm-radios').value.trim();
        const body = {
            hours: Number(document.getElementById('sm-hours').value) || 24,
            site: radios ? null : document.getElementById('sm-site').value || null,
            radios: radios || null
        };
        if (!body.site && !body.radios) {
            el.innerHTML = '<p class="log-empty">Pick a site or enter radio names.</p>';
            return;
        }
        const whatIf = document.getElementById('cm-whatif-text')?.value.trim();
        if (document.getElementById('sm-whatif').checked && whatIf) body.overrides = whatIf;
        button.disabled = true;
        this.setStatus('Loading...');
        try {
            const resp = await fetch('/api/reports/site-matrix', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
            const data = await resp.json();
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Matrix failed')}</p>`;
                this.setStatus('');
                return;
            }
            this.data = data;
            this.render();
            const stale = data.as_of && data.as_of.slice(0, 16) !== data.generated_at.slice(0, 16);
            this.setStatus(`${data.rows.length} radio(s) × ${data.columns.length} heard · last ${data.hours} h` +
                (stale ? ` · as of the last scan, ${data.as_of}` : ''));
        } catch (error) {
            console.error('Site matrix error:', error);
            el.innerHTML = '<p class="log-empty">Matrix failed.</p>';
            this.setStatus('');
        } finally {
            button.disabled = false;
        }
    },

    render() {
        const d = this.data;
        const c = d.counts;
        const el = document.getElementById('sm-report');
        el.innerHTML = `
            <div class="diag-verdict diag-${c.critical ? 'critical' : c.warning ? 'warning' : 'ok'}">
                <div class="diag-verdict-label">${d.site ? this.esc(d.site) : 'Radios'}: ${c.critical ? 'Problems found' : c.warning ? 'Issues found' : 'No problems found'}
                    <span class="diag-counts">${c.critical} critical · ${c.warning} warning · ${c.info} info</span></div>
            </div>
            ${this.renderMatrix()}
            <p class="form-hint">Cell: <strong>row hears column</strong> / column hears row (SNR dB), colored by the weaker
                direction. ${Object.values(this.FLAG_MARKS).map(f => `${f.mark} ${f.label}`).join(' · ')}.
                Faded = heard only in the window, not in the latest scan. Click a cell to open the pair in RF Stats.</p>
            <div class="diag-findings">${d.findings.map(f => this.renderFinding(f)).join('') ||
                '<p class="log-empty">No findings.</p>'}</div>`;
    },

    snrClass(v) {
        if (v == null) return 'sm-snr-none';
        if (v < 10) return 'sm-snr-0';
        if (v < 20) return 'sm-snr-1';
        if (v < 30) return 'sm-snr-2';
        return 'sm-snr-3';
    },

    renderMatrix() {
        const d = this.data;
        const cols = d.columns;
        const head = cols.map(col => `<th class="sm-col ${col.is_site ? 'sm-col-site' : ''}"
            title="${this.esc(this.radioTitle(col))}"><span>${this.esc(this.short(col.name, d.site))}</span>
            <small>${col.heard_by} hear</small></th>`).join('');
        const rows = d.rows.map(r => {
            const cells = cols.map(col => {
                if (col.name === r.name) return '<td class="sm-self"></td>';
                const c = d.cells[r.name]?.[col.name];
                if (!c) return '<td class="sm-empty"></td>';
                const weak = [c.snr, c.their_snr].filter(v => v != null);
                const cls = this.snrClass(weak.length ? Math.min(...weak) : null);
                const marks = c.flags.filter(f => this.FLAG_MARKS[f]).map(f => this.FLAG_MARKS[f].mark).join('');
                const named = !col.name.startsWith('mac-');
                return `<td class="sm-cell ${cls} ${c.current ? '' : 'sm-stale'} ${c.flags.includes('one_way') ? 'sm-oneway' : ''}
                    ${c.flags.includes('lopsided') ? 'sm-lopsided' : ''} ${c.flags.includes('blocked') ? 'sm-blocked' : ''}"
                    title="${this.esc(this.cellTitle(r, col, c))}" ${named ? `data-a="${this.esc(r.name)}" data-b="${this.esc(col.name)}"` : ''}>
                    ${c.snr ?? '–'}/${c.their_snr ?? '–'}${marks ? `<span class="sm-marks">${marks}</span>` : ''}</td>`;
            }).join('');
            const chan = r.frequency ? `${r.frequency}/${r.chanbw || '?'}` : (r.rf_status === 'off' ? 'off' : '–');
            const plan = r.planned ? (r.planned === 'off' ? ' → off' : ` → ${r.planned.frequency}/${r.planned.chanbw}`) : '';
            return `<tr><th class="sm-row" title="${this.esc(this.radioTitle(r))}">${this.esc(r.name)}
                <small>${this.esc(chan + plan)} · hears ${r.current}${r.heard > r.current ? ` (+${r.heard - r.current})` : ''}
                ${r.best_snr != null ? ` · ${r.worst_snr}–${r.best_snr} dB` : ''}${r.noise != null ? ` · noise ${r.noise}` : ''}</small></th>
                ${cells}</tr>`;
        }).join('');
        return `<div class="table-scroll-wrapper"><table class="info-table sm-table">
            <thead><tr><th class="sm-corner">${d.what_if ? 'with what-if' : ''}</th>${head}</tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    },

    short(name, site) {
        // Drop the call/site prefix the site's radios share (w0gq-col-rkt-n -> rkt-n).
        if (site) {
            const parts = name.split('-');
            if (parts.length > 2 && parts[1].toUpperCase() === site) return parts.slice(2).join('-');
        }
        return name;
    },

    radioTitle(r) {
        const bits = [r.name];
        if (r.probably) bits.push(`probably ${r.probably}`);
        if (r.frequency) bits.push(`${r.frequency}/${r.chanbw || '?'} MHz (ch ${r.channel ?? '?'})`);
        if (r.planned) bits.push(r.planned === 'off' ? 'planned: off' : `planned: ${r.planned.frequency}/${r.planned.chanbw}`);
        if (r.mode) bits.push(r.mode);
        if (r.azimuth != null) bits.push(`azimuth ${r.azimuth}°, beam ${r.beamwidth}°`);
        if (r.antenna) bits.push(r.antenna);
        if (r.is_infrastructure) bits.push(`infrastructure${r.site_code ? ' ' + r.site_code : ''}`);
        return bits.join(' · ');
    },

    cellTitle(r, col, c) {
        const lines = [
            `${r.name} hears ${col.name}: SNR ${c.snr ?? '?'} dB, quality ${c.quality ?? '?'}%` +
                (c.lq != null ? `, LQ ${c.lq}%` : '') + (c.signal != null ? `, signal ${c.signal} dBm` : ''),
            `${col.name} hears ${r.name}: SNR ${c.their_snr ?? '?'} dB, quality ${c.their_quality ?? '?'}%` +
                (c.their_lq != null ? `, LQ ${c.their_lq}%` : ''),
            `channel relation: ${c.relation}` + (c.planned_relation ? ` → ${c.planned_relation} after the plan` : ''),
        ];
        if (c.distance_m != null) {
            lines.push(`${(c.distance_m / 1000).toFixed(2)} km` + (c.a_beam != null
                ? ` · ${r.name} beam ${c.a_beam ? 'covers' : 'does not cover'} it, ${col.name} beam ${c.b_beam ? 'covers' : 'does not cover'} ${r.name}` : ''));
        }
        if (c.status) lines.push(`status ${c.status}${c.blocked_reason ? ` (${c.blocked_reason})` : ''}, last seen ${c.last_seen}`);
        if (c.window) lines.push(`window: ${c.window.samples} samples, median SNR ${c.window.snr ?? '?'} dB`);
        c.flags.filter(f => this.FLAG_MARKS[f]).forEach(f => lines.push(this.FLAG_MARKS[f].label +
            (f === 'also_heard' && c.serving_radio ? ` (serving: ${c.serving_radio})` : '')));
        c.one_way_reasons.forEach(x => lines.push(x));
        return lines.join('\n');
    },

    renderFinding(f) {
        const evidence = f.evidence.length
            ? `<ul class="diag-evidence">${f.evidence.map(e => `<li>${this.esc(e)}</li>`).join('')}</ul>` : '';
        const rec = f.recommendation ? `<div class="diag-rec"><strong>Try:</strong> ${this.esc(f.recommendation)}</div>` : '';
        const open = f.pair && !f.pair.some(n => n.startsWith('mac-'))
            ? ` <button class="btn btn-small btn-secondary sm-open" data-a="${this.esc(f.pair[0])}" data-b="${this.esc(f.pair[1])}">Diagnose</button>` : '';
        return `<div class="diag-finding diag-${f.severity}">
            <div class="diag-finding-head"><span class="diag-badge">${this.esc(f.severity)}</span>
                <span class="diag-cat">${this.esc(f.category.replace('_', ' '))}</span>
                <strong>${this.esc(f.title)}</strong>${open}</div>
            <div class="diag-finding-body">${this.esc(f.detail)}${evidence}${rec}</div>
        </div>`;
    },

    openPair(a, b) {
        document.querySelector('.tab-btn[data-tab="rf-stats"]')?.click();
        setTimeout(() => { if (typeof RFStats !== 'undefined') RFStats.selectLink(a, b); }, 300);
    },

    setStatus(text) {
        const el = document.getElementById('sm-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => SiteMatrix.init());
