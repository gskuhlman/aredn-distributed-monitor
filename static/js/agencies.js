// Agencies board (/board/agencies) and the agency tiles on the wall display.
(function () {
    'use strict';

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    const STATE = { ok: 'Serving', degraded: 'Degraded', down: 'Down' };

    function pathText(t) {
        if (!t.paths_total) return 'no watched paths';
        const p = t.paths, parts = [];
        const good = (p.normal || 0) + (p.learning || 0);
        parts.push(`${good}/${t.paths_total} usual`);
        if (p.changed) parts.push(`${p.changed} changed`);
        if (p.detour) parts.push(`${p.detour} detour`);
        if (p.failing) parts.push(`${p.failing} failing`);
        return parts.join(', ');
    }

    function tile(t) {
        const rows = [
            ['Nodes up', `${t.nodes_up}/${t.nodes_total}`, t.nodes_down.length ? 'Down: ' + t.nodes_down.join(', ') : ''],
            ['Services', t.services_total ? `${t.services_up}/${t.services_total}` : '—', t.services_down.join(', ')],
            ['Paths', pathText(t), ''],
            ['Uptime this month', t.uptime_month_pct == null ? '—' : `${t.uptime_month_pct.toFixed(2)}%`, ''],
        ];
        const extra = [];
        t.maintenance.forEach(m => extra.push(`<div class="ag-note ag-maint">Maintenance until ${esc((m.end || '').slice(11, 16))}${m.note ? ': ' + esc(m.note) : ''}</div>`));
        t.incidents.forEach(i => extra.push(`<div class="ag-note ag-inc">Incident: ${esc(i.title)} (${esc(i.status)})</div>`));
        const href = t.site ? `/sites/${encodeURIComponent(t.site)}` : '#';
        return `<div class="ag-tile ag-${esc(t.state)}">
            <div class="ag-head"><a href="${href}" class="ag-name">${esc(t.name)}</a>
                <span class="ag-state">${esc(STATE[t.state] || t.state)}</span></div>
            <dl>${rows.map(r => `<dt>${esc(r[0])}</dt><dd>${esc(r[1])}${r[2] ? `<small>${esc(r[2])}</small>` : ''}</dd>`).join('')}</dl>
            ${extra.join('')}</div>`;
    }

    function render(el, data) {
        el.innerHTML = data.agencies.length ? data.agencies.map(tile).join('')
            : '<p class="form-hint">No agencies: mark infrastructure nodes with a site, or add agencies with Edit agencies.</p>';
    }

    // Details under the tiles: what is wrong with each agency that isn't fully serving.
    const SVC = { up: 'Up', down: 'Down', alarm: 'Down (alarm)', node_down: 'Node down', unchecked: 'Not checked' };
    const PATH = { failing: 'Failing', detour: 'Detour', changed: 'Changed route', normal: 'Usual route', learning: 'Learning' };

    function table(head, rows) {
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table ag-dtable">
            <thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead>
            <tbody>${rows.join('')}</tbody></table></div>`;
    }

    function detail(t) {
        const d = t.details;
        if (!d) return '';
        const nodes = d.nodes.map(n => `<tr class="${n.up ? '' : 'ag-bad'}">
            <td><a href="/nodes/${encodeURIComponent(n.name)}">${esc(n.name)}</a></td>
            <td>${n.up ? 'Up' : esc(n.cause_label || 'Down')}</td>
            <td>${n.up ? '' : esc(n.down_for || '')}</td>
            <td>${esc(n.last_seen || 'never')}</td>
            <td>${n.up ? '' : esc([n.detail, n.evidence].filter(Boolean).join(' '))}</td></tr>`);
        const svcs = d.services.map(s => `<tr class="${s.problem ? 'ag-bad' : ''}">
            <td>${esc(s.service)}</td><td><a href="/nodes/${encodeURIComponent(s.node)}">${esc(s.node)}</a></td>
            <td>${esc(SVC[s.state] || 'Not checked')}</td><td>${esc(s.down_for || '')}</td><td>${esc(s.text)}</td></tr>`);
        const paths = d.paths.map(p => `<tr class="${p.problem ? 'ag-bad' : ''}">
            <td>${esc(p.label || p.target)}${p.role === 'access' ? ' <span class="form-hint">(from this monitor)</span>' : ''}</td>
            <td>${esc(PATH[p.status] || p.status || '')}${p.flapping ? ', flapping' : ''}</td>
            <td>${esc(p.down_for || '')}</td>
            <td>${p.fails_24h} of ${p.runs_24h}</td>
            <td>${esc(p.route_now)}${p.usual_route && p.usual_route !== p.route_now ? `<br><span class="form-hint">usual: ${esc(p.usual_route)}</span>` : ''}</td>
            <td>${esc(p.text)}</td></tr>`);
        const problems = d.problems.length ? `<ul class="ag-problems">${d.problems.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`
            : '<p>No node, service or path problem right now' + (t.incidents.length ? ' (see the open incident above).' : '.') + '</p>';
        return `<section class="node-page-section ag-detail ag-${esc(t.state)}">
            <h2>${esc(t.name)} &ndash; ${esc(STATE[t.state] || t.state)}</h2>
            <h3>What's wrong</h3>${problems}
            <h3>Nodes</h3>${nodes.length ? table(['Node', 'Status', 'Down for', 'Last answered', 'Cause'], nodes) : '<p class="form-hint">No nodes.</p>'}
            <h3>Services</h3>${svcs.length ? table(['Service', 'Node', 'State', 'Down for', 'Details'], svcs) : '<p class="form-hint">No advertised services.</p>'}
            <h3>Paths</h3>${paths.length ? table(['Path', 'Status', 'Failing for', 'Failed traces (24 h)', 'Route now', 'Details'], paths) : '<p class="form-hint">No watched paths end here.</p>'}
        </section>`;
    }

    function renderDetails(el, data) {
        const bad = data.agencies.filter(t => t.state !== 'ok');
        const good = data.agencies.filter(t => t.state === 'ok');
        el.innerHTML = bad.map(detail).join('') + (good.length ? `<section class="node-page-section">
            <h2>Serving normally</h2><ul>${good.map(t => `<li><strong>${esc(t.name)}</strong>: ${t.nodes_up}/${t.nodes_total} nodes up,
            ${t.services_total ? `${t.services_up}/${t.services_total} services` : 'no checked services'}, ${esc(pathText(t))}.</li>`).join('')}</ul></section>` : '');
    }

    window.AgencyTiles = { render, renderDetails, esc };

    const tilesEl = document.getElementById('ag-tiles');
    if (!tilesEl) return;     // wall display: only the renderer

    async function load() {
        try {
            const data = await (await fetch('/api/boards/agencies')).json();
            render(tilesEl, data);
            const detEl = document.getElementById('ag-details');
            if (detEl) renderDetails(detEl, data);
            document.getElementById('ag-updated').textContent = 'Updated ' + data.generated_at.slice(11, 16);
        } catch (e) {
            document.getElementById('ag-updated').textContent = 'Could not load (' + e.message + ')';
        }
    }

    const editBox = document.getElementById('ag-edit');
    document.getElementById('ag-edit-btn').addEventListener('click', async () => {
        editBox.hidden = !editBox.hidden;
        if (editBox.hidden) return;
        const cfg = await (await fetch('/api/boards/agencies/config')).json();
        document.getElementById('ag-edit-text').value = cfg.agencies
            .map(a => [a.name, a.site || '', (a.nodes || []).join(' ')].join(' | ').replace(/( \| )+$/, '')).join('\n');
    });
    document.getElementById('ag-save').addEventListener('click', async () => {
        const status = document.getElementById('ag-edit-status');
        const agencies = document.getElementById('ag-edit-text').value.split('\n').map(l => l.trim()).filter(Boolean)
            .map(l => { const [name, site, nodes] = l.split('|').map(x => (x || '').trim()); return { name, site, nodes }; });
        const res = await fetch('/api/boards/agencies/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agencies }) });
        const body = await res.json();
        status.textContent = res.ok ? 'Saved.' : (body.error || 'Failed');
        if (res.ok) load();
    });

    load();
    setInterval(load, 60000);
})();
