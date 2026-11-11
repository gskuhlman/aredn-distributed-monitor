/**
 * Global search (TODO #62): one box in the header of every page for node,
 * callsign, site, MAC, IP, LAN device, service or phone. Results come from
 * /api/search; pick one with the mouse or arrows + Enter (Enter alone opens
 * the top result). "/" focuses the box from anywhere on the page, Esc closes.
 */
const GlobalSearch = {
    TYPE_LABELS: {
        node: 'Node', callsign: 'Callsign', site: 'Site', mac: 'MAC', device: 'Device',
        service: 'Service', phone: 'Phone',
    },
    results: [],
    active: -1,
    timer: null,
    seq: 0,

    init() {
        const host = document.querySelector('.header .header-controls');
        if (!host || document.getElementById('global-search')) return;
        const wrap = document.createElement('div');
        wrap.className = 'global-search';
        wrap.innerHTML = `
            <input id="global-search" type="search" autocomplete="off" spellcheck="false"
                   placeholder="Search node, site, IP, MAC…  ( / )"
                   aria-label="Search nodes, sites, IPs, MACs, callsigns and services"
                   role="combobox" aria-expanded="false" aria-controls="global-search-results">
            <div id="global-search-results" class="global-search-results" role="listbox" hidden></div>`;
        host.prepend(wrap);
        this.input = wrap.querySelector('input');
        this.list = wrap.querySelector('.global-search-results');
        this.input.addEventListener('input', () => this.schedule());
        this.input.addEventListener('focus', () => { if (this.results.length) this.show(true); });
        this.input.addEventListener('keydown', e => this.onKey(e));
        this.list.addEventListener('mousedown', e => {
            const item = e.target.closest('[data-i]');
            if (item) { e.preventDefault(); this.go(Number(item.dataset.i)); }
        });
        document.addEventListener('click', e => { if (!wrap.contains(e.target)) this.show(false); });
        document.addEventListener('keydown', e => {
            if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
            const t = e.target;
            if (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
            e.preventDefault();
            this.input.focus();
            this.input.select();
        });
    },

    schedule() {
        clearTimeout(this.timer);
        this.timer = setTimeout(() => this.run(), 150);
    },

    async run() {
        const q = this.input.value.trim();
        const seq = ++this.seq;
        if (q.length < 2) { this.render([]); return; }
        try {
            const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
            const data = await r.json();
            if (seq === this.seq) this.render(data.results || [], q);
        } catch (err) {
            if (seq === this.seq) this.render([], q, 'Search failed');
        }
    },

    esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },

    render(results, q, error) {
        this.results = results;
        this.active = results.length ? 0 : -1;
        if (!q) { this.show(false); return; }
        this.list.innerHTML = results.length
            ? results.map((r, i) => `
                <div class="gs-item${i === this.active ? ' active' : ''}" role="option" data-i="${i}"
                     aria-selected="${i === this.active}">
                    <span class="gs-type gs-type-${this.esc(r.type)}">${this.esc(this.TYPE_LABELS[r.type] || r.type)}</span>
                    <span class="gs-text"><span class="gs-label">${this.esc(r.label)}</span>
                    <span class="gs-detail">${this.esc(r.detail)}</span></span>
                </div>`).join('')
            : `<div class="gs-empty">${this.esc(error || `Nothing found for “${q}”`)}</div>`;
        this.show(true);
    },

    show(on) {
        this.list.hidden = !on;
        this.input.setAttribute('aria-expanded', String(on));
    },

    highlight(i) {
        if (!this.results.length) return;
        this.active = (i + this.results.length) % this.results.length;
        this.list.querySelectorAll('.gs-item').forEach((el, j) => {
            el.classList.toggle('active', j === this.active);
            el.setAttribute('aria-selected', String(j === this.active));
            if (j === this.active) el.scrollIntoView({ block: 'nearest' });
        });
    },

    onKey(e) {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (!this.results.length) return;
            if (this.list.hidden) this.show(true); else this.highlight(this.active + 1);
        }
        else if (e.key === 'ArrowUp') { e.preventDefault(); this.highlight(this.active - 1); }
        else if (e.key === 'Enter') {
            e.preventDefault();
            clearTimeout(this.timer);
            if (this.results.length && this.active >= 0) this.go(this.active);
            else this.run();
        } else if (e.key === 'Escape') { this.show(false); this.input.blur(); }
    },

    go(i) {
        const r = this.results[i];
        if (!r) return;
        this.show(false);
        const url = new URL(r.url, location.origin);
        // Same page, other tab (/#/voip from the main page): let the router switch.
        if (url.pathname === location.pathname && url.search === location.search && url.hash) {
            location.hash = url.hash;
        } else {
            location.href = url.href;
        }
    },
};

document.addEventListener('DOMContentLoaded', () => GlobalSearch.init());
