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

    window.AgencyTiles = { render, esc };

    const tilesEl = document.getElementById('ag-tiles');
    if (!tilesEl) return;     // wall display: only the renderer

    async function load() {
        try {
            const data = await (await fetch('/api/boards/agencies')).json();
            render(tilesEl, data);
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
