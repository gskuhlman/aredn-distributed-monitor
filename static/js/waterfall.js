/**
 * Spectrum waterfall on the site dashboard (waterfall.py): one row per hour
 * (newest at the top), one column per 5 MHz slot. Cell colour = measured
 * noise of the site radios covering the slot (blue quiet -> red noisy), grey
 * = occupied without a noise reading; the number of site radios on a slot is
 * drawn as a darker band when more than one share it. Marks on the left edge:
 * channel changes, logged changes, maintenance; planned rollout channels are
 * outlined in the top row.
 */
const Waterfall = {
    data: null,

    async load(code) {
        const el = document.getElementById('site-waterfall');
        if (!el) return;
        const days = document.getElementById('wf-days')?.value || 7;
        try {
            const r = await fetch(`/api/sites/${encodeURIComponent(code)}/waterfall?days=${days}`);
            this.data = await r.json();
            if (!r.ok) throw new Error(this.data.error || 'failed');
            this.draw();
        } catch (error) {
            el.innerHTML = `<p class="log-empty">Waterfall unavailable: ${this.esc(error.message)}</p>`;
        }
    },

    noiseColor(v) {
        // -105 dBm (quiet) .. -80 dBm (noisy)
        const t = Math.max(0, Math.min(1, (v + 105) / 25));
        const hue = 230 - 230 * t;
        return `hsl(${hue}, 80%, ${45 + 10 * (1 - t)}%)`;
    },

    draw() {
        const d = this.data;
        const el = document.getElementById('site-waterfall');
        if (!d.slots.length) {
            el.innerHTML = '<p class="log-empty">No radios with a channel at this site.</p>';
            return;
        }
        const cw = Math.max(6, Math.min(14, Math.floor(900 / d.slots.length)));
        const rh = d.buckets.length > 240 ? 2 : d.buckets.length > 120 ? 3 : 4;
        const left = 70, top = 22;
        const w = left + cw * d.slots.length + 10, h = top + rh * d.buckets.length + 24;
        el.innerHTML = `<canvas width="${w}" height="${h}" class="wf-canvas"></canvas>
            <div class="wf-tip form-hint"></div>
            <div class="form-hint">Colour: measured noise (blue -105 dBm … red -80 dBm), grey: in use without a reading,
            darker: 2+ site radios on the slot. Left marks: <span style="color:#8e44ad">channel change</span>,
            <span style="color:#2980b9">logged change</span>, <span style="color:#e67e22">maintenance</span>.
            ${d.planned.length ? 'Outlined in the top row: channels a rollout still has to move radios to.' : ''}</div>
            ${d.marks.length ? `<details class="diag-details"><summary>${d.marks.length} mark(s)</summary><ul class="diag-evidence">
                ${d.marks.slice(-40).reverse().map(m => `<li>${this.esc(m.time.slice(0, 16))} ${this.esc(m.text)}</li>`).join('')}</ul></details>` : ''}`;
        const c = el.querySelector('canvas').getContext('2d');
        c.font = '10px sans-serif';
        c.fillStyle = '#566573';
        d.slots.forEach((s, k) => {
            if (s % 20 === 0) c.fillText(String(s), left + k * cw - 8, 12);
        });
        const n = d.buckets.length;
        for (let i = 0; i < n; i++) {
            const row = n - 1 - i;           // newest on top
            const y = top + row * rh;
            if (d.buckets[i].slice(11, 13) === '00') {
                c.fillStyle = '#566573';
                c.fillText(d.buckets[i].slice(5, 10), 2, y + 4);
            }
            d.slots.forEach((s, k) => {
                const occ = d.occupancy[i][k];
                if (!occ) return;
                const v = d.noise[i][k];
                c.fillStyle = v == null ? '#b2babb' : this.noiseColor(v);
                c.fillRect(left + k * cw, y, cw - 1, rh);
                if (occ > 1) {
                    c.fillStyle = 'rgba(0,0,0,0.25)';
                    c.fillRect(left + k * cw, y, cw - 1, rh);
                }
            });
        }
        const colors = { change: '#8e44ad', logged: '#2980b9', maintenance: '#e67e22' };
        for (const m of d.marks) {
            if (m.bucket < 0 || m.bucket >= n) continue;
            c.fillStyle = colors[m.kind] || '#000';
            c.fillRect(left - 8, top + (n - 1 - m.bucket) * rh, 6, Math.max(2, rh));
        }
        c.strokeStyle = '#000';
        for (const p of d.planned) {
            if (p.off || !p.frequency) continue;
            const w2 = (p.chanbw || 20) / 2;
            const k0 = d.slots.findIndex(s => s + d.slot_mhz > p.frequency - w2);
            const k1 = d.slots.findIndex(s => s >= p.frequency + w2);
            const kk = k1 < 0 ? d.slots.length : k1;
            if (k0 >= 0) c.strokeRect(left + k0 * cw - 0.5, top - 4, (kk - k0) * cw, 4 + rh * 3);
        }
        const canvas = el.querySelector('canvas');
        const tip = el.querySelector('.wf-tip');
        canvas.addEventListener('mousemove', (ev) => {
            const r = canvas.getBoundingClientRect();
            const k = Math.floor((ev.clientX - r.left - left) / cw);
            const row = Math.floor((ev.clientY - r.top - top) / rh);
            const i = n - 1 - row;
            if (k < 0 || k >= d.slots.length || i < 0 || i >= n) { tip.textContent = ''; return; }
            const v = d.noise[i][k];
            tip.textContent = `${d.buckets[i].slice(0, 16)} · ${d.slots[k]}–${d.slots[k] + d.slot_mhz} MHz · `
                + `${d.occupancy[i][k]} radio(s)` + (v != null ? ` · noise ${v} dBm` : '');
        });
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => {
    if (!window.SITE_CODE) return;
    document.getElementById('wf-days')?.addEventListener('change', () => Waterfall.load(window.SITE_CODE));
    Waterfall.load(window.SITE_CODE);
});
