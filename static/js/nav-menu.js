/**
 * Header page menu: one button showing the current page, opening a grouped
 * drop-down of the tab buttons. The items stay `.tab-btn[data-tab]` buttons,
 * so the existing tab switching (rf-stats.js, channel-map.js) and every
 * "open this tab" link keep working; this only shows/hides the drop-down and
 * keeps the button label in step with the active tab.
 */
const NavMenu = {
    /** Keep the open menu inside the window on small or short screens: fixed
     * under the button, no wider or taller than the window, scrolling inside. */
    fit(toggle, panel) {
        const gap = 8;
        const r = toggle.getBoundingClientRect();
        const top = Math.round(r.bottom + 6);
        panel.style.position = 'fixed';
        panel.style.top = `${top}px`;
        panel.style.maxWidth = `${window.innerWidth - 2 * gap}px`;
        panel.style.maxHeight = `${Math.max(160, window.innerHeight - top - gap)}px`;
        panel.style.left = `${gap}px`;           // measure at the left edge, then move under the button if it fits
        const w = panel.offsetWidth;
        panel.style.left = `${Math.max(gap, Math.min(r.left, window.innerWidth - w - gap))}px`;
    },

    init() {
        const toggle = document.getElementById('nav-toggle');
        const panel = document.getElementById('nav-panel');
        if (!toggle || !panel) return;
        const close = () => {
            panel.hidden = true;
            toggle.setAttribute('aria-expanded', 'false');
        };
        toggle.addEventListener('click', (event) => {
            event.stopPropagation();
            panel.hidden = !panel.hidden;
            toggle.setAttribute('aria-expanded', String(!panel.hidden));
            if (!panel.hidden) {
                this.fit(toggle, panel);
                panel.querySelector('.tab-btn.active, .tab-btn')?.focus({ preventScroll: true });
            }
        });
        window.addEventListener('resize', () => { if (!panel.hidden) this.fit(toggle, panel); });
        panel.querySelectorAll('.nav-link').forEach(link => link.addEventListener('click', close));
        panel.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => {
            document.getElementById('nav-current').textContent = btn.textContent.trim();
            this.showFilters(btn.dataset.tab);
            close();
        }));
        document.getElementById('scan-selected-only')?.addEventListener('change', () => this.showFilters());
        document.addEventListener('click', (event) => {
            if (!panel.hidden && !panel.contains(event.target) && event.target !== toggle) close();
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && !panel.hidden) {
                close();
                toggle.focus();
            }
        });
        const active = panel.querySelector('.tab-btn.active');
        if (active) document.getElementById('nav-current').textContent = active.textContent.trim();
        this.showFilters(active?.dataset.tab || 'status');
        // The selected-only box is loaded from settings after start-up.
        setTimeout(() => this.showFilters(), 1500);
    },

    // The old header graph filters stay in the page (network.js reads them)
    // but are set from the shared filter bar (global-filter.js). A
    // selected-only scan shows as a badge on every page because it also limits
    // what the scanner polls (set in Scanning settings).
    tab: 'status',

    showFilters(tab) {
        if (tab) this.tab = tab;
        const filters = document.getElementById('header-filters');
        if (filters) filters.hidden = true;
        const badge = document.getElementById('selected-scan-badge');
        if (badge) badge.hidden = !document.getElementById('scan-selected-only')?.checked;
    }
};

document.addEventListener('DOMContentLoaded', () => NavMenu.init());
