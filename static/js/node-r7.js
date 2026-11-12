/**
 * Node page additions: On the Air tape (tape.py), Interface Traffic history
 * from the SSH runs (iface_traffic.py, chart per interface), the node's
 * syslog / logread lines (syslog_rx.py) and call quality from this PC
 * (call_quality.py).
 */
(function () {
    const node = window.NODE_NAME;
    if (!node) return;
    let chart = null, iface = null;

    function load() {
        Tape.mountNode(document.getElementById('np-tape'), node);
        loadIface();
        loadLog();
        loadCall();
    }

    async function loadIface() {
        const el = document.getElementById('np-iface');
        if (!el) return;
        const hours = document.getElementById('np-iface-hours').value;
        try {
            const d = await R7.json(`/api/ssh/iface/${encodeURIComponent(node)}?hours=${hours}`);
            if (!d.interfaces.length) { el.innerHTML = (d.notes || []).map(n => `<p class="log-empty">${R7.esc(n)}</p>`).join(''); return; }
            el.innerHTML = `<table class="info-table"><thead><tr><th>Interface</th><th>Role</th><th>Received</th><th>Sent</th>
                <th>Average in / out</th><th>Peak in / out</th><th></th></tr></thead><tbody>${d.interfaces.map(i => `
                <tr><td>${R7.esc(i.iface)}</td><td>${R7.esc(i.role_label)}${i.peer ? ' to ' + R7.node(i.peer) : ''}</td>
                    <td>${R7.bytes(i.rx_total)}</td><td>${R7.bytes(i.tx_total)}</td>
                    <td>${R7.bps(i.rx_bps_avg)} / ${R7.bps(i.tx_bps_avg)}</td><td>${R7.bps(i.rx_bps_peak)} / ${R7.bps(i.tx_bps_peak)}</td>
                    <td><button class="btn btn-secondary btn-small" data-iface="${R7.esc(i.iface)}">Chart</button></td></tr>`).join('')}</tbody></table>`;
            el.querySelectorAll('[data-iface]').forEach(b => b.addEventListener('click', () => { iface = b.dataset.iface; draw(d); }));
            if (!iface || !d.interfaces.some(i => i.iface === iface)) iface = (d.tunnels[0] || d.interfaces[0]).iface;
            draw(d);
        } catch (e) { el.innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`; }
    }

    function draw(d) {
        const i = d.interfaces.find(x => x.iface === iface);
        const canvas = document.getElementById('np-iface-chart');
        if (!i || !canvas || typeof Chart === 'undefined') return;
        document.getElementById('np-iface-title').textContent = `${i.iface} (${i.role_label}${i.peer ? ' to ' + i.peer : ''})`;
        if (chart) chart.destroy();
        chart = new Chart(canvas, { type: 'line', data: { labels: i.series.map(p => p.t.slice(5, 16)), datasets: [
            { label: 'In (Mbps)', data: i.series.map(p => p.rx_bps / 1e6), borderColor: '#2471a3', pointRadius: 0, borderWidth: 1.5 },
            { label: 'Out (Mbps)', data: i.series.map(p => p.tx_bps / 1e6), borderColor: '#d35400', pointRadius: 0, borderWidth: 1.5 }] },
            options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { ticks: { maxTicksLimit: 10 } }, y: { beginAtZero: true } } } });
    }

    async function loadLog() {
        const el = document.getElementById('np-syslog');
        if (!el) return;
        try {
            const sev = document.getElementById('np-syslog-sev').value;
            const d = await R7.json(`/api/syslog?node=${encodeURIComponent(node)}&collapse=1&limit=150${sev ? '&severity=' + sev : ''}`);
            el.innerHTML = d.messages.length ? `<table class="info-table syslog-table"><tbody>${d.messages.map(m => `
                <tr><td>${R7.esc((m.node_time || m.received_at).slice(5, 19))}</td><td>${m.severity_name ? R7.badge(m.severity_name) : ''}</td>
                    <td>${R7.esc(m.program || '')}</td><td class="msg">${R7.esc(m.message)}</td><td>${m.count > 1 ? '×' + m.count : ''}</td></tr>`).join('')}</tbody></table>
                <p class="form-hint">${d.total} lines stored · <a href="/#/syslog">all node logs</a></p>`
                : '<p class="log-empty">No log lines stored for this node (logread is read over SSH from infrastructure radios; others need the syslog receiver).</p>';
        } catch (e) { el.innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`; }
    }

    // Read the node's log over SSH now; the collector otherwise reads it every 15 minutes.
    async function pullLog() {
        const btn = document.getElementById('np-syslog-pull');
        const status = document.getElementById('np-syslog-pull-status');
        btn.disabled = true;
        status.textContent = 'Reading the log over SSH…';
        try {
            const resp = await fetch(`/api/syslog/pull/${encodeURIComponent(node)}`, { method: 'POST' });
            const d = await resp.json();
            if (!resp.ok) status.textContent = d.error || 'Could not read the log.';
            else {
                status.textContent = d.recent ? `Already read at ${String(d.at).slice(11, 16)}.`
                    : `Read at ${String(d.at).slice(11, 16)}: ${d.added} new line${d.added === 1 ? '' : 's'}.`;
                await loadLog();
            }
        } catch (e) {
            status.textContent = e.message;
        } finally {
            btn.disabled = false;
        }
    }

    async function loadCall() {
        const el = document.getElementById('np-call');
        if (!el) return;
        try {
            const d = await R7.json(`/api/reports/call-quality?hours=24&node=${encodeURIComponent(node)}`);
            const n = d.nodes[0];
            el.innerHTML = n ? `<p>Last 30 min: jitter ${R7.num(n.recent_jitter, 1, ' ms')}, MOS ${R7.num(n.recent_mos, 2)},
                latency ${R7.num(n.recent_latency, 0, ' ms')}, loss ${R7.num(n.recent_loss, 0, '%')}
                ${n.grade ? R7.badge(n.grade === 'critical' ? 'bad' : n.grade === 'warning' ? 'poor' : 'fine', n.grade === 'ok' ? 'resolved' : n.grade) : ''}.
                24 h median jitter ${R7.num(n.jitter, 1, ' ms')}, MOS ${R7.num(n.mos, 2)}${n.worst_hour ? `; worst hour ${R7.esc(n.worst_hour.t.slice(11, 16))} (${R7.num(n.worst_hour.jitter, 0, ' ms')})` : ''}.
                ${n.route_flaps_24h ? `Route Watch: path flapping (${n.route_flaps_24h} switches in 24 h).` : ''}</p>
                <p class="form-hint">${R7.esc(d.notes[0] || '')}</p>`
                : '<p class="log-empty">Not pinged by the ping rounds (only RF link far ends are).</p>';
        } catch (e) { el.innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`; }
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('np-iface-hours')?.addEventListener('change', loadIface);
        document.getElementById('np-syslog-sev')?.addEventListener('change', loadLog);
        document.getElementById('np-syslog-pull')?.addEventListener('click', pullLog);
        load();
    });
})();
