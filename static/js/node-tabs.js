/**
 * Node page section groups: Overview · Radio · Links · History · Tools, plus
 * All (the whole page, as before). Each `.node-page-section[data-group]` is
 * shown with its group. The group is in the URL hash (`#radio`); a hash that
 * names an element inside a section (`#power-advisor`) opens that section's
 * group. The last group used is remembered in this browser.
 */
const NodeTabs = {
    KEY: 'node-page-group',
    GROUPS: ['overview', 'radio', 'links', 'history', 'tools', 'all'],

    init() {
        const nav = document.getElementById('node-group-tabs');
        if (!nav) return;
        nav.querySelectorAll('.node-group-btn').forEach(btn => btn.addEventListener('click', () => {
            this.show(btn.dataset.group);
            history.replaceState(null, '', `${location.pathname}${location.search}#${btn.dataset.group}`);
        }));
        window.addEventListener('hashchange', () => this.fromHash());
        if (!this.fromHash()) this.show(this.remembered() || 'overview', false);
    },

    /** Show the group the hash names, or the group holding the element it names. */
    fromHash() {
        const id = decodeURIComponent(location.hash.slice(1));
        if (!id) return false;
        if (this.GROUPS.includes(id)) { this.show(id); return true; }
        const group = document.getElementById(id)?.closest('.node-page-section[data-group]')?.dataset.group;
        if (!group) return false;
        this.show(group, false);
        return true;
    },

    show(group, remember = true) {
        if (!this.GROUPS.includes(group)) group = 'overview';
        document.querySelectorAll('#node-group-tabs .node-group-btn').forEach(b =>
            b.classList.toggle('active', b.dataset.group === group));
        document.querySelectorAll('.node-page-section[data-group]').forEach(s =>
            s.classList.toggle('ng-hidden', group !== 'all' && s.dataset.group !== group));
        // Charts drawn while hidden have no size; let Chart.js measure again.
        window.dispatchEvent(new Event('resize'));
        if (!remember) return;
        try { localStorage.setItem(this.KEY, group); } catch (e) { /* remembering is a convenience */ }
    },

    remembered() {
        try { return localStorage.getItem(this.KEY); } catch (e) { return null; }
    }
};

document.addEventListener('DOMContentLoaded', () => NodeTabs.init());
