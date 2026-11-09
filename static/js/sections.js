/**
 * Collapsible sections: click a section heading (`.diag-section > h3`) to
 * collapse it to the heading, click again to reopen it. Each section's state
 * is remembered in this browser (localStorage), keyed by its page and title.
 * Clicks on buttons, links or form controls inside a heading don't toggle.
 */
const Sections = {
    KEY: 'section-collapsed:',

    init(root = document) {
        root.querySelectorAll('.diag-section > h3').forEach(h3 => this.attach(h3));
    },

    attach(h3) {
        const section = h3.parentElement;
        if (section.dataset.collapsible) return;
        section.dataset.collapsible = '1';
        section.classList.add('collapsible');
        h3.setAttribute('role', 'button');
        h3.setAttribute('tabindex', '0');
        h3.title = h3.title || 'Click to collapse or open this section';
        const key = this.key(section, h3);
        if (this.load(key)) this.set(section, h3, true, key, false);
        else h3.setAttribute('aria-expanded', 'true');
        const toggle = () => this.set(section, h3, !section.classList.contains('collapsed'), key, true);
        h3.addEventListener('click', (event) => {
            if (event.target.closest('button, a, input, select, textarea, label')) return;
            toggle();
        });
        h3.addEventListener('keydown', (event) => {
            if (event.target !== h3 || (event.key !== 'Enter' && event.key !== ' ')) return;
            event.preventDefault();
            toggle();
        });
    },

    key(section, h3) {
        const page = section.closest('.view-container, .subpage, main, body');
        const where = page?.dataset.sub || page?.id || location.pathname;
        const title = (h3.childNodes[0]?.textContent || h3.textContent).trim();
        return `${this.KEY}${where}:${title}`;
    },

    set(section, h3, collapsed, key, save) {
        section.classList.toggle('collapsed', collapsed);
        h3.setAttribute('aria-expanded', String(!collapsed));
        if (!save) return;
        try {
            if (collapsed) localStorage.setItem(key, '1');
            else localStorage.removeItem(key);
        } catch (e) { /* remembering is a convenience */ }
    },

    load(key) {
        try { return localStorage.getItem(key) === '1'; } catch (e) { return false; }
    }
};

document.addEventListener('DOMContentLoaded', () => Sections.init());
