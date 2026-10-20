/**
 * Route Watch section on the Diagnostics tab: periodic traceroutes along key
 * paths from /api/routes (route_watch.py). Shows each path's status, current
 * route against its usual one, a hop-count sparkline, and per path the routes
 * it took (hops not on the usual route highlighted, how each hop was reached).
 * All change/detour rules live in route_watch.py; this only renders them.
 */
const RouteWatch = {
    initialized: false,
    data: null,
    open: new Set(),

    STATUS: {
        learning: { label: 'learning', cls: 'info' },
        normal: { label: 'usual route', cls: 'ok' },
        changed: { label: 'changed', cls: 'info' },
        detour: { label: 'detour', cls: 'warning' }
    },

    init() {
        if (this.initialized) return;
        if (!document.getElementById('rw-paths')) return;
        this.initialized = true;
        document.getElementById('rw-refresh')?.addEventListener('click', () => this.load());
        document.getElementById('rw-add')?.addEventListener('click', () => this.addPath());
        document.getElementById('rw-paths').addEventListener('click', (event) => {
            const btn = event.target.closest('button[data-rw-action]');
            if (btn) {
                const id = Number(btn.dataset.id);
                if (btn.dataset.rwAction === 'run') this.runPath(id, btn);
                if (btn.dataset.rwAction === 'delete') this.deletePath(id, btn);
                if (btn.dataset.rwAction === 'history') this.toggleHistory(id);
            }
        });
        this.load();
    },

    async load() {
        this.setStatus('Loading...');
        try {
            const resp = await fetch('/api/routes?hours=24');
            this.data = await resp.json();
            this.render();
            const d = this.data;
            this.setStatus(`${d.paths.filter(p => p.enabled).length} path(s), traced every ${d.interval_minutes} min` +
                (d.enabled ? '' : ' (Route Watch is disabled in config)') + ` · ${d.generated_at}`);
            for (const id of this.open) this.loadHistory(id);
        } catch (error) {
            console.error('Route watch error:', error);
            this.setStatus('Failed to load route watch.');
        }
    },

    render() {
        const el = document.getElementById('rw-paths');
        const paths = this.data.paths.filter(p => p.enabled);
        if (!paths.length) {
            el.innerHTML = `<p class="log-empty">No paths yet. Paths between the starting node and each infrastructure
                site (and site to site) are added on the first run; add your own below.</p>`;
            return;
        }
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table rw-table">
            <thead><tr><th>Status</th><th>Path</th><th>Route now</th><th>Hops</th><th>Usual</th>
            <th>Last 24 h</th><th>Last change</th><th></th></tr></thead>
            <tbody>${paths.map(p => this.renderRow(p)).join('')}</tbody></table></div>`;
    },

    renderRow(p) {
        const st = this.STATUS[p.status] || { label: p.status, cls: 'info' };
        const last = p.last;
        let route = '<span class="form-hint">not traced yet</span>';
        if (last && !last.ok) {
            route = `<span class="diag-badge infra-badge-warning">${last.reason === 'no_answer' ? 'no answer' : 'did not reach target'}</span>
                <small>${this.esc(last.timestamp)}</small>`;
        } else if (last) {
            route = this.renderHops(last.hops);
        }
        const off = last?.ok && last.off_backbone?.length
            ? `<div class="rw-off">off backbone: ${last.off_backbone.map(n => this.esc(n)).join(', ')}</div>` : '';
        const reasons = p.reasons?.length ? `<div class="rw-off">${p.reasons.map(r => this.esc(r)).join('; ')}</div>` : '';
        const from = p.source ? this.esc(p.source) : 'starting node';
        return `<tr class="${p.status === 'detour' ? 'rw-row-detour' : ''}">
            <td><span class="diag-badge infra-badge-${st.cls}">${this.esc(st.label)}</span></td>
            <td><strong>${this.esc(p.label)}</strong><br><small>${from} → ${this.esc(p.target)}${p.kind === 'auto' ? ' · auto' : ''}</small></td>
            <td class="rw-route">${route}${off}${reasons}</td>
            <td>${last?.ok ? last.hop_count : '-'}${last?.rtt_ms != null ? `<br><small>${last.rtt_ms} ms</small>` : ''}</td>
            <td>${p.baseline_hops ?? '-'}</td>
            <td>${this.sparkline(p.series)}</td>
            <td><small>${this.esc(p.last_change || '-')}</small></td>
            <td class="rw-actions">
                <button class="btn btn-small btn-secondary" data-rw-action="history" data-id="${p.id}">${this.open.has(p.id) ? 'Hide' : 'Routes'}</button>
                <button class="btn btn-small btn-secondary" data-rw-action="run" data-id="${p.id}">Trace now</button>
                <button class="btn btn-small btn-secondary" data-rw-action="delete" data-id="${p.id}"
                    title="${p.kind === 'auto' ? 'Stop watching this automatic path' : 'Delete this path and its history'}">Remove</button>
            </td></tr>
            ${this.open.has(p.id) ? `<tr class="rw-history-row"><td colspan="8"><div id="rw-history-${p.id}">
                <p class="log-empty">Loading...</p></div></td></tr>` : ''}`;
    },

    /** Hops as chips: hops not on the usual route highlighted, tunnels marked. */
    renderHops(hops) {
        return `<div class="rw-hops">${(hops || []).map(h => {
            if (h.timeout) return '<span class="rw-hop rw-hop-timeout" title="No answer from this hop">*</span>';
            const cls = ['rw-hop', h.new ? 'rw-hop-new' : '', h.tunnel ? 'rw-hop-tunnel' : '',
                h.infra ? 'rw-hop-infra' : ''].filter(Boolean).join(' ');
            const title = [h.ip, h.ms != null ? `${h.ms} ms` : null, h.via ? `reached via ${h.via}` : null,
                h.new ? 'not on the usual route' : null, h.infra ? 'infrastructure' : null].filter(Boolean).join(' · ');
            return `${h.via ? `<span class="rw-via">${this.esc(h.via)}</span>` : '<span class="rw-via">→</span>'}
                <span class="${cls}" title="${this.esc(title)}">${this.esc(h.name)}</span>`;
        }).join('')}</div>`;
    },

    /** Hop count over the window as a tiny SVG line; failed runs as red dots. */
    sparkline(series) {
        const pts = (series || []).filter(s => s.hops != null || !s.ok);
        if (pts.length < 2) return '<span class="form-hint">-</span>';
        const counts = pts.map(s => s.hops ?? 0);
        const max = Math.max(...counts, 1), W = 120, H = 28;
        const x = i => (i / (pts.length - 1)) * (W - 4) + 2;
        const y = v => H - 3 - (v / max) * (H - 8);
        const line = pts.map((s, i) => s.ok ? `${x(i).toFixed(1)},${y(s.hops).toFixed(1)}` : null)
            .filter(Boolean).join(' ');
        const fails = pts.map((s, i) => s.ok ? '' : `<circle cx="${x(i).toFixed(1)}" cy="${H - 3}" r="2" class="rw-spark-fail"/>`).join('');
        return `<svg class="rw-spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
            aria-label="Hop count over the last 24 hours, ${Math.min(...counts)} to ${max} hops">
            <polyline points="${line}" class="rw-spark-line"/>${fails}
            <title>hops ${Math.min(...counts)}–${max}</title></svg>`;
    },

    toggleHistory(id) {
        if (this.open.has(id)) this.open.delete(id);
        else this.open.add(id);
        this.render();
        if (this.open.has(id)) this.loadHistory(id);
    },

    async loadHistory(id) {
        const el = document.getElementById(`rw-history-${id}`);
        if (!el) return;
        try {
            const d = await (await fetch(`/api/routes/${id}/history?hours=72`)).json();
            if (!d.changes?.length) {
                el.innerHTML = '<p class="log-empty">No successful traces in the last 3 days.</p>';
                return;
            }
            const failed = d.runs.filter(r => !r.ok).length;
            el.innerHTML = `<p class="form-hint">${d.runs.length} trace(s) in the last 3 days${failed ? `, ${failed} failed` : ''}.
                Each row is a route the path switched to; highlighted hops are not on the usual route.</p>
                <table class="info-table diag-table rw-changes"><thead><tr><th>From</th><th>Hops</th><th>Route</th></tr></thead>
                <tbody>${d.changes.slice().reverse().map(c => `<tr>
                    <td><small>${this.esc(c.timestamp)}</small>${c.baseline ? '<br><span class="diag-badge infra-badge-ok">usual</span>' : ''}</td>
                    <td>${c.hop_count}${c.rtt_ms != null ? `<br><small>${c.rtt_ms} ms</small>` : ''}</td>
                    <td>${this.renderHops(c.hops)}${c.off_backbone?.length ? `<div class="rw-off">off backbone: ${c.off_backbone.map(n => this.esc(n)).join(', ')}</div>` : ''}</td>
                </tr>`).join('')}</tbody></table>`;
        } catch (error) {
            console.error('Route history error:', error);
            el.innerHTML = '<p class="log-empty">Failed to load routes.</p>';
        }
    },

    async runPath(id, button) {
        button.disabled = true;
        this.setStatus('Tracing...');
        try {
            const resp = await fetch(`/api/routes/${id}/run`, { method: 'POST' });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Trace failed');
            await this.load();
        } catch (error) {
            this.setStatus(error.message);
        } finally {
            button.disabled = false;
        }
    },

    async deletePath(id, button) {
        const p = this.data.paths.find(x => x.id === id);
        if (!p || !window.confirm(p.kind === 'auto'
            ? `Stop watching "${p.label}"? Its history is deleted.`
            : `Delete "${p.label}" and its history?`)) return;
        button.disabled = true;
        try {
            const resp = await fetch(`/api/routes/${id}`, { method: 'DELETE' });
            if (!resp.ok) throw new Error((await resp.json()).error || 'Delete failed');
            this.open.delete(id);
            await this.load();
        } catch (error) {
            this.setStatus(error.message);
            button.disabled = false;
        }
    },

    async addPath() {
        const source = document.getElementById('rw-source').value.trim();
        const target = document.getElementById('rw-target').value.trim();
        const label = document.getElementById('rw-label').value.trim();
        if (!target) {
            this.setStatus('Enter a target node.');
            return;
        }
        try {
            const resp = await fetch('/api/routes', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source, target, label })
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Could not add the path');
            ['rw-source', 'rw-target', 'rw-label'].forEach(id => { document.getElementById(id).value = ''; });
            await this.load();
            this.setStatus('Path added; it is traced on the next run, or press Trace now.');
        } catch (error) {
            this.setStatus(error.message);
        }
    },

    setStatus(text) {
        const el = document.getElementById('rw-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => RouteWatch.init());
