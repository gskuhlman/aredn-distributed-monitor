/**
 * Routing → Tunnels (tunnels.py): per tunnel up time, flaps, RTT, loss and a
 * health %, the nodes and services that reach the starting node only through
 * it, and the mesh split into RF-native / needs a tunnel / off the air.
 */
const TunnelHealth = {
    init() {
        if (!document.getElementById('tn-report')) return;
        document.getElementById('tn-refresh').addEventListener('click', () => this.load());
        document.getElementById('tn-all').addEventListener('change', () => this.render());
        R7.onShow('routing', 'tunnels', () => this.load());
    },

    async load() {
        const el = document.getElementById('tn-report');
        const status = document.getElementById('tn-status');
        status.textContent = 'Loading…';
        try {
            this.data = await R7.json(`/api/tunnels?hours=${R7.hours()}`);
            status.textContent = `Last ${this.data.hours} h · health below ${this.data.health_threshold}% is flagged`;
            this.render();
        } catch (e) {
            status.textContent = '';
            el.innerHTML = `<p class="log-empty">Could not load: ${R7.esc(e.message)}</p>`;
        }
    },

    render() {
        const d = this.data;
        if (!d) return;
        const all = document.getElementById('tn-all').checked;
        const c = d.dependency.counts;
        const rows = d.tunnels.filter(t => all || t.infrastructure || t.behind || (t.health_pct != null && t.health_pct < d.health_threshold));
        const cls = t => t.health_pct == null ? '' : t.health_pct < d.health_threshold ? 'critical' : 'resolved';
        document.getElementById('tn-report').innerHTML = `
            <div class="rpt-summary">
                <div class="rpt-tile"><div class="rpt-tile-value">${c.rf}</div><div class="rpt-tile-label">reach the starting node over RF / DtD</div></div>
                <div class="rpt-tile"><div class="rpt-tile-value">${c.tunnel}</div><div class="rpt-tile-label">need a tunnel (lost if the internet goes down)</div></div>
                <div class="rpt-tile"><div class="rpt-tile-value">${c.off_air}</div><div class="rpt-tile-label">off the air (no current path)</div></div>
            </div>
            ${d.dependency.tunnel_only.length ? `<details><summary>Nodes that need a tunnel (${d.dependency.tunnel_only.length})</summary>
                <p>${d.dependency.tunnel_only.map(R7.node).join(', ')}</p></details>` : ''}
            <table class="info-table"><thead><tr><th>Tunnel</th><th>Type</th><th>Now</th><th>Up time</th><th>Flaps</th><th>RTT</th>
                <th>Loss</th><th>Health</th><th>Depend on it</th></tr></thead><tbody>
            ${rows.map(t => `<tr><td>${R7.node(t.a)} ↔ ${R7.node(t.b)}</td><td>${R7.esc(t.type)}</td><td>${R7.esc(t.status)}</td>
                <td>${R7.num(t.uptime_pct, 1, '%')}</td><td>${t.flaps}</td>
                <td>${R7.num(t.rtt_ms, 0, ' ms')}${t.rtt_p90_ms != null ? ` <small>p90 ${R7.num(t.rtt_p90_ms, 0)}</small>` : ''}</td>
                <td>${R7.num(t.loss_pct, 1, '%')}</td><td>${t.health_pct == null ? '—' : R7.badge(R7.num(t.health_pct, 0, '%'), cls(t))}</td>
                <td>${t.behind ? `<span title="${R7.esc(t.nodes_behind.join(', '))}">${t.behind} nodes, ${t.services_behind} services</span>` : ''}</td></tr>`).join('')}
            </tbody></table>
            ${rows.length < d.tunnels.length ? `<p class="form-hint">${d.tunnels.length - rows.length} healthy tunnels without dependants hidden.</p>` : ''}
            ${(d.notes || []).map(n => `<p class="form-hint">${R7.esc(n)}</p>`).join('')}`;
    },
};

TunnelHealth.init();
