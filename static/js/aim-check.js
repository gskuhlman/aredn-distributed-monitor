/**
 * Antenna Aim (Diagnostics tab) from /api/reports/aim: each directional
 * radio's set azimuth vs. the bearings of the RF peers it hears, plus
 * location sanity. All rules live in aim_check.py; this renders the findings
 * and a per-radio table, the short aim block on the node page / Nodes detail
 * panel (`aim` from /api/nodes/full and /api/nodes/detail), and an Aim line
 * appended to the Channel Map radio panel (wraps ChannelMap.renderPanel).
 */
const AimCheck = {
    initialized: false,
    data: null,
    panelCache: {},

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('aim-run')?.addEventListener('click', () => this.run());
        this.hookChannelMap();
    },

    async run() {
        const button = document.getElementById('aim-run');
        const scope = document.getElementById('aim-scope')?.value || 'all';
        const el = document.getElementById('aim-report');
        if (button) button.disabled = true;
        this.setStatus('Checking...');
        try {
            const resp = await fetch(`/api/reports/aim?scope=${encodeURIComponent(scope)}`);
            const data = await resp.json();
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Aim check failed')}</p>`;
                this.setStatus('');
                return;
            }
            this.data = data;
            this.render();
            this.setStatus(`${data.directional_count} directional radio(s) checked at ${data.generated_at}`);
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Aim check failed.</p>';
            this.setStatus('');
            console.error('Antenna aim error:', error);
        } finally {
            if (button) button.disabled = false;
        }
    },

    render() {
        const d = this.data;
        const el = document.getElementById('aim-report');
        const c = d.counts || {};
        el.innerHTML = `
            <div class="diag-verdict diag-${c.critical ? 'critical' : c.warning ? 'warning' : 'ok'}">
                <div class="diag-verdict-label">${c.critical ? 'Misaims found' : c.warning ? 'Aim or location problems found' : 'No aim problems found'}
                    <span class="diag-counts">${c.critical || 0} critical · ${c.warning || 0} warning · ${c.info || 0} info</span></div>
            </div>
            <div class="diag-findings">${d.findings.map(f => this.renderFinding(f)).join('')}</div>
            <details class="diag-details" open>
                <summary>Directional radios (${d.radios.length})</summary>
                ${this.renderTable(d.radios)}
            </details>
            ${(d.notes || []).map(n => `<p class="form-hint">${this.esc(n)}</p>`).join('')}`;
    },

    renderFinding(f) {
        const evidence = f.evidence?.length
            ? `<ul class="diag-evidence">${f.evidence.map(e => `<li>${this.esc(e)}</li>`).join('')}</ul>` : '';
        const rec = f.recommendation ? `<div class="diag-rec"><strong>Try:</strong> ${this.esc(f.recommendation)}</div>` : '';
        return `<div class="diag-finding diag-${this.esc(f.severity)}">
            <div class="diag-finding-head"><span class="diag-badge">${this.esc(f.severity)}</span>
                <span class="diag-cat">${this.esc(f.category.replace(/_/g, ' '))}${f.site ? ' · ' + this.esc(f.site) : ''}</span>
                <strong>${this.esc(f.title)}</strong></div>
            <div class="diag-finding-body">${this.esc(f.detail)}${evidence}${rec}</div>
        </div>`;
    },

    deg(v) {
        return v == null ? '-' : `${Math.round(v)}°`;
    },

    verdictClass(v) {
        return { ok: 'clear', setting_wrong: 'adjacent', misaim: 'overlap', unset: 'near',
            outside_sector: 'adjacent', off_beam: 'near', sector_edge: 'near', no_peers: 'unknown' }[v] || 'unknown';
    },

    renderTable(radios) {
        if (!radios.length) return '<p class="form-hint">No located directional radios in scope.</p>';
        const rows = radios.map(r => {
            const rel = this.verdictClass(r.verdict);
            const peers = (r.peers || []).map(p => `${this.esc(p.name)} ${this.deg(p.bearing)}` +
                (p.offset != null ? ` (${Math.round(p.offset)}° off)` : '')).join('<br>');
            return `<tr class="rel-row rel-row-${rel}">
                <td><a href="/nodes/${encodeURIComponent(r.name)}">${this.esc(r.name)}</a>${r.is_infrastructure ? ' <small>(infra)</small>' : ''}</td>
                <td>${this.esc(r.antenna || '-')}<br><small>${this.esc(r.kind)} · ${r.beamwidth}° beam${r.beamwidth_assumed ? ' (estimated)' : ''}</small></td>
                <td>${this.deg(r.azimuth)}${r.elevation != null ? `<br><small>tilt ${r.elevation}°${r.expected_elevation != null ? ` (≈${r.expected_elevation}° expected)` : ''}</small>` : ''}</td>
                <td>${r.main_peer ? `${this.esc(r.main_peer)}<br><small>${this.deg(r.main_bearing)}, SNR ${r.main_snr ?? '?'} dB</small>` : '-'}</td>
                <td>${this.deg(r.best_bearing)}</td>
                <td>${r.offset != null ? `${Math.round(r.offset)}°` : '-'}</td>
                <td><span class="rel-badge rel-${rel}">${this.esc(r.verdict_label)}</span>
                    ${r.suggested_azimuth != null ? `<br><small>set ≈ ${r.suggested_azimuth}°</small>` : ''}
                    ${r.location_problem ? '<br><small>location problem</small>' : ''}</td>
                <td><small>${peers || '-'}</small></td></tr>`;
        }).join('');
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Radio</th><th>Antenna</th><th>Set azimuth</th><th>Strongest peer</th>
            <th>SNR-weighted bearing</th><th>Offset</th><th>Verdict</th><th>RF peers (bearing)</th></tr></thead>
            <tbody>${rows}</tbody></table></div>
            <p class="form-hint">Offset: strongest peer for a dish, SNR-weighted bearing of all clients for a sector.
            Peers on the same mast are left out.</p>`;
    },

    /** Short aim block for the node page and Nodes detail panel. */
    nodeBlock(aim, esc) {
        if (!aim) return '';
        const e = esc || (v => this.esc(v));
        const bits = [];
        if (aim.verdict) {
            bits.push(`<strong>Antenna aim: ${e(aim.verdict_label)}</strong>`);
            const set = aim.azimuth != null ? `set ${this.deg(aim.azimuth)}` : 'azimuth not set';
            const peer = aim.main_peer ? `strongest peer ${e(aim.main_peer)} at ${this.deg(aim.main_bearing)}` +
                (aim.offset != null ? ` (${Math.round(aim.offset)}° off)` : '') : null;
            bits.push(`<small>${[set, `${aim.beamwidth}° beam`, peer,
                aim.suggested_azimuth != null ? `suggested azimuth ≈ ${aim.suggested_azimuth}°` : null]
                .filter(Boolean).join(' · ')}</small>`);
        }
        const problems = (aim.findings || []).filter(f => f.severity !== 'ok');
        const list = problems.length ? `<ul>${problems.map(f =>
            `<li><strong>${e(f.severity)}</strong>: ${e(f.title)}${f.recommendation ? ` <small>${e(f.recommendation)}</small>` : ''}</li>`).join('')}</ul>` : '';
        if (!bits.length && !list) return '';
        const worst = problems.some(f => f.severity === 'critical') ? 'critical'
            : problems.some(f => f.severity === 'warning') ? 'warning' : 'info';
        return `<div class="unreach-cause aim-block rpt-cause-${worst}">${bits.join('<br>')}${list}</div>`;
    },

    /** Append an Aim line to the Channel Map radio panel (additive hook). */
    hookChannelMap() {
        if (typeof ChannelMap === 'undefined' || ChannelMap._aimHooked) return;
        ChannelMap._aimHooked = true;
        const original = ChannelMap.renderPanel.bind(ChannelMap);
        ChannelMap.renderPanel = (...args) => {
            original(...args);
            const name = ChannelMap.selected;
            if (name) this.panelAim(name);
        };
    },

    async panelAim(name) {
        let aim = this.panelCache[name];
        if (aim === undefined) {
            try {
                const resp = await fetch(`/api/reports/aim?node=${encodeURIComponent(name)}`);
                const data = await resp.json();
                const row = resp.ok ? (data.radios || [])[0] : null;
                aim = row ? { ...row, findings: data.findings } : (resp.ok && data.findings?.length ? { findings: data.findings } : null);
            } catch (error) {
                aim = null;
            }
            this.panelCache[name] = aim;
        }
        const el = document.getElementById('cm-panel');
        if (!aim || ChannelMap.selected !== name || !el || el.querySelector('.aim-block')) return;
        const table = el.querySelector('.cm-table');
        const html = this.nodeBlock(aim);
        if (html && table) table.insertAdjacentHTML('afterend', html);
    },

    setStatus(text) {
        const el = document.getElementById('aim-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => AimCheck.init());
