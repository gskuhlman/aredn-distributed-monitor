/**
 * WiFi scan results on the Channel Planning page: pick a radio's newest scan
 * or a site's scans pooled (GET /api/wifi-survey for the list,
 * /api/wifi-survey/detail for one), then draw the access points it heard by
 * frequency and signal, a 5 MHz busy strip (free / light / moderate / busy,
 * scored like the planner's WiFi cost in wifi_survey.py) and our radios'
 * channels, with the AP table below. Read-only.
 */
const WifiScans = {
    initialized: false,
    detail: null,
    LEVEL_COLORS: { free: '#58d68d', light: '#f7dc6f', moderate: '#f5b041', busy: '#e74c3c' },

    hookTab() {
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                if (btn.dataset.tab === 'planning') this.open();
            });
        });
    },

    open() {
        if (this.initialized) return;
        if (!document.getElementById('ws-pick')) return;
        this.initialized = true;
        document.getElementById('ws-pick').addEventListener('change', () => this.load());
        document.getElementById('ws-range').addEventListener('change', () => this.load());
        document.getElementById('ws-mesh').addEventListener('change', () => this.renderTable());
        document.getElementById('ws-reload').addEventListener('click', () => this.loadList());
        document.getElementById('ws-chart').addEventListener('click', (event) => {
            const slot = event.target.closest('[data-slot]');
            if (slot) this.showSlot(Number(slot.dataset.slot));
        });
        this.loadList();
    },

    /** Dropdown: sites (scans of their radios pooled) then single radios. */
    async loadList() {
        const select = document.getElementById('ws-pick');
        const keep = select.value;
        this.setStatus('Loading scans...');
        try {
            const data = await (await fetch('/api/wifi-survey')).json();
            const nodes = data.nodes || [];
            if (!nodes.length) {
                select.innerHTML = '';
                this.setStatus('');
                document.getElementById('ws-chart').innerHTML = `<p class="log-empty">No WiFi scans yet. Paste a radio's
                    scan table (WiFi Survey above, Import) or scan a radio from there.</p>`;
                document.getElementById('ws-table').innerHTML = '';
                return;
            }
            const sites = {};
            nodes.filter(n => n.site).forEach(n => { (sites[n.site] = sites[n.site] || []).push(n.node); });
            const siteOpts = Object.keys(sites).sort().map(code =>
                `<option value="site:${this.esc(code)}">${this.esc(code)}: all scans at the site (${sites[code].length})</option>`).join('');
            const nodeOpts = nodes.map(n => `<option value="node:${this.esc(n.node)}">${this.esc(n.node)}
                (${n.non_mesh} AP${n.non_mesh === 1 ? '' : 's'}, ${this.esc((n.taken_at || '').slice(0, 16))})</option>`).join('');
            select.innerHTML = (siteOpts ? `<optgroup label="Sites">${siteOpts}</optgroup>` : '') +
                `<optgroup label="Radios">${nodeOpts}</optgroup>`;
            if (keep && select.querySelector(`option[value="${CSS.escape(keep)}"]`)) select.value = keep;
            await this.load();
        } catch (error) {
            console.error('WiFi scan list error:', error);
            this.setStatus('Could not load the scan list');
        }
    },

    async load() {
        const pick = document.getElementById('ws-pick').value;
        if (!pick) return;
        const [kind, name] = [pick.slice(0, pick.indexOf(':')), pick.slice(pick.indexOf(':') + 1)];
        const [low, high] = document.getElementById('ws-range').value.split('-').map(Number);
        this.setStatus('Loading...');
        document.getElementById('ws-slot').innerHTML = '';
        try {
            const resp = await fetch(`/api/wifi-survey/detail?${kind}=${encodeURIComponent(name)}&low=${low}&high=${high}`);
            const data = await resp.json();
            if (!resp.ok) {
                this.detail = null;
                document.getElementById('ws-chart').innerHTML = `<p class="log-empty">${this.esc(data.error || 'No scan')}</p>`;
                document.getElementById('ws-table').innerHTML = '';
                this.setStatus('');
                return;
            }
            this.detail = data;
            this.renderChart();
            this.renderTable();
            const scans = data.scans.map(s => `${s.node} ${String(s.taken_at || '').slice(0, 16)}`).join(', ');
            this.setStatus(`${data.non_mesh} non-mesh AP(s) heard · ${data.signal_known} with a signal level · ${scans}`);
        } catch (error) {
            console.error('WiFi scan detail error:', error);
            this.setStatus('Load failed');
        }
    },

    /** Upper panel: each AP as a bar across its channel at its signal level;
     * middle strip: 5 MHz busy level; lower lanes: our radios' channels. */
    renderChart() {
        const d = this.detail;
        const el = document.getElementById('ws-chart');
        const lo = d.band.low, hi = d.band.high;
        const inRange = a => a.low != null && a.high > lo && a.low < hi;
        const wifi = d.aps.filter(a => !a.is_mesh && inRange(a));
        const leveled = wifi.filter(a => a.signal != null);
        const meshLeveled = d.aps.filter(a => a.is_mesh && a.signal != null && inRange(a));
        // Many radios' scan pages (Rocket sectors, omnis) report "-" for signal: those
        // APs only say a channel is in use, so they are counted per slot, not drawn by level.
        const showLevels = leveled.length + meshLeveled.length > 0;
        const W = 960, left = 70, right = 12, top = 8;
        const apH = showLevels ? 150 : 0;
        const countY = top + apH + (showLevels ? 14 : 0), countH = 90;
        const stripY = countY + countH + 10, stripH = 26;
        const oursY = stripY + stripH + 14, laneH = 14;
        const ours = this.lanes(d.ours);
        const lanes = Math.max(1, ...ours.map(o => o.lane + 1));
        const axisY = oursY + lanes * laneH + 6;
        const H = axisY + 34;
        const x = f => left + (Math.min(Math.max(f, lo), hi) - lo) / (hi - lo) * (W - left - right);
        const sigMin = -95, sigMax = -30;
        const y = s => top + (sigMax - Math.min(Math.max(s, sigMin), sigMax)) / (sigMax - sigMin) * apH;
        const parts = [];

        // Ham band and signal grid.
        const [hamLo, hamHi] = d.ham_band;
        if (hamHi > lo && hamLo < hi) {
            parts.push(`<rect x="${x(hamLo)}" y="${top}" width="${x(hamHi) - x(hamLo)}" height="${axisY - top}" class="ws-ham">
                <title>5 GHz ham band ${hamLo}–${hamHi} MHz</title></rect>`);
        }
        if (showLevels) {
            for (let s = -90; s <= -30; s += 10) {
                parts.push(`<line x1="${left}" x2="${W - right}" y1="${y(s)}" y2="${y(s)}" class="spec-grid"/>
                    <text x="${left - 6}" y="${y(s) + 4}" class="spec-label">${s} dBm</text>`);
            }
        }
        const step = hi - lo > 300 ? 50 : 10;
        for (let f = Math.ceil(lo / step) * step; f <= hi; f += step) {
            const major = f % 50 === 0;
            parts.push(`<line x1="${x(f)}" x2="${x(f)}" y1="${top}" y2="${axisY}" class="spec-grid${major ? ' ws-major' : ''}"/>`);
            if (major || step === 50) {
                parts.push(`<text x="${x(f)}" y="${axisY + 13}" class="spec-tick">${f}</text>`);
            }
            if (step === 10 && f % 20 === 0) {
                parts.push(`<text x="${x(f)}" y="${axisY + 27}" class="spec-tick ws-ch">ch ${(f - 5000) / 5}</text>`);
            }
        }
        parts.push(`<text x="${left - 6}" y="${axisY + 13}" class="spec-label">MHz</text>`);

        // Access points with a level: one bar across the channel at its signal.
        leveled.forEach(a => {
            const tip = `${a.ssid || '(hidden)'} ${a.bssid || ''}\n${a.frequency} MHz ch ${a.channel ?? '?'} · ` +
                `${a.low}–${a.high} MHz${a.width_known ? '' : ' (20 MHz assumed)'}\n${a.signal} dBm · heard by ${a.heard_by}`;
            parts.push(`<line x1="${x(a.low) + 1}" x2="${x(a.high) - 1}" y1="${y(a.signal)}" y2="${y(a.signal)}"
                class="ws-ap${a.width_known ? '' : ' ws-ap-assumed'}"><title>${this.esc(tip)}</title></line>`);
        });
        meshLeveled.forEach(a => {
            parts.push(`<line x1="${x(a.low) + 1}" x2="${x(a.high) - 1}" y1="${y(a.signal)}" y2="${y(a.signal)}" class="ws-ap-mesh">
                <title>${this.esc(`mesh: ${a.ssid || ''} ${a.frequency} MHz ${a.signal} dBm, heard by ${a.heard_by}`)}</title></line>`);
        });
        if (showLevels) {
            parts.push(`<text x="${left + 4}" y="${top + 12}" class="spec-label ws-panel">access points with a signal level (higher = louder)</text>`);
        }

        // WiFi APs per 5 MHz slot, stacked: with a level / no level in the scan.
        const counts = d.slots.map(s => {
            const here = wifi.filter(a => Math.min(s.high, a.high) > Math.max(s.low, a.low));
            const known = here.filter(a => a.signal != null).length;
            return { known, unknown: here.length - known };
        });
        const maxCount = Math.max(1, ...counts.map(c => c.known + c.unknown));
        const ch = n => n / maxCount * (countH - 16);
        const base = countY + countH;
        [0.5, 1].forEach(f => {
            const n = Math.round(maxCount * f);
            parts.push(`<line x1="${left}" x2="${W - right}" y1="${base - ch(n)}" y2="${base - ch(n)}" class="spec-grid"/>
                <text x="${left - 6}" y="${base - ch(n) + 4}" class="spec-label">${n}</text>`);
        });
        d.slots.forEach((s, i) => {
            const c = counts[i];
            if (!c.known && !c.unknown) return;
            const xw = Math.max(1, x(s.high) - x(s.low) - 0.5);
            const tip = `${s.low}–${s.high} MHz: ${c.known + c.unknown} WiFi AP(s)` +
                (c.unknown ? `, ${c.unknown} with no signal level` : '') + (s.ssids.length ? `\n${s.ssids.join(', ')}` : '');
            parts.push(`<g class="ws-count" data-slot="${i}"><title>${this.esc(tip)}</title>
                <rect x="${x(s.low)}" y="${base - ch(c.unknown)}" width="${xw}" height="${ch(c.unknown)}" class="ws-count-unknown"/>
                <rect x="${x(s.low)}" y="${base - ch(c.known + c.unknown)}" width="${xw}" height="${ch(c.known)}" class="ws-count-known"/></g>`);
        });
        parts.push(`<text x="${left + 4}" y="${countY + 10}" class="spec-label ws-panel">WiFi APs heard per 5 MHz</text>
            <text x="${left - 6}" y="${base - 2}" class="spec-label">APs</text>`);

        // Busy strip.
        d.slots.forEach((s, i) => {
            const tip = `${s.low}–${s.high} MHz: ${s.level}` +
                (s.aps ? ` · ${s.aps} AP(s)${s.strongest != null ? `, strongest ${s.strongest} dBm` : ''}` : '') +
                (s.mesh ? ` · ${s.mesh} mesh` : '') + (s.ssids.length ? `\n${s.ssids.join(', ')}` : '');
            parts.push(`<rect x="${x(s.low)}" y="${stripY}" width="${Math.max(1, x(s.high) - x(s.low) - 0.5)}" height="${stripH}"
                fill="${this.LEVEL_COLORS[s.level] || '#ccc'}" class="ws-slot" data-slot="${i}"><title>${this.esc(tip)}</title></rect>`);
        });
        parts.push(`<text x="${left - 6}" y="${stripY + stripH / 2 + 4}" class="spec-label">busy</text>`);

        // Our radios.
        ours.forEach(o => {
            const yy = oursY + o.lane * laneH;
            parts.push(`<rect x="${x(o.low)}" y="${yy}" width="${Math.max(2, x(o.high) - x(o.low))}" height="${laneH - 3}" rx="2"
                class="ws-ours${o.scanned ? '' : ' ws-ours-unscanned'}"><title>${this.esc(`${o.name}: ${o.frequency}/${o.chanbw} MHz ` +
                `(ch ${(o.frequency - 5000) / 5})${o.scanned ? ', scanned' : ', no scan from this radio'}`)}</title></rect>`);
            if (x(o.high) - x(o.low) > 70) {
                parts.push(`<text x="${(x(o.low) + x(o.high)) / 2}" y="${yy + laneH - 5}" class="ws-ours-label">${this.esc(this.short(o.name))}</text>`);
            }
        });
        parts.push(`<text x="${left - 6}" y="${oursY + laneH - 4}" class="spec-label">ours</text>`);

        const legend = Object.entries(this.LEVEL_COLORS).map(([k, c]) =>
            `<span class="ws-key"><i style="background:${c}"></i>${k}</span>`).join('');
        el.innerHTML = `<div class="spec-wrap"><svg viewBox="0 0 ${W} ${H}" class="spec-chart ws-chart" role="img"
                aria-label="WiFi heard by frequency and signal">${parts.join('')}</svg></div>
            <div class="ws-legend">${legend}
                ${showLevels ? `<span class="ws-key"><i class="ws-key-ap"></i>WiFi AP by level (dashed = width assumed 20 MHz)</span>
                <span class="ws-key"><i class="ws-key-mesh"></i>mesh radio</span>` : ''}
                <span class="ws-key"><i class="ws-key-known"></i>APs with a level</span>
                <span class="ws-key"><i class="ws-key-unknown"></i>APs, no level in the scan</span>
                <span class="ws-key"><i class="ws-key-ours"></i>our radio (hatched = not scanned)</span>
                <span class="ws-key"><i class="ws-key-ham"></i>ham band</span></div>
            <p class="form-hint">Busy = WiFi APs overlapping each 5 MHz slot, weighted by signal (louder counts more,
                unknown signal counted as weak, -80 dBm), the same weighting the channel planner uses. Click a slot for its APs.
                ${wifi.length && leveled.length < wifi.length ? `<strong>${wifi.length - leveled.length} of ${wifi.length}
                AP(s) here have no signal level:</strong> ${d.scans.length > 1 ? 'some radios\'' : 'this radio\'s'} scan page showed "-" for
                SNR and Signal (seen on the Rocket sectors and omnis), so they only show which channels are in use, not how loud. The planner counts each of them as a weak (-80 dBm) AP.` : ''}</p>`;
    },

    /** Stack overlapping radio bars into lanes. */
    lanes(list) {
        const ends = [];
        return [...list].sort((a, b) => a.low - b.low).map(o => {
            let lane = ends.findIndex(e => e <= o.low);
            if (lane < 0) { lane = ends.length; ends.push(0); }
            ends[lane] = o.high;
            return { ...o, lane };
        });
    },

    short(name) {
        return String(name).replace(/^[a-z0-9]+-/i, '');
    },

    showSlot(i) {
        const s = this.detail?.slots[i];
        const el = document.getElementById('ws-slot');
        if (!s) return;
        const here = this.detail.aps.filter(a => a.low != null && Math.min(s.high, a.high) > Math.max(s.low, a.low));
        const ours = this.detail.ours.filter(o => Math.min(s.high, o.high) > Math.max(s.low, o.low));
        el.innerHTML = `<p><strong>${s.low}–${s.high} MHz</strong> (ch ${(s.low + 2.5 - 5000) / 5}):
            <span class="ws-level" style="background:${this.LEVEL_COLORS[s.level]}">${s.level}</span>
            score ${s.score} · ${s.aps} WiFi AP(s), ${s.mesh} mesh
            ${ours.length ? ` · ours: ${ours.map(o => this.esc(o.name)).join(', ')}` : ''}</p>
            ${here.length ? `<ul class="diag-evidence">${here.map(a => `<li>${a.is_mesh ? '<small>mesh</small> ' : ''}
                <strong>${this.esc(a.ssid || '(hidden)')}</strong> ${a.frequency} MHz ${a.low}–${a.high}
                · ${a.signal != null ? a.signal + ' dBm' : 'signal unknown'} · heard by ${this.esc(a.heard_by)}</li>`).join('')}</ul>` : ''}`;
    },

    renderTable() {
        const d = this.detail;
        const el = document.getElementById('ws-table');
        if (!d) { el.innerHTML = ''; return; }
        const mesh = document.getElementById('ws-mesh').checked;
        const rows = d.aps.filter(a => (mesh || !a.is_mesh) && a.low != null && a.high > d.band.low && a.low < d.band.high);
        if (!rows.length) { el.innerHTML = '<p class="log-empty">Nothing heard in this range.</p>'; return; }
        const pooled = d.scans.length > 1;
        el.innerHTML = `<details class="diag-details" open><summary>${rows.length} access point(s) in ${d.band.low}–${d.band.high} MHz</summary>
            <div class="table-scroll-wrapper"><table class="info-table diag-table ws-table">
            <thead><tr><th>SSID</th><th>BSSID</th><th>Ch</th><th>MHz</th><th>Span</th><th>Signal</th>
                ${pooled ? '<th>Heard by</th>' : ''}<th></th></tr></thead>
            <tbody>${rows.map(a => `<tr${a.is_mesh ? ' class="ws-mesh-row"' : ''}>
                <td>${this.esc(a.ssid || '(hidden)')}</td><td><small>${this.esc(a.bssid || '')}</small></td>
                <td>${a.channel ?? ''}</td><td>${a.frequency ?? ''}</td>
                <td><small>${a.low ?? ''}–${a.high ?? ''}${a.width_known ? '' : ' (assumed)'}</small></td>
                <td>${a.signal != null ? a.signal + ' dBm' : '–'}</td>
                ${pooled ? `<td><small>${this.esc(a.heard_by)}</small></td>` : ''}
                <td>${a.is_mesh ? '<small>mesh</small>' : ''}</td></tr>`).join('')}</tbody></table></div></details>`;
    },

    setStatus(text) {
        const el = document.getElementById('ws-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => WifiScans.hookTab());
