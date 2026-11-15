/**
 * Spectrum page: "Who else uses these frequencies" - a fixed reference list
 * of the bands near AREDN's (US rules): ham allocations and AREDN channels,
 * WiFi / U-NII and ISM, radars, cellular / CBRS, vehicle (C-V2X), drone video
 * and satellite. Each entry says whether it shares spectrum with ham / AREDN
 * (the "dual use" column). The list can be limited to the MHz range shown in
 * the chart above and filtered by kind; below it, WiFi and AREDN channel
 * tables. Rules change: the notes say where to check. Read-only, no server.
 */
const SpectrumReference = {
    KINDS: {
        ham: ['Ham / AREDN', '#2e86c1'], wifi: ['WiFi / U-NII', '#28b463'], ism: ['ISM / Part 15', '#7d8b99'],
        radar: ['Radar', '#c0392b'], cell: ['Cellular / CBRS', '#8e44ad'], vehicle: ['Vehicle (C-V2X)', '#d35400'],
        drone: ['Drone / FPV video', '#b7950b'], sat: ['Satellite', '#16a085'], fixed: ['Fixed links', '#5d6d7e'],
    },

    // [low MHz, high MHz, kind, name, users, notes]
    BANDS: [
        [902, 928, 'ham', '33 cm ham band', 'Amateur (secondary)', 'AREDN 900 MHz radios. Shared with ISM below.'],
        [902, 928, 'ism', '900 MHz ISM', 'LoRa / Meshtastic, smart meters, cordless phones, RFID, WISP gear', 'Part 15: no protection, lots of hopping traffic.'],
        [2390, 2450, 'ham', '13 cm ham band', 'Amateur', 'AREDN 2 GHz. Channels -2 and -1 (2397, 2402 MHz) sit below WiFi channel 1; 1-11 are the WiFi channels.'],
        [2400, 2483.5, 'ism', '2.4 GHz ISM', 'Microwave ovens (strongest near 2450), Bluetooth (2402-2480), Zigbee, baby monitors, video senders, drones', 'Very crowded everywhere people live.'],
        [2401, 2495, 'wifi', '2.4 GHz WiFi', 'WiFi channels 1-11 (US); 1, 6 and 11 do not overlap', 'Channel 14 (2484) is Japan only.'],
        [2700, 3000, 'radar', 'S-band radar', 'NEXRAD weather radar (2700-3000), airport surveillance radar ASR (2700-2900)', 'High power; can desensitise nearby 2-3 GHz receivers.'],
        [3300, 3500, 'ham', '9 cm ham band', 'Amateur (secondary)', 'The FCC ordered the US ham allocation here removed (2020) and 3450-3550 went to 5G. Check current rules before using AREDN 3 GHz.'],
        [3100, 3450, 'radar', 'Military radar', 'Shipborne and airborne radar', 'Primary users of the 3 GHz ham band.'],
        [3450, 3550, 'cell', '5G (3.45 GHz)', 'Cellular 5G, shared with military radar', 'Auctioned 2021.'],
        [3550, 3700, 'cell', 'CBRS', 'Citizens Broadband Radio Service: private LTE/5G, fixed wireless ISPs', 'Navy radar has priority.'],
        [3700, 3980, 'cell', 'C-band 5G', 'Cellular 5G', 'Strong signals near towers; filters help 3 GHz radios.'],
        [5150, 5250, 'wifi', 'U-NII-1', 'WiFi channels 36-48', 'No radar detection needed.'],
        [5250, 5350, 'wifi', 'U-NII-2A', 'WiFi channels 52-64', 'DFS: WiFi must detect radar and move.'],
        [5250, 5925, 'radar', 'C-band radiolocation', 'Government / military radar (primary user)', 'Ham is secondary here: we must not interfere with radar and must accept it.'],
        [5350, 5650, 'radar', 'TV weather radar', 'TV-station Doppler weather radars (C band)', 'Bursty, very strong pulses; show up as intermittent noise.'],
        [5470, 5725, 'wifi', 'U-NII-2C', 'WiFi channels 100-144', 'DFS: WiFi must detect radar and move.'],
        [5600, 5650, 'radar', 'TDWR', 'Terminal Doppler Weather Radar at large airports', 'WiFi is barred from interfering; channels 120-128 are often avoided.'],
        [5645, 5945, 'drone', '5.8 GHz FPV video', 'Analog / digital drone and RC video (bands A, B, E, F, R; raceband 5658-5917)', 'Mobile, strong at short range, comes and goes.'],
        [5650, 5925, 'ham', '5 cm ham band', 'Amateur (secondary): AREDN channels 131-184 (5655-5920 MHz)', 'Channel number = (MHz - 5000) / 5.'],
        [5650, 5670, 'sat', 'Ham satellite uplink', 'Amateur-satellite Earth-to-space', 'Keep AREDN channels clear of it where satellites are worked.'],
        [5725, 5850, 'wifi', 'U-NII-3', 'WiFi channels 149-165; WISP and point-to-point links (Ubiquiti, Cambium, Mikrotik)', 'Overlaps AREDN channels 145-170.'],
        [5725, 5875, 'ism', '5.8 GHz ISM', 'Cordless phones, video senders, ISM devices', 'Part 15.'],
        [5759, 5761, 'ham', '5760 MHz weak signal', 'Narrowband ham (SSB/CW, EME) calling frequency', 'Leave 5760 clear of AREDN wideband channels where weak-signal operators are active.'],
        [5850, 5895, 'wifi', 'U-NII-4', 'WiFi channels 169-177 (opened to WiFi in 2020)', 'Overlaps AREDN channels 170-179; expect more WiFi here over time.'],
        [5895, 5925, 'vehicle', 'ITS / C-V2X', 'Vehicle-to-everything: traffic signals, connected cars', 'Overlaps AREDN channels 179-184.'],
        [5925, 7125, 'wifi', '6 GHz WiFi 6E / 7', 'WiFi 6E and 7 (channels 1-233)', 'Just above the ham band; out-of-band splatter possible near APs.'],
        [5925, 7125, 'fixed', '6 GHz fixed microwave', 'Licensed point-to-point links (utilities, public safety, carriers)', 'Shared with 6 GHz WiFi through automatic frequency coordination.'],
        [10000, 10500, 'ham', '3 cm ham band', 'Amateur; 10368 MHz weak-signal calling', 'AREDN has little gear here.'],
        [10500, 10550, 'radar', 'X-band speed radar', 'Police speed radar, motion sensors', ''],
    ],

    hamRanges: [[902, 928], [2390, 2450], [3300, 3500], [5650, 5925], [10000, 10500]],

    init() {
        const el = document.getElementById('sp-ref');
        if (!el || el.dataset.ready) return;
        el.dataset.ready = '1';
        el.innerHTML = `<h3>Who else uses these frequencies <small class="form-hint">US allocations near the AREDN bands; reference only</small></h3>
            <div class="mh-controls">
                <label class="header-checkbox"><input type="checkbox" id="sp-ref-range" checked> Only the MHz range above</label>
                <span class="sp-ref-kinds">${Object.entries(this.KINDS).map(([k, [label, color]]) =>
                    `<label class="header-checkbox"><input type="checkbox" data-ref-kind="${k}" checked>
                        <i class="sp-ref-dot" style="background:${color}"></i> ${label}</label>`).join('')}</span>
            </div>
            <div id="sp-ref-strip"></div>
            <div id="sp-ref-table"></div>
            <details class="diag-details"><summary>WiFi and AREDN channel numbers</summary><div id="sp-ref-channels"></div></details>`;
        el.addEventListener('change', () => this.render());
        ['sp-low', 'sp-high'].forEach(id => document.getElementById(id)?.addEventListener('change', () => this.render()));
        document.getElementById('sp-reload')?.addEventListener('click', () => this.render());
        this.render();
        document.getElementById('sp-ref-channels').innerHTML = this.channels();
    },

    range() {
        const lo = Number(document.getElementById('sp-low')?.value) || 5640;
        const hi = Number(document.getElementById('sp-high')?.value) || 5930;
        return [Math.min(lo, hi), Math.max(lo, hi)];
    },

    sharesHam(lo, hi, kind) {
        return kind !== 'ham' && this.hamRanges.some(([a, b]) => lo < b && hi > a);
    },

    rows() {
        const [lo, hi] = this.range();
        const only = document.getElementById('sp-ref-range')?.checked;
        const kinds = new Set([...document.querySelectorAll('[data-ref-kind]')].filter(c => c.checked).map(c => c.dataset.refKind));
        return this.BANDS.filter(b => kinds.has(b[2]) && (!only || (b[0] < hi && b[1] > lo)))
            .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    },

    render() {
        const rows = this.rows();
        const [lo, hi] = this.range();
        const only = document.getElementById('sp-ref-range')?.checked;
        const fmt = v => Number.isInteger(v) ? String(v) : v.toFixed(1);
        // Strip: one lane per entry across the chart's MHz range.
        const strip = document.getElementById('sp-ref-strip');
        if (only && rows.length) {
            const pct = f => Math.max(0, Math.min(100, (f - lo) / (hi - lo) * 100));
            strip.innerHTML = `<div class="sp-ref-strip">${rows.map(b => {
                const [label, color] = this.KINDS[b[2]];
                return `<div class="sp-ref-lane" title="${this.esc(`${b[3]}: ${fmt(b[0])}-${fmt(b[1])} MHz (${label})`)}">
                    <span class="sp-ref-lane-name">${this.esc(b[3])}</span>
                    <span class="sp-ref-lane-bar"><i style="left:${pct(b[0])}%;width:${Math.max(0.5, pct(b[1]) - pct(b[0]))}%;background:${color}"></i></span></div>`;
            }).join('')}
                <div class="sp-ref-lane sp-ref-axis"><span class="sp-ref-lane-name"></span>
                    <span class="sp-ref-lane-bar"><small style="left:0">${lo}</small><small style="right:0">${hi} MHz</small></span></div></div>`;
        } else {
            strip.innerHTML = '';
        }
        document.getElementById('sp-ref-table').innerHTML = rows.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>MHz</th><th>Kind</th><th>Band</th><th>Who uses it</th><th>Shares with ham / AREDN</th><th>Notes</th></tr></thead>
            <tbody>${rows.map(b => {
                const [label, color] = this.KINDS[b[2]];
                const share = b[2] === 'ham' ? '<small>ham band</small>' : this.sharesHam(b[0], b[1], b[2])
                    ? '<strong class="sp-ref-shared">yes</strong>' : '<small>no</small>';
                return `<tr><td class="nowrap">${fmt(b[0])}&ndash;${fmt(b[1])}</td>
                    <td class="nowrap"><i class="sp-ref-dot" style="background:${color}"></i> ${label}</td>
                    <td>${this.esc(b[3])}</td><td>${this.esc(b[4])}</td><td>${share}</td><td><small>${this.esc(b[5])}</small></td></tr>`;
            }).join('')}</tbody></table></div>`
            : `<p class="log-empty">Nothing listed${only ? ` between ${lo} and ${hi} MHz` : ''} for the kinds picked.</p>`;
    },

    /** WiFi 20 MHz channels (2.4 and 5 GHz) with the AREDN channels they cover. */
    channels() {
        const w24 = [];
        for (let c = 1; c <= 11; c++) w24.push([c, 2407 + 5 * c]);
        const w5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
            149, 153, 157, 161, 165, 169, 173, 177].map(c => [c, 5000 + 5 * c]);
        const band = c => c <= 48 ? 'U-NII-1' : c <= 64 ? 'U-NII-2A (DFS)' : c <= 144 ? 'U-NII-2C (DFS)' : c <= 165 ? 'U-NII-3' : 'U-NII-4';
        const aredn5 = f => {
            const lo = Math.max(131, Math.ceil((f - 10 - 5000) / 5)), hi = Math.min(184, Math.floor((f + 10 - 5000) / 5));
            return lo <= hi ? `${lo}&ndash;${hi}` : '';
        };
        const aredn24 = f => {
            const chs = [-2, -1, ...Array.from({ length: 11 }, (_, i) => i + 1)].filter(c => Math.abs(2407 + 5 * c - f) <= 10);
            return chs.length ? `${chs[0]}&ndash;${chs[chs.length - 1]}` : '';
        };
        const table = (rows, kind) => `<table class="info-table diag-table"><thead><tr><th>WiFi ch</th><th>Centre MHz</th><th>20 MHz span</th>
            ${kind === 5 ? '<th>Band</th>' : ''}<th>AREDN channels inside (centres)</th></tr></thead><tbody>${rows.map(([c, f]) => `<tr>
                <td>${c}</td><td>${f}</td><td>${f - 10}&ndash;${f + 10}</td>${kind === 5 ? `<td>${band(c)}</td>` : ''}
                <td>${kind === 5 ? aredn5(f) : aredn24(f)}</td></tr>`).join('')}</tbody></table>`;
        return `<p class="form-hint">AREDN 5 GHz channel = (MHz &minus; 5000) / 5, from 131 (5655) to 184 (5920). A 10 MHz AREDN channel
            covers its centre &plusmn;5 MHz, 20 MHz covers &plusmn;10. 40 and 80 MHz WiFi bond neighbouring channels, so one AP can cover far more.</p>
            <div class="sp-ref-chans"><div><h4>2.4 GHz</h4>${table(w24, 2)}</div><div><h4>5 GHz</h4>${table(w5, 5)}</div></div>`;
    },

    esc(v) {
        return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
};

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tab-btn[data-tab="spectrum"]').forEach(b => b.addEventListener('click', () => SpectrumReference.init()));
});
