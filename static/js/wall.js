// Wall display (/wall): agency tiles, the Status page's ticker line, agency
// details (as on /board/agencies), located mesh plot and the on-the-air tape, refreshed every 30 s; F = full screen, keeps the screen
// awake where the browser allows it, reloads itself every 6 hours.
(function () {
    'use strict';
    const esc = window.AgencyTiles.esc;
    let lastOk = 0;

    async function get(url) {
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) throw new Error(url + ' ' + res.status);
        return res.json();
    }

    // The Status page's scrolling ticker line (ticker.js), same items and wording.
    function ticker(data) {
        const el = document.getElementById('wall-scroll');
        const text = data.items.slice(0, 10).map(i => `${(i.t || '').slice(11, 16)} ${i.text}`).join('   •   ');
        const next = text || 'All quiet in the last 24 hours.';
        if (el.dataset.text === next) return;      // don't restart the scroll when nothing changed
        el.dataset.text = next;
        el.innerHTML = `<span>${esc(next)}</span>`;
    }

    function agencies(d) {
        window.AgencyTiles.render(document.getElementById('wall-agencies'), d);
        window.AgencyTiles.renderDetails(document.getElementById('wall-details'), d);
    }

    function plot(net) {
        const svg = document.getElementById('wall-plot');
        const nodes = net.nodes.filter(n => n.lat != null && n.lon != null && !n.is_link_only);
        if (!nodes.length) { svg.innerHTML = '<text x="300" y="200" text-anchor="middle" class="wp-empty">No located nodes</text>'; return; }
        // Fit the densest part: drop the 5% farthest from the median so one far node doesn't squash the rest.
        const med = a => a.slice().sort((x, y) => x - y)[Math.floor(a.length / 2)];
        const mlat = med(nodes.map(n => n.lat)), mlon = med(nodes.map(n => n.lon));
        const dist = n => Math.hypot(n.lat - mlat, (n.lon - mlon) * Math.cos(mlat * Math.PI / 180));
        const keep = nodes.slice().sort((a, b) => dist(a) - dist(b)).slice(0, Math.max(1, Math.ceil(nodes.length * 0.95)));
        const k = Math.cos(mlat * Math.PI / 180);
        const xs = keep.map(n => n.lon * k), ys = keep.map(n => n.lat);
        const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
        const span = Math.max(x1 - x0, (y1 - y0) * 1.5, 1e-3), pad = 24;
        const sx = (600 - 2 * pad) / span, cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
        const P = {};
        nodes.forEach(n => {
            const x = 300 + (n.lon * k - cx) * sx, y = 200 - (n.lat - cy) * sx;
            if (x > -10 && x < 610 && y > -10 && y < 410) P[n.id] = [x, y, n];
        });
        const lines = net.edges.filter(e => P[e.from] && P[e.to] && /rf|dtd/i.test(e.link_type || '')).map(e => {
            const a = P[e.from], b = P[e.to];
            const cls = e.status === 'good' ? 'wp-good' : e.status === 'degraded' ? 'wp-degr' : 'wp-bad';
            return `<line x1="${a[0].toFixed(1)}" y1="${a[1].toFixed(1)}" x2="${b[0].toFixed(1)}" y2="${b[1].toFixed(1)}" class="${cls}"/>`;
        });
        const dots = Object.values(P).map(([x, y, n]) => {
            const cls = n.is_inactive ? 'wp-down' : n.is_infrastructure ? 'wp-infra' : 'wp-node';
            const r = n.is_infrastructure ? 5 : 3;
            return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}" class="${cls}"><title>${esc(n.id)}</title></circle>`;
        });
        svg.innerHTML = lines.join('') + dots.join('');
    }

    function tape(data) {
        document.getElementById('wall-headline').textContent = data.headline ? data.headline.text : '';
        const el = document.getElementById('wall-tape');
        const rows = data.nodes.slice().sort((a, b) => (b.is_infrastructure - a.is_infrastructure) || a.name.localeCompare(b.name));
        el.innerHTML = rows.map(n => `<div class="wtape-row"><span class="wtape-name${n.is_infrastructure ? ' infra' : ''}">${esc(n.name)}</span>
            <span class="wtape-bar">${[...n.tape].map(c => `<i class="ts-${c === '-' ? 'n' : c}"></i>`).join('')}</span>
            <span class="wtape-pct">${n.heard_pct == null ? '' : Math.round(n.heard_pct) + '%'}</span></div>`).join('');
    }

    function storm(s) {
        const el = document.getElementById('wall-storm');
        el.hidden = !s.warning;
        el.textContent = s.warning ? '⛈ ' + s.text : '';
    }

    async function refresh() {
        const jobs = [
            get('/api/boards/agencies').then(agencies),
            get('/api/ticker?limit=20').then(ticker),
            get('/api/network').then(plot),
            get('/api/reports/tape?hours=24&scope=infrastructure-connected').then(tape),
            get('/api/weather/storm').then(storm).catch(() => {}),
        ];
        const res = await Promise.allSettled(jobs);
        if (res.some(r => r.status === 'fulfilled')) lastOk = Date.now();
    }

    function clock() {
        const now = new Date();
        document.getElementById('wall-clock').textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const live = document.getElementById('wall-live');
        const age = (Date.now() - lastOk) / 1000;
        live.className = 'wall-live ' + (!lastOk || age > 180 ? 'stale' : 'ok');
        live.title = lastOk ? `Updated ${Math.round(age)} s ago` : 'Not updated yet';
    }

    let wakeLock = null;
    async function keepAwake() {
        try { if ('wakeLock' in navigator && document.visibilityState === 'visible') wakeLock = await navigator.wakeLock.request('screen'); }
        catch (e) { /* not allowed here */ }
    }
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') keepAwake(); });
    document.addEventListener('keydown', e => {
        if ((e.key === 'f' || e.key === 'F') && !e.ctrlKey && !e.metaKey) {
            if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {});
        }
    });

    refresh();
    keepAwake();
    setInterval(refresh, 30000);
    setInterval(clock, 1000);
    setTimeout(() => location.reload(), 6 * 3600 * 1000);
})();
