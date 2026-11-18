/**
 * Reports page "Long-term" (monthly.py, TODO #83): month-by-month summary of
 * the mesh, kept forever, plus node uptime and RF link SNR grids (one column
 * per month, the current month so far last). Loads when Reports opens.
 */
const Monthly = {
    data: null,

    init() {
        if (!document.getElementById('mo-summary')) return;
        R7.onShow('reports', null, () => { if (!this.data) this.load(); });
        document.getElementById('mo-refresh').addEventListener('click', () => this.load());
        document.getElementById('mo-months').addEventListener('change', () => this.load());
        document.getElementById('mo-search').addEventListener('input', () => this.renderGrids());
    },

    async load() {
        const st = document.getElementById('mo-status');
        st.textContent = 'Loading…';
        try {
            this.data = await R7.json(`/api/reports/monthly?months=${document.getElementById('mo-months').value}`);
            st.textContent = this.data.meta.monthly_until ? `complete months stored up to ${this.data.meta.monthly_until}` : 'no complete month stored yet';
            this.render();
        } catch (e) {
            st.textContent = 'Could not load: ' + e.message;
        }
    },

    pct(v) { return v == null ? '–' : `${v >= 99.995 ? 100 : v.toFixed(v >= 99 ? 2 : 1)}%`; },
    pctClass(v) { return v == null ? '' : v >= 99 ? 'sh-pct-good' : v >= 95 ? 'sh-pct-warn' : 'sh-pct-bad'; },
    snrClass(v) { return v == null ? '' : v >= 25 ? 'sh-pct-good' : v >= 15 ? 'sh-pct-warn' : 'sh-pct-bad'; },
    label(m, partial) { return `${m}${partial ? ' <small class="form-hint">so far</small>' : ''}`; },

    render() {
        const s = this.data.summary;
        document.getElementById('mo-summary').innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Month</th><th>Nodes</th><th>Uptime avg</th><th>Median</th><th>Below 95%</th><th>Reboots</th>
                <th>RF links</th><th>RF SNR median</th><th>Services</th><th>Answered</th></tr></thead><tbody>
            ${s.slice().reverse().map(r => `<tr><td>${this.label(r.month, r.partial)}</td><td>${r.nodes}</td>
                <td class="${this.pctClass(r.uptime_avg)}">${this.pct(r.uptime_avg)}</td><td>${this.pct(r.uptime_median)}</td>
                <td>${r.nodes_below_95}</td><td>${r.reboots}</td><td>${r.rf_links}</td>
                <td class="${this.snrClass(r.rf_snr_median)}">${r.rf_snr_median ?? '–'}${r.rf_snr_median != null ? ' dB' : ''}</td>
                <td>${r.services}</td><td class="${this.pctClass(r.service_availability)}">${this.pct(r.service_availability)}</td></tr>`).join('')}
            </tbody></table></div>
            <p class="form-hint">Raw history is kept about a month and the daily/hourly rollups about 400 days; these monthly rows are never deleted. Stored (retired) nodes are left out.</p>`;
        this.renderGrids();
    },

    grid(rows, months, fmt, cls, firstHead) {
        const q = (document.getElementById('mo-search').value || '').trim().toLowerCase();
        const names = Object.keys(rows).filter(n => !q || n.toLowerCase().includes(q)).sort();
        if (!names.length) return '<p class="log-empty">Nothing to show.</p>';
        const partial = this.data.summary.at(-1)?.partial;
        return `<table class="info-table diag-table mo-grid"><thead><tr><th>${firstHead}</th>${months.map((m, i) =>
            `<th>${this.label(m, partial && i === months.length - 1)}</th>`).join('')}</tr></thead><tbody>
            ${names.slice(0, 400).map(n => `<tr><td>${R7.esc(n)}</td>${months.map(m => {
                const v = rows[n][m];
                return `<td class="${cls(v)}">${fmt(v)}</td>`;
            }).join('')}</tr>`).join('')}</tbody></table>${names.length > 400 ? `<p class="form-hint">First 400 of ${names.length}; filter to narrow.</p>` : ''}`;
    },

    renderGrids() {
        if (!this.data) return;
        const months = this.data.months;
        document.getElementById('mo-nodes').innerHTML = this.grid(this.data.nodes, months, v => this.pct(v), v => this.pctClass(v), 'Node');
        document.getElementById('mo-links').innerHTML = this.grid(this.data.links, months,
            v => v == null ? '–' : `${v} dB`, v => this.snrClass(v), 'RF link');
    }
};

document.addEventListener('DOMContentLoaded', () => Monthly.init());
