/**
 * Services & Devices page (menu Nodes & links → Services & Devices,
 * services_catalog.py): every advertised service grouped by category with a
 * "new" badge (first seen in the last 24 h), the service check state where
 * probed, services that left this week, and the devices (hosts) on each
 * node's LAN from the mesh host list. Each service and device has Silence /
 * Ignore / Enable buttons (mute-controls.js); muted ones are hidden unless
 * "Show silenced and ignored" is ticked.
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
        document.getElementById('svc-show-muted').addEventListener('change', () => this.render());
        MuteControls.bind(document.getElementById('services-view'), () => this.load());
        R7.onShow('services', null, () => this.load());
    },

    async load() {
        const q = encodeURIComponent(document.getElementById('svc-q').value.trim());
        try {
            const [c, d] = await Promise.all([R7.json(`/api/services/catalog?q=${q}`), R7.json(`/api/devices?q=${q}`)]);
            this.c = c;
            this.d = d;
            this.render();
        } catch (e) {
            document.getElementById('svc-catalog').innerHTML = `<p class="log-empty">Could not load: ${R7.esc(e.message)}</p>`;
        }
    },

    /** Silenced / ignored services and devices show only with the checkbox. */
    render() {
        const c = this.c, d = this.d;
        if (!c || !d) return;
        const showMuted = document.getElementById('svc-show-muted')?.checked;
        const keep = (x) => showMuted || !x.mute;
        const cat = document.getElementById('svc-catalog');
        const dev = document.getElementById('svc-devices');
        const devCount = d.nodes.reduce((n, x) => n + x.devices.length, 0);
        const devMuted = d.nodes.reduce((n, x) => n + x.devices.filter(h => h.mute).length, 0);
        const muted = (c.counts.muted || 0) + devMuted;
        document.getElementById('svc-status').textContent =
            `${c.counts.services} services · ${c.counts.new} new today · ${c.counts.left} left this week · ${devCount} devices`
            + (muted ? ` · ${muted} silenced or ignored${showMuted ? '' : ' (hidden)'}` : '');
        const groups = c.groups.map(g => ({ ...g, services: g.services.filter(keep) })).filter(g => g.services.length);
        cat.innerHTML = groups.length ? groups.map(g => `<div class="diag-section"><h3>${R7.esc(g.label)} <small class="form-hint">${g.services.length}</small></h3>
            <div class="table-scroll-wrapper"><table class="info-table"><thead><tr><th>Service</th><th>Node</th><th>Address</th><th>Check</th><th>First seen</th><th>Alerts</th><th></th></tr></thead><tbody>
            ${g.services.map(s => `<tr><td>${s.link ? `<a href="${R7.esc(s.link)}" target="_blank" rel="noopener">${R7.esc(s.name)}</a>` : R7.esc(s.name)}
                ${s.new ? R7.badge('new', 'info') : ''}</td><td>${R7.node(s.node)}${s.node_active ? '' : ' <small>(node down)</small>'}</td>
                <td>${R7.esc(s.ip || '')}${s.protocol ? ` <small>${R7.esc(s.protocol)}</small>` : ''}</td>
                <td>${s.mute?.mode === 'ignore' ? '<small>not checked</small>' : s.check_state ? R7.badge(s.check_state, s.check_state === 'up' ? 'resolved' : s.check_state === 'alarm' ? 'critical' : 'warning') : ''}
                    ${s.check_detail && s.check_state !== 'up' && s.mute?.mode !== 'ignore' ? `<small>${R7.esc(s.check_detail)}</small>` : ''}</td>
                <td>${R7.hm(s.first_seen)}</td>
                <td>${MuteControls.label(s.mute)}</td>
                <td>${MuteControls.buttons(s.node, s.name, s.mute)}</td></tr>`).join('')}</tbody></table></div></div>`).join('')
            : '<p class="log-empty">No advertised services match.</p>';
        if (c.left.length) {
            cat.innerHTML += `<div class="diag-section"><h3>Left this week <small class="form-hint">no longer advertised while the node still answers</small></h3>
                <table class="info-table"><thead><tr><th>Service</th><th>Category</th><th>Node</th><th>Address</th><th>Last seen</th></tr></thead><tbody>
                ${c.left.map(s => `<tr><td>${R7.esc(s.name)}</td><td>${R7.esc(s.category_label)}</td><td>${R7.node(s.node)}</td>
                    <td>${R7.esc(s.ip || '')}</td><td>${R7.hm(s.last_seen)}</td></tr>`).join('')}</tbody></table></div>`;
        }
        const nodes = d.nodes.map(n => ({ ...n, devices: n.devices.filter(keep) })).filter(n => n.devices.length);
        dev.innerHTML = nodes.length ? `<div class="table-scroll-wrapper"><table class="info-table"><thead><tr><th>Node</th><th>Device</th><th>Address</th><th>Alerts</th><th></th></tr></thead><tbody>
            ${nodes.map(n => n.devices.map((h, i) => `<tr>
                <td>${i === 0 ? `${R7.node(n.node)}${n.active ? '' : ' <small>(down)</small>'}` : ''}</td>
                <td title="first seen ${R7.esc(h.first_seen || '')}, last ${R7.esc(h.last_seen || '')}">${R7.esc(h.name || '?')}</td>
                <td>${R7.esc(h.ip)}</td>
                <td>${MuteControls.label(h.mute)}</td>
                <td>${MuteControls.buttons(n.node, 'device:' + h.ip, h.mute)}</td></tr>`).join('')).join('')}</tbody></table></div>`
            : '<p class="log-empty">No devices in the mesh host list match.</p>';
    },
};

ServicesCatalog.init();
