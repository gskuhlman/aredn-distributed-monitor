/**
 * Rollouts section on the Changes tab (rollout.py): a channel plan as an
 * ordered checklist (clients, then sectors, then backbones). Steps tick
 * themselves off after each scan when the radio reports its new channel, and
 * turn red when a moved radio's RF links haven't come back. Created from the
 * Channel Map planner ("Start rollout…") or from pasted what-if lines here.
 */
const Rollouts = {
    initialized: false,
    data: null,

    init() {
        if (this.initialized || !document.getElementById('ro-list')) return;
        this.initialized = true;
        document.getElementById('ro-refresh')?.addEventListener('click', () => this.load());
        document.getElementById('ro-check')?.addEventListener('click', () => this.check());
        document.getElementById('ro-create')?.addEventListener('click', () => this.createFromText());
        document.getElementById('ro-list').addEventListener('click', (event) => this.onClick(event));
        // Follow the scans while the Changes tab is open.
        setInterval(() => {
            if (document.getElementById('changes-view')?.classList.contains('active')) this.load();
        }, 60000);
    },

    async load() {
        this.init();
        try {
            this.data = await (await fetch('/api/rollouts')).json();
            this.render();
        } catch (error) {
            this.setStatus('Failed to load rollouts.');
        }
    },

    async check() {
        this.setStatus('Checking against the last scan…');
        const r = await fetch('/api/rollouts/check', { method: 'POST' });
        if (r.ok) await this.load();
        this.setStatus(r.ok ? 'Checked.' : 'Check failed.');
    },

    /** From the planner: radios = plan.radios (changed ones). */
    async create(radios, name, note) {
        const r = await fetch('/api/rollouts', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ radios, name, note })
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'could not create the rollout');
        return d;
    },

    async createFromText() {
        const text = document.getElementById('ro-text')?.value || '';
        const name = document.getElementById('ro-name')?.value || '';
        if (!text.trim()) return this.setStatus('Paste "name freq width" or "name off" lines first.');
        try {
            const r = await fetch('/api/rollouts', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ plan: text, name: name || undefined })
            });
            const d = await r.json();
            if (!r.ok) throw new Error(d.error || 'failed');
            document.getElementById('ro-text').value = '';
            this.setStatus(`Rollout #${d.id} created with ${d.steps.length} step(s).`);
            this.load();
        } catch (error) {
            this.setStatus(error.message);
        }
    },

    stepBadge(s) {
        const cls = { pending: 'info', changed: 'warning', verified: 'ok', problem: 'critical', skipped: 'info' };
        const label = { pending: 'to do', changed: 'changed, links coming back', verified: 'done', problem: 'problem', skipped: 'skipped' };
        return `<span class="diag-badge infra-badge-${cls[s.status] || 'info'}">${label[s.status] || s.status}</span>`;
    },

    chan(f, w, ch) {
        return f == null ? '?' : `${f}${w ? '/' + w : ''} MHz${ch ? ` (ch ${ch})` : ''}`;
    },

    render() {
        const el = document.getElementById('ro-list');
        const list = this.data.rollouts;
        if (!list.length) {
            el.innerHTML = '<p class="log-empty">No rollouts. Use "Start rollout…" on a channel plan, or paste what-if lines below.</p>';
            return;
        }
        el.innerHTML = list.map(ro => {
            const p = ro.progress;
            return `<details class="diag-details ro-rollout" ${ro.status === 'active' ? 'open' : ''} data-id="${ro.id}">
                <summary><strong>#${ro.id} ${this.esc(ro.name)}</strong> · ${this.esc(ro.status)} ·
                    ${p.done} of ${p.total} done${p.problem ? `, <strong class="ro-bad">${p.problem} problem(s)</strong>` : ''}
                    <span class="form-hint">created ${this.esc(ro.created_at)}${ro.finished_at ? ', finished ' + this.esc(ro.finished_at) : ''}</span></summary>
                <div class="ro-progress"><div style="width:${p.total ? Math.round(100 * p.done / p.total) : 0}%"></div></div>
                <table class="info-table diag-table"><thead><tr><th>#</th><th>Radio</th><th>Role</th><th>From</th><th>To</th><th>Status</th><th>Detail</th><th></th></tr></thead>
                <tbody>${ro.steps.map(s => `<tr class="${s.id === ro.next_step ? 'ro-next' : ''}">
                    <td>${s.seq}</td>
                    <td><a href="/nodes/${encodeURIComponent(s.node_name)}">${this.esc(s.node_name)}</a>${s.site ? ` <small>${this.esc(s.site)}</small>` : ''}</td>
                    <td>${this.esc(s.role)}</td>
                    <td>${this.chan(s.old_frequency, s.old_chanbw, s.old_channel)}</td>
                    <td>${s.turn_off ? 'radio off' : this.chan(s.new_frequency, s.new_chanbw, s.new_channel)}</td>
                    <td>${this.stepBadge(s)}${s.id === ro.next_step ? ' <small>next</small>' : ''}</td>
                    <td class="form-hint">${this.esc(s.detail || (s.peers.length ? 'peers: ' + s.peers.join(', ') : ''))}</td>
                    <td>${ro.status === 'active' ? (s.status === 'pending'
                        ? `<button class="btn btn-small btn-secondary" data-ro="skipped" data-step="${s.id}">Skip</button>
                           <button class="btn btn-small btn-secondary" data-ro="changed" data-step="${s.id}" title="The scanner can't read this radio: mark it changed by hand">Done by hand</button>`
                        : `<button class="btn btn-small btn-secondary" data-ro="pending" data-step="${s.id}">Reset</button>`) : ''}</td>
                </tr>`).join('')}</tbody></table>
                <div class="cm-whatif-actions">
                    ${ro.status === 'active' ? '<button class="btn btn-small btn-secondary" data-ro-action="cancelled">Cancel rollout</button>'
                        : '<button class="btn btn-small btn-secondary" data-ro-action="active">Reopen</button>'}
                    <button class="btn btn-small btn-secondary" data-ro-action="delete">Delete</button>
                    <button class="btn btn-small btn-secondary" data-ro-action="map" title="Load the plan into the Channel Map what-if">Show on map</button>
                </div></details>`;
        }).join('');
        this.setStatus(`${list.filter(r => r.status === 'active').length} active · links must be back within ${this.data.grace_minutes} min of a change`);
    },

    async onClick(event) {
        const step = event.target.closest('button[data-ro]');
        const act = event.target.closest('button[data-ro-action]');
        const box = event.target.closest('.ro-rollout');
        if (!box || (!step && !act)) return;
        const id = box.dataset.id;
        if (step) {
            await fetch(`/api/rollouts/${id}/steps/${step.dataset.step}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status: step.dataset.ro })
            });
        } else if (act.dataset.roAction === 'delete') {
            if (!window.confirm('Delete this rollout and its checklist?')) return;
            await fetch(`/api/rollouts/${id}`, { method: 'DELETE' });
        } else if (act.dataset.roAction === 'map') {
            const ro = this.data.rollouts.find(r => String(r.id) === id);
            document.querySelector('.tab-btn[data-tab="channel-map"]')?.click();
            setTimeout(() => {
                const ta = document.getElementById('cm-whatif-text');
                if (ta) {
                    ta.value = ro.plan_text || '';
                    document.getElementById('cm-whatif-box')?.setAttribute('open', '');
                    document.getElementById('cm-whatif-run')?.click();
                }
            }, 600);
            return;
        } else {
            await fetch(`/api/rollouts/${id}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status: act.dataset.roAction })
            });
        }
        this.load();
    },

    setStatus(text) {
        const el = document.getElementById('ro-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => Rollouts.init());
