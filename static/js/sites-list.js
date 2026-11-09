/**
 * Sites page (menu Network → Sites): the infrastructure sites with links to
 * their dashboards (/sites/<code>), from /api/sites.
 */
const SitesList = {
    loaded: false,

    async load() {
        const el = document.getElementById('sites-list');
        if (!el) return;
        try {
            const resp = await fetch('/api/sites');
            const data = await resp.json();
            const sites = data.sites || [];
            this.loaded = true;
            el.innerHTML = sites.length ? `
                <table class="info-table">
                    <thead><tr><th>Site</th><th>Name</th><th>Radios</th><th></th></tr></thead>
                    <tbody>${sites.map(s => `
                        <tr>
                            <td><a href="/sites/${encodeURIComponent(s.code)}"><strong>${this.esc(s.code)}</strong></a></td>
                            <td>${this.esc(s.name)}</td>
                            <td>${(s.nodes || []).map(n => `<a href="/nodes/${encodeURIComponent(n)}">${this.esc(n)}</a>`).join(', ')}</td>
                            <td><a class="btn btn-small btn-primary" href="/sites/${encodeURIComponent(s.code)}">Open dashboard</a></td>
                        </tr>`).join('')}</tbody>
                </table>`
                : '<p class="log-empty">No infrastructure sites yet. Mark a node as infrastructure (with a site) from its node page.</p>';
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Could not load the site list.</p>';
            console.error('Sites load error:', error);
        }
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};
