/**
 * Small shared helpers for the Round 7 pages (incidents, node logs, services,
 * tunnels, degradation, noise sources, call quality, tape, ticker,
 * suppressions): escaping, JSON fetch with the error message, number
 * formatting and `onShow(tab, sub, fn)`, which calls fn whenever that page
 * (and sub-page) becomes visible.
 */
const R7 = {
    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    async json(url, opts) {
        const init = opts && opts.body !== undefined && typeof opts.body !== 'string'
            ? { ...opts, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(opts.body) } : opts;
        const resp = await fetch(url, init);
        let data = {};
        try { data = await resp.json(); } catch (e) { /* empty body */ }
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        return data;
    },

    hours(fallback = 24) {
        return (typeof GlobalFilter !== 'undefined' && GlobalFilter.state) ? GlobalFilter.hours() : fallback;
    },

    num(v, digits = 1, unit = '') {
        return v == null || Number.isNaN(Number(v)) ? '—' : `${Number(v).toFixed(digits).replace(/\.0+$/, '')}${unit}`;
    },

    bps(v) {
        if (v == null) return '—';
        const u = [['Gbps', 1e9], ['Mbps', 1e6], ['kbps', 1e3]].find(([, f]) => v >= f);
        return u ? `${(v / u[1]).toFixed(v / u[1] >= 100 ? 0 : 1)} ${u[0]}` : `${Math.round(v)} bps`;
    },

    bytes(v) {
        if (v == null) return '—';
        const u = [['TB', 1e12], ['GB', 1e9], ['MB', 1e6], ['kB', 1e3]].find(([, f]) => v >= f);
        return u ? `${(v / u[1]).toFixed(1)} ${u[0]}` : `${v} B`;
    },

    hm(ts) { return ts ? String(ts).slice(5, 16) : '—'; },

    node(name) {
        return name ? `<a href="/nodes/${encodeURIComponent(name)}">${R7.esc(name)}</a>` : '—';
    },

    badge(text, cls) { return `<span class="badge-sev ${R7.esc(cls || text)}">${R7.esc(text)}</span>`; },

    onShow(tab, sub, fn) {
        const check = () => {
            const view = document.getElementById(`${tab}-view`);
            if (!view || !view.classList.contains('active')) return;
            if (sub && !view.querySelector(`.subpage.active[data-sub="${sub}"]`)) return;
            fn();
        };
        document.querySelector(`.tab-btn[data-tab="${tab}"]`)?.addEventListener('click', () => setTimeout(check, 0));
        document.addEventListener('subpage-shown', e => { if (e.detail.tab === tab) setTimeout(check, 0); });
        document.addEventListener('DOMContentLoaded', () => setTimeout(check, 50));
    },
};
