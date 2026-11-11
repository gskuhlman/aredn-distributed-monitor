/**
 * PBX Status section on the VoIP & Phones page (pbx_status.py, TODO #79):
 * per Asterisk PBX, calls in progress and which phones are registered, read
 * over SSH every few minutes; the list of PBXs and the public key to install.
 * Loads with the VoIP tab and again after each check (`pbx_update`).
 */
const PBXStatus = {
    initialized: false,
    data: null,

    init() {
        if (this.initialized || !document.getElementById('pbx-report')) return;
        this.initialized = true;
        document.getElementById('pbx-check').addEventListener('click', () => this.checkNow());
        document.getElementById('pbx-save').addEventListener('click', () => this.save());
        if (typeof socket !== 'undefined' && socket) socket.on('pbx_update', () => this.load());
    },

    async load() {
        this.init();
        try {
            this.data = await (await fetch('/api/voip/pbx')).json();
            this.render();
        } catch (error) {
            document.getElementById('pbx-report').innerHTML = '<p class="log-empty">Failed to load.</p>';
        }
    },

    esc(value) {
        return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    duration(sec) {
        if (sec == null) return '';
        const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
        return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + `:${String(s).padStart(2, '0')}`;
    },

    nodeLink(name) {
        return name ? `<a href="/nodes/${encodeURIComponent(name)}">${this.esc(name)}</a>` : '';
    },

    render() {
        const d = this.data;
        if (!d) return;
        const ta = document.getElementById('pbx-hosts');
        if (document.activeElement !== ta) {
            ta.value = d.pbxes.map(p => [p.name, p.host, p.port, p.user].join(', ')).join('\n');
        }
        document.getElementById('pbx-key').innerHTML = d.public_key
            ? `<p class="form-hint">Add this public key to <code>~/.ssh/authorized_keys</code> of that user on each PBX:</p>
               <pre class="pbx-key">${this.esc(d.public_key)}</pre>`
            : '<p class="form-hint">No SSH key yet: create one on the Admin page (SSH).</p>';
        const report = document.getElementById('pbx-report');
        if (!d.pbxes.length) {
            report.innerHTML = '<p class="log-empty">No PBX to check yet: add one under "PBXs to check".</p>';
            document.getElementById('pbx-setup').open = true;
            return;
        }
        report.innerHTML = d.pbxes.map(p => this.card(p)).join('');
    },

    card(p) {
        const tile = (label, value, cls) => `<div class="np-tile${cls ? ' ' + cls : ''}"><div class="np-tile-label">${label}</div>
            <div class="np-tile-value">${value}</div></div>`;
        const head = `<h4>${this.esc(p.name)} <small class="form-hint">${this.esc(p.user)}@${this.esc(p.host)}:${p.port}
            ${p.version ? ' · ' + this.esc(p.version) : ''}</small></h4>`;
        if (!p.last_try) return `<div class="pbx-card">${head}<p class="form-hint">Not checked yet.</p></div>`;
        const err = p.error ? `<p class="pbx-error ${p.alarm ? 'sc-down' : ''}"><strong>${p.alarm ? 'Not answering' : 'Last check failed'}:</strong>
            ${this.esc(p.error)}${p.hint ? `<br><span class="form-hint">${this.esc(p.hint)}</span>` : ''}
            ${p.last_ok ? `<br><span class="form-hint">last good check ${this.esc(p.last_ok)}</span>` : ''}</p>` : '';
        if (!p.last_ok) return `<div class="pbx-card">${head}${err}</div>`;
        const eps = p.endpoints || [];
        const tiles = `<div class="np-tiles">${[
            tile('Calls now', p.active_calls ?? '-'),
            tile('Phones registered', `${p.registered} of ${eps.length}`),
            tile('Dropped off', p.unregistered_alarm, p.unregistered_alarm ? 'np-tile-warn' : ''),
            tile('Calls processed', p.calls_processed ?? '-'),
            tile('Checked', this.esc((p.last_ok || '').slice(5, 16))),
        ].join('')}</div>`;
        const ch = p.channels || [];
        const calls = ch.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Channel</th><th>Caller</th><th>Dialled</th><th>State</th><th>Doing</th><th>Time</th></tr></thead>
            <tbody>${ch.map(c => `<tr><td>${this.esc(c.channel)}</td><td>${this.esc(c.callerid)}</td><td>${this.esc(c.exten)}</td>
                <td>${this.esc(c.state)}</td><td>${this.esc(c.app)}${c.data ? ` <span class="form-hint">${this.esc(c.data)}</span>` : ''}</td>
                <td>${this.duration(c.duration)}</td></tr>`).join('')}</tbody></table></div>`
            : '<p class="form-hint">No calls in progress.</p>';
        const order = e => (e.alarm ? 0 : e.registered ? 2 : e.last_registered ? 1 : 3);
        const rows = [...eps].sort((a, b) => order(a) - order(b) || a.endpoint.localeCompare(b.endpoint, undefined, { numeric: true }));
        const state = e => e.alarm ? `<span class="sc-down">dropped off since ${this.esc((e.since || '').slice(5, 16))}</span>`
            : e.registered ? '<span class="sc-up">registered</span>'
            : e.last_registered ? `<span class="form-hint">not registered (${e.fail_count} check${e.fail_count === 1 ? '' : 's'})</span>`
            : '<span class="form-hint">never registered</span>';
        const phones = eps.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Phone</th><th>Status</th><th>Asterisk state</th><th>Address</th><th>Node</th><th>RTT</th><th>Last registered</th></tr></thead>
            <tbody>${rows.map(e => `<tr><td>${this.esc(e.endpoint)} <span class="form-hint">${this.esc(e.tech)}</span></td>
                <td>${state(e)}</td><td>${this.esc(e.state)}</td><td>${this.esc(e.contact_ip || e.last_ip || '')}</td>
                <td>${this.nodeLink(e.node_name)}</td><td>${e.rtt_ms != null ? e.rtt_ms.toFixed(0) + ' ms' : ''}</td>
                <td>${this.esc((e.last_registered || '').slice(5, 16))}</td></tr>`).join('')}</tbody></table></div>`
            : '<p class="form-hint">No PJSIP endpoints or SIP peers.</p>';
        return `<div class="pbx-card">${head}${err}${tiles}<h5>Calls in progress</h5>${calls}<h5>Phones</h5>${phones}</div>`;
    },

    setStatus(text) {
        document.getElementById('pbx-status').textContent = text || '';
    },

    async checkNow() {
        const btn = document.getElementById('pbx-check');
        btn.disabled = true;
        this.setStatus('Checking…');
        try {
            const resp = await fetch('/api/voip/pbx/check', { method: 'POST' });
            const d = await resp.json();
            await this.load();
            this.setStatus(resp.ok ? '' : (d.error || 'Failed'));
        } catch (error) {
            this.setStatus('Failed');
        } finally {
            btn.disabled = false;
        }
    },

    async save() {
        const msg = document.getElementById('pbx-save-msg');
        const pbxes = document.getElementById('pbx-hosts').value.split('\n').map(l => l.trim()).filter(Boolean)
            .map(l => { const [name, host, port, user] = l.split(',').map(x => x.trim()); return { name, host, port, user }; });
        const resp = await fetch('/api/voip/pbx/hosts', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pbxes }) });
        const d = await resp.json();
        msg.textContent = resp.ok ? `Saved ${d.pbxes.length} PBX${d.pbxes.length === 1 ? '' : 's'}.` : (d.error || 'Failed');
        if (resp.ok) {
            document.getElementById('pbx-hosts').blur();
            await this.load();
        }
    },
};

document.addEventListener('DOMContentLoaded', () => {
    PBXStatus.init();
    if (typeof VOIPModule !== 'undefined') {
        const original = VOIPModule.load.bind(VOIPModule);
        VOIPModule.load = function (...args) {
            PBXStatus.load();
            return original(...args);
        };
    }
});
