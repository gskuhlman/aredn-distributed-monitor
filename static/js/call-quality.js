/**
 * VoIP & Phones → Call Quality (call_quality.py): jitter, estimated MOS,
 * latency and loss from this PC to each node (the ping rounds), graded
 * against the jitter / MOS thresholds, with Route Watch flapping beside it.
 */
const CallQuality = {
    init() {
        if (!document.getElementById('cq-report')) return;
        document.getElementById('cq-refresh').addEventListener('click', () => this.load());
        document.getElementById('cq-all').addEventListener('change', () => this.render());
        R7.onShow('voip', null, () => this.load());
    },

    async load() {
        const status = document.getElementById('cq-status');
        status.textContent = 'Loading…';
        try {
            this.data = await R7.json(`/api/reports/call-quality?hours=${R7.hours()}`);
            const c = this.data.counts, t = this.data.thresholds;
            status.textContent = `${c.critical} bad · ${c.warning} poor · ${c.ok} fine (last 30 min) · warn at jitter > ${t.jitter_warn_ms} ms or MOS ≤ ${t.mos_warn}`;
            this.render();
        } catch (e) {
            status.textContent = '';
            document.getElementById('cq-report').innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`;
        }
    },

    spark(series) {
        const pts = series.filter(b => b.jitter != null);
        if (pts.length < 2) return '';
        const hi = Math.max(...pts.map(b => b.jitter), this.data.thresholds.jitter_crit_ms);
        const y = v => (22 - v * 20 / hi).toFixed(1);
        const d = pts.map((b, i) => `${(i * 100 / (pts.length - 1)).toFixed(1)},${y(b.jitter)}`).join(' ');
        return `<svg viewBox="0 0 100 24" width="120" height="24" preserveAspectRatio="none">
            <line x1="0" x2="100" y1="${y(this.data.thresholds.jitter_warn_ms)}" y2="${y(this.data.thresholds.jitter_warn_ms)}" stroke="#d68910" stroke-dasharray="2 2" stroke-width=".6"/>
            <polyline points="${d}" fill="none" stroke="#2471a3" stroke-width="1.5"/></svg>`;
    },

    render() {
        const d = this.data;
        if (!d) return;
        const rows = document.getElementById('cq-all').checked ? d.nodes : d.nodes.filter(n => n.grade && n.grade !== 'ok' || n.window_grade && n.window_grade !== 'ok');
        const g = x => x ? R7.badge(x === 'critical' ? 'bad' : x === 'warning' ? 'poor' : 'fine', x === 'ok' ? 'resolved' : x) : '';
        document.getElementById('cq-report').innerHTML = rows.length ? `<table class="info-table"><thead><tr><th>Node</th><th>Now</th>
            <th>Jitter now</th><th>MOS now</th><th>Latency</th><th>Loss</th><th>Window jitter / MOS</th><th>Worst hour</th><th>Jitter</th><th>Route</th></tr></thead><tbody>
            ${rows.map(n => `<tr><td>${R7.node(n.node)}${n.is_infrastructure ? ' ◆' : ''}</td><td>${g(n.grade)}</td>
                <td>${R7.num(n.recent_jitter, 1, ' ms')}</td><td>${R7.num(n.recent_mos, 2)}</td><td>${R7.num(n.recent_latency, 0, ' ms')}</td>
                <td>${R7.num(n.recent_loss, 0, '%')}</td><td>${R7.num(n.jitter, 1, ' ms')} / ${R7.num(n.mos, 2)}</td>
                <td>${n.worst_hour ? `${R7.esc(n.worst_hour.t.slice(11, 16))} ${R7.num(n.worst_hour.jitter, 0, ' ms')}` : '—'}</td>
                <td>${this.spark(n.series || [])}</td><td>${n.route_flaps_24h ? `flapping (${n.route_flaps_24h} switches/24 h)` : ''}</td></tr>`).join('')}
            </tbody></table>` : '<p class="log-empty">Every measured node has fine call quality. Tick Show all nodes to list them.</p>';
        document.getElementById('cq-report').innerHTML += (d.notes || []).map(n => `<p class="form-hint">${R7.esc(n)}</p>`).join('');
    },
};

CallQuality.init();
