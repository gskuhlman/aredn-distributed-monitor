/**
 * Path View section on the Diagnostics tab (path_view.py): the route between
 * two nodes with every hop coloured by the quality of the link it crossed.
 * Shows the expected path from the links table, routes Route Watch recorded
 * for the same ends, and a live traceroute on demand. "Watch this path" adds
 * it to Route Watch. Grading rules live in path_view.py.
 */
const PathView = {
    initialized: false,

    init() {
        if (this.initialized) return;
        if (!document.getElementById('pv-result')) return;
        this.initialized = true;
        document.getElementById('pv-show')?.addEventListener('click', () => this.show());
        document.getElementById('pv-trace')?.addEventListener('click', () => this.trace());
        document.getElementById('pv-watch')?.addEventListener('click', () => this.watch());
        ['pv-source', 'pv-target'].forEach(id => document.getElementById(id)?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this.show();
        }));
        document.getElementById('pv-result').addEventListener('click', (event) => {
            const seg = event.target.closest('[data-pv-a]');
            if (seg && typeof RFStats !== 'undefined') {
                document.querySelector('.tab-btn[data-tab="rf-stats"]')?.click();
                setTimeout(() => RFStats.selectLink(seg.dataset.pvA, seg.dataset.pvB), 300);
            }
        });
    },

    /** Open the view for a pair from elsewhere (e.g. Route Watch). */
    open(source, target) {
        this.init();
        document.getElementById('pv-source').value = source || '';
        document.getElementById('pv-target').value = target || '';
        document.getElementById('pv-result')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        this.show();
    },

    ends() {
        return {
            source: document.getElementById('pv-source').value.trim().toLowerCase(),
            target: document.getElementById('pv-target').value.trim().toLowerCase(),
            hours: document.getElementById('pv-hours').value
        };
    },

    async show() {
        const { source, target, hours } = this.ends();
        if (!target) {
            this.setStatus('Enter a target node.');
            return;
        }
        this.setStatus('Loading...');
        try {
            const resp = await fetch(`/api/path?${new URLSearchParams({ source, target, hours })}`);
            const d = await resp.json();
            if (!resp.ok) {
                this.setStatus(d.error || 'Failed');
                return;
            }
            this.data = d;
            this.live = null;
            this.render();
            this.setStatus(`${d.source} → ${d.target}`);
        } catch (error) {
            this.setStatus('Failed to load the path.');
        }
    },

    async trace() {
        const { source, target, hours } = this.ends();
        if (!target) {
            this.setStatus('Enter a target node.');
            return;
        }
        const btn = document.getElementById('pv-trace');
        btn.disabled = true;
        this.setStatus('Tracing (up to 45 s)...');
        try {
            const resp = await fetch('/api/path/live', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source, target, hours: Number(hours) })
            });
            const d = await resp.json();
            if (!resp.ok) {
                this.setStatus(d.error || 'Trace failed');
                return;
            }
            this.live = d;
            if (!this.data || this.data.target !== target) {
                const r = await fetch(`/api/path?${new URLSearchParams({ source, target, hours })}`);
                if (r.ok) this.data = await r.json();
            }
            this.render();
            this.setStatus(`Traced ${d.timestamp}${d.reached ? '' : ' (did not reach the target)'}`);
        } catch (error) {
            this.setStatus('Trace failed.');
        } finally {
            btn.disabled = false;
        }
    },

    async watch() {
        const { source, target } = this.ends();
        if (!target) return;
        try {
            const resp = await fetch('/api/routes', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source, target, label: `${source || 'starting node'} → ${target}` })
            });
            const d = await resp.json();
            this.setStatus(resp.ok ? 'Added to Route Watch: traced every run from now on.' : (d.error || 'Failed'));
            if (resp.ok && typeof RouteWatch !== 'undefined') RouteWatch.load();
        } catch (error) {
            this.setStatus('Failed to add the path.');
        }
    },

    chain(nodes, segs, extra) {
        const parts = [`<span class="rw-hop pv-node">${this.esc(nodes[0])}</span>`];
        segs.forEach((s, i) => {
            const q = [s.quality, s.rev_quality].map(v => v == null ? '?' : v).join('/');
            const label = s.type === 'DTD' ? 'DtD' : s.type === 'RF' ? `RF ${q}` : s.grade === 'tunnel' ? (s.via || s.type || 'tunnel') : (s.type || '?');
            const tip = [`${s.from} → ${s.to}`, s.type || 'link not in the links table',
                s.quality != null ? `quality ${s.quality}% → / ${s.rev_quality ?? '?'}% ←` : '',
                s.snr != null ? `SNR ${s.snr} / ${s.rev_snr ?? '?'} dB` : '',
                s.median_quality != null ? `median ${s.median_quality}% over the window (${s.samples} samples)` : '',
                s.ms != null ? `${s.ms} ms` : ''].filter(Boolean).join(' · ');
            const clickable = s.type === 'RF' ? ` data-pv-a="${this.esc(s.from)}" data-pv-b="${this.esc(s.to)}"` : '';
            parts.push(`<span class="pv-seg pv-${s.grade}"${clickable} title="${this.esc(tip)}">${this.esc(label)}${extra ? extra(s, i) : ''}</span>`);
            const n = nodes[i + 1];
            parts.push(`<span class="rw-hop pv-node${n === '*' ? ' rw-hop-timeout' : ''}">${this.esc(n)}</span>`);
        });
        return `<div class="pv-chain">${parts.join('')}</div>`;
    },

    summary(sum) {
        if (!sum) return '';
        const weak = sum.weakest ? ` · weakest RF hop ${this.esc(sum.weakest.from)} ↔ ${this.esc(sum.weakest.to)}` : '';
        return `<span class="form-hint">${sum.hops} hop(s)${sum.tunnels ? `, ${sum.tunnels} tunnel` : ''} · worst: ${this.esc(sum.worst || '-')}${weak}</span>`;
    },

    render() {
        const el = document.getElementById('pv-result');
        const d = this.data;
        if (!d) return;
        let html = '';
        if (this.live) {
            const L = this.live;
            html += `<h4>Live traceroute <small class="form-hint">${this.esc(L.timestamp)}${L.reached ? '' : ' · did not reach the target'}</small></h4>
                ${this.chain(L.nodes, L.segments, s => s.ms != null ? ` <small>${s.ms} ms</small>` : '')}${this.summary(L.summary)}`;
        }
        if (d.expected) {
            html += `<h4>Expected path <small class="form-hint">lowest cost over current links (RF by quality, DtD free, tunnels expensive)</small></h4>
                ${this.chain(d.expected.nodes, d.expected.segments)}${this.summary(d.expected.summary)}`;
        } else {
            html += `<p class="log-empty">No path between ${this.esc(d.source)} and ${this.esc(d.target)} in the current links
                ${d.known.target ? '' : `(${this.esc(d.target)} is not in the links table)`}.</p>`;
        }
        if (d.history.length) {
            html += `<h4>Recorded by Route Watch <small class="form-hint">last ${d.hours} h, newest first</small></h4>` +
                d.history.map(h => `<div class="pv-history"><div class="form-hint">${this.esc(h.first.slice(5, 16))} – ${this.esc(h.last.slice(5, 16))}
                    · ${h.runs} run(s)${h.rtt_ms != null ? ` · ${h.rtt_ms} ms` : ''}</div>
                    ${this.chain(h.nodes, h.segments)}${this.summary(h.summary)}</div>`).join('');
        } else {
            html += `<p class="form-hint">${d.watched.length ? 'Route Watch has no runs for this path in the window yet.'
                : 'Not watched by Route Watch: "Watch this path" records its route every run.'}</p>`;
        }
        html += `<div class="pv-legend"><span class="pv-seg pv-good">good ≥ 80%</span><span class="pv-seg pv-fair">fair 50–79%</span>
            <span class="pv-seg pv-poor">poor &lt; 50%</span><span class="pv-seg pv-tunnel">tunnel</span><span class="pv-seg pv-unknown">unknown</span>
            <span class="form-hint">RF quality shown as forward / reverse; click an RF hop to open it in RF Stats.</span></div>`;
        el.innerHTML = html;
    },

    setStatus(text) {
        const el = document.getElementById('pv-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => PathView.init());
