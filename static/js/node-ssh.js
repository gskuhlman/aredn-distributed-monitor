/**
 * Node page "Radio Details (SSH)" (ssh_collect.py): what the radio itself
 * reports over SSH -- per station the signal on each antenna chain, the
 * transmit/receive rate with MCS, streams and width, retries and failures;
 * the in-use channel's measured busy time (and the part used by others);
 * load, memory, uptime and clock offset; TX power, channel and the ACK window
 * (coverage class); Ethernet ports; per-station retry rate, reconnects,
 * connected time and ACK signal; ath10k receive-error / missing-ACK rates;
 * the SSH alarms (ssh_health.py); kernel and
 * radio firmware errors in its log; and the stored snapshots (iw phy,
 * ath10k fw_stats, dmesg, logread, uci show with secrets removed). Only
 * infrastructure nodes (and SSH_NODES) are collected.
 */
const NodeSsh = {
    charts: [],
    ALARM_LABELS: { chain: 'Chain / stream', retries: 'Heavy retries', reconnects: 'Reconnects',
        interference: 'Airtime used by others', rx_errors: 'Receive errors', ethernet: 'Ethernet',
        coverage: 'ACK window', clock: 'Clock', memory: 'Memory' },

    async load(node, el) {
        this.node = node;
        this.el = el;
        let d;
        try {
            d = await (await fetch(`/api/ssh/node/${encodeURIComponent(node)}?hours=24`)).json();
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Could not ask the server for SSH radio details.</p>';
            return;
        }
        this.render(d);
    },

    render(d) {
        const el = this.el;
        this.charts.forEach(c => c.destroy());
        this.charts = [];
        if (!d.key_present) {
            el.innerHTML = `<p class="log-empty">SSH collection isn't set up yet: run <code>python ssh_collect.py --keygen</code>
                on the monitor PC and add the public key to the node (see the <a href="/admin">Admin page</a>).</p>`;
            return;
        }
        if (!d.targeted) {
            el.innerHTML = '<p class="log-empty">Only infrastructure nodes (and nodes listed in SSH_NODES) are collected over SSH.</p>';
            return;
        }
        const st = d.status || {};
        const statusLine = st.error
            ? `<p class="node-warning"><strong>Last try ${this.esc(st.last_try)} failed:</strong> ${this.esc(st.error)}${d.hint ? ` (${this.esc(d.hint)})` : ''}</p>`
            : '';
        const chains = list => (list || []).length ? list.map(v => v == null ? '?' : this.esc(v)).join(' / ') : '-';
        const rate = (r, kind, mcs, nss, w) => r == null ? '-' : `${this.esc(r)} Mbps`
            + (kind && kind !== 'legacy' ? ` <small>${this.esc(kind)}-MCS ${this.esc(mcs)} · ${this.esc(nss)} stream${nss > 1 ? 's' : ''} · ${this.esc(w)} MHz</small>`
                : kind === 'legacy' ? ' <small>legacy</small>' : '');
        const gap = list => {
            const v = (list || []).filter(x => x != null && x > -95 && x < 0);
            return v.length >= 2 ? Math.max(...v) - Math.min(...v) : null;
        };
        const sr = d.station_rates || {};
        const dur = sec => sec == null ? '-' : sec >= 86400 ? `${Math.floor(sec / 86400)} d ${Math.floor(sec % 86400 / 3600)} h`
            : sec >= 3600 ? `${Math.floor(sec / 3600)} h ${Math.floor(sec % 3600 / 60)} m` : `${Math.round(sec / 60)} m`;
        const rows = d.stations.map(s => {
            const g = gap(s.chains_avg.length ? s.chains_avg : s.chains);
            const r = sr[s.peer || s.mac] || {};
            return `<tr>
                <td>${s.peer ? `<a href="/nodes/${encodeURIComponent(s.peer)}">${this.esc(s.peer)}</a>` : `<code>${this.esc(s.mac)}</code>`}</td>
                <td>${this.esc(s.signal ?? '-')} dBm <small>chains ${chains(s.chains)}</small></td>
                <td class="${g != null && g >= 10 ? 'air-fail-poor' : ''}">${chains(s.chains_avg)}${g != null ? ` <small>(${g} dB apart)</small>` : ''}</td>
                <td class="${s.tx_nss === 1 ? 'air-fail-marginal' : ''}">${rate(s.tx_rate, s.tx_kind, s.tx_mcs, s.tx_nss, s.tx_width)}</td>
                <td>${rate(s.rx_rate, s.rx_kind, s.rx_mcs, s.rx_nss, s.rx_width)}</td>
                <td>${this.esc(s.tx_retries ?? '-')} / ${this.esc(s.tx_failed ?? '-')}</td>
                <td class="${r.retry_ratio != null && r.retry_ratio >= 0.5 ? 'air-fail-poor' : ''}">${r.retry_ratio != null
                    ? `${this.esc(r.retry_ratio)} <small>at ${this.esc(r.pps)} pkt/s</small>` : '-'}</td>
                <td class="${r.reconnects_24h ? 'air-fail-marginal' : ''}">${dur(s.connected_s)}${r.reconnects_24h
                    ? ` <small>${r.reconnects_24h} reconnect${r.reconnects_24h > 1 ? 's' : ''} in 24 h</small>` : ''}</td>
                <td>${s.ack_signal != null ? this.esc(s.ack_signal) + ' dBm' : '-'}</td>
                <td>${s.expected_mbps != null ? this.esc(s.expected_mbps) + ' Mbps' : '-'}</td></tr>`;
        }).join('');
        const b = d.busy_now;
        const snaps = d.snapshots.map(s => `<button class="btn btn-small btn-secondary ssh-snap" data-id="${s.id}"
            title="${this.esc(s.label || '')}">${this.esc(s.kind)} · ${this.esc(s.timestamp)}</button>`).join(' ');
        const sys = d.system;
        const sysLine = sys ? `<p>Load ${this.esc(sys.load1 ?? '-')} / ${this.esc(sys.load5 ?? '-')} / ${this.esc(sys.load15 ?? '-')}
            ${sys.cores ? `(${this.esc(sys.cores)} core${sys.cores > 1 ? 's' : ''})` : ''}
            · memory free ${sys.mem_avail_kb != null ? `${Math.round(sys.mem_avail_kb / 1024)} of ${Math.round(sys.mem_total_kb / 1024)} MB (${this.esc(sys.mem_avail_pct)}%)` : '-'}
            · up ${dur(sys.uptime_s)}
            · clock ${sys.clock_offset_s == null ? '-' : Math.abs(sys.clock_offset_s) < 5 ? 'in step'
                : `<strong>${sys.clock_offset_s > 0 ? '+' : ''}${Math.round(sys.clock_offset_s)} s</strong> vs. this PC`}</p>` : '';
        const set = d.settings;
        const radioLine = (d.radios || []).filter(x => x.frequency).map(x => `<li><code>${this.esc(x.iface)}</code>
            ${this.esc(x.type || '')} ${this.esc(x.frequency)} MHz${x.width ? ` · ${this.esc(x.width)} MHz wide (centre ${this.esc(x.center1)})` : ''}
            · TX power ${x.txpower_dbm != null ? this.esc(x.txpower_dbm) + ' dBm' : '-'}</li>`).join('');
        const setLine = set ? `<p class="form-hint">Settings (uci, ${this.esc(set.timestamp)}): mode ${this.esc(set.mode || '-')}
            · channel ${this.esc(set.channel || '-')} / ${this.esc(set.chanbw || '-')} MHz · country ${this.esc(set.country || '-')}
            · distance ${set.distance_m != null ? this.esc(set.distance_m) + ' m' : '-'}
            · TX power set ${set.txpower_set != null ? this.esc(set.txpower_set) + ' dBm' : '-'}
            · ACK window ${set.coverage_class != null ? `class ${this.esc(set.coverage_class)} (up to ${this.esc(set.coverage_m)} m)` : '-'}
            ${set.longest_link ? ` · longest RF link <span class="${set.coverage_m != null && set.coverage_m < set.longest_link.distance_m ? 'air-fail-marginal' : ''}">${Math.round(set.longest_link.distance_m)} m</span>
                to ${this.esc(set.longest_link.peer)}` : ''}
            ${Object.keys(set.lqm || {}).length ? ` · LQM ${Object.entries(set.lqm).map(([k, v]) => this.esc(`${k}=${v}`)).join(', ')}` : ''}</p>` : '';
        const eth = (d.ethernet || []).map(e => `<tr><td><code>${this.esc(e.iface)}</code></td>
            <td class="${e.speed != null && e.speed < 1000 || (e.duplex && e.duplex !== 'full') ? 'air-fail-marginal' : ''}">${e.speed != null ? this.esc(e.speed) + ' Mbps' : '-'} ${this.esc(e.duplex || '')}</td>
            <td class="${e.flaps ? 'air-fail-poor' : ''}">${this.esc(e.flaps)} <small>(${this.esc(e.carrier_changes ?? '-')} since boot)</small></td>
            <td>${this.esc(e.rx_errors ?? '-')} / ${this.esc(e.rx_crc_errors ?? '-')} / ${this.esc(e.tx_errors ?? '-')}</td></tr>`).join('');
        const fwRows = Object.entries(d.fw || {}).filter(([, f]) => f.last).map(([phy, f]) => `<tr><td><code>${this.esc(phy)}</code></td>
            <td class="${f.last.rx_error_pct >= 20 ? 'air-fail-poor' : ''}">${this.esc(f.last.rx_error_pct ?? '-')}%</td>
            <td class="${f.last.no_ack_pct >= 30 ? 'air-fail-poor' : ''}">${this.esc(f.last.no_ack_pct ?? '-')}</td>
            <td>${this.esc(f.last.ppdu_noack_pct ?? '-')}%</td><td>${this.esc(f.last.rx_mpdus_ps)}</td></tr>`).join('');
        const logs = (d.log_findings || []).map(x => `<li><strong>${this.esc(x.label)}</strong> ×${x.count}:
            <code>${this.esc(x.last)}</code></li>`).join('');
        el.innerHTML = `
            <p class="health-footnote">Read from the radio over SSH${d.as_of ? ` at ${this.esc(d.as_of)}` : ''}
                ${st.last_slow ? ` · logs and settings ${this.esc(st.last_slow)}` : ''}
                <button id="ssh-collect" class="btn btn-small btn-secondary">Collect now</button>
                <span id="ssh-collect-status" class="form-hint"></span></p>
            ${statusLine}
            ${(d.alarms || []).map(a => `<p class="node-warning"><strong>${this.esc(this.ALARM_LABELS[a.kind] || a.kind)}</strong>
                since ${this.esc(a.since)}: ${this.esc(a.detail)}</p>`).join('')}
            ${sysLine}
            ${radioLine ? `<ul class="diag-evidence">${radioLine}</ul>` : ''}
            ${setLine}
            ${logs ? `<p class="node-warning">Kernel / radio log in the last 24 h:</p><ul class="diag-evidence">${logs}</ul>` : ''}
            ${b ? `<p>Channel ${this.esc(b.frequency)} MHz busy <strong>${this.esc(b.busy_pct)}%</strong> over the last hour
                (receive ${this.esc(b.rx_pct)}%, transmit ${this.esc(b.tx_pct)}%, <span class="${b.others_pct >= 20 ? 'air-fail-poor' : ''}">used by
                others ${this.esc(b.others_pct ?? '-')}%</span>), noise ${this.esc(b.noise ?? '-')} dBm.</p>` : ''}
            ${rows ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Station</th><th>Signal</th><th>Chains (avg)</th><th>TX rate</th><th>RX rate</th>
                <th>Retries / failed</th><th>Retries per packet</th><th>Connected</th><th>ACK signal</th><th>Expected</th></tr></thead>
                <tbody>${rows}</tbody></table></div>`
                : '<p class="log-empty">No station data yet.</p>'}
            ${fwRows ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Radio firmware</th><th>Receive errors</th><th title="Every attempt without an ACK counts, retries too">Missed ACKs per 100 frames</th><th>Aggregates without ACK</th>
                <th>Received frames/s</th></tr></thead><tbody>${fwRows}</tbody></table></div>` : ''}
            ${eth ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Ethernet</th><th>Speed</th><th>Link drops (24 h)</th><th>RX errors / CRC / TX errors</th></tr></thead>
                <tbody>${eth}</tbody></table></div>` : ''}
            <div class="node-page-charts">
                <div class="chart-card"><h4>Signal per chain, dBm (24 h)</h4><canvas id="np-ssh-chains"></canvas></div>
                <div class="chart-card"><h4>Channel busy, % (24 h)</h4><canvas id="np-ssh-busy"></canvas></div>
            </div>
            ${snaps ? `<p class="form-hint">Snapshots (secrets in uci are removed before storing):</p><p>${snaps}</p>
                <pre id="ssh-snap-text" class="ssh-snap-text" hidden></pre>` : ''}`;
        el.querySelector('#ssh-collect')?.addEventListener('click', () => this.collect());
        el.querySelectorAll('.ssh-snap').forEach(btn => btn.addEventListener('click', () => this.showSnapshot(btn.dataset.id)));
        this.drawCharts(d);
    },

    drawCharts(d) {
        if (typeof Chart === 'undefined') return;
        const colors = ['#2563eb', '#dc2626', '#16a34a', '#d97706', '#7c3aed', '#0891b2'];
        const sets = [];
        Object.entries(d.series || {}).slice(0, 3).forEach(([peer, pts], i) => {
            const n = Math.max(0, ...pts.map(p => p.chains.length));
            for (let c = 0; c < n; c++) {
                sets.push({ label: `${peer} chain ${c}`, borderColor: colors[(i * 2 + c) % colors.length],
                    borderDash: c ? [4, 3] : [], pointRadius: 0, borderWidth: 1.5,
                    data: pts.filter(p => p.chains[c] != null && p.chains[c] > -95 && p.chains[c] < 0).map(p => ({ x: p.t, y: p.chains[c] })) });
            }
        });
        const opts = { animation: false, maintainAspectRatio: false,
            scales: { x: { type: 'time', time: { tooltipFormat: 'MMM d HH:mm' } } },
            plugins: { legend: { labels: { boxWidth: 10 } } } };
        const c1 = document.getElementById('np-ssh-chains');
        if (c1 && sets.length) this.charts.push(new Chart(c1, { type: 'line', data: { datasets: sets }, options: opts }));
        const c2 = document.getElementById('np-ssh-busy');
        if (c2 && d.busy.length) {
            this.charts.push(new Chart(c2, { type: 'line', options: opts, data: { datasets: [
                { label: 'busy', borderColor: '#dc2626', pointRadius: 0, data: d.busy.map(p => ({ x: p.t, y: p.busy_pct })) },
                { label: 'receive', borderColor: '#2563eb', pointRadius: 0, data: d.busy.map(p => ({ x: p.t, y: p.rx_pct })) },
                { label: 'transmit', borderColor: '#16a34a', pointRadius: 0, data: d.busy.map(p => ({ x: p.t, y: p.tx_pct })) },
                { label: 'others', borderColor: '#d97706', pointRadius: 0, data: d.busy.map(p => ({ x: p.t, y: p.others_pct })) }] } }));
        }
    },

    async collect() {
        const status = document.getElementById('ssh-collect-status');
        status.textContent = 'Collecting over SSH…';
        const r = await fetch('/api/ssh/collect', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ node: this.node, slow: true }) });
        const d = await r.json().catch(() => ({}));
        const res = (d.results || [])[0];
        if (!r.ok || !res || !res.ok) {
            status.textContent = d.error || (res && (res.error + (res.hint ? ` (${res.hint})` : ''))) || 'Failed.';
            return;
        }
        this.load(this.node, this.el);
    },

    async showSnapshot(id) {
        const pre = document.getElementById('ssh-snap-text');
        pre.hidden = false;
        pre.textContent = 'Loading…';
        const r = await fetch(`/api/ssh/snapshot/${id}`);
        const d = await r.json().catch(() => ({}));
        pre.textContent = r.ok ? `${d.kind}${d.label ? ' ' + d.label : ''} · ${d.timestamp}\n\n${d.text || ''}` : (d.error || 'Could not load it.');
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => {
    const el = document.getElementById('node-ssh');
    if (el && window.NODE_NAME) NodeSsh.load(window.NODE_NAME, el);
});
