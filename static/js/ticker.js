/**
 * Status page "What's happening" (boards.ticker): a scrolling line of the
 * newest incidents, maintenance and warning / critical events (recoveries
 * too), the list under it, a storm warning (storm.py) and a banner when an
 * alarm suppression is past its review date. Every minute while Status is open.
 * Each list line links to the page that shows that problem.
 */
const Ticker = {
    timer: null,

    init() {
        if (!document.getElementById('tk-list')) return;
        document.getElementById('tk-refresh').addEventListener('click', () => this.load());
        document.getElementById('sn-toggle')?.addEventListener('click', () => {
            const ed = document.getElementById('sn-editor');
            ed.hidden = !ed.hidden;
            if (!ed.hidden) this.load();
        });
        document.getElementById('sn-add')?.addEventListener('click', () => this.addNote());
        document.getElementById('sn-list')?.addEventListener('click', (e) => {
            const b = e.target.closest('[data-sn-del]');
            if (b) this.deleteNote(b.dataset.snDel);
        });
        R7.onShow('status', null, () => {
            this.load();
            clearInterval(this.timer);
            this.timer = setInterval(() => {
                if (document.getElementById('status-view')?.classList.contains('active')) this.load();
            }, 60000);
        });
    },

    /** Each line links to the page that shows its problem (boards.event_link). */
    line(i) {
        const l = i.link;
        const inc = i.kind === 'incident' ? ` data-inc="${i.id}"` : '';
        const text = l ? `<a href="${R7.esc(l.href)}"${inc} title="Open ${R7.esc(l.label)}">${R7.esc(i.text)}</a>
            <a class="tk-go" href="${R7.esc(l.href)}"${inc}>${R7.esc(l.label)} →</a>` : R7.esc(i.text);
        return `<li class="wt-${R7.esc(i.severity)} wt-${R7.esc(i.kind)}"><time>${R7.esc((i.t || '').slice(11, 16))}</time>${text}</li>`;
    },

    async load() {
        try {
            const [t, storm, sup, notes] = await Promise.all([
                R7.json('/api/ticker?limit=25'),
                R7.json('/api/weather/storm').catch(() => null),
                R7.json('/api/suppressions').catch(() => null),
                R7.json('/api/status-notes').catch(() => null),
            ]);
            this.renderNotes(notes);
            const items = t.items;
            const scroll = document.getElementById('tk-scroll');
            const text = items.slice(0, 10).map(i => `${(i.t || '').slice(11, 16)} ${i.text}`).join('   •   ');
            scroll.innerHTML = text ? `<span>${R7.esc(text)}</span>` : '<span>All quiet in the last 24 hours.</span>';
            document.getElementById('tk-list').innerHTML = items.length ? items.map(i => this.line(i)).join('')
                : '<li>Nothing to report in the last 24 hours.</li>';
            document.querySelectorAll('#tk-list [data-inc]').forEach(a => a.addEventListener('click', () => {
                if (typeof Incidents !== 'undefined') Incidents.pendingId = Number(a.dataset.inc);
            }));
            const banners = (notes?.notes || []).map(n =>
                `<div class="overdue-banner sn-banner sn-${R7.esc(n.level)}">📌 ${R7.esc(n.text)}${n.ends_at
                    ? ` <small>(until ${R7.esc(n.ends_at.slice(0, 16))})</small>` : ''}</div>`);
            if (storm && storm.warning) banners.push(`<div class="overdue-banner">⛈ ${R7.esc(storm.text)}</div>`);
            if (sup && sup.overdue) banners.push(`<div class="overdue-banner">${sup.overdue} alarm suppression${sup.overdue > 1 ? 's are' : ' is'} past the review date.
                <a href="#/notifications">Review on the Notifications page</a>.</div>`);
            document.getElementById('tk-banners').innerHTML = banners.join('');
        } catch (e) {
            document.getElementById('tk-list').innerHTML = `<li>${R7.esc(e.message)}</li>`;
        }
    },

    renderNotes(d) {
        const el = document.getElementById('sn-list');
        if (!el || !d) return;
        const rows = d.all || d.notes || [];
        el.innerHTML = rows.length ? `<table class="info-table diag-table"><tbody>${rows.map(n => `<tr>
            <td>${n.showing === false ? R7.badge('scheduled', 'info') : R7.badge('showing', 'resolved')}</td>
            <td>${R7.esc(n.text)}</td>
            <td><small>${R7.esc(n.starts_at.slice(0, 16))} → ${n.ends_at ? R7.esc(n.ends_at.slice(0, 16)) : 'until removed'}</small></td>
            <td><small>${R7.esc(n.created_by || '')}</small></td>
            <td>${d.all ? `<button class="btn btn-small btn-secondary" data-sn-del="${n.id}">Remove</button>` : ''}</td></tr>`).join('')}
            </tbody></table>` : '<p class="log-empty">No status notes.</p>';
    },

    async addNote() {
        const msg = document.getElementById('sn-msg');
        const r = await fetch('/api/status-notes', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: document.getElementById('sn-text').value,
                level: document.getElementById('sn-level').value,
                starts_at: document.getElementById('sn-start').value || null,
                ends_at: document.getElementById('sn-end').value || null,
            })
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { msg.textContent = d.error || `Failed (${r.status})`; return; }
        msg.textContent = 'Added.';
        ['sn-text', 'sn-start', 'sn-end'].forEach(id => { document.getElementById(id).value = ''; });
        this.load();
    },

    async deleteNote(id) {
        const r = await fetch(`/api/status-notes/${encodeURIComponent(id)}`, { method: 'DELETE' });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            document.getElementById('sn-msg').textContent = d.error || `Failed (${r.status})`;
        }
        this.load();
    },
};

Ticker.init();
