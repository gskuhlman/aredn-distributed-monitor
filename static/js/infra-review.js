/**
 * Infrastructure Review (Diagnostics tab): audit of every infrastructure
 * node's radio settings from /api/reports/infrastructure. All rules live in
 * infra_audit.py; this renders findings, a per-site spectrum chart and
 * settings table, and a verdict for each RF link touching infrastructure.
 */
const InfraReview = {
    initialized: false,
    data: null,

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('infra-run')?.addEventListener('click', () => this.run());
    },

    async run() {
        const button = document.getElementById('infra-run');
        const refresh = document.getElementById('infra-refresh')?.checked;
        const el = document.getElementById('infra-review');
        if (button) button.disabled = true;
        this.setStatus(refresh ? 'Reading settings from nodes...' : 'Reviewing...');
        try {
            const resp = refresh
                ? await fetch('/api/reports/infrastructure', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ refresh: true })
                })
                : await fetch('/api/reports/infrastructure');
            const data = await resp.json();
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Review failed')}</p>`;
                this.setStatus('');
                return;
            }
            this.data = data;
            this.render();
            const r = data.refresh;
            this.setStatus(`Reviewed ${data.infrastructure_count} node(s) at ${data.generated_at}` +
                (r ? ` · settings read from ${r.refreshed} node(s)` +
                    (Object.keys(r.errors || {}).length ? `, ${Object.keys(r.errors).length} unreachable` : '') : ''));
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Review failed.</p>';
            console.error('Infrastructure review error:', error);
        } finally {
            if (button) button.disabled = false;
        }
    },

    render() {
        const d = this.data;
        const el = document.getElementById('infra-review');
        if (!d.infrastructure_count) {
            el.innerHTML = `<p class="log-empty">No infrastructure nodes are marked yet. Mark them from a node's
                detail page or the network side panel.</p>`;
            return;
        }
        const c = d.counts || {};
        const problems = d.findings.filter(f => f.severity !== 'ok');
        el.innerHTML = `
            <div class="diag-verdict diag-${c.critical ? 'critical' : c.warning ? 'warning' : 'ok'}">
                <div class="diag-verdict-label">${c.critical ? 'Problems found' : c.warning ? 'Issues found' : 'No problems found'}
                    <span class="diag-counts">${c.critical || 0} critical · ${c.warning || 0} warning ·
                    ${c.info || 0} info · ${c.ok || 0} ok</span></div>
            </div>
            <div class="diag-findings">${problems.map(f => this.renderFinding(f)).join('')}</div>
            ${d.sites.map(s => this.renderSite(s)).join('')}
            <details class="diag-details" open>
                <summary>RF links touching infrastructure (${d.links.length})</summary>
                ${this.renderLinks()}
            </details>
            ${d.findings.some(f => f.severity === 'ok') ? `<details class="diag-details">
                <summary>Checks that passed</summary>
                <div class="diag-findings">${d.findings.filter(f => f.severity === 'ok').map(f => this.renderFinding(f)).join('')}</div>
            </details>` : ''}`;
    },

    renderFinding(f) {
        const evidence = f.evidence.length
            ? `<ul class="diag-evidence">${f.evidence.map(e => `<li>${this.esc(e)}</li>`).join('')}</ul>` : '';
        const rec = f.recommendation
            ? `<div class="diag-rec"><strong>Try:</strong> ${this.esc(f.recommendation)}</div>` : '';
        return `<div class="diag-finding diag-${f.severity}">
            <div class="diag-finding-head"><span class="diag-badge">${this.esc(f.severity)}</span>
                <span class="diag-cat">${this.esc(f.category.replace('_', ' '))}${f.site ? ' · ' + this.esc(f.site) : ''}</span>
                <strong>${this.esc(f.title)}</strong></div>
            <div class="diag-finding-body">${this.esc(f.detail)}${evidence}${rec}</div>
        </div>`;
    },

    // Worst channel relation of each radio against the others at its site.
    radioRelations(site) {
        const rank = { overlap: 0, adjacent: 1, near: 2, same: 3, clear: 4, unknown: 5 };
        const worst = {};
        for (const p of site.pairs) {
            for (const n of [p.a, p.b]) {
                if (!worst[n] || rank[p.relation] < rank[worst[n]]) worst[n] = p.relation;
            }
        }
        return worst;
    },

    renderSite(site) {
        const worst = this.radioRelations(site);
        const rows = site.radios.map(r => {
            const rel = r.frequency ? (worst[r.name] || 'clear') : 'unknown';
            const az = r.azimuth != null ? `${r.azimuth}°` : '-';
            return `<tr class="rel-row rel-row-${rel}">
                <td>${this.esc(r.name)}${r.not_infrastructure ? ' <small>(not infra)</small>' : ''}</td>
                <td>${r.frequency ? `${r.frequency} MHz` : (r.rf_status === 'off' ? 'radio off' : '-')}</td>
                <td>${this.esc(r.channel || '-')}</td><td>${r.chanbw ? r.chanbw + ' MHz' : '-'}</td>
                <td>${r.frequency ? `<span class="rel-badge rel-${rel}">${rel}</span>` : ''}</td>
                <td>${this.esc(r.mode || '-')}</td><td>${this.esc(r.ssid || '-')}</td>
                <td>${az}</td><td>${this.esc(r.antenna || '-')}</td>
                <td>${r.height != null ? r.height + ' m' : '-'}</td>
                <td>${this.esc(r.firmware || '-')}</td></tr>`;
        }).join('');
        return `<details class="diag-details" open>
            <summary>${this.esc(site.name)} (${this.esc(site.code)}) · ${site.radios.length} node(s)</summary>
            ${this.renderSpectrum(site, worst)}
            <div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Node</th><th>Freq</th><th>Ch</th><th>Width</th><th>Channel</th><th>Mode</th>
            <th>SSID</th><th>Azimuth</th><th>Antenna</th><th>Height</th><th>Firmware</th></tr></thead>
            <tbody>${rows}</tbody></table></div>
        </details>`;
    },

    /** Each radio's occupied spectrum as a bar; overlapping/adjacent bars are colored. */
    renderSpectrum(site, worst) {
        const radios = site.radios.filter(r => r.frequency);
        if (!radios.length) return '';
        const spans = radios.map(r => {
            const bw = r.chanbw || 20;
            return { r, lo: r.frequency - bw / 2, hi: r.frequency + bw / 2, assumed: !r.chanbw };
        });
        const min = Math.floor((Math.min(...spans.map(s => s.lo)) - 10) / 10) * 10;
        const max = Math.ceil((Math.max(...spans.map(s => s.hi)) + 10) / 10) * 10;
        const W = 900, labelW = 190, rowH = 18, top = 6, H = top + spans.length * rowH + 22;
        const x = f => labelW + (f - min) / (max - min) * (W - labelW - 10);
        const colors = { overlap: '#c0392b', adjacent: '#e67e22', near: '#d4ac0d', same: '#2e86c1', clear: '#7f8c8d' };
        const step = (max - min) > 200 ? 50 : (max - min) > 80 ? 20 : 10;
        let ticks = '';
        for (let f = min; f <= max; f += step) {
            ticks += `<line x1="${x(f)}" x2="${x(f)}" y1="${top}" y2="${H - 18}" class="spec-grid"/>
                <text x="${x(f)}" y="${H - 5}" class="spec-tick">${f}</text>`;
        }
        const bars = spans.map((s, i) => {
            const rel = worst[s.r.name] || 'clear';
            const y = top + i * rowH;
            return `<text x="${labelW - 6}" y="${y + 12}" class="spec-label">${this.esc(s.r.name)}</text>
                <rect x="${x(s.lo)}" y="${y + 2}" width="${Math.max(2, x(s.hi) - x(s.lo))}" height="${rowH - 5}"
                  rx="2" fill="${colors[rel] || colors.clear}" fill-opacity="${s.assumed ? 0.45 : 0.85}">
                  <title>${this.esc(s.r.name)}: ${s.lo}–${s.hi} MHz${s.assumed ? ' (width assumed 20 MHz)' : ''} · ${rel}</title></rect>`;
        }).join('');
        return `<div class="spec-wrap"><svg viewBox="0 0 ${W} ${H}" class="spec-chart" role="img"
            aria-label="Occupied spectrum of radios at ${this.esc(site.name)}">${ticks}${bars}</svg>
            <p class="form-hint">Occupied spectrum (MHz). Red = overlaps another radio here, orange = no guard band,
            faded = width unknown (20 MHz assumed). Hover a bar for details.</p></div>`;
    },

    renderLinks() {
        const links = this.data.links;
        if (!links.length) return '<p class="form-hint">No current RF links touch infrastructure.</p>';
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table infra-links">
            <thead><tr><th>Status</th><th>Link</th><th>Diagnosis</th></tr></thead>
            <tbody>${links.map(l => `<tr>
                <td><span class="diag-badge infra-badge-${this.esc(l.severity)}">${this.esc(l.severity)}</span></td>
                <td>${this.esc(l.a)} ↔ ${this.esc(l.b)}${l.both_infrastructure ? ' <small>(both infra)</small>' : ''}</td>
                <td class="infra-verdict">${this.esc(l.verdict)}</td></tr>`).join('')}</tbody></table></div>
            <p class="form-hint">Open a link on the RF Stats tab for its full diagnosis.</p>`;
    },

    setStatus(text) {
        const el = document.getElementById('infra-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
