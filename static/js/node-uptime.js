/**
 * Status page "Node Uptime" (uptime.py, /api/reports/uptime): uptime % per
 * node over the filter bar's Time, for its Nodes and Search, lowest first.
 * By default only nodes below 100% (and ones the monitor can't judge) are
 * listed; "Show every node" lists all. Loads when Status opens and when the
 * filter bar changes there; dispatches `uptime-loaded` for the Status tiles.
 */
const NodeUptime = {
    data: null,
    loading: false,

    init() {
        if (!document.getElementById('nu-report')) return;
        document.getElementById('nu-refresh')?.addEventListener('click', () => this.load());
        document.getElementById('nu-all')?.addEventListener('change', () => this.render());
        document.querySelector('.tab-btn[data-tab="status"]')?.addEventListener('click', () => this.load());
        if (document.getElementById('status-view')?.classList.contains('active')) this.load();
    },

    query() {
        const gf = (typeof GlobalFilter !== 'undefined') ? GlobalFilter : null;
        const p = new URLSearchParams({
            hours: gf ? gf.hours() : 24,
            scope: gf ? gf.scope() : 'all',
            search: gf?.state?.q || '',
        });
        return p.toString();
    },

    async load() {
        if (this.loading) return;
        this.loading = true;
        const status = document.getElementById('nu-status');
        if (status) status.textContent = 'Loading…';
        try {
            const resp = await fetch(`/api/reports/uptime?${this.query()}`);
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            this.data = data;
            this.render();
            document.dispatchEvent(new CustomEvent('uptime-loaded', { detail: data }));
        } catch (error) {
            document.getElementById('nu-report').innerHTML =
                `<p class="log-empty">Failed to load uptime: ${this.esc(error.message)}</p>`;
            if (status) status.textContent = '';
        } finally {
            this.loading = false;
        }
    },

    render() {
        const d = this.data;
        if (!d) return;
        const all = document.getElementById('nu-all')?.checked;
        const rows = all ? d.nodes : d.nodes.filter(n => n.uptime_pct === null || n.uptime_pct < 100);
        const s = d.summary;
        document.getElementById('nu-status').textContent =
            `${s.nodes} nodes · average ${s.average_pct ?? '-'}% · ${s.below_99} below 99% · ` +
            `${s.below_90} below 90%${s.unknown ? ` · ${s.unknown} n/a` : ''} · ${d.start.slice(5, 16)} to ${d.end.slice(5, 16)}`;
        if (!rows.length) {
            document.getElementById('nu-report').innerHTML =
                `<p class="log-empty">Every node in scope was up the whole time it was checked${all ? '' : ' (tick "Show every node" to list them)'}.</p>`;
            return;
        }
        document.getElementById('nu-report').innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table nu-table">
            <thead><tr><th>Node</th><th>Uptime</th><th>Down</th><th>Reboots</th><th>Unchecked</th><th>Polls answered</th><th>Checked until</th><th>Unreachable cause</th></tr></thead>
            <tbody>${rows.map(n => this.row(n)).join('')}</tbody></table></div>`;
    },

    row(n) {
        const pct = n.uptime_pct;
        const cls = pct === null ? 'nu-na' : pct >= 99.9 ? 'nu-ok' : pct >= 99 ? 'nu-fair' : pct >= 90 ? 'nu-warn' : 'nu-bad';
        const bar = pct === null ? '<span class="form-hint">n/a</span>'
            : `<span class="nu-bar ${cls}"><span style="width:${Math.max(0, Math.min(100, pct))}%"></span></span> <strong class="${cls}">${pct.toFixed(2)}%</strong>`;
        const name = `<a href="/nodes/${encodeURIComponent(n.name)}">${this.esc(n.name)}</a>${n.is_infrastructure ? ' <small class="form-hint">infra</small>' : ''}`;
        return `<tr><td>${name}</td><td class="nu-pct">${bar}</td><td>${this.minutes(n.down_minutes)}</td>
            <td>${n.reboots || 0}</td><td>${this.minutes(n.unchecked_minutes)}</td><td>${n.answered} / ${n.polls}</td>
            <td>${this.esc((n.checked_until || '').slice(5, 16))}${n.stale ? ' <small class="nu-stale">stale</small>' : ''}</td>
            <td>${this.esc(n.cause_label || '')}</td></tr>`;
    },

    minutes(m) {
        if (!m) return '-';
        if (m < 90) return `${Math.round(m)} min`;
        if (m < 48 * 60) return `${(m / 60).toFixed(1)} h`;
        return `${(m / 1440).toFixed(1)} d`;
    },

    esc(value) {
        return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
};

document.addEventListener('DOMContentLoaded', () => setTimeout(() => NodeUptime.init(), 0));
