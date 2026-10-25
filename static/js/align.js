/**
 * Antenna alignment page (/field/align, align.py): polls one radio's LQM
 * every 1-5 s and shows the SNR to one neighbour as a big number, a bar with
 * the best value seen, a sparkline and an optional tone (pitch rises with
 * SNR). Keeps the screen awake while running where the browser allows it.
 */
const Align = {
    timer: null,
    best: null,
    history: [],
    lastValue: null,
    lastChange: null,
    audio: null,
    wake: null,

    init() {
        document.getElementById('al-start').addEventListener('click', () => this.timer ? this.stop() : this.start());
        document.getElementById('al-reset').addEventListener('click', () => { this.best = null; this.history = []; this.paint(); });
        document.getElementById('al-node').addEventListener('change', () => { this.best = null; this.history = []; this.peers = null; });
        document.getElementById('al-peer').addEventListener('change', () => { this.best = null; this.history = []; });
        fetch('/api/nodes/all').then(r => r.json()).then(nodes => {
            document.getElementById('al-nodes').innerHTML = (nodes || []).map(n => `<option value="${this.esc(n.name || n)}">`).join('');
        }).catch(() => {});
        if (window.ALIGN?.node) {
            this.wantPeer = window.ALIGN.peer;
            this.start();
        }
    },

    async start() {
        const node = document.getElementById('al-node').value.trim().toLowerCase();
        if (!node) return;
        document.getElementById('al-node-name').textContent = node;
        document.getElementById('al-start').textContent = 'Stop';
        if (document.getElementById('al-tone').checked) this.toneOn();
        try { this.wake = await navigator.wakeLock?.request('screen'); } catch (e) { /* optional */ }
        const tick = async () => {
            await this.read(node);
            const sec = Number(document.getElementById('al-interval').value) || 2;
            if (this.timer !== null) this.timer = setTimeout(tick, sec * 1000);
        };
        this.timer = 0;
        tick();
    },

    stop() {
        if (this.timer) clearTimeout(this.timer);
        this.timer = null;
        document.getElementById('al-start').textContent = 'Start';
        document.getElementById('al-conn').textContent = 'stopped';
        this.toneOff();
        try { this.wake?.release(); } catch (e) { /* ignore */ }
    },

    async read(node) {
        const conn = document.getElementById('al-conn');
        try {
            const r = await fetch(`/api/align/${encodeURIComponent(node)}`);
            const d = await r.json();
            if (!r.ok) throw new Error(d.error || 'no answer');
            conn.className = 'field-conn ok';
            conn.textContent = `${d.at} · ${d.response_ms} ms`;
            this.fillPeers(d.neighbours);
            const want = document.getElementById('al-peer').value;
            const n = want ? d.neighbours.find(x => x.node === want) : d.neighbours[0];
            this.show(n);
        } catch (error) {
            conn.className = 'field-conn off';
            conn.textContent = error.message;
        }
    },

    fillPeers(list) {
        const sel = document.getElementById('al-peer');
        const names = list.map(n => n.node).join('|');
        if (names === this.peers) return;
        this.peers = names;
        const keep = sel.value || this.wantPeer || '';
        this.wantPeer = null;
        sel.innerHTML = '<option value="">Neighbour (strongest)</option>' + list.map(n =>
            `<option value="${this.esc(n.node)}">${this.esc(n.node)} (${n.snr ?? '?'} dB)</option>`).join('');
        if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
    },

    show(n) {
        const set = (id, v) => { document.getElementById(id).textContent = v; };
        if (!n) {
            set('al-snr', '–');
            return;
        }
        const snr = n.snr;
        if (snr !== this.lastValue) {
            this.lastValue = snr;
            this.lastChange = Date.now();
        }
        set('al-snr', snr == null ? '–' : `${snr} dB`);
        set('al-rev', n.rev_snr == null ? '–' : `${n.rev_snr} dB`);
        set('al-signal', n.signal == null ? '–' : `${n.signal} dBm`);
        set('al-lq', `${n.lq ?? '–'} / ${n.quality ?? '–'}`);
        set('al-rate', `${n.tx_bitrate ?? '–'} / ${n.rx_bitrate ?? '–'} Mbps`);
        set('al-age', this.lastChange ? `${Math.round((Date.now() - this.lastChange) / 1000)} s ago` : '–');
        if (snr != null) {
            this.best = this.best == null ? snr : Math.max(this.best, snr);
            this.history.push(snr);
            if (this.history.length > 120) this.history.shift();
        }
        set('al-best', this.best == null ? '–' : `${this.best} dB`);
        this.paint();
        this.tone(snr);
    },

    paint() {
        const pct = v => Math.max(0, Math.min(100, (v / 60) * 100));
        const bar = document.getElementById('al-bar');
        const v = this.lastValue;
        bar.style.width = v == null ? '0' : `${pct(v)}%`;
        bar.style.background = v == null ? '#bdc3c7' : v >= 25 ? '#27ae60' : v >= 15 ? '#f1c40f' : '#e74c3c';
        document.getElementById('al-best-mark').style.left = this.best == null ? '-10px' : `${pct(this.best)}%`;
        const c = document.getElementById('al-spark');
        const g = c.getContext('2d');
        g.clearRect(0, 0, c.width, c.height);
        if (this.history.length < 2) return;
        const max = Math.max(60, ...this.history), step = c.width / 119;
        g.strokeStyle = '#2980b9';
        g.lineWidth = 2;
        g.beginPath();
        this.history.forEach((h, i) => {
            const x = i * step, y = c.height - (h / max) * (c.height - 4) - 2;
            i ? g.lineTo(x, y) : g.moveTo(x, y);
        });
        g.stroke();
    },

    toneOn() {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            gain.gain.value = 0;
            osc.connect(gain).connect(ctx.destination);
            osc.start();
            this.audio = { ctx, osc, gain };
        } catch (e) { this.audio = null; }
    },

    tone(snr) {
        if (!document.getElementById('al-tone').checked) {
            if (this.audio) this.toneOff();
            return;
        }
        if (!this.audio) this.toneOn();
        if (!this.audio || snr == null) return;
        const { ctx, osc, gain } = this.audio;
        osc.frequency.setValueAtTime(220 + Math.max(0, snr) * 25, ctx.currentTime);
        gain.gain.setValueAtTime(0.15, ctx.currentTime);
        gain.gain.setValueAtTime(0, ctx.currentTime + 0.25);   // short beep per reading
    },

    toneOff() {
        try { this.audio?.osc.stop(); this.audio?.ctx.close(); } catch (e) { /* ignore */ }
        this.audio = null;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => Align.init());
