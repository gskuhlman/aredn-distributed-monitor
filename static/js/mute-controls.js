/**
 * Silence / Ignore / Enable controls for one service or device on one node
 * (service_check.py service_mutes; POST/DELETE /api/services/mute). Used by
 * the node page Services table and the Services & Devices page. The buttons
 * show for everyone; without a login they open the login box first. Silence
 * asks how long inline (no browser dialog).
 *
 *   MuteControls.label(mute)              alerts state badge
 *   MuteControls.buttons(node, key, mute) buttons; key = service name or 'device:<ip>'
 *   MuteControls.bind(container, onDone)  wire clicks (once per container)
 */
const MuteControls = {
    admin: null,
    SILENCE: [[1, '1 hour'], [6, '6 hours'], [24, '1 day'], [168, '1 week'], [720, '1 month'], ['', 'Until enabled again']],

    esc(v) {
        return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    async loadAuth() {
        const s = typeof Auth !== 'undefined' ? await Auth.status() : null;
        this.admin = !s || !s.enabled || !!s.logged_in;
        return this.admin;
    },

    label(m) {
        if (!m) return '<span class="svc-alerts svc-alerts-on">on</span>';
        if (m.mode === 'ignore') return '<span class="svc-alerts svc-alerts-ignored">ignored</span>';
        return `<span class="svc-alerts svc-alerts-silenced">silenced</span> <small>${
            m.until ? `until ${this.esc(m.until.slice(0, 16))}` : 'until enabled again'}</small>`;
    },

    buttons(node, key, m) {
        const d = `data-mute-node="${this.esc(node)}" data-mute-key="${this.esc(key)}"`;
        if (m) return `<span class="svc-actions"><button class="btn btn-small btn-secondary" data-mute-act="enable" ${d}>Enable</button></span>`;
        return `<span class="svc-actions"><button class="btn btn-small btn-secondary" data-mute-act="silence" ${d}
                title="Keep checking but send no alerts for a while">Silence</button>
            <button class="btn btn-small btn-secondary" data-mute-act="ignore" ${d}
                title="Stop checking and never alert, until enabled again">Ignore</button></span>`;
    },

    bind(container, onDone) {
        if (!container || container.dataset.muteBound) return;
        container.dataset.muteBound = '1';
        container.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-mute-act]');
            if (btn && container.contains(btn)) {
                e.preventDefault();
                this.act(btn, onDone);
            }
        });
    },

    async act(btn, onDone) {
        const { muteAct: action, muteNode: node, muteKey: key } = btn.dataset;
        if (this.admin === null) await this.loadAuth();
        if (!this.admin) {
            if (typeof Auth === 'undefined' || !await Auth.prompt('Log in to silence or ignore')) return;
            await this.loadAuth();
        }
        if (action === 'enable') return this.send('DELETE', node, key, null, btn, onDone);
        if (action === 'ignore') return this.send('POST', node, key, { mode: 'ignore' }, btn, onDone);
        const box = btn.closest('.svc-actions') || btn.parentElement;
        box.innerHTML = `<span class="svc-silence">
            <select class="form-input svc-hours" aria-label="Silence for">
                ${this.SILENCE.map(([v, l]) => `<option value="${v}"${v === 24 ? ' selected' : ''}>${l}</option>`).join('')}
            </select>
            <button class="btn btn-small btn-primary" data-mute-ok>Silence</button>
            <button class="btn btn-small btn-secondary" data-mute-cancel>Cancel</button></span>`;
        box.querySelector('[data-mute-cancel]').addEventListener('click', (e) => {
            e.stopPropagation();
            box.outerHTML = this.buttons(node, key, null);
        });
        box.querySelector('[data-mute-ok]').addEventListener('click', (e) => {
            e.stopPropagation();
            const v = box.querySelector('.svc-hours').value;
            this.send('POST', node, key, { mode: 'silence', hours: v ? Number(v) : null }, box, onDone);
        });
    },

    async send(method, node, key, body, el, onDone) {
        const r = method === 'DELETE'
            ? await fetch(`/api/services/mute?node=${encodeURIComponent(node)}&service=${encodeURIComponent(key)}`,
                { method: 'DELETE' })
            : await fetch('/api/services/mute', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ node, service: key, ...body })
            });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
            const msg = document.createElement('small');
            msg.className = 'sc-down';
            msg.textContent = ` ${d.error || `Failed (${r.status})`}`;
            el.after(msg);
            return;
        }
        document.dispatchEvent(new CustomEvent('mutes-changed'));
        if (onDone) await onDone();
    }
};

document.addEventListener('auth-changed', () => { MuteControls.admin = null; });
