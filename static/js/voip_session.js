/**
 * VoIP streamed-session panel: two-ended (remote agent) or one-ended voice-like
 * UDP probes with live metrics, route watch, and audio-condition event marks.
 * Renders + drives only; all probing/scoring happens server-side (/api/voip/session/*).
 */
const VOIPSessionModule = {
    initialized: false,
    pollTimer: null,
    running: false,

    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.getElementById('voip-sess-start')?.addEventListener('click', () => this.start());
        document.getElementById('voip-sess-stop')?.addEventListener('click', () => this.stop());
        document.getElementById('voip-sess-routes')?.addEventListener('click', () => this.refreshRoutes());
        document.querySelectorAll('.vs-mark').forEach(b =>
            b.addEventListener('click', () => this.mark(b.getAttribute('data-tag'))));
        // If the page was reloaded mid-session, reflect current state (and resume polling).
        this.poll(false);
    },

    setRunning(running) {
        this.running = running;
        const startBtn = document.getElementById('voip-sess-start');
        const stopBtn = document.getElementById('voip-sess-stop');
        const routesBtn = document.getElementById('voip-sess-routes');
        if (startBtn) startBtn.disabled = running;
        if (stopBtn) stopBtn.disabled = !running;
        if (routesBtn) routesBtn.disabled = !running;
    },

    log(text) {
        const el = document.getElementById('voip-sess-log');
        if (!el) return;
        if (el.querySelector('.log-empty')) el.innerHTML = '';
        const line = document.createElement('div');
        line.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
        el.appendChild(line);
        el.scrollTop = el.scrollHeight;
    },

    async start() {
        const mode = document.getElementById('voip-sess-mode')?.value || 'full';
        const remote = (document.getElementById('voip-sess-remote')?.value || '').trim();
        const key = (document.getElementById('voip-sess-key')?.value || '').trim();
        const source = (document.getElementById('voip-sess-source')?.value || '').trim();
        const pps = parseFloat(document.getElementById('voip-sess-pps')?.value || '50');
        const bytes = parseInt(document.getElementById('voip-sess-bytes')?.value || '172', 10);
        if (!remote) { this.log('Enter the remote host (agent PC or target).'); return; }
        if (mode === 'full' && !key) { this.log('Enter the agent key for a two-ended session.'); return; }

        try {
            const resp = await fetch('/api/voip/session/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mode, remote_host: remote, agent_key: key,
                    source: source, target: remote, pps, packet_bytes: bytes,
                }),
            });
            const data = await resp.json();
            if (!resp.ok || !data.success) throw new Error(data.error || 'start failed');
            this.log(`Session #${data.session.session_id} started (${data.session.mode}) ${data.session.source} -> ${data.session.target}`);
            this.setRunning(true);
            this.schedulePoll();
        } catch (e) {
            this.log(`Start failed: ${e.message}`);
        }
    },

    async stop() {
        try {
            const resp = await fetch('/api/voip/session/stop', { method: 'POST' });
            const data = await resp.json();
            this.log(`Session #${data.session_id || '?'} stopped; report saved.`);
        } catch (e) {
            this.log('Stop failed.');
        } finally {
            this.setRunning(false);
            if (this.pollTimer) { clearTimeout(this.pollTimer); this.pollTimer = null; }
        }
    },

    async refreshRoutes() {
        try {
            await fetch('/api/voip/session/refresh-routes', { method: 'POST' });
            this.log('Route refresh requested.');
        } catch (e) { /* ignore */ }
    },

    async mark(tag) {
        const note = (document.getElementById('voip-sess-note')?.value || '').trim();
        try {
            const resp = await fetch('/api/voip/session/event', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tag, note }),
            });
            const data = await resp.json();
            if (data.success) {
                this.log(`MARK ${tag}${note ? ': ' + note : ''}`);
                const noteEl = document.getElementById('voip-sess-note');
                if (noteEl) noteEl.value = '';
            }
        } catch (e) { /* ignore */ }
    },

    schedulePoll() {
        if (this.pollTimer) clearTimeout(this.pollTimer);
        this.pollTimer = setTimeout(() => this.poll(), 1500);
    },

    async poll(single) {
        let snap = null;
        try {
            const resp = await fetch('/api/voip/session/status');
            snap = await resp.json();
        } catch (e) { /* ignore */ }
        if (snap && snap.session_id !== undefined) {
            this.render(snap);
            this.setRunning(!!snap.running);
            if (!snap.running) this.renderStopped(snap);
        } else if (snap && snap.running === false) {
            this.setRunning(false);
        }
        // Keep polling while the session is live, unless this was a one-shot probe.
        if (!single && this.running) this.schedulePoll();
    },

    fmt(v, suffix = '') {
        return (v === null || v === undefined) ? '—' : `${v}${suffix}`;
    },

    render(s) {
        const smp = s.last_sample || {};
        const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        set('vs-floss', this.fmt(smp.fwd_loss !== undefined && smp.fwd_loss !== null ? Number(smp.fwd_loss).toFixed(2) : null, '%'));
        set('vs-fjit', this.fmt(smp.fwd_jitter !== undefined && smp.fwd_jitter !== null ? Number(smp.fwd_jitter).toFixed(1) : null, ' ms'));
        set('vs-rloss', this.fmt(smp.rev_loss !== undefined && smp.rev_loss !== null ? Number(smp.rev_loss).toFixed(2) : null, '%'));
        set('vs-rjit', this.fmt(smp.rev_jitter !== undefined && smp.rev_jitter !== null ? Number(smp.rev_jitter).toFixed(1) : null, ' ms'));
        set('vs-rtt', this.fmt(smp.rtt !== undefined && smp.rtt !== null ? Number(smp.rtt).toFixed(1) : null, ' ms'));
        const rec = s.route_reciprocal;
        set('vs-route', rec === null || rec === undefined ? 'unknown' : (rec ? 'reciprocal' : 'different'));
        const diag = s.last_diag || {};
        set('vs-diag', diag.severity || '—');

        // Color the diagnosis cell by severity.
        const diagEl = document.getElementById('vs-diag');
        if (diagEl) {
            diagEl.style.color = { BAD: '#e74c3c', WARN: '#f39c12', INFO: '#3498db', GOOD: '#2ecc71' }[diag.severity] || '';
        }

        // Only log a transition into BAD/WARN so the log stays readable.
        if (diag.severity && diag.severity !== this._lastSev) {
            this._lastSev = diag.severity;
            if (diag.severity === 'BAD' || diag.severity === 'WARN') {
                this.log(`${diag.severity}: ${diag.text}`);
            }
        }
    },

    renderStopped(s) {
        if (s.session_id) {
            this.log(`Session ended; report saved for session #${s.session_id}.`);
        }
    },
};

// Wire in alongside the main VOIP module's tab initialization.
(function () {
    const orig = VOIPModule.init.bind(VOIPModule);
    VOIPModule.init = function () {
        orig();
        VOIPSessionModule.init();
    };
})();
