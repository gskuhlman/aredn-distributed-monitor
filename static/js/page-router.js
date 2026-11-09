/**
 * Page URLs and sub-pages. Each page (tab) has a URL hash `#/<tab>` and pages
 * with a side menu (`.subnav`) add the sub-page: `#/radio/sectors`. Clicking
 * a tab or sub-page updates the hash (browser back/forward work); opening a
 * URL with a hash shows that page. Other hashes (`#diagnose/a/b`,
 * `#changes/12`, `#power-advisor`) are left to the modules that own them.
 * With no hash the Status page opens. Tabs still switch through their
 * `.tab-btn[data-tab]` buttons, so every existing "open this tab" click works.
 */
const PageRouter = {
    applying: false,
    tab: 'status',
    // Old page names that moved.
    ALIASES: { diagnostics: 'status', stability: 'rf-stats/stability' },
    // `#/<tab>/<anchor>` on pages without a side menu scrolls to an element.
    ANCHORS: { reports: { weekly: 'weekly-report-section' }, 'rf-stats': { stability: 'rf-stability' } },

    init() {
        document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => btn.addEventListener('click', () => {
            this.tab = btn.dataset.tab;
            if (!this.applying) this.push(this.tab, this.currentSub(this.tab));
        }));
        document.querySelectorAll('.subnav').forEach(nav => {
            const view = nav.closest('.view-container');
            const tab = view?.id.replace(/-view$/, '');
            nav.querySelectorAll('.subnav-btn').forEach(btn => btn.addEventListener('click', () => {
                this.showSub(view, btn.dataset.sub);
                if (!this.applying) this.push(tab, btn.dataset.sub);
            }));
            this.showSub(view, this.remembered(tab) || nav.querySelector('.subnav-btn')?.dataset.sub);
        });
        // Menu entries that act instead of opening a page (e.g. Scanning settings).
        document.querySelectorAll('[data-click]').forEach(el => el.addEventListener('click', () => {
            document.getElementById(el.dataset.click)?.click();
        }));
        window.addEventListener('popstate', () => this.apply());
        window.addEventListener('hashchange', () => this.apply());
        this.apply(true);
    },

    /**
     * Older deep links, on a fresh page load or a click inside the page:
     * #diagnose/<a>/<b> (site dashboard) opens the pair on Links;
     * #changes and #changes/<id> (node page, maintenance) open Changes and
     * that change's before/after comparison. #power-advisor belongs to the node page.
     */
    legacy(first) {
        const h = location.hash || '';
        let m = /^#diagnose\/([^/]+)\/([^/]+)$/.exec(h);
        if (m) {
            const [a, b] = [decodeURIComponent(m[1]), decodeURIComponent(m[2])];
            this.open('rf-stats', null, first);
            setTimeout(() => { if (typeof RFStats !== 'undefined') RFStats.selectLink(a, b); }, 500);
            return true;
        }
        m = /^#changes(?:\/(\d+))?$/.exec(h);
        if (m) {
            this.open('changes', null, first);
            if (m[1]) {
                const id = Number(m[1]);
                const wait = setInterval(() => {
                    if (typeof ChangesModule !== 'undefined' && ChangesModule.changes.length) {
                        clearInterval(wait);
                        ChangesModule.compare(id);
                    }
                }, 200);
                setTimeout(() => clearInterval(wait), 10000);
            }
            return true;
        }
        return false;
    },

    parse() {
        const h = location.hash || '';
        if (!h.startsWith('#/')) return null;
        // `?…` after the page is the shared filter bar's state (global-filter.js).
        let [tab, sub] = h.slice(2).split('?')[0].split('/').map(decodeURIComponent);
        if (this.ALIASES[tab]) [tab, sub = sub] = this.ALIASES[tab].split('/');
        return { tab, sub };
    },

    apply(first = false) {
        const route = this.parse();
        if (!route) {
            if (this.legacy(first)) return;
            if (first) this.open('status', null, true);   // bare URL (or an unknown hash): Status
            return;
        }
        this.open(route.tab, route.sub, first);
    },

    open(tab, sub, force = false) {
        const btn = document.querySelector(`.tab-btn[data-tab="${CSS.escape(tab)}"]`);
        if (!btn) return;
        this.applying = true;
        try {
            // force: the first page is marked active in the HTML but not loaded yet.
            if (force || this.tab !== tab || !btn.classList.contains('active')) btn.click();
            this.tab = tab;
            const view = document.getElementById(`${tab}-view`);
            if (sub && view?.querySelector(`.subnav-btn[data-sub="${CSS.escape(sub)}"]`)) {
                this.showSub(view, sub);
            } else if (sub && this.ANCHORS[tab]?.[sub]) {
                // Again once the page's data has drawn the sections above it.
                const go = () => document.getElementById(this.ANCHORS[tab][sub])
                    ?.scrollIntoView({ block: 'start' });
                [300, 2500, 5000].forEach(ms => setTimeout(go, ms));
            }
        } finally {
            this.applying = false;
        }
    },

    push(tab, sub) {
        const filters = (typeof GlobalFilter !== 'undefined') ? GlobalFilter.query() : '';
        const hash = `#/${tab}${sub ? '/' + sub : ''}${filters}`;
        if (location.hash !== hash) history.pushState(null, '', hash);
    },

    currentSub(tab) {
        return document.querySelector(`#${tab}-view .subnav-btn.active`)?.dataset.sub || '';
    },

    showSub(view, sub) {
        if (!view || !sub) return;
        const tab = view.id.replace(/-view$/, '');
        view.querySelectorAll('.subnav-btn').forEach(b => b.classList.toggle('active', b.dataset.sub === sub));
        view.querySelectorAll('.subpage').forEach(p => p.classList.toggle('active', p.dataset.sub === sub));
        view.querySelector('.subnav-body')?.scrollTo(0, 0);
        try { localStorage.setItem(`subpage:${tab}`, sub); } catch (e) { /* remembering is a convenience */ }
        document.dispatchEvent(new CustomEvent('subpage-shown', { detail: { tab, sub } }));
    },

    remembered(tab) {
        try { return localStorage.getItem(`subpage:${tab}`); } catch (e) { return null; }
    }
};

// After the modules' own DOMContentLoaded handlers have hooked the tab buttons.
document.addEventListener('DOMContentLoaded', () => setTimeout(() => PageRouter.init(), 0));
