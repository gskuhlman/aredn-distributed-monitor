/**
 * Site dashboard page (/sites/<code>, site_dashboard.py): radios with a
 * spectrum chart, clients per sector, uplink paths, what the site depends on
 * with the internet down, node health scores, events and site notes.
 * Reuses InfraReview's spectrum chart and the Notes widget.
 */
const SitePage = {
    code: window.SITE_CODE,
    data: null,

    init() {
        document.getElementById('site-refresh')?.addEventListener('click', () => this.load());
        document.getElementById('site-hours')?.addEventListener('change', () => this.load());
        this.load();
        if (typeof Notes !== 'undefined') {
            Notes.mount(document.getElementById('site-notes'), { site: this.code, title: 'Notes' });
        }
    },

    async load() {
        const hours = document.getElementById('site-hours').value;
        this.setStatus('Loading...');
        try {
            const resp = await fetch(`/api/sites/${encodeURIComponent(this.code)}?hours=${hours}`);
            const d = await resp.json();
            if (!resp.ok) {
                document.getElementById('site-summary').innerHTML = `<p class="log-empty">${this.esc(d.error || 'Not found')}</p>`;
                this.setStatus('');
                this.renderNav(d.sites || []);
                return;
            }
            this.data = d;
            this.render();
            this.setStatus(`Updated ${d.generated_at.slice(11, 16)}`);
        } catch (error) {
            this.setStatus('Failed to load');
            console.error('Site page error:', error);
        }
    },

    renderNav(sites) {
        document.getElementById('site-nav').innerHTML = sites.map(s =>
            `<a class="tab-btn${s.code === this.code ? ' active' : ''}" href="/sites/${encodeURIComponent(s.code)}" title="${this.esc(s.name)}">${this.esc(s.code)}</a>`).join('');
    },

    render() {
        const d = this.data;
        document.title = `${d.name} (${d.code}) - AREDN Site Dashboard`;
        document.getElementById('site-title').textContent = `${d.name} (${d.code})`;
        this.renderNav(d.sites);
        this.renderSummary();
        this.renderRadios();
        this.renderSectors();
        this.renderPaths();
        this.renderHealth();
        this.renderEvents();
    },

    node(name) {
        return `<a href="/nodes/${encodeURIComponent(name)}">${this.esc(name)}</a>`;
    },

    q(v) {
        if (v == null) return '<span class="form-hint">?</span>';
        const c = v >= 80 ? '#1e8449' : v >= 50 ? '#b7950b' : '#c0392b';
        return `<span style="color:${c};font-weight:600">${v}%</span>`;
    },

    renderSummary() {
        const d = this.data;
        const radios = d.radios.filter(r => !r.not_infrastructure);
        const up = radios.filter(r => r.is_active).length;
        const clients = d.sectors.reduce((n, s) => n + s.clients.length, 0);
        const worstPair = (d.pairs || []).filter(p => ['overlap', 'adjacent'].includes(p.relation));
        const dep = d.depends_on;
        const maint = d.maintenance.map(w => `<li>${this.esc(w.status)}: ${this.esc(w.start_time.slice(5, 16))} – ${this.esc(w.end_time.slice(5, 16))}
            ${w.note ? '· ' + this.esc(w.note) : ''}</li>`).join('');
        const counts = d.event_counts;
        document.getElementById('site-summary').innerHTML = `
            <div class="site-tiles">
                <div class="site-tile"><strong>${up}/${radios.length}</strong><span>infrastructure radios up</span></div>
                <div class="site-tile"><strong>${clients}</strong><span>RF clients on ${d.sectors.length} radio(s)</span></div>
                <div class="site-tile ${worstPair.length ? 'site-tile-bad' : ''}"><strong>${worstPair.length}</strong><span>overlapping / no-guard radio pairs on the mast</span></div>
                <div class="site-tile ${(counts.node_reboot || 0) ? 'site-tile-bad' : ''}"><strong>${counts.node_reboot || 0}</strong><span>reboots in ${d.hours} h</span></div>
                <div class="site-tile ${(counts.link_dropped || 0) > 10 ? 'site-tile-bad' : ''}"><strong>${counts.link_dropped || 0}</strong><span>link drops in ${d.hours} h</span></div>
            </div>
            ${maint ? `<p><strong>Maintenance:</strong></p><ul class="diag-evidence">${maint}</ul>` : ''}
            ${dep.length ? `<p class="node-warning"><strong>With the internet down</strong> this site reaches the starting node (${this.esc(d.starting_node)}) only through
                ${dep.map(l => `<strong>${this.esc(l.a)} ↔ ${this.esc(l.b)}</strong> (${this.esc(l.types.join('/'))}${l.quality != null ? `, quality ${l.quality}%` : ''},
                ${l.behind} node(s) behind)`).join('; ')}.</p>` : ''}
            ${d.carries.length ? `<p class="form-hint">Single paths this site carries for others: ${d.carries.map(l =>
                `${this.esc(l.a)} ↔ ${this.esc(l.b)} (${l.behind} behind)`).join(', ')}.</p>` : ''}`;
    },

    renderRadios() {
        const d = this.data;
        const site = { name: d.name, code: d.code, radios: d.radios, pairs: d.pairs };
        const worst = typeof InfraReview !== 'undefined' ? InfraReview.radioRelations(site) : {};
        document.getElementById('site-spectrum').innerHTML = typeof InfraReview !== 'undefined'
            ? InfraReview.renderSpectrum(site, worst) : '';
        const up = s => s == null ? '-' : s >= 86400 ? `${Math.floor(s / 86400)} d` : `${Math.floor(s / 3600)} h`;
        document.getElementById('site-radios').innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Radio</th><th>Channel</th><th>Span</th><th>Relation</th><th>Mode / SSID</th><th>Azimuth</th><th>Antenna</th>
            <th>Polls answered</th><th>Response</th><th>Uptime</th><th>Load</th><th>Reboots</th><th>Firmware</th></tr></thead>
            <tbody>${d.radios.map(r => {
                const rel = r.frequency ? (worst[r.name] || 'clear') : 'unknown';
                const h = r.health || {};
                return `<tr class="rel-row rel-row-${rel}${r.is_active ? '' : ' site-down'}">
                    <td>${this.node(r.name)}${r.not_infrastructure ? ' <small>(not infra)</small>' : ''}${r.is_active ? '' : ' <span class="diag-badge infra-badge-critical">down</span>'}</td>
                    <td>${r.span ? `ch ${this.esc(r.span.channel)} · ${r.span.frequency}/${r.span.chanbw ?? '20?'}` : (r.rf_status === 'off' ? 'radio off' : '-')}</td>
                    <td>${r.span ? `${r.span.low}–${r.span.high}` : '-'}</td>
                    <td>${r.frequency ? `<span class="rel-badge rel-${rel}">${rel}</span>` : ''}</td>
                    <td>${this.esc(r.mode || '-')} ${r.ssid ? `<small>${this.esc(r.ssid)}</small>` : ''}</td>
                    <td>${r.azimuth != null ? r.azimuth + '°' : '-'}</td><td>${this.esc(r.antenna || '-')}</td>
                    <td>${h.reach_pct != null ? `${h.reach_pct}% <small>(${h.ok}/${h.polls})</small>` : '-'}</td>
                    <td>${h.response_ms != null ? h.response_ms + ' ms' : '-'}</td>
                    <td>${up(r.uptime_seconds)}</td><td>${r.load1 ?? '-'}</td>
                    <td>${r.reboots ? `<strong>${r.reboots}</strong>` : 0}</td><td>${this.esc(r.firmware || '-')}</td></tr>`;
            }).join('')}</tbody></table></div>`;
    },

    renderSectors() {
        const d = this.data;
        const el = document.getElementById('site-sectors');
        if (!d.sectors.length) {
            el.innerHTML = '<p class="log-empty">No current RF clients outside the site.</p>';
            return;
        }
        el.innerHTML = d.sectors.map(s => {
            const r = d.radios.find(x => x.name === s.name) || {};
            return `<div class="site-sector"><h4>${this.node(s.name)} <small class="form-hint">${r.azimuth != null ? `aimed ${r.azimuth}°` : ''}
                ${r.span ? ` · ch ${this.esc(r.span.channel)} ${r.span.frequency}/${r.span.chanbw ?? '20?'}` : ''} · ${s.clients.length} client(s)</small></h4>
                <div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Client</th><th>Quality → / ←</th><th>Median</th><th>SNR → / ←</th><th>Distance</th><th></th></tr></thead>
                <tbody>${s.clients.map(c => `<tr><td>${this.node(c.name)}${c.infrastructure ? ` <small class="hs-infra">${this.esc(c.site || 'infra')}</small>` : ''}</td>
                    <td>${this.q(c.quality)} / ${this.q(c.rev_quality)}</td><td>${this.q(c.median_quality)}</td>
                    <td>${c.snr ?? '?'} / ${c.rev_snr ?? '?'} dB</td><td>${c.distance_km != null ? c.distance_km + ' km' : '-'}</td>
                    <td><a class="btn btn-small btn-secondary" href="/#diagnose/${encodeURIComponent(s.name)}/${encodeURIComponent(c.name)}"
                        title="Open RF Stats and diagnose this pair">Diagnose</a></td></tr>`).join('')}</tbody></table></div></div>`;
        }).join('');
    },

    chain(nodes, segs) {
        const parts = [`<span class="rw-hop">${this.esc(nodes[0])}</span>`];
        segs.forEach((s, i) => {
            const q = [s.quality, s.rev_quality].map(v => v == null ? '?' : v).join('/');
            const label = s.type === 'DTD' ? 'DtD' : s.type === 'RF' ? `RF ${q}` : (s.via || s.type || '?');
            parts.push(`<span class="pv-seg pv-${s.grade}">${this.esc(label)}</span><span class="rw-hop">${this.esc(nodes[i + 1])}</span>`);
        });
        return `<div class="pv-chain">${parts.join('')}</div>`;
    },

    renderPaths() {
        const d = this.data;
        let html = '';
        if (d.expected_path) {
            html += `<h4>Expected path from the starting node <small class="form-hint">lowest cost over current links</small></h4>
                ${this.chain(d.expected_path.nodes, d.expected_path.segments)}`;
        }
        if (d.paths.length) {
            html += `<h4>Route Watch</h4><div class="table-scroll-wrapper"><table class="info-table diag-table rw-table">
                <thead><tr><th>Status</th><th>Path</th><th>Route now</th><th>Hops</th><th>Usual</th></tr></thead>
                <tbody>${d.paths.map(p => {
                    const last = p.last;
                    const hops = last?.ok ? `<div class="rw-hops">${last.hops.map(h => `<span class="rw-hop${h.infra ? ' rw-hop-infra' : ''}${h.new ? ' rw-hop-new' : ''}${h.tunnel ? ' rw-hop-tunnel' : ''}">${this.esc(h.name)}</span>`).join('›')}</div>`
                        : last ? '<span class="diag-badge infra-badge-warning">no answer</span>' : '<span class="form-hint">not traced yet</span>';
                    return `<tr class="${p.status === 'detour' ? 'rw-row-detour' : ''}"><td>${this.esc(p.status)}</td>
                        <td>${this.esc(p.label || `${p.source || 'starting node'} → ${p.target}`)}</td><td class="rw-route">${hops}</td>
                        <td>${last?.hop_count ?? '-'}</td><td>${p.baseline_hops ?? '-'}</td></tr>`;
                }).join('')}</tbody></table></div>`;
        } else {
            html += '<p class="form-hint">No Route Watch paths touch this site yet (they are added on the first Route Watch run).</p>';
        }
        document.getElementById('site-paths').innerHTML = html;
    },

    renderHealth() {
        const d = this.data;
        const s = d.scores;
        if (!s.items.length) {
            document.getElementById('site-health').innerHTML = '<p class="log-empty">No health scores in the window.</p>';
            return;
        }
        const color = v => v == null ? '#d5d8dc' : v >= 80 ? '#27ae60' : v >= 60 ? '#f1c40f' : v >= 40 ? '#e67e22' : '#c0392b';
        const spark = item => {
            const n = item.series.length, w = 300, h = 28, step = w / Math.max(1, n);
            return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none">${item.series.map((v, i) => {
                const bh = v == null ? 2 : Math.max(2, v / 100 * (h - 2));
                return `<rect x="${(i * step).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${Math.max(1, step - 0.5).toFixed(1)}" height="${bh.toFixed(1)}" fill="${color(v)}"><title>${this.esc(s.window.buckets[i])}: ${v ?? 'no data'}</title></rect>`;
            }).join('')}</svg>`;
        };
        document.getElementById('site-health').innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table hs-table">
            <thead><tr><th>Radio</th><th>Now</th><th>Avg</th><th>Min</th><th>Health score (${d.hours} h)</th><th>Biggest drop</th></tr></thead>
            <tbody>${s.items.map(i => `<tr><td>${this.node(i.name)}</td>
                ${[i.now, i.avg, i.min].map(v => `<td>${v == null ? '-' : `<span class="hs-score" style="background:${color(v)}">${v}</span>`}</td>`).join('')}
                <td>${spark(i)}</td><td>${i.drop ? `−${i.drop.points} at ${this.esc(i.drop.at.slice(5, 16))}` : ''}</td></tr>`).join('')}</tbody></table></div>
            <p class="form-hint">Scores as on the Reports tab Health Score (reachability and RF quality, minus reboots and drops). Open Reports for "what changed".</p>`;
    },

    renderEvents() {
        const d = this.data;
        const el = document.getElementById('site-events');
        if (!d.events.length) {
            el.innerHTML = '<p class="log-empty">No events in the window.</p>';
            return;
        }
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Time</th><th>Node</th><th>Event</th><th>Detail</th></tr></thead>
            <tbody>${d.events.map(e => `<tr class="${e.severity === 'warning' || e.severity === 'critical' ? 'rel-row rel-row-adjacent' : ''}">
                <td>${this.esc(e.timestamp.slice(5, 19))}</td><td>${this.node(e.node_name)}</td>
                <td>${this.esc(e.event_type.replace(/_/g, ' '))}</td><td>${this.esc(e.details)}</td></tr>`).join('')}</tbody></table></div>`;
    },

    setStatus(text) {
        const el = document.getElementById('site-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => SitePage.init());
