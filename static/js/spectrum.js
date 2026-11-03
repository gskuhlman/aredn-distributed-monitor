/**
 * Spectrum tab: frequency across, locations down (spectrum_view.py).
 * Each site row shows our radios as bars over 5 MHz slots coloured free /
 * partly used / busy (WiFi surveys + mesh radios in range); each scanned
 * radio gets its own row of what that antenna hears. 10 MHz gridlines.
 * Click a slot for what is in it, a row name for the row's free spans.
 */
const Spectrum = {
    data: null,
    picked: null,
    LABEL_W: 250,
    COLORS: {
        free: '#d9f2dc', unsurveyed: '#f3f3f3', guard: '#dcdcdc', some: '#ffe08a', busy: '#f2a19d',
        ours: '#ffffff',
    },
    LABELS: {
        free: 'Free', unsurveyed: 'No mesh radios; WiFi not scanned here', guard: 'Within 10 MHz of one of ours',
        some: 'Partly used', busy: 'Busy', ours: 'Ours',
    },

    hookTab() {
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const view = document.getElementById('spectrum-view');
                if (!view) return;
                if (btn.dataset.tab === 'spectrum') {
                    view.classList.add('active');
                    this.open();
                } else {
                    view.classList.remove('active');
                }
            });
        });
    },

    open() {
        if (!this.initialized) {
            this.initialized = true;
            ['sp-scope', 'sp-inactive', 'sp-whatif', 'sp-at'].forEach(id =>
                document.getElementById(id)?.addEventListener('change', () => this.load()));
            document.getElementById('sp-reload')?.addEventListener('click', () => this.load());
            let timer = null;
            window.addEventListener('resize', () => {
                clearTimeout(timer);
                timer = setTimeout(() => { if (this.isOpen()) this.render(); }, 150);
            });
        }
        this.load();
    },

    isOpen() {
        return document.getElementById('spectrum-view')?.classList.contains('active');
    },

    esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },

    async load() {
        const status = document.getElementById('sp-status');
        const body = {
            scope: document.getElementById('sp-scope')?.value || 'infrastructure',
            low: Number(document.getElementById('sp-low')?.value) || 5640,
            high: Number(document.getElementById('sp-high')?.value) || 5930,
            include_inactive: document.getElementById('sp-inactive')?.checked || false,
        };
        const whatIf = document.getElementById('sp-whatif')?.checked
            ? (document.getElementById('cm-whatif-text')?.value || '').trim() : '';
        if (whatIf) body.overrides = whatIf;
        const at = document.getElementById('sp-at')?.value;
        if (at) body.at = at;
        if (status) status.textContent = 'Loading…';
        try {
            const resp = await fetch('/api/spectrum', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            const data = await resp.json();
            if (!resp.ok || data.error) throw new Error(data.error || resp.statusText);
            this.data = data;
            this.picked = null;
            this.render();
            this.fillTimes(data);
            this.renderReminders();
            const wi = data.what_if ? ` · what-if: ${Object.keys(data.what_if.applied || {}).length} radio(s)` : '';
            const asOf = data.as_of ? ` · as of ${data.as_of.slice(0, 16)} (${Object.keys(data.history_channels || {}).length} radio(s) on another channel then)` : '';
            if (status) status.textContent = `${data.rows.length} location(s) · ${data.generated_at}${wi}${asOf}`;
        } catch (err) {
            if (status) status.textContent = `Failed: ${err.message}`;
        }
    },

    // History picker (TODO #70): "Now" plus the times of the scans shown.
    fillTimes(data) {
        const sel = document.getElementById('sp-at');
        if (!sel) return;
        const keep = sel.value;
        const times = data.scan_times || [];
        sel.innerHTML = '<option value="">Now</option>' + times.map(t =>
            `<option value="${this.esc(t)}">${this.esc(t.slice(0, 16))} (scan)</option>`).join('');
        if (keep && !times.includes(keep)) sel.add(new Option(keep.slice(0, 16), keep));
        sel.value = keep;
    },

    // Scan reminders (TODO #71) with "queue for tonight".
    renderReminders() {
        const box = document.getElementById('sp-reminders');
        const d = this.data;
        if (!box || !d) return;
        const rem = d.reminders || [];
        const survey = d.survey || {};
        box.hidden = !!d.as_of;
        if (d.as_of) { box.innerHTML = ''; return; }
        if (!rem.length) {
            box.innerHTML = `<p class="form-hint">Every transmitting infrastructure radio has a WiFi scan from the last
                ${survey.reminder_days ?? 14} days.</p>`;
            return;
        }
        box.innerHTML = `<h4>Scan reminders (${rem.length})</h4>
            <p class="form-hint">A scan shows what WiFi each antenna really hears. Queued radios are scanned at
                ${String(survey.hour ?? 3).padStart(2, '0')}:15 tonight${survey.enabled ? ' with the nightly survey' : ' (the nightly survey is off; only queued radios are scanned)'}.</p>
            <div class="table-scroll-wrapper"><table class="info-table diag-table"><thead><tr><th></th><th>Radio</th><th>Site</th><th>Why</th><th>Cost of scanning</th></tr></thead>
            <tbody>${rem.map(r => `<tr><td><input type="checkbox" class="sp-rem" value="${this.esc(r.radio)}" ${r.queued ? 'disabled checked' : ''}></td>
                <td>${this.esc(r.radio)}${r.queued ? ' <small class="hs-infra">queued</small>' : ''}</td><td>${this.esc(r.site)}</td>
                <td>${this.esc(r.why)}</td><td class="form-hint">${this.esc(r.impact)}</td></tr>`).join('')}</tbody></table></div>
            <div class="mh-controls"><button id="sp-queue" class="btn btn-small btn-primary">Scan checked radios tonight</button>
                ${(survey.queued || []).length ? '<button id="sp-unqueue" class="btn btn-small btn-secondary">Clear tonight\'s queue</button>' : ''}
                <span id="sp-queue-msg" class="form-hint"></span></div>`;
        box.querySelector('#sp-queue')?.addEventListener('click', async () => {
            const radios = [...box.querySelectorAll('.sp-rem:checked:not(:disabled)')].map(i => i.value);
            const msg = box.querySelector('#sp-queue-msg');
            if (!radios.length) { msg.textContent = 'Check one or more radios first.'; return; }
            const resp = await fetch('/api/wifi-survey/tonight', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                                  body: JSON.stringify({ radios, reason: 'spectrum scan reminder' }) });
            const r = await resp.json();
            msg.textContent = resp.ok ? `Queued ${r.added.length} radio(s).` : (r.error || 'Failed');
            if (resp.ok) this.load();
        });
        box.querySelector('#sp-unqueue')?.addEventListener('click', async () => {
            await fetch('/api/wifi-survey/tonight', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            this.load();
        });
    },

    short(name, codes) {
        const parts = String(name).split('-');
        if (parts.length > 2 && (codes || []).some(c => c.toLowerCase() === parts[1].toLowerCase())) {
            return parts.slice(2).join('-');
        }
        return name;
    },

    fmt(f) {
        return Number.isInteger(f) ? String(f) : f.toFixed(1);
    },

    render() {
        const host = document.getElementById('sp-chart');
        const d = this.data;
        if (!host || !d) return;
        const lo = d.band.low, hi = d.band.high;
        const width = Math.max(720, host.clientWidth - 2);
        const plotW = width - this.LABEL_W - 12;
        const x = f => this.LABEL_W + (f - lo) / (hi - lo) * plotW;
        const pxPer10 = plotW / ((hi - lo) / 10);
        const labelStep = pxPer10 >= 34 ? 10 : 20;
        const AXIS = 46, SITE_H = 46, SCAN_H = 26, GAP = 10;

        // Layout rows first so the gridlines can span the full height.
        const rows = [{ kind: 'typical', h: SCAN_H, cells: d.typical }];
        d.rows.forEach(r => {
            rows.push({ kind: 'site', h: SITE_H, row: r, cells: r.cells, gapBefore: GAP });
            r.scans.forEach(s => rows.push({ kind: 'scan', h: SCAN_H, row: r, scan: s, cells: s.cells }));
        });
        rows.push({ kind: 'common', h: SCAN_H, cells: d.common.cells, gapBefore: GAP });
        let y = AXIS;
        rows.forEach(r => { y += r.gapBefore || 0; r.y = y; y += r.h; });
        const height = y + 8;

        const out = [];
        out.push(`<svg class="sp-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">`);
        out.push(`<defs><pattern id="sp-may" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill="${this.COLORS.some}" opacity="0.45"/><line x1="0" y1="0" x2="0" y2="6" stroke="#c9a227" stroke-width="2"/></pattern></defs>`);

        // Axis: MHz labels and channel numbers.
        const first = Math.ceil(lo / labelStep) * labelStep;
        for (let f = first; f <= hi; f += labelStep) {
            out.push(`<text class="sp-axis" x="${x(f)}" y="16" text-anchor="middle">${f}</text>`);
            out.push(`<text class="sp-axis sp-ch" x="${x(f)}" y="30" text-anchor="middle">ch ${Math.round((f - 5000) / 5)}</text>`);
        }
        out.push(`<text class="sp-axis" x="${this.LABEL_W - 8}" y="16" text-anchor="end">MHz</text>`);
        out.push(`<text class="sp-axis sp-ch" x="${this.LABEL_W - 8}" y="30" text-anchor="end">channel</text>`);

        // Rows.
        rows.forEach((r, ri) => {
            const top = r.y, h = r.h;
            let label = '', cls = 'sp-label';
            if (r.kind === 'typical') {
                label = 'Typical WiFi / radar (planner guide)';
                cls += ' sp-label-muted';
            } else if (r.kind === 'site') {
                const tag = r.row.surveyed ? '' : ' · no scan';
                label = `${r.row.name}${tag}`;
                cls += ' sp-label-site';
            } else if (r.kind === 'scan') {
                const az = r.scan.azimuth != null ? ` → ${Math.round(r.scan.azimuth)}°` : '';
                label = `↳ ${this.short(r.scan.radio, r.row.codes)} hears${az}`;
                cls += ' sp-label-scan';
            } else {
                label = 'Free at every location shown';
                cls += ' sp-label-common';
            }
            out.push(`<text class="${cls}" data-row="${ri}" x="8" y="${top + h / 2 + 4}">${this.esc(label)}</text>`);
            r.cells.forEach((c, ci) => {
                let fill;
                if (r.kind === 'common') fill = c.free ? '#8fd19e' : '#f3f3f3';
                else if (r.kind === 'typical') fill = this.COLORS[c.status];
                else fill = this.COLORS[c.status];
                const mayOnly = (r.kind === 'site' || r.kind === 'scan') && c.status === 'some'
                    && c.score === 0 && (c.may || []).length;
                const w = x(c.high) - x(c.low);
                out.push(`<rect class="sp-cell" data-row="${ri}" data-cell="${ci}" x="${x(c.low)}" y="${top}" width="${w}" height="${h}"
                    fill="${mayOnly ? 'url(#sp-may)' : fill}"><title>${this.esc(this.cellTitle(r, c))}</title></rect>`);
                const changed = r.kind === 'scan' && r.scan.changes && r.scan.changes.cells[ci];
                if (changed) {
                    out.push(`<rect class="sp-change sp-change-${changed}" x="${x(c.low)}" y="${top}" width="${w}" height="4" pointer-events="none"/>`);
                }
            });
        });

        // Gridlines over the cells: every 10 MHz, darker every 50, ham band edges.
        const gridTop = AXIS - 6, gridBottom = height - 8;
        for (let f = Math.ceil(lo / 10) * 10; f <= hi; f += 10) {
            const strong = f % 50 === 0;
            out.push(`<line class="${strong ? 'sp-grid sp-grid-strong' : 'sp-grid'}" x1="${x(f)}" x2="${x(f)}" y1="${gridTop}" y2="${gridBottom}"/>`);
        }
        [d.ham_band.low, d.ham_band.high].forEach(f => {
            if (f > lo && f < hi) {
                out.push(`<line class="sp-ham" x1="${x(f)}" x2="${x(f)}" y1="${gridTop - 4}" y2="${gridBottom}"><title>Edge of the ham allocation (${f} MHz)</title></line>`);
            }
        });

        // Our radios as bars on the site rows.
        rows.forEach(r => {
            if (r.kind !== 'site') return;
            const n = r.row.ours.length;
            const lanes = this.stack(r.row.ours);
            const laneCount = Math.max(1, ...lanes.map(l => l.lane + 1));
            const barH = Math.min(22, (r.h - 6) / laneCount);
            lanes.forEach(({ o, lane }) => {
                const bx = x(Math.max(lo, o.low)), bw = Math.max(2, x(Math.min(hi, o.high)) - bx);
                const by = r.y + 3 + lane * barH;
                const name = this.short(o.name, r.row.codes);
                const text = `${name} ${this.fmt(o.frequency)}/${o.chanbw || '?'}`;
                const tip = `${o.name}: ${this.fmt(o.low)}–${this.fmt(o.high)} MHz (ch ${o.channel ?? '?'}, ${o.chanbw || '?'} MHz)`
                    + (o.azimuth != null ? `, aimed ${Math.round(o.azimuth)}°` : '')
                    + (o.what_if && o.original ? (d.as_of ? ` · then; today ${this.fmt(o.original.frequency)}/${o.original.chanbw}`
                                                         : ` · what-if, now ${this.fmt(o.original.frequency)}/${o.original.chanbw}`) : '');
                out.push(`<rect class="sp-ours${o.what_if ? ' sp-ours-whatif' : ''}${o.is_infrastructure ? '' : ' sp-ours-client'}"
                    x="${bx}" y="${by}" width="${bw}" height="${barH - 2}" rx="3"><title>${this.esc(tip)}</title></rect>`);
                if (bw > text.length * 5.6 + 6) {
                    out.push(`<text class="sp-ours-text" x="${bx + bw / 2}" y="${by + barH / 2 + 2}" text-anchor="middle">${this.esc(text)}</text>`);
                } else if (bw > name.length * 5.6 + 4) {
                    out.push(`<text class="sp-ours-text" x="${bx + bw / 2}" y="${by + barH / 2 + 2}" text-anchor="middle">${this.esc(name)}</text>`);
                }
                if (o.what_if && o.original && o.original.frequency) {
                    const w0 = (o.original.chanbw || 20) / 2;
                    const ox = x(o.original.frequency - w0), ow = x(o.original.frequency + w0) - ox;
                    out.push(`<rect class="sp-ours-old" x="${ox}" y="${by}" width="${ow}" height="${barH - 2}" rx="3"><title>${this.esc(o.name)} today</title></rect>`);
                }
            });
            if (!n) out.push(`<text class="sp-none" x="${this.LABEL_W + 6}" y="${r.y + r.h / 2 + 4}">no radios of ours in this range</text>`);
        });
        out.push('</svg>');
        host.innerHTML = out.join('');
        this.rows = rows;

        host.querySelectorAll('.sp-cell').forEach(el => el.addEventListener('click', () =>
            this.showCell(Number(el.dataset.row), Number(el.dataset.cell))));
        host.querySelectorAll('.sp-label').forEach(el => el.addEventListener('click', () =>
            this.showRow(Number(el.dataset.row))));
        this.renderSummary();
    },

    // Overlapping bars (e.g. Collins sectors 10 MHz apart) go on separate lanes.
    stack(ours) {
        const sorted = [...ours].sort((a, b) => a.low - b.low);
        const ends = [];
        return sorted.map(o => {
            let lane = ends.findIndex(e => e <= o.low);
            if (lane < 0) { lane = ends.length; ends.push(o.high); } else ends[lane] = o.high;
            return { o, lane };
        });
    },

    cellTitle(r, c) {
        const span = `${this.fmt(c.low)}–${this.fmt(c.high)} MHz`;
        if (r.kind === 'typical') return `${span}: ${c.labels.length ? c.labels.join('; ') : 'no typical users'}`;
        if (r.kind === 'common') return `${span}: ${c.free ? 'free (or not scanned) at every location' : 'used somewhere'}`;
        const parts = [`${span}: ${this.LABELS[c.status]}`];
        if (c.ours?.length) parts.push(`ours: ${c.ours.join(', ')}`);
        if (c.mesh?.length) parts.push(`mesh in range: ${c.mesh.join(', ')}`);
        if (c.wifi?.length) parts.push(`${c.wifi.length} WiFi AP(s)`);
        if (c.may?.length) parts.push(`${c.may.length} AP(s) of unknown width may reach here`);
        if (c.guard && !c.ours?.length) parts.push('within 10 MHz of one of ours');
        return parts.join(' · ') + ' — click for details';
    },

    apList(aps, idx) {
        if (!idx.length) return '';
        const items = idx.map(i => aps[i]).sort((a, b) => (b.signal ?? -200) - (a.signal ?? -200)).map(a => {
            const span = `${this.fmt(a.low)}–${this.fmt(a.high)}`;
            const may = a.may_low != null ? ` (width not in scan; may extend ${this.fmt(a.may_low)}–${this.fmt(a.may_high)})` : '';
            return `<li>${this.esc(a.ssid && a.ssid !== '-' ? a.ssid : '(hidden)')} <span class="form-hint">${this.esc(a.bssid || '')}</span>
                ${a.signal ?? '?'} dBm · ${span} MHz${may}</li>`;
        });
        return `<ul class="sp-list">${items.join('')}</ul>`;
    },

    showCell(ri, ci) {
        const r = this.rows[ri];
        const c = r.cells[ci];
        const box = document.getElementById('sp-detail');
        if (!box || !r || !c) return;
        const span = `${this.fmt(c.low)}–${this.fmt(c.high)} MHz`;
        let html = '';
        if (r.kind === 'typical') {
            html = `<h4>${span}: typical users</h4><p>${this.esc(c.labels.join('; ') || 'None listed')}.</p>
                <p class="form-hint">General guide the channel planner uses where no scan exists; real scans below override it.</p>`;
        } else if (r.kind === 'common') {
            const where = this.data.rows.map(row => `${this.esc(row.name)}: ${this.esc(this.LABELS[row.cells[ci].status])}`);
            html = `<h4>${span} across locations</h4><ul class="sp-list">${where.map(w => `<li>${w}</li>`).join('')}</ul>`;
        } else {
            const row = r.row;
            const title = r.kind === 'scan' ? `${r.scan.radio} scan` : row.name;
            const ours = row.ours.filter(o => c.ours.includes(o.name));
            const mesh = row.mesh.filter(m => c.mesh.includes(m.name));
            const aps = r.kind === 'scan' ? r.scan.aps : row.aps;
            html = `<h4>${this.esc(title)} · ${span}: ${this.esc(this.LABELS[c.status])}</h4>`;
            if (ours.length) html += `<p><strong>Ours:</strong> ${ours.map(o => `${this.esc(o.name)} ${this.fmt(o.low)}–${this.fmt(o.high)}`).join(', ')}</p>`;
            if (mesh.length) {
                html += `<p><strong>Mesh radios in range</strong> (other sites, facing this one or faced by it):</p><ul class="sp-list">`
                    + mesh.map(m => `<li>${this.esc(m.name)} ${this.fmt(m.low)}–${this.fmt(m.high)} MHz · ${(m.distance_m / 1000).toFixed(1)} km · ${this.esc(m.facing)}</li>`).join('')
                    + '</ul>';
            }
            if (c.wifi.length) html += `<p><strong>WiFi access points</strong> (${c.wifi.length}):</p>${this.apList(aps, c.wifi)}`;
            if (c.may.length) html += `<p><strong>May reach here</strong> (scan didn't give their width):</p>${this.apList(aps, c.may)}`;
            const ch = r.kind === 'scan' ? r.scan.changes : null;
            if (ch && ch.cells[ci]) {
                const inBin = list => list.filter(a => a.low < c.high && a.high > c.low);
                const fmtAp = a => `${this.esc(a.ssid && a.ssid !== '-' ? a.ssid : '(hidden)')} ${a.signal ?? '?'} dBm`;
                html += `<p><strong>Since the previous scan</strong> (${this.esc(ch.previous_taken_at)}):
                    ${inBin(ch.new).map(a => `<span class="sp-new">+ ${fmtAp(a)}</span>`).join(' ')}
                    ${inBin(ch.gone).map(a => `<span class="sp-gone">− ${fmtAp(a)}</span>`).join(' ')}</p>`;
            }
            if (!ours.length && !mesh.length && !c.wifi.length && !c.may.length) {
                html += `<p>${c.status === 'unsurveyed'
                    ? 'No mesh radios here. Nothing at this site has been scanned, so WiFi is unknown — scan one of its radios (Diagnostics → WiFi Survey).'
                    : c.guard ? 'Nothing heard, but within 10 MHz of one of our radios on this mast.' : 'Nothing heard here.'}</p>`;
            }
        }
        box.innerHTML = html;
    },

    showRow(ri) {
        const r = this.rows[ri];
        const box = document.getElementById('sp-detail');
        if (!box || !r) return;
        const runs = list => list.length
            ? list.map(s => `${this.fmt(s.low)}–${this.fmt(s.high)} (${s.mhz} MHz)`).join(', ') : 'none 20 MHz or wider';
        if (r.kind === 'site') {
            const row = r.row;
            box.innerHTML = `<h4>${this.esc(row.name)}</h4>
                <p><strong>Our radios:</strong> ${row.ours.map(o => `${this.esc(o.name)} ${this.fmt(o.frequency)}/${o.chanbw || '?'}`).join(', ') || 'none'}</p>
                <p><strong>Free here:</strong> ${runs(row.free)}${row.surveyed ? '' : ' — not scanned, so only mesh radios were checked'}</p>
                <p><strong>Scans:</strong> ${row.scans.map(s => `${this.esc(s.radio)} (${this.esc(s.taken_at || '?')})`).join(', ') || 'none'}</p>
                <p><strong>Mesh radios in range:</strong> ${row.mesh.length} · <strong>WiFi APs heard:</strong> ${row.aps.length}</p>`;
        } else if (r.kind === 'scan') {
            const ch = r.scan.changes;
            box.innerHTML = `<h4>${this.esc(r.scan.radio)} scan (${this.esc(r.scan.taken_at || '?')})</h4>
                <p>${r.scan.aps.length} WiFi access point(s). <strong>Free in this antenna's view:</strong> ${runs(r.scan.free)}</p>
                <p><strong>Since the previous scan:</strong> ${ch ? `${ch.new.length} new, ${ch.gone.length} gone (previous ${this.esc(ch.previous_taken_at)})`
                    : 'no earlier scan to compare'}</p>`;
        } else if (r.kind === 'common') {
            box.innerHTML = `<h4>Free at every location shown</h4><p>${runs(this.data.common.free)}</p>
                <p><strong>Free at every scanned location</strong> (${this.data.common.surveyed_sites.map(s => this.esc(s)).join(', ') || 'none'}): ${runs(this.data.common.free_surveyed)}</p>`;
        }
    },

    renderSummary() {
        const box = document.getElementById('sp-summary');
        if (!box || !this.data) return;
        const runs = list => list.length
            ? list.map(s => `<strong>${this.fmt(s.low)}–${this.fmt(s.high)}</strong> (${s.mhz} MHz)`).join(', ') : 'none 20 MHz or wider';
        const perSite = this.data.rows.map(r =>
            `<li>${this.esc(r.name)}: ${runs(r.free)}${r.surveyed ? '' : ' <span class="form-hint">(not scanned)</span>'}</li>`).join('');
        box.innerHTML = `<p>Free at every location shown: ${runs(this.data.common.free)}</p><ul class="sp-list">${perSite}</ul>`;
        const legend = document.getElementById('sp-legend');
        if (legend) {
            legend.innerHTML = ['free', 'some', 'busy', 'guard', 'unsurveyed'].map(k =>
                `<span class="sp-key"><i style="background:${this.COLORS[k]}"></i>${this.LABELS[k]}</span>`).join('')
                + '<span class="sp-key"><i class="sp-key-may"></i>AP of unknown width may reach</span>'
                + '<span class="sp-key"><i class="sp-key-ours"></i>Our radio</span>'
                + '<span class="sp-key"><i class="sp-key-whatif"></i>What-if / as of (dashed = today)</span>'
                + '<span class="sp-key"><i class="sp-key-change"></i>Changed since the previous scan</span>';
        }
    },
};

document.addEventListener('DOMContentLoaded', () => Spectrum.hookTab());
