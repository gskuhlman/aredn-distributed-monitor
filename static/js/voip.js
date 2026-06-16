/**
 * VOIP tab: endpoint inventory + live end-to-end call-quality test.
 * Renders only — all scoring/probing is server-side (/api/voip/*).
 */
const VOIPModule = {
    initialized: false,
    endpoints: [],

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('voip-run')?.addEventListener('click', () => this.runTest());
        document.getElementById('voip-ping-all')?.addEventListener('click', () => this.pingAll());
    },

    async load() {
        this.setStatus('Loading endpoints...');
        try {
            const resp = await fetch('/api/voip/endpoints');
            this.endpoints = resp.ok ? await resp.json() : [];
            this.renderEndpoints();
            this.populatePickers();
            this.setStatus(`${this.endpoints.length} endpoint(s)`);
            // Populate reachability/latency/jitter/MOS automatically on load.
            if (this.endpoints.length) this.pingAll();
        } catch (e) {
            this.setStatus('Failed to load');
            console.error('VOIP load error:', e);
        }
    },

    populatePickers() {
        const src = document.getElementById('voip-source');
        const tgt = document.getElementById('voip-target');
        if (!src || !tgt) return;
        // Source value = node (the AREDN vantage that runs the probes).
        // Target value = the device IP when known (test the specific device), else node.
        const label = e => `${this.esc(e.node)} — ${this.esc(e.device)} (${this.esc(e.type)})`;
        src.innerHTML = this.endpoints.map(e => `<option value="${this.esc(e.node)}">${label(e)}</option>`).join('');
        tgt.innerHTML = this.endpoints.map(e => `<option value="${this.esc(e.device_ip || e.node)}">${label(e)}</option>`).join('');
        if (this.endpoints.length) {
            const pbx = this.endpoints.find(e => e.type === 'pbx') || this.endpoints[0];
            src.value = pbx.node;
            const other = this.endpoints.find(e => e.node !== pbx.node) || this.endpoints[0];
            tgt.value = other.device_ip || other.node;
        }
    },

    renderEndpoints() {
        const body = document.getElementById('voip-endpoints-body');
        if (!body) return;
        if (!this.endpoints.length) {
            body.innerHTML = '<tr><td colspan="8" class="log-empty">No phone/PBX services detected.</td></tr>';
            return;
        }
        body.innerHTML = this.endpoints.map(e => {
            const ip = e.device_ip || '';
            return `
            <tr data-ip="${this.esc(ip)}">
                <td><a href="/nodes/${encodeURIComponent(e.node)}">${this.esc(e.node)}</a></td>
                <td>${this.esc(e.device || '')}</td>
                <td>${this.esc(e.type)}</td>
                <td>${this.esc(ip || '-')}</td>
                <td class="voip-reach"><span class="health-val health-${e.reachable_hint ? 'good' : 'unknown'}">${e.reachable_hint ? 'active' : '?'}</span></td>
                <td class="voip-latency">-</td>
                <td class="voip-jitter">-</td>
                <td class="voip-mos">-</td>
            </tr>`;
        }).join('');
    },

    async pingAll() {
        const btn = document.getElementById('voip-ping-all');
        const original = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = 'Pinging...'; }
        this.setStatus('Pinging all endpoints...');
        try {
            const resp = await fetch('/api/voip/ping-all', { method: 'POST' });
            const data = await resp.json();
            const byIp = {};
            (data.results || []).forEach(r => { if (r.ip) byIp[r.ip] = r; });
            document.querySelectorAll('#voip-endpoints-body tr[data-ip]').forEach(tr => {
                const r = byIp[tr.getAttribute('data-ip')];
                const reach = tr.querySelector('.voip-reach');
                const lat = tr.querySelector('.voip-latency');
                const jit = tr.querySelector('.voip-jitter');
                const mos = tr.querySelector('.voip-mos');
                if (!r) return;
                if (reach) reach.innerHTML = `<span class="health-val health-${r.reachable ? 'good' : 'poor'}">${r.reachable ? 'reachable' : 'no reply'}</span>`;
                if (lat) lat.textContent = (r.avg !== null && r.avg !== undefined) ? `${r.avg} ms` : '-';
                if (jit) jit.textContent = (r.jitter !== null && r.jitter !== undefined) ? `${r.jitter} ms` : '-';
                if (mos) mos.innerHTML = (r.mos !== null && r.mos !== undefined)
                    ? `<span class="health-val health-${this.mosClass(r.mos_rating)}">${r.mos}</span>` : '-';
            });
            const reached = (data.results || []).filter(r => r.reachable).length;
            this.setStatus(`Pinged ${data.results ? data.results.length : 0} endpoint(s); ${reached} reachable`);
        } catch (e) {
            this.setStatus('Ping all failed');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = original; }
        }
    },

    async runTest() {
        // A hand-entered IP/host overrides the dropdown selection.
        const src = (document.getElementById('voip-source-ip')?.value || '').trim()
            || document.getElementById('voip-source')?.value;
        const tgt = (document.getElementById('voip-target-ip')?.value || '').trim()
            || document.getElementById('voip-target')?.value;
        const codec = document.getElementById('voip-codec')?.value || 'mixed';
        const btn = document.getElementById('voip-run');
        const out = document.getElementById('voip-result');
        if (!src || !tgt) { this.setStatus('Pick a source and target'); return; }
        if (src === tgt) { this.setStatus('Source and target must differ'); return; }
        const original = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = 'Testing...'; }
        if (out) out.innerHTML = `<p class="loading">Running live call test ${this.esc(src)} → ${this.esc(tgt)} (ping, traceroute, iperf, MTU)…</p>`;
        try {
            const resp = await fetch(`/api/voip/call-quality/${encodeURIComponent(src)}/${encodeURIComponent(tgt)}?codec=${encodeURIComponent(codec)}`, { method: 'POST' });
            const data = await resp.json();
            if (!resp.ok || !data.success) throw new Error(data.error || 'Call test failed');
            this.renderResult(data.result);
            this.setStatus(`Tested ${new Date().toLocaleTimeString()}`);
        } catch (e) {
            if (out) out.innerHTML = `<p class="error">${this.esc(e.message)}</p>`;
            this.setStatus('Test failed');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = original; }
        }
    },

    mosClass(rating) { return { good: 'good', marginal: 'marginal', poor: 'poor' }[rating] || 'unknown'; },
    fmt(v, suffix = '') { return (v === null || v === undefined) ? '-' : `${v}${suffix}`; },

    renderResult(r) {
        const out = document.getElementById('voip-result');
        if (!out) return;
        const e2e = r.end_to_end || {};
        const g114 = r.g114 || {};
        const cap = r.capacity || {};
        const mtu = r.mtu || {};
        const seg = r.segments || {};

        let head;
        if (e2e.reachable) {
            head = `<p class="trace-head"><strong>${this.esc(r.source)} → ${this.esc(r.target)}</strong>
                — codec ${this.esc(r.codec_label || r.codec)}
                ${e2e.origin === 'collector' ? '<span class="health-val health-marginal" title="Source has no IP; measured from the collector, not the true call path">collector-origin</span>' : ''}</p>`;
        } else {
            head = `<p class="trace-head"><strong>${this.esc(r.source)} → ${this.esc(r.target)}</strong> — <span class="health-val health-poor">unreachable</span> ${this.esc(e2e.note || '')}</p>`;
        }

        const summary = e2e.reachable ? `
            <table class="info-table">
                <tr><td>Call MOS</td><td><span class="health-val health-${this.mosClass(e2e.mos_rating)}">${this.fmt(e2e.mos)} (${this.esc(e2e.mos_label)})</span></td></tr>
                <tr><td>One-way delay (G.114)</td><td><span class="health-val health-${this.mosClass(g114.rating)}">${this.fmt(g114.one_way_ms, ' ms')}</span> &mdash; budget ${this.fmt(g114.budget_ms, ' ms')}, max ${this.fmt(g114.max_ms, ' ms')} (${this.esc(g114.label || '')})</td></tr>
                <tr><td>Latency / Jitter / Loss</td><td>${this.fmt(e2e.latency_ms, ' ms')} / ${this.fmt(e2e.jitter_ms, ' ms')} / ${this.fmt(e2e.loss_pct, '%')}</td></tr>
                <tr><td>Concurrent calls</td><td>${cap.status === 'ok'
                    ? `<span class="health-val health-${cap.max_calls >= 3 ? 'good' : cap.max_calls >= 1 ? 'marginal' : 'poor'}">~${cap.max_calls}</span> at ${this.fmt(cap.per_call_kbps, ' kbps')}/call (${this.fmt(cap.capacity_mbps, ' Mbps')} measured)`
                    : `<span class="health-val health-unknown">n/a</span> ${this.esc(cap.note || '')}`}</td></tr>
                <tr><td>Path MTU</td><td>${mtu.status === 'ok'
                    ? `<span class="health-val health-${mtu.mtu_warning ? 'poor' : 'good'}">${this.fmt(mtu.path_mtu)}</span> ${mtu.mtu_warning ? this.esc(mtu.note || '') : ''}`
                    : `<span class="health-val health-unknown">n/a</span> ${this.esc(mtu.note || '')}`}</td></tr>
            </table>` : '';

        let segHtml = '';
        if (seg.status === 'ok' && (seg.segments || []).length) {
            const rows = seg.segments.map(s => {
                const isWorst = seg.worst && s.from === seg.worst.from && s.to === seg.worst.to;
                return `<tr${isWorst ? ' style="font-weight:600;background:#fdf2f2"' : ''}>
                    <td>${this.esc(s.label)}</td>
                    <td>${this.esc(s.bucket)}${s.link_type ? ` (${this.esc(s.link_type)})` : ''}</td>
                    <td>${s.timeout ? '<span class="health-val health-poor">* timeout *</span>' : this.fmt(s.rtt_delta_ms, ' ms')}</td>
                </tr>`;
            }).join('');
            const worst = seg.worst ? `<p class="trace-head">Worst segment: <strong>${this.esc(seg.worst.label)}</strong> (${this.esc(seg.worst.bucket)}) — ${this.esc(seg.worst.reason || '')}</p>` : '';
            segHtml = `${worst}
                <table class="info-table">
                    <thead><tr><th>Segment</th><th>Media</th><th>Added latency</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>`;
        } else if (seg.status === 'failed') {
            segHtml = `<p class="health-footnote">Segment trace unavailable: ${this.esc(seg.note || '')}</p>`;
        }

        out.innerHTML = head + summary + segHtml;
    },

    setStatus(text) { const el = document.getElementById('voip-status'); if (el) el.textContent = text; },
    esc(v) {
        return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
