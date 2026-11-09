/**
 * Settings Drift (Diagnostics tab): firmware and radio-settings mismatches
 * from /api/reports/drift. All rules live in drift.py; this renders the
 * findings, a per-site comparison table (cells that differ from the site's
 * majority are highlighted), the RF link table and a firmware histogram.
 */
const SettingsDrift = {
    initialized: false,
    data: null,

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('drift-run')?.addEventListener('click', () => this.run());
    },

    async run() {
        const button = document.getElementById('drift-run');
        const scope = document.getElementById('drift-scope')?.value || 'all';
        const el = document.getElementById('drift-report');
        if (button) button.disabled = true;
        this.setStatus('Comparing settings...');
        try {
            const resp = await fetch(`/api/reports/drift?scope=${encodeURIComponent(scope)}`);
            const data = await resp.json();
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Report failed')}</p>`;
                this.setStatus('');
                return;
            }
            this.data = data;
            this.render();
            this.setStatus(`Compared ${data.node_count} node(s) at ${data.generated_at}` +
                (data.latest_release ? ` · newest release seen ${data.latest_release}` : ''));
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Report failed.</p>';
            this.setStatus('');
            console.error('Settings drift error:', error);
        } finally {
            if (button) button.disabled = false;
        }
    },

    render() {
        const d = this.data;
        const el = document.getElementById('drift-report');
        const c = d.counts || {};
        el.innerHTML = `
            <div class="diag-verdict diag-${c.critical ? 'critical' : c.warning ? 'warning' : 'ok'}">
                <div class="diag-verdict-label">${c.critical ? 'Mismatches found' : c.warning ? 'Drift found' : 'No problem mismatches'}
                    <span class="diag-counts">${c.critical || 0} critical · ${c.warning || 0} warning · ${c.info || 0} info</span></div>
            </div>
            <div class="diag-findings">${d.findings.map(f => InfraReview.renderFinding(f)).join('')}</div>
            <details class="diag-details" open>
                <summary>Firmware versions (${d.firmware.length})</summary>
                ${this.renderFirmware()}
            </details>
            <details class="diag-details">
                <summary>Sites (${d.sites.length})</summary>
                ${d.sites.map(s => this.renderSite(s)).join('') || '<p class="form-hint">No sites with two or more located radios.</p>'}
            </details>
            <details class="diag-details">
                <summary>RF links (${d.links.length})</summary>
                ${this.renderLinks()}
            </details>
            ${(d.ssh_settings || []).length ? `<details class="diag-details">
                <summary>Radio settings read over SSH (${d.ssh_settings.length})</summary>
                ${this.renderSsh()}
            </details>` : ''}
            ${(d.radio_firmware || []).length ? `<details class="diag-details">
                <summary>Radio firmware read over SSH (${d.radio_firmware.length})</summary>
                ${this.renderRadioFirmware()}
            </details>` : ''}
            ${(d.notes || []).map(n => `<p class="form-hint">${this.esc(n)}</p>`).join('')}`;
    },

    renderRadioFirmware() {
        const v = x => (x === null || x === undefined || x === '') ? '-' : this.esc(String(x));
        const rows = this.data.radio_firmware.map(r => `<tr>
            <td>${v(r.name)}</td><td>${v(r.model)}</td><td>${v(r.chip)}</td><td>${v(r.aredn)}</td>
            <td class="${r.differs ? 'drift-diff' : ''}">${v(r.version)}${r.differs ? `<br><small>others: ${v(r.expected)}</small>` : ''}</td>
            <td class="${r.ct ? '' : 'drift-diff'}">${r.ct ? 'yes' : 'no'}</td>
            <td class="${r.replaced ? 'drift-diff' : ''}">${r.replaced ? 'replaced in overlay' : (r.md5 ? 'as shipped' : '-')}</td>
            <td><code>${v(r.md5 ? r.md5.slice(0, 8) : null)}</code></td>
            <td>${v(r.read_at)} <small>(${v(r.source)})</small></td></tr>`).join('');
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Radio</th><th>Model</th><th>Chip</th><th>AREDN</th><th>Radio firmware</th><th>CT build</th>
            <th>File</th><th>md5</th><th>Read</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    },

    renderSsh() {
        const v = x => (x === null || x === undefined || x === '') ? '-' : this.esc(String(x));
        const rows = this.data.ssh_settings.map(s => {
            const short = s.coverage_m != null && s.longest_m != null && s.coverage_m < s.longest_m;
            const capped = s.txpower_set != null && s.txpower_dbm != null && Math.abs(s.txpower_set - s.txpower_dbm) >= 1;
            const lqm = Object.entries(s.lqm || {}).map(([k, x]) => `${k}=${x}`).join(', ') || 'defaults';
            return `<tr>
                <td>${v(s.name)}</td><td>${v(s.mode)}</td><td>${v(s.channel)} / ${v(s.chanbw)}</td><td>${v(s.country)}</td>
                <td>${v(s.distance_m)}</td>
                <td class="${short ? 'drift-diff' : ''}">${s.coverage_class != null ? `${s.coverage_class} (${v(s.coverage_m)} m)` : '-'}</td>
                <td>${s.longest_m != null ? `${Math.round(s.longest_m)} m to ${v(s.longest_peer)}` : '-'}</td>
                <td class="${capped ? 'drift-diff' : ''}">${v(s.txpower_set)} / ${v(s.txpower_dbm)}</td>
                <td>${this.esc(lqm)}</td><td>${v(s.read_at)}</td></tr>`;
        }).join('');
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Radio</th><th>Mode</th><th>Channel / width</th><th>Country</th><th>Distance set (m)</th>
            <th>ACK window</th><th>Longest RF link</th><th>TX power set / used (dBm)</th><th>LQM options</th><th>Read</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    },

    renderFirmware() {
        const rows = this.data.firmware;
        const max = Math.max(1, ...rows.map(r => r.count));
        const labels = { current: 'current', behind: 'older point release', nightly: 'nightly',
            old: 'previous release line', very_old: 'very old', unknown: 'unknown' };
        return `<table class="info-table diag-table drift-fw">
            <thead><tr><th>Version</th><th>Status</th><th>Nodes</th><th></th></tr></thead>
            <tbody>${rows.map(r => `<tr>
                <td>${this.esc(r.version)}</td>
                <td><span class="drift-fw-class drift-fw-${this.esc(r.class)}">${this.esc(labels[r.class] || r.class)}</span></td>
                <td>${r.count}</td>
                <td class="drift-fw-bar-cell"><div class="drift-fw-bar drift-fw-${this.esc(r.class)}"
                    style="width:${Math.round(r.count / max * 100)}%" title="${this.esc(r.nodes.join(', '))}"></div></td>
            </tr>`).join('')}</tbody></table>
            <p class="form-hint">Hover a bar for the node names.</p>`;
    },

    cell(row, key, text) {
        const cls = row.differs.includes(key) ? ' class="drift-diff" title="Differs from most radios at this site"' : '';
        return `<td${cls}>${text}</td>`;
    },

    renderSite(site) {
        const bands = Object.entries(site.norms.bands || {})
            .filter(([, n]) => n.ssid || n.chanbw)
            .map(([b, n]) => `${b} mesh: ${this.esc(n.ssid || '?')}, ${n.chanbw ? n.chanbw + ' MHz' : '?'}`);
        const norms = [site.norms.firmware ? `firmware ${this.esc(site.norms.firmware)}` : null, ...bands].filter(Boolean);
        const rows = site.radios.map(r => `<tr>
            <td>${this.esc(r.name)}${r.is_infrastructure ? ' <small>(infra)</small>' : ''}</td>
            <td>${this.esc(r.model || '-')}</td>
            ${this.cell(r, 'firmware', this.esc(r.firmware || '-'))}
            <td>${r.rf_status === 'off' ? 'radio off' : this.esc(r.mode || '-')}</td>
            ${this.cell(r, 'ssid', this.esc(r.ssid || '-'))}
            ${this.cell(r, 'chanbw', r.chanbw ? r.chanbw + ' MHz' : '-')}
            <td>${r.frequency ? `${r.frequency} MHz${r.channel ? ' (ch ' + this.esc(r.channel) + ')' : ''}` : '-'}</td>
            <td>${this.esc(r.user_blocks || '-')}</td>
            <td>${this.esc(r.config_at || 'never')}</td></tr>`).join('');
        return `<h4 class="drift-site">${this.esc(site.name)}${site.code ? ` (${this.esc(site.code)})` : ''}
                <small class="form-hint">${norms.length ? 'Most radios: ' + norms.join(' · ') : ''}</small></h4>
            <div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Node</th><th>Model</th><th>Firmware</th><th>Mode</th><th>SSID</th><th>Width</th>
            <th>Frequency</th><th>User blocks</th><th>Settings read</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    },

    renderLinks() {
        const links = this.data.links;
        if (!links.length) return '<p class="form-hint">No current RF links in scope.</p>';
        const end = e => e.known === false && !e.model ? `${this.esc(e.name)} <small>(settings unknown)</small>`
            : `${this.esc(e.name)}<br><small>${this.esc([e.mode, e.ssid, e.chanbw ? e.chanbw + ' MHz' : null,
                e.frequency ? e.frequency + ' MHz' : null, e.firmware].filter(Boolean).join(' · '))}</small>`;
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table infra-links">
            <thead><tr><th>Status</th><th>End A</th><th>End B</th><th>Distance</th><th>Mismatches</th></tr></thead>
            <tbody>${links.map(l => `<tr>
                <td><span class="diag-badge infra-badge-${this.esc(l.severity)}">${this.esc(l.severity)}</span></td>
                <td>${end(l.ends[0])}</td><td>${end(l.ends[1])}</td>
                <td>${l.distance_km != null ? l.distance_km + ' km' : '-'}</td>
                <td class="infra-verdict">${l.problems.length ? this.esc(l.problems.join('; ')) : 'Settings match'}</td>
            </tr>`).join('')}</tbody></table></div>`;
    },

    setStatus(text) {
        const el = document.getElementById('drift-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => SettingsDrift.init());
