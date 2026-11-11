/**
 * Route Watch (Routing page): periodic traceroutes along key paths from
 * /api/routes (route_watch.py). Site-to-site paths (and operator paths) come
 * first; the starting node's own paths are listed after them as context (they
 * never raise warnings). Each row shows the status, current route against its
 * usual one, route switches in 24 h and a hop-count sparkline; "Why changed?"
 * opens /api/routes/<id>/why: the routes the path used (share, delay), where
 * they part, and each switch with its likely cause. All change/detour/flap
 * rules live in route_watch.py; this only renders them.
 */
const RouteWatch = {
    initialized: false,
    data: null,
    open: new Set(),
    showAll: new Set(),
    CAUSE_CLS: {
        maintenance: 'info', node_rebooted: 'critical', link_failed: 'critical', node_trouble: 'warning',
        link_degraded: 'warning', link_restored: 'ok', better_route: 'ok', trace_incomplete: 'info',
        site_internal: 'info', near_tie: 'info', unknown: 'info'
    },

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
                if (btn.dataset.rwAction === 'all') { this.showAll.add(id); this.loadHistory(id); }
                if (btn.dataset.rwAction === 'path' && typeof PathView !== 'undefined') {
                    const p = this.data.paths.find(x => x.id === id);
                    if (p) PathView.open(p.source || '', p.target);
                }
            }
        });
        this.load();
    },

    async load() {
        this.setStatus('Loading...');
        try {
            const resp = await fetch(`/api/routes?hours=${((typeof GlobalFilter !== 'undefined') ? GlobalFilter.hours() : 24)}`);
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
        const backbone = paths.filter(p => p.role !== 'access');
        const access = paths.filter(p => p.role === 'access');
        const table = (rows) => `<div class="table-scroll-wrapper"><table class="info-table diag-table rw-table">
            <thead><tr><th>Status</th><th>Path</th><th>Route now</th><th>Hops</th><th>Usual</th>
            <th>Last 24 h</th><th>Last change</th><th></th></tr></thead>
            <tbody>${rows.map(p => this.renderRow(p)).join('')}</tbody></table></div>`;
        el.innerHTML = `<h4 class="rw-group">Site to site</h4>
            <p class="form-hint">Paths between infrastructure sites (and paths you added). A detour, or a path that keeps
            switching routes (${this.data.flap_changes}+ switches in 24 h, even between equally good routes), raises a
            warning: each switch can cost a voice call a short gap or reordered packets.</p>
            ${backbone.length ? table(backbone) : '<p class="log-empty">No site-to-site paths yet.</p>'}
            ${access.length ? `<h4 class="rw-group">From the starting node <small class="form-hint">context</small></h4>
            <p class="form-hint">How this monitor reaches each site. These change often (the starting node picks between
            tunnels) and only matter for this PC, so they never raise warnings.</p>${table(access)}` : ''}`;
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
        const reasons = p.reasons?.length
            ? `<div class="rw-off">${p.back_on_usual ? 'was: ' : ''}${p.reasons.map(r => this.esc(r)).join('; ')}</div>` : '';
        const back = p.back_on_usual ? '<div class="form-hint">back on the usual route; clears after the next trace</div>' : '';
        const internal = p.flaps_internal_24h ? `, plus ${p.flaps_internal_24h} between radios of one site` : '';
        let flaps = '';
        if (p.flapping) {
            flaps = `<br><span class="diag-badge infra-badge-warning" title="Route switches in the last 24 h${internal}">flapping: ${p.flaps_24h} switches</span>`;
        } else if (p.flaps_24h) {
            flaps = `<br><small class="form-hint" title="Route switches in the last 24 h${internal}">${p.flaps_24h} switch${p.flaps_24h === 1 ? '' : 'es'} / 24 h</small>`;
        }
        const from = p.source ? this.esc(p.source) : 'starting node';
        return `<tr class="${(p.status === 'detour' || p.flapping) && p.role !== 'access' ? 'rw-row-detour' : ''}">
            <td><span class="diag-badge infra-badge-${st.cls}">${this.esc(st.label)}</span>${flaps}</td>
            <td><strong>${this.esc(p.label)}</strong><br><small>${from} → ${this.esc(p.target)}${p.kind === 'auto' ? ' · auto' : ''}</small></td>
            <td class="rw-route">${route}${off}${reasons}${back}</td>
            <td>${last?.ok ? last.hop_count : '-'}${last?.rtt_ms != null ? `<br><small>${last.rtt_ms} ms</small>` : ''}</td>
            <td>${p.baseline_hops ?? '-'}</td>
            <td>${this.sparkline(p.series)}</td>
            <td><small>${this.esc(p.last_change || '-')}</small></td>
            <td class="rw-actions">
                <button class="btn btn-small btn-secondary" data-rw-action="history" data-id="${p.id}"
                    title="Routes used, where they part, and the likely cause of each switch (last 3 days)">${this.open.has(p.id) ? 'Hide' : 'Why changed?'}</button>
                <button class="btn btn-small btn-secondary" data-rw-action="path" data-id="${p.id}" title="Each hop coloured by link quality (Path View)">Path</button>
                <a class="btn btn-small btn-secondary" href="/routes/${p.id}/traffic" target="_blank" rel="noopener"
                    title="The traffic travelling between these nodes: type, source and destination (read over SSH)">Traffic</a>
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
            const resp = await fetch(`/api/routes/${id}/why?hours=72`);
            const d = await resp.json();
            if (!resp.ok) throw new Error(d.error || `HTTP ${resp.status}`);
            el.innerHTML = this.renderWhy(d, id);
        } catch (error) {
            console.error('Route why error:', error);
            el.innerHTML = '<p class="log-empty">Failed to load the route history.</p>';
        }
    },

    /** "Why did it change?" panel: summary, routes used, where they part, switches with causes. */
    renderWhy(d, id) {
        if (!d.routes.length) return '<p class="log-empty">No successful traces in the last 3 days.</p>';
        const counted = d.switches - (d.switches_internal || 0);
        const summary = `${d.runs} trace(s) in the last ${d.hours / 24} days${d.failed_runs ? `, ${d.failed_runs} failed` : ''};
            ${d.routes.length} route(s); ${counted} switch${counted === 1 ? '' : 'es'} (${d.flaps_per_day} a day)${d.switches_internal
                ? ` plus ${d.switches_internal} between radios of one site` : ''};
            delay spread between routes ${d.rtt_spread_ms} ms.`;
        const voip = d.voip_risk
            ? '<span class="diag-badge infra-badge-warning">VoIP: route switches will be heard</span>'
            : '<span class="diag-badge infra-badge-ok">VoIP: steady enough</span>';
        const notes = d.notes.length ? `<ul class="rw-notes">${d.notes.map(n => `<li>${this.esc(n)}</li>`).join('')}</ul>` : '';
        const causes = d.causes.length ? `<div class="rw-causes">${d.causes.map(c =>
            `<span class="diag-badge infra-badge-${this.CAUSE_CLS[c.cause] || 'info'}">${this.esc(c.label)}: ${c.count}</span>`).join(' ')}</div>` : '';
        const name = (g) => (g == null ? '?' : `R${g + 1}`);
        const routes = `<table class="info-table diag-table rw-changes"><thead><tr><th></th><th>Used</th><th>Delay (median / 90%)</th>
            <th>Hops</th><th>Route</th><th>Last used</th></tr></thead><tbody>${d.routes.map(g => `<tr>
                <td><strong>${name(g.id)}</strong>${g.usual ? '<br><span class="diag-badge infra-badge-ok">usual</span>' : ''}</td>
                <td>${g.share_pct}%<br><small>${g.runs} trace(s)</small></td>
                <td>${g.rtt_median ?? '-'} / ${g.rtt_p90 ?? '-'} ms</td>
                <td>${g.hop_count}</td>
                <td>${this.renderRoute(g.route, d.baseline)}</td>
                <td><small>${this.esc(g.last_seen)}</small></td></tr>`).join('')}</tbody></table>`;
        const seg = (s) => (s.length ? s.map(n => this.esc(n)).join(' → ') : '<em>direct</em>');
        const hotspots = d.hotspots.length ? `<h5>Where the routes part</h5><table class="info-table diag-table rw-changes">
            <thead><tr><th>At</th><th>One way</th><th>Other way</th><th>Switches</th></tr></thead><tbody>${d.hotspots.map(h => `<tr>
                <td><strong>${this.esc(h.node)}</strong></td><td>${seg(h.old)}</td><td>${seg(h.new)}</td><td>${h.count}</td></tr>`).join('')}
            </tbody></table>` : '';
        const all = this.showAll.has(id);
        const shown = all ? d.changes : d.changes.slice(0, 15);
        const delta = (v) => (v == null ? '-' : `${v > 0 ? '+' : ''}${v} ms`);
        const switches = d.changes.length ? `<h5>Switches, newest first</h5><table class="info-table diag-table rw-changes">
            <thead><tr><th>When</th><th>Cause</th><th>Change</th><th>Where they part</th><th>Delay</th><th>Evidence</th></tr></thead>
            <tbody>${shown.map(c => `<tr>
                <td><small>${this.esc(c.at)}</small><br><small class="form-hint">last old trace ${this.esc(c.prev_at.slice(11, 16))}</small></td>
                <td><span class="diag-badge infra-badge-${this.CAUSE_CLS[c.cause] || 'info'}">${this.esc(c.cause_label)}</span></td>
                <td>${name(c.from_group)} → ${name(c.to_group)}${c.to_usual ? ' <small>(usual)</small>' : ''}</td>
                <td><strong>${this.esc(c.divergence.node || '?')}</strong>: was ${seg(c.divergence.old)}
                    <br>now ${seg(c.divergence.new)}${c.divergence.rejoin ? ` <small>→ ${this.esc(c.divergence.rejoin)}</small>` : ''}</td>
                <td>${delta(c.rtt_delta_ms)}</td>
                <td>${c.evidence.length ? `<ul class="rw-evidence">${c.evidence.map(e => `<li>${this.esc(e)}</li>`).join('')}</ul>` : '-'}</td>
            </tr>`).join('')}</tbody></table>
            ${!all && d.changes.length > shown.length
                ? `<button class="btn btn-small btn-secondary" data-rw-action="all" data-id="${id}">Show all ${d.changes.length} switches</button>` : ''}`
            : '<p class="log-empty">No route switches in the last 3 days.</p>';
        return `<p class="form-hint">${summary} ${voip}</p>${notes}${causes}
            <h5>Routes used</h5>${routes}${hotspots}${switches}
            <p class="form-hint">Causes are looked up around each switch, from 10 min before the last trace on the old route
            to 5 min after the first trace on the new one: maintenance, reboots, node and link state changes on the old route,
            and each hop's quality, SNR and Babel cost from the scans (RF links only: DtD and tunnel hops have no cost
            history). A near-tie is a switch with nothing behind it.</p>`;
    },

    /** A route as hop chips; hops not on the usual route highlighted. */
    renderRoute(route, base) {
        const usual = new Set(base || []);
        return `<div class="rw-hops">${route.map((n, i) => {
            const cls = n === '*' ? ' rw-hop-timeout' : (base && !usual.has(n) ? ' rw-hop-new' : '');
            return `${i ? '<span class="rw-via">→</span>' : ''}<span class="rw-hop${cls}">${this.esc(n)}</span>`;
        }).join('')}</div>`;
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
