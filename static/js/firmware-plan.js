/**
 * Firmware Upgrades (Setup page; firmware_plan.py, TODO #58): plan which
 * nodes to upgrade in which order (far ends first, in waves of nodes that
 * don't depend on each other), then track it: steps tick themselves off when
 * the scanner reads the new version. Delete asks inline (no browser dialog).
 */
const FirmwarePlan = {
    plan: null,
    data: null,

    esc(v) { return R7.esc(v); },

    init() {
        if (!document.getElementById('fw-list')) return;
        document.getElementById('fw-preview').addEventListener('click', () => this.preview());
        document.getElementById('fw-start').addEventListener('click', () => this.start());
        document.getElementById('fw-refresh').addEventListener('click', () => this.load());
        document.getElementById('fw-list').addEventListener('click', (e) => this.onClick(e));
        R7.onShow('setup', 'firmware', () => this.load());
        setInterval(() => {
            if (document.getElementById('fw-list')?.offsetParent && this.data?.rollouts.some(r => r.status === 'active')) this.load();
        }, 60000);
    },

    params() {
        const p = new URLSearchParams();
        const t = document.getElementById('fw-target').value;
        const nodes = document.getElementById('fw-nodes').value.trim();
        if (t) p.set('target', t);
        if (nodes) p.set('nodes', nodes);
        if (document.getElementById('fw-infra').checked) p.set('infra', '1');
        return p;
    },

    setStatus(t) { document.getElementById('fw-status').textContent = t; },

    async preview() {
        this.setStatus('Working out the order…');
        try {
            this.plan = await R7.json(`/api/firmware/plan?${this.params()}`);
        } catch (e) {
            this.setStatus(e.message);
            return;
        }
        this.fillTargets(this.plan.versions, this.plan.target);
        this.renderPlan();
        this.setStatus('');
    },

    fillTargets(versions, chosen) {
        const sel = document.getElementById('fw-target');
        const keep = chosen || sel.value;
        sel.innerHTML = '<option value="">Newest release on the mesh</option>' + (versions || []).map(v =>
            `<option value="${this.esc(v.version)}">${this.esc(v.version)} (${v.count} node${v.count === 1 ? '' : 's'})${v.release ? '' : ' nightly'}</option>`).join('');
        if (keep && [...sel.options].some(o => o.value === keep)) sel.value = keep;
    },

    renderPlan() {
        const p = this.plan;
        const el = document.getElementById('fw-plan');
        document.getElementById('fw-start').disabled = !p.steps.length;
        if (!p.steps.length) {
            el.innerHTML = `<p class="log-empty">Nothing to upgrade: every chosen node runs ${this.esc(p.target)} or newer.</p>` + this.extras(p);
            return;
        }
        const waveNote = w => w === 1 ? 'nothing reaches the mesh through these: any order, at the same time'
            : 'after every wave before it';
        el.innerHTML = `<p>${p.steps.length} node(s) to <strong>${this.esc(p.target)}</strong>, in ${p.waves.length} wave(s)${
            p.unreached_wave ? ' plus nodes the last scan did not reach' : ''}. Each upgrade takes the node off the air for a few
            minutes; the nodes listed as "goes dark with it" lose the mesh at the same time, which is why they come first.</p>
            <table class="info-table diag-table"><thead><tr><th>#</th><th>Wave</th><th>Node</th><th>Now</th><th>Hops</th><th>Goes dark with it</th></tr></thead><tbody>
            ${p.steps.map((s, i) => {
                const first = i === 0 || p.steps[i - 1].wave !== s.wave;
                return (first ? `<tr class="fw-wave-row"><td colspan="6"><strong>${s.wave ? `Wave ${s.wave}` : 'Not reached by the last scan'}</strong>
                    <small class="form-hint">${s.wave ? waveNote(s.wave) : 'position unknown: do these last, or check them first'}</small></td></tr>` : '') +
                `<tr><td>${s.seq}</td><td>${s.wave || '–'}</td>
                    <td><a href="/nodes/${encodeURIComponent(s.node)}">${this.esc(s.node)}</a>${s.is_infrastructure ? ' <span class="diag-badge infra-badge-info">infra</span>' : ''}${
                        s.is_starting_node ? ' <span class="diag-badge infra-badge-warning">starting node: the monitor loses the mesh while it reboots</span>' : ''}</td>
                    <td>${this.esc(s.version || '?')}</td><td>${s.hops ?? '–'}</td>
                    <td class="form-hint">${s.behind_count ? `${s.behind_count}: ${this.esc(s.behind.join(', '))}${s.behind_count > s.behind.length ? '…' : ''}` : 'none'}</td></tr>`;
            }).join('')}</tbody></table>` + this.extras(p);
    },

    extras(p) {
        const parts = [];
        if (p.left_out.length) parts.push(`<p class="form-hint">Left out: ${p.left_out.map(l =>
            `${this.esc(l.node)}${l.version ? ` (${this.esc(l.version)})` : ''}: ${this.esc(l.reason)}`).join('; ')}. Name them under Nodes to include them.</p>`);
        if (p.already.length) parts.push(`<p class="form-hint">Already on ${this.esc(p.target)} or newer: ${p.already.length} node(s).</p>`);
        return parts.join('');
    },

    async start() {
        if (!this.plan?.steps.length) return;
        try {
            const body = { target: this.plan.target, infra: document.getElementById('fw-infra').checked,
                name: document.getElementById('fw-name').value.trim() || undefined,
                nodes: document.getElementById('fw-nodes').value.trim() || undefined };
            const r = await R7.json('/api/firmware/rollouts', { method: 'POST', body });
            this.setStatus(`Tracking #${r.id}: ${r.steps.length} node(s). Steps tick off after each scan.`);
            this.plan = null;
            document.getElementById('fw-plan').innerHTML = '';
            document.getElementById('fw-start').disabled = true;
            this.load();
        } catch (e) {
            this.setStatus(e.message);
        }
    },

    async load() {
        try {
            this.data = await R7.json('/api/firmware/rollouts');
            if (!this.plan) {
                const v = await R7.json('/api/firmware/plan').catch(() => null);
                if (v) this.fillTargets(v.versions);
            }
            this.render();
        } catch (e) {
            this.setStatus('Could not load firmware rollouts: ' + e.message);
        }
    },

    badge(s) {
        const cls = { pending: 'info', upgraded: 'ok', problem: 'critical', skipped: 'info' };
        const label = { pending: 'to do', upgraded: 'upgraded', problem: 'problem', skipped: 'skipped' };
        return `<span class="diag-badge infra-badge-${cls[s.status] || 'info'}">${label[s.status] || this.esc(s.status)}</span>`;
    },

    render() {
        const el = document.getElementById('fw-list');
        const list = this.data.rollouts;
        if (!list.length) {
            el.innerHTML = '<p class="log-empty">No firmware rollouts yet. Preview a plan above and press Track this plan.</p>';
            return;
        }
        el.innerHTML = list.map(ro => {
            const p = ro.progress;
            const next = new Set(ro.next_nodes || []);
            return `<details class="diag-details ro-rollout" ${ro.status === 'active' ? 'open' : ''} data-id="${ro.id}">
                <summary><strong>#${ro.id} ${this.esc(ro.name)}</strong> · ${this.esc(ro.status)} · ${p.done} of ${p.total} done${
                    p.problem ? `, <strong class="ro-bad">${p.problem} problem(s)</strong>` : ''}
                    <span class="form-hint">created ${this.esc(ro.created_at)}${ro.author ? ' by ' + this.esc(ro.author) : ''}${ro.finished_at ? ', finished ' + this.esc(ro.finished_at) : ''}</span></summary>
                <div class="ro-progress"><div style="width:${p.total ? Math.round(100 * p.done / p.total) : 0}%"></div></div>
                ${ro.status === 'active' && next.size ? `<p>Next: ${ro.next_wave ? `wave ${ro.next_wave}` : 'nodes not reached'}: <strong>${this.esc([...next].join(', '))}</strong></p>` : ''}
                <table class="info-table diag-table"><thead><tr><th>#</th><th>Wave</th><th>Node</th><th>Was</th><th>Now</th><th>Status</th><th>Detail</th><th></th></tr></thead>
                <tbody>${ro.steps.map(s => `<tr class="${next.has(s.node_name) && ro.status === 'active' ? 'ro-next' : ''}">
                    <td>${s.seq}</td><td>${s.wave || '–'}</td>
                    <td><a href="/nodes/${encodeURIComponent(s.node_name)}">${this.esc(s.node_name)}</a></td>
                    <td>${this.esc(s.old_version || '?')}</td><td>${this.esc(s.version_now || '?')}</td>
                    <td>${this.badge(s)}</td>
                    <td class="form-hint">${this.esc(s.detail || (s.behind.length ? 'do after: ' + s.behind.join(', ') : ''))}${s.upgraded_at ? ` <small>${this.esc(s.upgraded_at.slice(5, 16))}</small>` : ''}</td>
                    <td>${ro.status === 'active' ? (['pending', 'problem'].includes(s.status)
                        ? `<button class="btn btn-small btn-secondary" data-fw="skipped" data-step="${s.id}">Skip</button>
                           <button class="btn btn-small btn-secondary" data-fw="upgraded" data-step="${s.id}" title="The scanner can't read this node: mark it upgraded by hand">Done by hand</button>`
                        : `<button class="btn btn-small btn-secondary" data-fw="pending" data-step="${s.id}">Reset</button>`) : ''}</td>
                </tr>`).join('')}</tbody></table>
                <div class="cm-whatif-actions">
                    ${ro.status === 'active' ? '<button class="btn btn-small btn-secondary" data-fw-action="cancelled">Cancel rollout</button>'
                        : '<button class="btn btn-small btn-secondary" data-fw-action="active">Reopen</button>'}
                    <span class="fw-del"><button class="btn btn-small btn-secondary" data-fw-action="ask-delete">Delete</button></span>
                </div></details>`;
        }).join('');
    },

    async onClick(e) {
        const step = e.target.closest('button[data-fw]');
        const act = e.target.closest('button[data-fw-action]');
        const box = e.target.closest('.ro-rollout');
        if (!box || (!step && !act)) return;
        const id = box.dataset.id;
        try {
            if (step) {
                await R7.json(`/api/firmware/rollouts/${id}/steps/${step.dataset.step}`, { method: 'POST', body: { status: step.dataset.fw } });
            } else if (act.dataset.fwAction === 'ask-delete') {
                act.parentElement.innerHTML = `Delete this rollout and its checklist?
                    <button class="btn btn-small btn-danger" data-fw-action="delete">Delete</button>
                    <button class="btn btn-small btn-secondary" data-fw-action="keep">Keep</button>`;
                return;
            } else if (act.dataset.fwAction === 'keep') {
                return this.render();
            } else if (act.dataset.fwAction === 'delete') {
                await R7.json(`/api/firmware/rollouts/${id}`, { method: 'DELETE' });
            } else {
                await R7.json(`/api/firmware/rollouts/${id}`, { method: 'PATCH', body: { status: act.dataset.fwAction } });
            }
        } catch (err) {
            this.setStatus(err.message);
        }
        this.load();
    }
};

document.addEventListener('DOMContentLoaded', () => FirmwarePlan.init());
