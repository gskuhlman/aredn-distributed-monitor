/**
 * Sector Airtime (Diagnostics tab): per-sector clients, who-hears-whom matrix,
 * hidden pairs, contention groups and mast sharing from /api/reports/airtime.
 * All rules live in airtime.py; this renders them. "Preview plan" posts the
 * what-if lines (Channel Map syntax) so contention can be checked before
 * channels change; measured data always comes from today's channels.
 */
const SectorAirtime = {
    initialized: false,
    data: null,

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('air-run')?.addEventListener('click', () => this.run(false));
        document.getElementById('air-preview')?.addEventListener('click', () => this.run(true));
        document.getElementById('air-use-map')?.addEventListener('click', () => {
            const text = document.getElementById('cm-whatif-text')?.value || '';
            const box = document.getElementById('air-overrides');
            if (box) box.value = text;
            this.setStatus(text.trim() ? 'Copied the Channel Map what-if plan.' : 'The Channel Map what-if box is empty.');
        });
    },

    async run(preview) {
        const scope = document.getElementById('air-scope')?.value || 'all';
        const hours = Number(document.getElementById('air-hours')?.value) || 24;
        const overrides = (document.getElementById('air-overrides')?.value || '').trim();
        const el = document.getElementById('air-report');
        if (preview && !overrides) {
            this.setStatus('Enter a plan ("name freq width" or "name off" per line) to preview.');
            return;
        }
        const buttons = ['air-run', 'air-preview'].map(id => document.getElementById(id));
        buttons.forEach(b => { if (b) b.disabled = true; });
        this.setStatus(preview ? 'Previewing plan...' : 'Checking sectors...');
        try {
            const resp = preview
                ? await fetch('/api/reports/airtime', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ scope, hours, overrides })
                })
                : await fetch(`/api/reports/airtime?scope=${encodeURIComponent(scope)}&hours=${hours}`);
            const data = await resp.json();
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Report failed')}</p>`;
                this.setStatus('');
                return;
            }
            this.data = data;
            this.render();
            const wi = data.what_if;
            this.setStatus(`${data.sectors.length} sector(s), ${data.masts.length} mast(s) at ${data.generated_at}` +
                (wi ? ` · plan applied to ${Object.keys(wi.applied).length} radio(s)` +
                    (wi.unknown.length ? `, unknown: ${wi.unknown.join(', ')}` : '') : ''));
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Report failed.</p>';
            this.setStatus('');
            console.error('Sector airtime error:', error);
        } finally {
            buttons.forEach(b => { if (b) b.disabled = false; });
        }
    },

    render() {
        const d = this.data;
        const c = d.counts;
        const el = document.getElementById('air-report');
        el.innerHTML = `
            ${d.what_if ? '<p class="node-warning">Plan preview: channels from the plan, measured hearing and counters from today\'s channels.</p>' : ''}
            <div class="diag-verdict diag-${c.critical ? 'critical' : c.warning ? 'warning' : 'ok'}">
                <div class="diag-verdict-label">${c.critical ? 'Problems found' : c.warning ? 'Issues found' : 'No problems found'}
                    <span class="diag-counts">${c.critical} critical · ${c.warning} warning · ${c.info} info</span></div>
            </div>
            <div class="diag-findings">${d.findings.map(f => this.renderFinding(f)).join('')}</div>
            ${d.masts.map(m => this.renderMast(m)).join('')}
            ${d.sectors.length ? d.sectors.map(s => this.renderSector(s)).join('')
                : '<p class="form-hint">No radio has two or more current RF neighbours in this scope.</p>'}
            <details class="diag-details"><summary>About the data</summary>
                <ul class="diag-evidence">${d.data_notes.map(n => `<li>${this.esc(n)}</li>`).join('')}</ul></details>`;
    },

    renderFinding(f) {
        const evidence = f.evidence.length
            ? `<ul class="diag-evidence">${f.evidence.map(e => `<li>${this.esc(e)}</li>`).join('')}</ul>` : '';
        const rec = f.recommendation ? `<div class="diag-rec"><strong>Try:</strong> ${this.esc(f.recommendation)}</div>` : '';
        return `<div class="diag-finding diag-${f.severity}">
            <div class="diag-finding-head"><span class="diag-badge">${this.esc(f.severity)}</span>
                <span class="diag-cat">${this.esc(f.category.replace(/_/g, ' '))}</span>
                <strong>${this.esc(f.title)}</strong></div>
            <div class="diag-finding-body">${this.esc(f.detail)}${evidence}${rec}</div>
        </div>`;
    },

    chan(r) {
        return r.frequency != null ? `${r.frequency}/${r.chanbw || '20?'}` : 'off';
    },

    renderMast(m) {
        const members = m.members.map(r => `<tr>
            <td>${this.esc(r.name)}${r.what_if ? ' <small>(plan)</small>' : ''}</td>
            <td>${this.esc(this.chan(r))} <small>ch ${this.esc(r.channel ?? '?')}</small></td>
            <td>${r.azimuth != null ? `${r.azimuth}°` : '-'} / ${r.beamwidth != null ? `${r.beamwidth}°` : '-'}</td>
            <td>${this.esc(r.clients.join(', ') || '-')}</td>
            <td>${r.airtime_pct != null ? `${r.airtime_pct}%` : '-'} <small>${this.esc(r.load)}</small></td></tr>`).join('');
        const pairs = m.pairs.map(p => `<li><span class="rel-badge rel-${this.esc(p.relation)}">${this.esc(p.relation)}</span>
            ${this.esc(p.a)} / ${this.esc(p.b)}${p.angle_apart != null ? ` · ${p.angle_apart}° apart` : ''}
            ${p.beams_overlap ? ' · <strong>beams overlap</strong>' : ''}
            ${p.hear_measured ? ` · hear each other (SNR ${this.esc(p.hear_snr)})` : ''}</li>`).join('');
        const cross = m.cross_sector.map(x => `<li>${this.esc(x.client)} (on ${this.esc(x.sector)}) ·
            ${x.off_boresight != null ? `${x.off_boresight}° off ` : ''}${this.esc(x.other)}'s boresight ·
            ${x.in_beam ? '<strong>inside its beam</strong>' : 'back/side lobe only'}</li>`).join('');
        const wrong = m.wrong_sector.map(w => `<li>${this.esc(w.client)} on ${this.esc(w.sector)} is ${w.off_boresight}° off its boresight;
            ${this.esc(w.best_sector)} is ${w.best_off}° off${w.outside_beam ? ' (outside the beam)' : ''}</li>`).join('');
        return `<details class="diag-details" open>
            <summary>Mast: ${this.esc(m.name)} · ${m.members.length} radio(s)</summary>
            <div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Radio</th><th>Channel</th><th>Azimuth / beam</th><th>RF clients</th><th>Est. airtime</th></tr></thead>
                <tbody>${members}</tbody></table></div>
            ${pairs ? `<p class="form-hint">Same, overlapping or adjacent channels on this mast:</p><ul class="diag-evidence">${pairs}</ul>` : ''}
            ${cross ? `<p class="form-hint">Clients on the same channel as another sector here:</p><ul class="diag-evidence">${cross}</ul>` : ''}
            ${wrong ? `<p class="form-hint">Clients on a sector not aimed at them:</p><ul class="diag-evidence">${wrong}</ul>` : ''}
        </details>`;
    },

    pct(v) {
        return v == null ? '-' : `${v}%`;
    },

    renderSector(s) {
        const failClass = v => v == null ? '' : v >= 10 ? 'air-fail-poor' : v >= 5 ? 'air-fail-marginal' : '';
        const rows = s.clients.map(c => {
            const dn = c.down || {}, up = c.up || {};
            const snr = [c.hub_hears?.snr, c.hears_hub?.snr].map(v => v ?? '?').join(' / ');
            return `<tr>
                <td>${this.esc(c.name)}</td>
                <td>${c.distance_m != null ? `${(c.distance_m / 1000).toFixed(1)} km` : '-'}
                    ${c.off_boresight != null ? `<small>${c.off_boresight}° off</small>` : ''}</td>
                <td>${this.esc(snr)}</td>
                <td>${this.esc(dn.lq ?? '-')} / ${this.esc(up.lq ?? '-')}</td>
                <td class="${failClass(dn.fail_pct)}">${this.pct(dn.fail_pct)}</td>
                <td class="${failClass(up.fail_pct)}">${this.pct(up.fail_pct)}</td>
                <td>${this.esc(dn.tx_bitrate ?? '-')} / ${this.esc(up.tx_bitrate ?? '-')}</td>
                <td>${this.esc(dn.pps ?? '-')} / ${this.esc(up.pps ?? '-')}</td>
                <td>${this.pct(c.airtime_pct)}</td></tr>`;
        }).join('');
        const t = s.totals;
        const contention = s.contention.map(x => `<li><span class="rel-badge rel-${this.esc(x.relation)}">${this.esc(x.relation)}</span>
            ${this.esc(x.name)} ${this.esc(this.chan(x))} · ${x.colocated ? 'same mast' : x.distance_m != null ? `${(x.distance_m / 1000).toFixed(1)} km` : '?'}
            ${x.hub_hears ? ` · heard (SNR ${this.esc(x.hub_hears.snr ?? '?')})` : ''}
            ${x.mutual_beam ? ' · facing each other' : x.hub_sees ? ' · in this radio\'s beam' : x.sees_hub ? ' · aimed at this radio' : ''}
            <small>${this.esc(x.kind)}</small></li>`).join('');
        return `<details class="diag-details" ${s.hidden_pairs.length || (t.fail_pct ?? 0) >= 5 ? 'open' : ''}>
            <summary>Sector: ${this.esc(s.name)} · ${this.esc(this.chan(s))}${s.what_if ? ' (plan)' : ''} ·
                ${s.clients.length} clients · fail ${this.pct(t.fail_pct)} · airtime ${this.pct(t.airtime_pct)} (${this.esc(t.load)})
                ${s.hidden_pairs.length ? ` · <strong>${s.hidden_pairs.length} hidden pair(s)</strong>` : ''}</summary>
            <div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Client</th><th>Distance</th><th>SNR (hub/client)</th><th>LQ (out/in)</th>
                <th>Fail out</th><th>Fail in</th><th>Mbps (out/in)</th><th>Pkt/s (out/in)</th><th>Est. airtime</th></tr></thead>
                <tbody>${rows}</tbody></table></div>
            ${this.renderMatrix(s)}
            ${contention ? `<p class="form-hint">Contention group (same or overlapping channel):</p><ul class="diag-evidence">${contention}</ul>`
                : '<p class="form-hint">No other radio on this channel can hear or face this sector.</p>'}
        </details>`;
    },

    /** Row hears column: SNR when it holds a tracker, ✕ when it doesn't, ? when we don't have its trackers. */
    renderMatrix(s) {
        const names = s.matrix_names;
        const hidden = new Set(s.hidden_pairs.flatMap(p => [`${p.a}|${p.b}`, `${p.b}|${p.a}`]));
        const short = n => n.length > 18 ? n.slice(0, 17) + '…' : n;
        const head = names.map(n => `<th title="${this.esc(n)}">${this.esc(short(n))}</th>`).join('');
        const rows = names.map(a => `<tr><th title="${this.esc(a)}">${this.esc(short(a))}</th>${names.map(b => {
            if (a === b) return '<td class="air-self">—</td>';
            const cell = s.matrix[`${a}|${b}`];
            const cls = hidden.has(`${a}|${b}`) ? 'air-hidden' : cell ? 'air-hears' : cell === false ? 'air-deaf' : 'air-unknown';
            const text = cell ? (cell.snr ?? '✓') : cell === false ? '✕' : '?';
            return `<td class="${cls}">${this.esc(text)}</td>`;
        }).join('')}</tr>`).join('');
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table air-matrix">
            <thead><tr><th>hears →</th>${head}</tr></thead><tbody>${rows}</tbody></table></div>
            <p class="form-hint">Row hears column (SNR of its LQM tracker). ✕ = doesn't hear it, ? = that radio wasn't polled.
            Red = hidden pair (both talk to ${this.esc(s.name)} but can't hear each other).</p>`;
    },

    setStatus(text) {
        const el = document.getElementById('air-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => SectorAirtime.init());
