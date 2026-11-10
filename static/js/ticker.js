/**
 * Status page "What's happening" (boards.ticker): a scrolling line of the
 * newest incidents, maintenance and warning / critical events (recoveries
 * too), the list under it, a storm warning (storm.py) and a banner when an
 * alarm suppression is past its review date. Every minute while Status is open.
 */
const Ticker = {
    timer: null,

    init() {
        if (!document.getElementById('tk-list')) return;
        document.getElementById('tk-refresh').addEventListener('click', () => this.load());
        R7.onShow('status', null, () => {
            this.load();
            clearInterval(this.timer);
            this.timer = setInterval(() => {
                if (document.getElementById('status-view')?.classList.contains('active')) this.load();
            }, 60000);
        });
    },

    line(i) {
        return `<li class="wt-${R7.esc(i.severity)} wt-${R7.esc(i.kind)}"><time>${R7.esc((i.t || '').slice(11, 16))}</time>${
            i.kind === 'incident' ? `<a href="#/incidents" data-inc="${i.id}">${R7.esc(i.text)}</a>` : R7.esc(i.text)}</li>`;
    },

    async load() {
        try {
            const [t, storm, sup] = await Promise.all([
                R7.json('/api/ticker?limit=25'),
                R7.json('/api/weather/storm').catch(() => null),
                R7.json('/api/suppressions').catch(() => null),
            ]);
            const items = t.items;
            const scroll = document.getElementById('tk-scroll');
            const text = items.slice(0, 10).map(i => `${(i.t || '').slice(11, 16)} ${i.text}`).join('   •   ');
            scroll.innerHTML = text ? `<span>${R7.esc(text)}</span>` : '<span>All quiet in the last 24 hours.</span>';
            document.getElementById('tk-list').innerHTML = items.length ? items.map(i => this.line(i)).join('')
                : '<li>Nothing to report in the last 24 hours.</li>';
            document.querySelectorAll('#tk-list [data-inc]').forEach(a => a.addEventListener('click', () => {
                if (typeof Incidents !== 'undefined') Incidents.pendingId = Number(a.dataset.inc);
            }));
            const banners = [];
            if (storm && storm.warning) banners.push(`<div class="overdue-banner">⛈ ${R7.esc(storm.text)}</div>`);
            if (sup && sup.overdue) banners.push(`<div class="overdue-banner">${sup.overdue} alarm suppression${sup.overdue > 1 ? 's are' : ' is'} past the review date.
                <a href="#/notifications">Review on the Notifications page</a>.</div>`);
            document.getElementById('tk-banners').innerHTML = banners.join('');
        } catch (e) {
            document.getElementById('tk-list').innerHTML = `<li>${R7.esc(e.message)}</li>`;
        }
    },
};

Ticker.init();
