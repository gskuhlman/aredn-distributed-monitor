/**
 * Header page menu: one button showing the current page, opening a grouped
 * drop-down of the tab buttons. The items stay `.tab-btn[data-tab]` buttons,
 * so the existing tab switching (rf-stats.js, channel-map.js) and every
 * "open this tab" link keep working; this only shows/hides the drop-down and
 * keeps the button label in step with the active tab.
 */
const NavMenu = {
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
            if (!panel.hidden) panel.querySelector('.tab-btn.active, .tab-btn')?.focus();
        });
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
        this.showFilters(active?.dataset.tab || 'network');
        // The selected-only box is loaded from settings after start-up.
        setTimeout(() => this.showFilters(), 1500);
    },

    // Header graph filters only affect the Topology and Map pages; elsewhere
    // they are hidden, but a selected-only scan stays visible as a badge
    // because it also limits what the scanner polls.
    FILTER_TABS: ['network', 'geo-map'],
    tab: 'network',

    showFilters(tab) {
        if (tab) this.tab = tab;
        const onMap = this.FILTER_TABS.includes(this.tab);
        const filters = document.getElementById('header-filters');
        if (filters) filters.hidden = !onMap;
        const badge = document.getElementById('selected-scan-badge');
        if (badge) badge.hidden = onMap || !document.getElementById('scan-selected-only')?.checked;
    }
};

document.addEventListener('DOMContentLoaded', () => NavMenu.init());
