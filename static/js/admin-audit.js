// Admin page: audit log (audit.py) and the history rollup status (rollup.py).
(function () {
    let timer = null;

    async function load() {
        const el = document.getElementById('au-list');
        const q = document.getElementById('au-q').value.trim();
        try {
            const d = await R7.json('/api/audit?limit=300' + (q ? '&q=' + encodeURIComponent(q) : ''));
            el.innerHTML = d.entries.length ? `<table class="info-table"><thead><tr><th>When</th><th>Action</th><th>Result</th>
                <th>Who</th><th>Details</th></tr></thead><tbody>${d.entries.map(e => `<tr><td>${R7.esc(e.timestamp)}</td>
                <td>${R7.esc(e.action)}<br><small>${R7.esc(e.method)} ${R7.esc(e.path)}</small></td><td>${R7.esc(e.status)}</td>
                <td>${R7.esc(e.user || 'not logged in')}<br><small>${R7.esc(e.client || '')}${e.session ? ' · session ' + R7.esc(e.session) : ''}</small></td>
                <td><small>${R7.esc((e.body || '').slice(0, 300))}</small></td></tr>`).join('')}</tbody></table>`
                : '<p class="log-empty">No changes recorded yet.</p>';
        } catch (e) { el.innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`; }
        try {
            const r = await R7.json('/api/admin/rollup');
            document.getElementById('au-rollup').textContent = `Link history: raw scan samples kept ${r.raw_days} days, ` +
                `then hourly rollups (${r.rows} rows, ${r.oldest || '—'} to ${r.newest || '—'}) kept ${r.retention_days} days.`;
        } catch (e) { /* rollup status is extra */ }
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!document.getElementById('au-list')) return;
        document.getElementById('au-refresh').addEventListener('click', load);
        document.getElementById('au-q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(load, 300); });
        load();
    });
})();
