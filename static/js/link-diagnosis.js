/**
 * Pair diagnosis on the RF Stats link view.
 *
 * Shows /api/rf-stats/diagnose/<a>/<b>: a verdict, ranked findings with
 * evidence, a side-by-side of both directions and both nodes, nearby
 * radios, state changes, and charts of the LQM internals for both directions.
 * "Live Path Check" pings/traceroutes between the two nodes from the nodes.
 * All diagnosis rules live server-side in link_diagnostics.py.
 */
const LinkDiagnosis = {
    pair: null,
    data: null,
    charts: [],
    requestSeq: 0,
    DIR_COLORS: ['#2980b9', '#c0392b'],

    async load(source, target, hours) {
        this.pair = [source, target];
        const seq = ++this.requestSeq;
        const el = document.getElementById('rf-diagnosis');
        el.classList.remove('hidden');
        el.innerHTML = '<p class="log-empty">Diagnosing link...</p>';
        try {
            const resp = await fetch(
                `/api/rf-stats/diagnose/${encodeURIComponent(source)}/${encodeURIComponent(target)}?hours=${hours}`);
            const data = await resp.json();
            if (seq !== this.requestSeq) return;
            this.data = data;
            this.render();
            this.renderCharts();
        } catch (error) {
            if (seq === this.requestSeq) el.innerHTML = '<p class="log-empty">Diagnosis failed to load.</p>';
            console.error('Link diagnosis error:', error);
        }
    },

    hide() {
        this.pair = null;
        this.requestSeq++;
        document.getElementById('rf-diagnosis')?.classList.add('hidden');
        document.getElementById('rf-diag-charts')?.classList.add('hidden');
        this.destroyCharts();
    },

    // ------------------------------------------------------------ render --

    render() {
        const d = this.data;
        const el = document.getElementById('rf-diagnosis');
        const counts = { critical: 0, warning: 0, info: 0, ok: 0 };
        d.findings.forEach(f => counts[f.severity]++);

        el.innerHTML = `
            <div class="diag-verdict diag-${d.verdict.severity}">
                <div class="diag-verdict-label">${this.esc(this.severityLabel(d.verdict.severity))}
                    <span class="diag-counts">${counts.critical} critical · ${counts.warning} warning ·
                    ${counts.info} info · ${counts.ok} ok · last ${d.hours}h</span></div>
                <div class="diag-verdict-text">${this.esc(d.verdict.text)}</div>
            </div>
            <div id="diag-live" class="diag-live hidden"></div>
            <div class="diag-findings">${d.findings.map(f => this.renderFinding(f)).join('')}</div>
            <details class="diag-details" open>
                <summary>Both directions</summary>
                ${this.renderDirections()}
            </details>
            <details class="diag-details">
                <summary>Both nodes</summary>
                ${this.renderNodes()}
            </details>
            <details class="diag-details" ${this.nearbyCounts().bad ? 'open' : ''}>
                <summary>Radios near each end ${this.nearbySummary()}</summary>
                ${this.renderNearby()}
            </details>
            <details class="diag-details" ${d.findings.some(f => f.category === 'near_far' || f.category === 'receiver') ? 'open' : ''}>
                <summary>What each end hears</summary>
                ${this.renderNeighbors()}
            </details>
            <details class="diag-details">
                <summary>Link state changes (${d.state_log.length})</summary>
                ${this.renderStateLog()}
            </details>`;
    },

    severityLabel(sev) {
        return { critical: 'Problem found', warning: 'Issues found', ok: 'Healthy', info: 'Notes' }[sev] || sev;
    },

    renderFinding(f) {
        const evidence = f.evidence.length
            ? `<ul class="diag-evidence">${f.evidence.map(e => `<li>${this.esc(e)}</li>`).join('')}</ul>` : '';
        const rec = f.recommendation
            ? `<div class="diag-rec"><strong>Try:</strong> ${this.esc(f.recommendation)}</div>` : '';
        return `<div class="diag-finding diag-${f.severity}">
            <div class="diag-finding-head"><span class="diag-badge">${this.esc(f.severity)}</span>
                <span class="diag-cat">${this.esc(f.category)}</span>
                <strong>${this.esc(f.title)}</strong></div>
            <div class="diag-finding-body">${this.esc(f.detail)}${evidence}${rec}</div>
        </div>`;
    },

    renderDirections() {
        const dirs = this.data.directions;
        const rows = [
            ['Seen as', d => d.found ? (d.via_alias ? `${d.via_alias} (MAC only)` : d.target) : 'not seen'],
            ['Last seen', d => d.last_seen ? `${d.last_seen} (${d.fresh ? 'current' : 'stale'})` : '-'],
            ['Delivery', d => d.delivery || '-'],
            ['LQM quality', d => this.pct(d.quality)],
            ['  TX success (tx_quality)', d => this.pct(d.tx_quality)],
            ['  Ping success', d => this.pct(d.ping_quality)],
            ['  Hellos heard (LQ)', d => this.pct(d.lq)],
            ['TX failure (window)', d => this.pct(d.window?.tx_fail_pct)],
            ['TX failure (since assoc.)', d => this.pct(d.tx_fail_pct_lifetime)],
            ['TX retries (window)', d => this.pct(d.window?.tx_retry_pct)],
            ['SNR now / avg / min', d => this.trio(d.snr, d.window?.snr_avg, d.window?.snr_min, ' dB')],
            ['Signal now / avg / min', d => this.trio(d.signal, d.window?.signal_avg, d.window?.signal_min, ' dBm')],
            ['Noise', d => d.noise != null ? `${d.noise} dBm` : '-'],
            ['PHY TX / RX rate', d => d.tx_bitrate != null ? `${d.tx_bitrate} / ${d.rx_bitrate} Mbps` : '-'],
            ['Routable', d => d.routable == null ? '-' : (d.routable ? 'yes' : 'no')],
            ['Babel txcost / rxcost', d => d.txcost != null ? `${d.txcost} / ${d.rxcost}` : '-'],
            ['Routes over link', d => d.route_count ?? '-'],
            ['LQM RTT', d => d.lqm_rtt_ms != null ? `${d.lqm_rtt_ms} ms` : '-'],
            ['Ping from collector', d => d.window?.ping_samples
                ? `${d.window.ping_avg} ms, ${d.window.ping_loss}% loss, ${d.window.jitter} ms jitter` : '-'],
            ['Interface', d => [d.device, d.subdevice, d.mode, d.band].filter(Boolean).join(' / ') || '-'],
            ['Distance', d => d.distance_m ? `${(d.distance_m / 1000).toFixed(2)} km` : '-'],
            ['Drops recorded', d => d.drop_count ?? '-'],
            ['Samples in window', d => d.window ? `${d.window.samples} (${d.window.lqm_samples} LQM)` : '-'],
        ];
        const head = dirs.map(d => `<th>${this.esc(d.source)} → ${this.esc(d.target)}</th>`).join('');
        const body = rows.map(([label, fn]) =>
            `<tr><td>${this.esc(label)}</td>${dirs.map(d => `<td>${this.esc(fn(d))}</td>`).join('')}</tr>`).join('');
        return `<p class="form-hint">Each column is one node's view of the other (its LQM tracker):
            "TX" is that node transmitting, "LQ" is how much of the other node's hellos it hears.</p>
            <div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th></th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
    },

    renderNodes() {
        const names = this.data.pair;
        const nodes = names.map(n => this.data.nodes[n]);
        const rows = [
            ['IP', n => n.ip], ['Model', n => n.model], ['Firmware', n => n.firmware_version],
            ['Frequency / channel', n => n.rf_frequency ? `${n.rf_frequency} MHz / ch ${n.rf_channel}` : '-'],
            ['Uptime', n => n.uptime], ['Booted', n => n.booted_at],
            ['Hops from scan seed', n => n.scan_depth],
            ['Collector polls (window)', n => n.health?.polls
                ? `${n.health.polls}, ${n.health.unreachable_pct ?? 0}% unreachable` : '-'],
            ['Poll response avg / max', n => n.health?.avg_response_ms != null
                ? `${Math.round(n.health.avg_response_ms)} / ${n.health.max_response_ms} ms` : '-'],
            ['Max load', n => n.health?.max_load1], ['Reboots (window)', n => (n.reboots || []).length],
            ['Location', n => n.lat != null ? `${n.lat}, ${n.lon}` : '-'],
            ['Description', n => n.description],
        ];
        const extra = [];
        if (this.data.inferred_width_mhz) extra.push(`Channel width appears to be ${this.data.inferred_width_mhz} MHz (from PHY rates).`);
        const b = this.data.rf_budget;
        if (b) extra.push(`Free-space loss over ${(b.distance_m / 1000).toFixed(2)} km at ${b.frequency_mhz} MHz ≈ ${b.fspl_db} dB; ` +
            `${b.signal_dbm} dBm received implies ${b.implied_power_plus_gains_db} dB of TX power + antenna gains.`);
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th></th>${names.map(n => `<th>${this.esc(n)}</th>`).join('')}</tr></thead>
            <tbody>${rows.map(([label, fn]) => `<tr><td>${this.esc(label)}</td>${nodes.map(n =>
                `<td>${this.esc(fn(n) ?? '-')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
            ${extra.map(t => `<p class="form-hint">${this.esc(t)}</p>`).join('')}`;
    },

    RELATIONS: {
        overlap: 'Overlapping channel: cannot be decoded, raises the noise floor',
        adjacent: 'Adjacent channel: strong signals bleed into the receiver',
        same: 'Same channel: shares airtime (normal on a mesh channel)',
        near: 'Near: a channel or so away; only matters when very strong',
        clear: "Clear of this link's channel",
        unknown: 'Channel unknown'
    },

    nearbyCounts() {
        const all = Object.values(this.data.nearby || {}).flat().filter(r => r.is_active);
        const overlap = all.filter(r => r.relation === 'overlap').length;
        const adjacent = all.filter(r => r.relation === 'adjacent').length;
        return { overlap, adjacent, bad: overlap + adjacent };
    },

    nearbySummary() {
        const c = this.nearbyCounts();
        if (!c.bad) return '<span class="diag-counts">none on overlapping or adjacent channels</span>';
        return [c.overlap ? `<span class="rel-badge rel-overlap">${c.overlap} overlapping</span>` : '',
                c.adjacent ? `<span class="rel-badge rel-adjacent">${c.adjacent} adjacent</span>` : ''].join(' ');
    },

    relBadge(relation) {
        return `<span class="rel-badge rel-${this.esc(relation)}" title="${this.esc(this.RELATIONS[relation] || '')}">${this.esc(relation)}</span>`;
    },

    fmtDistance(m) {
        if (m === null || m === undefined) return '-';
        return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${m} m`;
    },

    renderNearby() {
        const freq = this.data.link_frequency_mhz;
        const width = this.data.inferred_width_mhz;
        const legend = ['overlap', 'adjacent', 'same', 'near', 'clear'].map(r => this.relBadge(r)).join(' ');
        const intro = `<p class="form-hint">This link: ${this.esc(freq ?? '?')} MHz` +
            (width ? `, ${width} MHz wide (from PHY rates)` : '') +
            `. Radios at each end's site, and those in the direction each end's antenna points ` +
            `(toward its RF neighbors). "Est. level" compares a radio with the neighbor in that beam, ` +
            `assuming similar power. Hover a tag for its meaning.</p><div class="rel-legend">${legend}</div>`;
        return intro + this.data.pair.map(name => {
            const list = this.data.nearby[name] || [];
            if (!list.length) return `<p class="form-hint">No known radios near ${this.esc(name)}.</p>`;
            return `<h4 class="diag-subhead">Near ${this.esc(name)}</h4>
                <div class="table-scroll-wrapper diag-scroll-table"><table class="info-table diag-table">
                <thead><tr><th>Radio</th><th>Freq</th><th>Ch</th><th>&Delta; MHz</th><th>Channel</th>
                <th>Where</th><th>Distance</th><th>Est. level</th><th>Model</th></tr></thead>
                <tbody>${list.map(c => `<tr class="rel-row rel-row-${this.esc(c.relation)}${c.is_active ? '' : ' rel-inactive'}">
                    <td>${this.esc(c.name)}${c.is_active ? '' : ' <small>(inactive)</small>'}</td>
                    <td>${this.esc(c.frequency ?? '-')}</td><td>${this.esc(c.channel || '-')}</td>
                    <td>${this.esc(c.delta_mhz ?? '-')}</td>
                    <td>${this.relBadge(c.relation)}</td>
                    <td>${c.site ? 'same site' : `in beam toward ${this.esc(c.in_beam_of)}`}</td>
                    <td>${this.fmtDistance(c.distance_m)}</td>
                    <td>${c.level_vs_neighbor_db != null ? `${c.level_vs_neighbor_db > 0 ? '+' : ''}${c.level_vs_neighbor_db} dB` : '-'}</td>
                    <td>${this.esc(c.model || '-')}</td></tr>`).join('')}
                </tbody></table></div>`;
        }).join('');
    },

    renderNeighbors() {
        const intro = `<p class="form-hint">Every RF neighbor each end currently hears. <b>Loud</b>: stronger
            than any link needs (SNR &ge; 40 dB or signal &ge; &minus;45 dBm), which can overpower the receiver.
            <b>Struggling</b>: hellos or quality under 50%. A loud neighbor next to struggling ones is the
            classic case for turning the loud node's power down.</p>`;
        return intro + this.data.pair.map(name => {
            const list = (this.data.neighbors || {})[name] || [];
            if (!list.length) return `<p class="form-hint">${this.esc(name)} has no current RF neighbors.</p>`;
            return `<h4 class="diag-subhead">${this.esc(name)} hears</h4>
                <div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Neighbor</th><th>SNR</th><th>Signal</th><th>LQ (hellos heard)</th>
                <th>Quality</th><th>They hear ${this.esc(name)} (LQ)</th><th>Distance</th><th></th></tr></thead>
                <tbody>${list.map(n => `<tr class="${n.hot ? 'nb-hot' : n.poor ? 'nb-poor' : ''}">
                    <td>${this.esc(n.node)}</td><td>${this.esc(n.snr ?? '-')} dB</td>
                    <td>${this.esc(n.signal ?? '-')} dBm</td><td>${this.pct(n.lq)}</td><td>${this.pct(n.quality)}</td>
                    <td>${this.pct(n.their_lq)}</td><td>${this.fmtDistance(n.distance_m)}</td>
                    <td>${n.hot ? '<span class="rel-badge rel-overlap">loud</span> ' : ''}${n.poor ? '<span class="rel-badge rel-adjacent">struggling</span>' : ''}${n.blocked ? ' blocked' : ''}</td>
                </tr>`).join('')}</tbody></table></div>`;
        }).join('');
    },

    renderStateLog() {
        const log = this.data.state_log;
        if (!log.length) return '<p class="form-hint">No state changes in this window.</p>';
        return `<div class="table-scroll-wrapper diag-scroll-table"><table class="info-table diag-table">
            <thead><tr><th>Time</th><th>Direction</th><th>State</th><th>Q</th><th>SNR</th><th>Detail</th><th>Origin</th></tr></thead>
            <tbody>${[...log].reverse().map(r => `<tr>
                <td>${this.esc(r.timestamp)}</td><td>${this.esc(r.source_node)} → ${this.esc(r.target_node)}</td>
                <td class="rpt-${this.esc(r.state)}">${this.esc(r.state)}</td><td>${this.esc(r.quality ?? '-')}</td>
                <td>${this.esc(r.snr ?? '-')}</td>
                <td>${this.esc([r.blocked_reason, r.detail].filter(Boolean).join('; ') || '-')}</td>
                <td>${this.esc(r.origin || '-')}</td></tr>`).join('')}</tbody></table></div>`;
    },

    // ---------------------------------------------------------- live check --

    async runLive(button) {
        if (!this.pair) return;
        const [a, b] = this.pair;
        const el = document.getElementById('diag-live');
        const original = button ? button.textContent : '';
        if (button) { button.disabled = true; button.textContent = 'Checking...'; }
        if (el) {
            el.classList.remove('hidden');
            el.innerHTML = '<p class="log-empty">Pinging and tracing between the nodes (up to ~45 s)...</p>';
        }
        try {
            const resp = await fetch(`/api/rf-stats/diagnose/${encodeURIComponent(a)}/${encodeURIComponent(b)}/live`,
                { method: 'POST' });
            const data = await resp.json();
            if (!resp.ok) {
                if (el) el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Live check failed')}</p>`;
                return;
            }
            const paths = data.results.map(r => {
                const p = r.ping;
                const ping = p ? `${p.avg ?? '-'} ms avg, ${p.loss ?? '-'}% loss` : 'no ping result';
                return `<tr><td>${this.esc(r.source)} → ${this.esc(r.target)}</td>
                    <td>${this.esc(ping)}</td><td>${r.hop_count || '-'}</td>
                    <td>${this.esc(r.path.join(' → ') || '-')}</td></tr>`;
            }).join('');
            if (el) el.innerHTML = `<h4 class="diag-subhead">Live path check</h4>
                ${data.findings.map(f => this.renderFinding(f)).join('')}
                <div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Direction</th><th>Ping (from node)</th><th>Hops</th><th>Path</th></tr></thead>
                <tbody>${paths}</tbody></table></div>`;
        } catch (error) {
            if (el) el.innerHTML = '<p class="log-empty">Live check failed.</p>';
            console.error('Live check error:', error);
        } finally {
            if (button) { button.disabled = false; button.textContent = original; }
        }
    },

    // -------------------------------------------------------------- charts --

    destroyCharts() {
        this.charts.forEach(c => c.destroy());
        this.charts = [];
    },

    renderCharts() {
        const grid = document.getElementById('rf-diag-charts');
        if (!grid || typeof Chart === 'undefined') return;
        this.destroyCharts();
        grid.innerHTML = '';
        grid.classList.remove('hidden');
        const s = this.data.series;
        const dirKeys = Object.keys(s.directions);
        const label = key => key.replace('>', ' → ');

        // One dataset per direction for `field`; dashed for the second direction.
        const perDir = (field, opts = {}) => dirKeys.map((k, i) => ({
            label: `${label(k)}`,
            borderColor: this.DIR_COLORS[i], backgroundColor: 'transparent',
            borderDash: i ? [5, 4] : [], tension: 0.25, spanGaps: true,
            pointRadius: s.directions[k].length > 60 ? 0 : 2,
            data: this.points(s.directions[k], field), ...opts
        }));

        this.addChart(grid, 'TX failure % (sender gives up)', perDir('tx_fail_pct'), { y: { min: 0, max: 100 } });
        this.addChart(grid, 'LQM components: TX success / hellos heard (%)', [
            ...perDir('tx_quality').map(d => ({ ...d, label: `TX success ${d.label}` })),
            ...perDir('lq').map(d => ({ ...d, label: `LQ ${d.label}`, borderColor: d.borderColor + '88', borderWidth: 3 }))
        ], { y: { min: 0, max: 100 } });
        this.addChart(grid, 'LQM quality (%) per direction', perDir('quality'), { y: { min: 0, max: 100 } });
        this.addChart(grid, 'SNR (dB) per direction', perDir('snr'));
        this.addChart(grid, 'Signal (dBm) per direction', perDir('signal'), { y: { beginAtZero: false } });
        this.addChart(grid, 'PHY TX rate (Mbps) per direction', perDir('tx_bitrate'));
        this.addChart(grid, 'TX retries % per direction', perDir('tx_retry_pct'), { y: { min: 0 } });
        const nodeKeys = Object.keys(s.nodes);
        this.addChart(grid, 'Collector poll response (ms) per node', nodeKeys.map((n, i) => ({
            label: n, borderColor: this.DIR_COLORS[i], backgroundColor: 'transparent',
            borderDash: i ? [5, 4] : [], tension: 0.25, spanGaps: true, pointRadius: 2,
            data: this.points(s.nodes[n], 'response_ms')
        })));
    },

    points(rows, field) {
        return rows.filter(r => r[field] !== null && r[field] !== undefined)
            .map(r => ({ x: new Date(r.t.replace(' ', 'T')), y: r[field] }));
    },

    addChart(grid, title, datasets, scales = {}) {
        const card = document.createElement('div');
        card.className = 'chart-card';
        card.innerHTML = `<h4>${this.esc(title)}</h4><canvas></canvas>`;
        grid.appendChild(card);
        const empty = datasets.every(ds => !ds.data.length);
        if (empty) {
            card.insertAdjacentHTML('beforeend', '<p class="form-hint">No data in this window.</p>');
        }
        this.charts.push(new Chart(card.querySelector('canvas'), {
            type: 'line',
            data: { datasets },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { intersect: false, mode: 'index' },
                scales: { x: { type: 'time' }, y: { beginAtZero: true, ...(scales.y || {}) } },
                plugins: { legend: { display: true, position: 'top', labels: { boxWidth: 12, font: { size: 10 } } } }
            }
        }));
    },

    // ------------------------------------------------------------ helpers --

    pct(v) { return v === null || v === undefined ? '-' : `${Number(v).toFixed(1)}%`; },

    trio(now, avg, min, unit) {
        if (now == null && avg == null) return '-';
        return `${now ?? '-'} / ${avg ?? '-'} / ${min ?? '-'}${unit}`;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
