/**
 * Services & Devices page (menu Nodes & links → Services & Devices,
 * services_catalog.py): every advertised service grouped by category with a
 * "new" badge (first seen in the last 24 h), the service check state where
 * probed, services that left this week, and the devices (hosts) on each
 * node's LAN from the mesh host list.
 */
const ServicesCatalog = {
    timer: null,

    init() {
        if (!document.getElementById('svc-catalog')) return;
        document.getElementById('svc-q').addEventListener('input', () => {
            clearTimeout(this.timer);
            this.timer = setTimeout(() => this.load(), 300);
        });
        document.getElementById('svc-refresh').addEventListener('click', () => this.load());
        R7.onShow('services', null, () => this.load());
    },

    async load() {
        const q = encodeURIComponent(document.getElementById('svc-q').value.trim());
        const cat = document.getElementById('svc-catalog');
        const dev = document.getElementById('svc-devices');
        try {
            const [c, d] = await Promise.all([R7.json(`/api/services/catalog?q=${q}`), R7.json(`/api/devices?q=${q}`)]);
            document.getElementById('svc-status').textContent =
                `${c.counts.services} services · ${c.counts.new} new today · ${c.counts.left} left this week · ${d.count} devices`;
            cat.innerHTML = c.groups.length ? c.groups.map(g => `<div class="diag-section"><h3>${R7.esc(g.label)} <small class="form-hint">${g.count}</small></h3>
                <table class="info-table"><thead><tr><th>Service</th><th>Node</th><th>Address</th><th>Check</th><th>First seen</th></tr></thead><tbody>
                ${g.services.map(s => `<tr><td>${s.link ? `<a href="${R7.esc(s.link)}" target="_blank" rel="noopener">${R7.esc(s.name)}</a>` : R7.esc(s.name)}
                    ${s.new ? R7.badge('new', 'info') : ''}</td><td>${R7.node(s.node)}${s.node_active ? '' : ' <small>(node down)</small>'}</td>
                    <td>${R7.esc(s.ip || '')}${s.protocol ? ` <small>${R7.esc(s.protocol)}</small>` : ''}</td>
                    <td>${s.check_state ? R7.badge(s.check_state, s.check_state === 'up' ? 'resolved' : s.check_state === 'alarm' ? 'critical' : 'warning') : ''}
                        ${s.check_detail && s.check_state !== 'up' ? `<small>${R7.esc(s.check_detail)}</small>` : ''}</td>
                    <td>${R7.hm(s.first_seen)}</td></tr>`).join('')}</tbody></table></div>`).join('')
                : '<p class="log-empty">No advertised services match.</p>';
            if (c.left.length) {
                cat.innerHTML += `<div class="diag-section"><h3>Left this week <small class="form-hint">no longer advertised while the node still answers</small></h3>
                    <table class="info-table"><thead><tr><th>Service</th><th>Category</th><th>Node</th><th>Address</th><th>Last seen</th></tr></thead><tbody>
                    ${c.left.map(s => `<tr><td>${R7.esc(s.name)}</td><td>${R7.esc(s.category_label)}</td><td>${R7.node(s.node)}</td>
                        <td>${R7.esc(s.ip || '')}</td><td>${R7.hm(s.last_seen)}</td></tr>`).join('')}</tbody></table></div>`;
            }
            dev.innerHTML = d.nodes.length ? `<table class="info-table"><thead><tr><th>Node</th><th>Devices on its LAN</th></tr></thead><tbody>
                ${d.nodes.map(n => `<tr><td>${R7.node(n.node)}${n.active ? '' : ' <small>(down)</small>'}</td>
                    <td>${n.devices.map(h => `<span title="first seen ${R7.esc(h.first_seen || '')}, last ${R7.esc(h.last_seen || '')}">${R7.esc(h.name || '?')} <small>${R7.esc(h.ip)}</small></span>`).join(' · ')}</td></tr>`).join('')}</tbody></table>`
                : '<p class="log-empty">No devices in the mesh host list match.</p>';
        } catch (e) {
            cat.innerHTML = `<p class="log-empty">Could not load: ${R7.esc(e.message)}</p>`;
        }
    },
};

ServicesCatalog.init();
