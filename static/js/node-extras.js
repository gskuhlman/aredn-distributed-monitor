/**
 * Node page additions (TODO round 2): new-node banner while the node is
 * watched (welcome.py), setup checklist (hygiene.py), a link to the antenna
 * alignment page and its
 * Babel neighbour costs (babel_view.py).
 */
document.addEventListener('DOMContentLoaded', async () => {
    const node = window.NODE_NAME;
    if (!node) return;
    const esc = (v) => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

    const banner = document.getElementById('node-welcome');
    if (banner && typeof NewNodes !== 'undefined') NewNodes.banner(node, banner);

    const setup = document.getElementById('node-setup');
    if (setup && typeof Hygiene !== 'undefined') Hygiene.block(node, setup);
    const align = document.getElementById('node-align-link');
    if (align) align.href = `/field/align?node=${encodeURIComponent(node)}`;

    try {
        const b = await (await fetch(`/api/reports/babel?node=${encodeURIComponent(node)}`)).json();
        const el = document.getElementById('node-babel');
        const n = b.nodes[0];
        if (!el || !n) return;
        const v = (x) => x == null ? '-' : Math.round(x);
        el.innerHTML = `${b.findings.map(f => `<p class="node-warning">${esc(f.title)}: ${esc(f.detail)}</p>`).join('')}
            <div class="table-scroll-wrapper"><table class="info-table diag-table"><thead><tr><th>Neighbour</th><th>Type</th>
            <th>LQ</th><th>rxcost</th><th>txcost</th><th>metric</th><th>routes</th></tr></thead>
            <tbody>${n.neighbours.map(x => `<tr><td>${esc(x.node)}</td><td>${esc(x.link_type)}</td><td>${v(x.lq)}</td>
                <td>${v(x.rxcost)}</td><td>${v(x.txcost)}</td><td>${v(x.metric)}</td><td>${x.routes ?? '-'}</td></tr>`).join('')}
            </tbody></table></div>`;
    } catch (e) { /* optional */ }
});
