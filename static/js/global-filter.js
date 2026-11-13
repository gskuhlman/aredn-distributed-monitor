/**
 * Shared filter bar under the header: one Nodes / Tag / Links / Time / Search
 * filter for every page, remembered in this browser and kept in the page URL
 * (`#/radio/sectors?nodes=infrastructure&nb=1&hours=72`).
 *
 * Each page keeps reading its own controls (drift-scope, rf-status-filter,
 * rpt-link-type ...); this bar writes into them and hides them, so the page
 * modules need no rewrite. A page that can't use part of the bar shows that
 * part greyed out; a page that uses none of it hides the bar. Page-specific
 * controls (sort, node status, severity, Reports' custom window) stay on the
 * page.
 */
const GlobalFilter = {
    DEFAULT: { nodes: 'infrastructure', nb: true, tag: 'all', links: 'all', hours: 24, q: '' },
    STORE: 'global-filter-v2',   // v2: defaults changed to Infrastructure + neighbours
    HOURS: [1, 6, 24, 72, 168, 720],
    state: null,
    tab: 'status',
    searchTimer: null,

    // Controls the bar replaces (hidden; their values are set from the bar).
    REPLACED: ['cm-scope', 'sp-scope', 'mh-search', 'mh-hours', 'nodes-search', 'nodes-infra-filter',
        'nodes-type-filter', 'rf-search', 'rf-status-filter', 'rf-infra-filter', 'diag-node-filter',
        'diag-infra-filter', 'diag-type-filter', 'diag-hours', 'voip-infra-filter', 'ph-hours', 'air-scope',
        'aim-scope', 'air-hours', 'sm-hours', 'pv-hours', 'drift-scope', 'rpt-scope', 'rpt-link-type',
        'rpt-search', 'chg-scope', 'chg-search', 'chg-hours'],

    /**
     * Per page: what it uses (`nodes`: the Nodes choices it understands,
     * `nb`: + neighbours, `tag`, `links`: link types, `time`, `search`),
     * `set(st, live)` writing the page's controls (live = the bar changed while
     * the page is open, so refresh it), optional `afterOpen(st)` and `note`.
     */
    PAGES: {
        status: {
            uses: { nodes: true, nb: true, time: true, search: true },
            set(st, live, gf) {
                const hoursChanged = gf.put('mh-hours', st.hours);
                // Mesh Health is scoped on the server: reload when Nodes changes too.
                const scopeChanged = this.lastScope !== undefined && this.lastScope !== gf.scope(st);
                this.lastScope = gf.scope(st);
                gf.put('mh-search', st.q);
                if (live && typeof NodeUptime !== 'undefined') NodeUptime.load();
                if (!live || typeof DiagnosticsModule === 'undefined' || !DiagnosticsModule.initialized) return;
                if (hoursChanged || scopeChanged) DiagnosticsModule.loadHealth(); else DiagnosticsModule.applyView();
                if (hoursChanged && typeof LinkWatch !== 'undefined') LinkWatch.load();
            }
        },
        network: {
            uses: { nodes: true, nb: true, tag: true, links: ['all', 'rf'] },
            set(st, live, gf) {
                // Always fire: the graph redraws from these on change, also when
                // the filters changed while another page was open.
                gf.put('filter-infra-only', st.nodes === 'infrastructure', true);
                gf.put('filter-selected-view', st.nodes === 'selected', true);
                gf.put('filter-neighbours', st.nb, true);
                gf.put('filter-rf-only', st.links === 'rf', true);
                gf.put('filter-infra', st.tag, true);
            }
        },
        'geo-map': null,   // same as network (filled in below)
        'channel-map': {
            uses: { nodes: true, nb: true, links: ['all', 'rf'] },
            set(st, live, gf) {
                const scope = gf.scope(st);
                gf.put('cm-scope', scope === 'all' && st.links === 'rf' ? 'rf' : scope, true);
            }
        },
        spectrum: {
            uses: { nodes: ['all', 'infrastructure'] },
            set(st, live, gf) {
                gf.put('sp-scope', st.nodes === 'infrastructure' ? 'infrastructure' : 'all', true);
            }
        },
        nodes: {
            uses: { nodes: true, nb: true, tag: true, links: true, search: true },
            set(st, live, gf) {
                gf.put('nodes-scope', gf.scope(st));
                gf.put('nodes-infra-filter', st.tag);
                gf.put('nodes-type-filter', st.links);
                gf.put('nodes-search', st.q);
                if (live && typeof NodesModule !== 'undefined' && NodesModule.initialized) NodesModule.filterNodes();
            }
        },
        'rf-stats': {
            uses: { nodes: true, nb: true, tag: true, links: true, time: true, search: true },
            note: 'Links applies to Link Stability (bottom of the page); the RF link list is RF only.',
            set(st, live, gf) {
                gf.put('rf-status-filter', gf.scope(st));
                gf.put('rf-infra-filter', st.tag);
                gf.put('rf-search', st.q);
                // Link Stability (flapping / asymmetric) at the bottom of the page.
                gf.put('diag-scope', gf.scope(st));
                gf.put('diag-infra-filter', st.tag);
                gf.put('diag-type-filter', st.links);
                gf.put('diag-node-filter', st.q);
                const hoursChanged = gf.put('diag-hours', st.hours);
                if (live && typeof DiagnosticsModule !== 'undefined' && DiagnosticsModule.initialized) {
                    if (hoursChanged) DiagnosticsModule.load({ health: false }); else DiagnosticsModule.applyView();
                }
                if (typeof RFStats === 'undefined') return;
                if (live) {
                    RFStats.setHours(st.hours);
                    if (RFStats.initialized) RFStats.filterLinks();
                } else {
                    RFStats.timeRangeHours = st.hours;
                    document.querySelectorAll('#rf-stats-view .time-btn').forEach(b =>
                        b.classList.toggle('active', Number(b.dataset.hours) === st.hours));
                }
            }
        },
        voip: {
            uses: { tag: true, time: true },
            set(st, live, gf) {
                gf.put('voip-infra-filter', st.tag, true);
                gf.put('ph-hours', st.hours, true);
            }
        },
        radio: {
            uses: { nodes: true, nb: true, time: true },
            note: 'Nodes and Time apply the next time you run a check.',
            set(st, live, gf) {
                const scope = gf.scope(st);
                gf.put('air-scope', scope);
                gf.put('aim-scope', scope);
                gf.put('air-hours', st.hours);
                gf.put('sm-hours', st.hours);
            }
        },
        routing: {
            uses: { time: true },
            note: 'Time applies to Route Watch now and to Path View the next time you show a path.',
            set(st, live, gf) {
                gf.put('pv-hours', st.hours);
                if (live && typeof RouteWatch !== 'undefined' && RouteWatch.initialized) RouteWatch.load();
            }
        },
        setup: {
            uses: { nodes: true, nb: true },
            note: 'Nodes applies to Settings Drift the next time you compare settings.',
            set(st, live, gf) {
                gf.put('drift-scope', gf.scope(st));
            }
        },
        reports: {
            uses: { nodes: true, nb: true, links: true, time: true, search: true },
            note: 'Reports’ own window controls (start, end, slider) pick any other window.',
            set(st, live, gf) {
                gf.put('rpt-scope', gf.scope(st), live);
                gf.put('rpt-link-type', st.links, live);
                gf.put('rpt-search', st.q, live, 'input');
                if (live) gf.reportsWindow(st);
            },
            afterOpen(st, gf) { gf.reportsWindow(st); }
        },
        changes: {
            uses: { nodes: ['all', 'selected', 'infrastructure'], time: true, search: true },
            set(st, live, gf) {
                gf.put('chg-scope', st.nodes, true);
                gf.put('chg-hours', st.hours, live);
                gf.put('chg-search', st.q, true, 'input');
            }
        }
        // sites, planning, maintenance, notifications: no filters (bar hidden)
    },

    init() {
        this.PAGES['geo-map'] = this.PAGES.network;
        this.state = { ...this.DEFAULT, ...this.loadStored(), ...this.fromUrl() };
        this.bar = document.getElementById('global-filters');
        if (!this.bar) return;
        this.hideReplaced();
        this.bindBar();
        this.render();
        // Before a page's own tab handler loads it, so it loads with these filters.
        document.addEventListener('click', (event) => {
            const btn = event.target.closest?.('.tab-btn[data-tab]');
            if (btn) this.onTab(btn.dataset.tab);
        }, true);
        // Reports' own window length keeps the bar's Time in step.
        document.getElementById('rpt-duration')?.addEventListener('change', (event) => {
            const h = Number(event.target.value);
            if (this.HOURS.includes(h)) { this.state.hours = h; this.save(); this.render(); }
        });
        const active = document.querySelector('.tab-btn.active[data-tab]');
        if (active) this.onTab(active.dataset.tab);
    },

    // ------------------------------------------------------------ state
    scope(st = this.state) {
        return st.nodes === 'all' ? 'all' : st.nodes + (st.nb ? '-connected' : '');
    },
    hours() { return this.state?.hours || 24; },

    loadStored() {
        try { return JSON.parse(localStorage.getItem(this.STORE) || '{}') || {}; } catch (e) { return {}; }
    },

    fromUrl() {
        const i = location.hash.indexOf('?');
        if (!location.hash.startsWith('#/') || i < 0) return {};
        const p = new URLSearchParams(location.hash.slice(i + 1));
        const out = {};
        if (p.has('nodes')) out.nodes = p.get('nodes');
        if (p.has('nb')) out.nb = p.get('nb') === '1';
        if (p.has('tag')) out.tag = p.get('tag');
        if (p.has('links')) out.links = p.get('links');
        if (p.has('hours') && this.HOURS.includes(Number(p.get('hours')))) out.hours = Number(p.get('hours'));
        if (p.has('q')) out.q = p.get('q');
        return out;
    },

    /** URL query for the current filters (only what differs from the defaults). */
    query() {
        const st = this.state || this.DEFAULT;
        const p = new URLSearchParams();
        const d = this.DEFAULT;
        if (st.nodes !== d.nodes) p.set('nodes', st.nodes);
        if (st.nb !== d.nb) p.set('nb', st.nb ? '1' : '0');
        if (st.tag !== 'all') p.set('tag', st.tag);
        if (st.links !== 'all') p.set('links', st.links);
        if (st.hours !== 24) p.set('hours', String(st.hours));
        if (st.q) p.set('q', st.q);
        const q = p.toString();
        return q ? `?${q}` : '';
    },

    save() {
        try { localStorage.setItem(this.STORE, JSON.stringify(this.state)); } catch (e) { /* convenience */ }
        // A bare URL (first page) gets its page path too; other hashes
        // (#diagnose/…, #power-advisor) are left alone.
        const h = location.hash;
        if (h.startsWith('#/') || !h) {
            const base = h ? h.split('?')[0] : `#/${(typeof PageRouter !== 'undefined' && PageRouter.tab) || 'status'}`;
            history.replaceState(null, '', base + this.query());
        }
    },

    // ------------------------------------------------------------ bar
    bindBar() {
        this.bar.querySelectorAll('[data-nodes]').forEach(b => b.addEventListener('click', () =>
            this.change({ nodes: b.dataset.nodes })));
        this.bar.querySelectorAll('[data-hours]').forEach(b => b.addEventListener('click', () =>
            this.change({ hours: Number(b.dataset.hours) })));
        document.getElementById('gf-neighbours')?.addEventListener('change', (e) => this.change({ nb: e.target.checked }));
        document.getElementById('gf-tag')?.addEventListener('change', (e) => this.change({ tag: e.target.value }));
        document.getElementById('gf-links')?.addEventListener('change', (e) => this.change({ links: e.target.value }));
        document.getElementById('gf-search')?.addEventListener('input', (e) => {
            clearTimeout(this.searchTimer);
            this.searchTimer = setTimeout(() => this.change({ q: e.target.value.trim() }), 250);
        });
        document.getElementById('gf-reset')?.addEventListener('click', () => this.change({ ...this.DEFAULT }));
    },

    change(patch) {
        this.state = { ...this.state, ...patch };
        this.save();
        this.render();
        const page = this.PAGES[this.tab];
        if (page) page.set(this.state, true, this);
    },

    render() {
        const st = this.state;
        const page = this.PAGES[this.tab];
        const uses = page?.uses || {};
        this.bar.hidden = !page;
        const off = (el, isOff) => {
            if (!el) return;
            if (!('title' in el.dataset)) el.dataset.title = el.title || '';
            el.classList.toggle('gf-off', !!isOff);
            el.title = isOff ? 'Not used on this page' : el.dataset.title;
            el.querySelectorAll('button, input, select').forEach(c => { c.disabled = !!isOff; });
        };
        this.bar.querySelectorAll('[data-nodes]').forEach(b => {
            b.classList.toggle('active', b.dataset.nodes === st.nodes);
            const ok = uses.nodes === true || (Array.isArray(uses.nodes) && uses.nodes.includes(b.dataset.nodes));
            b.disabled = !ok;
            b.title = ok ? '' : 'Not used on this page';
        });
        this.bar.querySelectorAll('[data-hours]').forEach(b => b.classList.toggle('active', Number(b.dataset.hours) === st.hours));
        const g = (name) => this.bar.querySelector(`[data-gf="${name}"]`);
        off(g('nodes')?.querySelector('.gf-label'), !uses.nodes);
        off(g('neighbours'), !uses.nb);
        off(g('tag'), !uses.tag);
        off(g('time'), !uses.time);
        off(g('search'), !uses.search);
        off(g('links'), !uses.links);
        const nb = document.getElementById('gf-neighbours');
        if (nb) nb.checked = !!st.nb;
        const tag = document.getElementById('gf-tag');
        if (tag) tag.value = st.tag;
        const links = document.getElementById('gf-links');
        if (links) {
            links.value = st.links;
            [...links.options].forEach(o => {
                o.disabled = Array.isArray(uses.links) && !uses.links.includes(o.value);
            });
        }
        const search = document.getElementById('gf-search');
        if (search && document.activeElement !== search) search.value = st.q;
        const note = document.getElementById('gf-note');
        if (note) {
            const unused = Array.isArray(uses.nodes) && !uses.nodes.includes(st.nodes)
                ? `This page shows ${uses.nodes.join(' or ')} nodes; "${st.nodes}" counts as all here. ` : '';
            note.textContent = unused + (page?.note || '');
        }
    },

    onTab(tab) {
        this.tab = tab;
        this.render();
        const page = this.PAGES[tab];
        if (!page) return;
        page.set(this.state, false, this);
        if (page.afterOpen) setTimeout(() => page.afterOpen(this.state, this), 0);
    },

    // ------------------------------------------------------------ helpers
    /**
     * Set a page control to `value` (adding a select option when it lacks
     * one); with `fire`, dispatch its change (or input) event so the page
     * reacts. Returns true when the value changed.
     */
    put(id, value, fire = false, eventName = 'change') {
        const el = document.getElementById(id);
        if (!el) return false;
        let changed;
        if (el.type === 'checkbox') {
            changed = el.checked !== !!value;
            el.checked = !!value;
        } else {
            const v = String(value ?? '');
            if (el.tagName === 'SELECT' && ![...el.options].some(o => o.value === v)) {
                el.add(new Option(this.hoursLabel(v), v));
            }
            changed = el.value !== v;
            el.value = v;
        }
        if (fire && changed) el.dispatchEvent(new Event(eventName, { bubbles: true }));
        return changed;
    },

    hoursLabel(v) {
        const h = Number(v);
        if (!h) return v;
        return h >= 48 && h % 24 === 0 ? `Last ${h / 24} days` : `Last ${h} hours`;
    },

    reportsWindow(st) {
        if (typeof ReportsModule === 'undefined' || !document.getElementById('rpt-duration')) return;
        if (ReportsModule.durationHours() === st.hours) return;
        this.put('rpt-duration', st.hours);
        ReportsModule.latest();
    },

    hideReplaced() {
        for (const id of this.REPLACED) {
            const el = document.getElementById(id);
            if (!el) continue;
            const label = el.closest('label');
            const target = label && label.querySelectorAll('input, select, textarea').length === 1 ? label : el;
            target.classList.add('gf-replaced');
        }
        document.querySelector('#rf-stats-view .time-range-selector')?.classList.add('gf-replaced');
    }
};

document.addEventListener('DOMContentLoaded', () => GlobalFilter.init());
