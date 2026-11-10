/**
 * On-the-air tape (tape.py): one slot per poll interval per node, heard /
 * degraded / missed / not polled, grouped by callsign with an "N of M on the
 * air" headline. Status page section (Nodes, Time and Search from the filter
 * bar) and `Tape.mountNode(el, name)` for the node page.
 */
const Tape = {
    row(n) {
        const title = `${n.heard_pct == null ? '' : Math.round(n.heard_pct) + '% heard'} · ${n.polls} polls · last ${n.last || '—'}`;
        return `<div class="tape-row" title="${R7.esc(title)}"><span class="tape-name">${R7.node(n.name)}</span>
            <span class="tape-bar">${[...n.tape].map(c => `<i class="ts-${c === '-' ? 'n' : c}"></i>`).join('')}</span>
            <span class="tape-pct">${n.heard_pct == null ? '' : Math.round(n.heard_pct) + '%'}</span></div>`;
    },

    legend(d) {
        return `<p class="form-hint">${d.slot_minutes}-minute slots, ${R7.esc(d.start.slice(5, 16))} → ${R7.esc(d.end.slice(11, 16))}:
            <i class="ts-h tape-key"></i> heard <i class="ts-d tape-key"></i> degraded (bare sysinfo) <i class="ts-m tape-key"></i> missed, blank = not polled</p>`;
    },

    init() {
        if (!document.getElementById('tape-report')) return;
        document.getElementById('tape-refresh').addEventListener('click', () => this.load());
        document.getElementById('tape-group').addEventListener('change', () => this.render());
        R7.onShow('status', null, () => this.load());
    },

    async load() {
        const gf = (typeof GlobalFilter !== 'undefined' && GlobalFilter.state) ? GlobalFilter : null;
        const p = new URLSearchParams({ hours: Math.min(R7.hours(), 168), scope: gf ? gf.scope() : 'all', search: gf?.state?.q || '' });
        try {
            this.data = await R7.json('/api/reports/tape?' + p);
            this.render();
        } catch (e) {
            document.getElementById('tape-report').innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`;
        }
    },

    render() {
        const d = this.data;
        if (!d) return;
        document.getElementById('tape-headline').textContent = d.headline.text;
        const byCall = document.getElementById('tape-group').checked;
        let html = this.legend(d);
        if (byCall) {
            d.groups.forEach(g => {
                html += `<div class="tape-group">${R7.esc(g.callsign)} <small class="form-hint">${g.on_air} of ${g.nodes} on the air</small></div>`;
                html += d.nodes.filter(n => n.callsign === g.callsign).map(n => this.row(n)).join('');
            });
        } else {
            html += d.nodes.map(n => this.row(n)).join('');
        }
        document.getElementById('tape-report').innerHTML = d.nodes.length ? html : '<p class="log-empty">No polled nodes in scope.</p>';
    },

    async mountNode(el, name, hours = 24) {
        if (!el) return;
        try {
            const d = await R7.json(`/api/reports/tape?hours=${hours}&node=${encodeURIComponent(name)}`);
            const n = d.nodes[0];
            el.innerHTML = n ? this.row(n) + this.legend(d) : '<p class="log-empty">Not polled in the last 24 hours.</p>';
        } catch (e) { el.innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`; }
    },
};

Tape.init();
