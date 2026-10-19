/**
 * Cleanup section on the Diagnostics tab: junk, stale and known-unreachable
 * nodes from /api/cleanup/candidates, grouped by reason, with bulk Exclude /
 * Un-exclude / Delete. All rules (and the protections for infrastructure,
 * tagged, selected and starting nodes) live in cleanup.py.
 */
const NodeCleanup = {
    data: null,

    init() {
        document.getElementById('cleanup-run')?.addEventListener('click', () => this.load());
        document.getElementById('cleanup-exclude')?.addEventListener('click', () => this.act('exclude'));
        document.getElementById('cleanup-unexclude')?.addEventListener('click', () => this.act('unexclude'));
        document.getElementById('cleanup-delete')?.addEventListener('click', () => this.act('delete'));
        const el = document.getElementById('cleanup-list');
        el?.addEventListener('change', (event) => {
            const group = event.target.dataset.groupAll;
            if (group) {
                el.querySelectorAll(`input[data-group="${CSS.escape(group)}"]:not(:disabled)`)
                    .forEach(box => { box.checked = event.target.checked; });
            }
            this.updateButtons();
        });
    },

    async load() {
        const days = parseInt(document.getElementById('cleanup-stale-days')?.value, 10) || 30;
        const button = document.getElementById('cleanup-run');
        if (button) button.disabled = true;
        this.setStatus('Looking for candidates...');
        try {
            const resp = await fetch(`/api/cleanup/candidates?stale_days=${days}`);
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Request failed');
            this.data = data;
            this.render();
            const total = data.groups.reduce((n, g) => n + g.nodes.length, 0);
            this.setStatus(`${total} candidate(s) at ${data.generated_at}` +
                (data.starting_node ? ` · starting node ${data.starting_node}` : ''));
        } catch (error) {
            console.error('Cleanup error:', error);
            this.setStatus('Failed to load cleanup candidates.');
        } finally {
            if (button) button.disabled = false;
        }
    },

    render() {
        const el = document.getElementById('cleanup-list');
        const groups = this.data.groups;
        if (!groups.length) {
            el.innerHTML = '<p class="log-empty">Nothing to clean up.</p>';
            this.updateButtons();
            return;
        }
        el.innerHTML = groups.map(g => this.renderGroup(g)).join('');
        this.updateButtons();
    },

    renderGroup(g) {
        const actions = { delete: 'Delete', exclude: 'Exclude', unexclude: 'Un-exclude', review: 'Review', keep: 'Keep' };
        const rows = g.nodes.map(n => {
            const locked = n.protected.length > 0;
            const facts = [
                n.in_nodes ? (n.is_active ? 'active' : 'inactive') : 'no node record',
                `${n.link_count} link(s)`,
                n.health_rows ? `${n.health_rows} poll row(s)` : null,
                n.scan_boundary ? `boundary: ${n.scan_boundary}` : null
            ].filter(Boolean).join(' · ');
            const notes = [
                ...n.evidence,
                ...(locked ? [`Protected (${n.protected.join(', ')}): never deleted or excluded`] : []),
                ...(n.cuts_off.length ? [`Excluding it would cut off ${n.cuts_off.length} healthy node(s): ${n.cuts_off.join(', ')}`] : [])
            ];
            return `<tr class="${n.suggested_action === 'review' ? 'rel-row rel-row-adjacent' : ''}">
                <td><input type="checkbox" data-name="${this.esc(n.name)}" data-group="${this.esc(g.reason)}"
                    data-action="${this.esc(n.suggested_action)}" ${locked ? 'disabled' : ''}
                    aria-label="Select ${this.esc(n.name)}"></td>
                <td>${n.in_nodes ? `<a href="/nodes/${encodeURIComponent(n.name)}" target="_blank">${this.esc(n.name)}</a>` : this.esc(n.name)}</td>
                <td><span class="diag-badge cleanup-act-${this.esc(n.suggested_action)}">${actions[n.suggested_action] || this.esc(n.suggested_action)}</span></td>
                <td>${this.esc(n.last_answered || 'never')}</td>
                <td>${this.esc(n.reached_through.join(', ') || '-')}</td>
                <td><small>${this.esc(facts)}</small></td>
                <td class="cleanup-notes">${notes.map(x => this.esc(x)).join('<br>')}</td>
            </tr>`;
        }).join('');
        return `<details class="diag-details" open>
            <summary>${this.esc(g.label)} (${g.nodes.length}) <small class="form-hint">${this.esc(g.detail)}</small></summary>
            <div class="table-scroll-wrapper"><table class="info-table diag-table cleanup-table">
                <thead><tr><th><input type="checkbox" data-group-all="${this.esc(g.reason)}" aria-label="Select all ${this.esc(g.label)}"></th>
                    <th>Node</th><th>Suggested</th><th>Last answered</th><th>Reached through</th><th>State</th><th>Why</th></tr></thead>
                <tbody>${rows}</tbody></table></div>
        </details>`;
    },

    selected() {
        return [...document.querySelectorAll('#cleanup-list input[data-name]:checked')].map(box => box.dataset.name);
    },

    updateButtons() {
        const n = this.selected().length;
        for (const id of ['cleanup-exclude', 'cleanup-unexclude', 'cleanup-delete']) {
            const button = document.getElementById(id);
            if (button) button.disabled = n === 0;
        }
        const count = document.getElementById('cleanup-count');
        if (count) count.textContent = n ? `${n} selected` : '';
    },

    async act(action) {
        const names = this.selected();
        if (!names.length) return;
        const verbs = {
            exclude: `Exclude ${names.length} node(s) from scanning? They will not be polled until un-excluded.`,
            unexclude: `Un-exclude ${names.length} node(s)? They will be polled again on the next scan.`,
            delete: `Permanently delete ${names.length} node record(s) with their links, link history and poll history? This cannot be undone.`
        };
        if (!window.confirm(verbs[action])) return;
        try {
            const resp = await fetch(`/api/cleanup/${action}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ names })
            });
            const result = await resp.json();
            if (!resp.ok) throw new Error(result.error || 'Request failed');
            const done = result.excluded || result.unexcluded || result.deleted || [];
            const refused = Object.entries(result.refused || {});
            const label = { exclude: 'Excluded', unexclude: 'Un-excluded', delete: 'Deleted' }[action];
            this.notify(refused.length ? 'warning' : 'success', `${label} ${done.length} node(s)`,
                refused.length ? `Refused: ${refused.map(([n, why]) => `${n} (${why})`).join('; ')}` :
                    (action === 'delete' ? 'Records removed' : 'Takes effect on the next scan'));
            await this.load();
        } catch (error) {
            this.notify('error', 'Cleanup Failed', error.message);
        }
    },

    notify(type, title, message) {
        // showToast renders HTML; node names come from the mesh.
        if (typeof showToast === 'function') showToast(type, this.esc(title), this.esc(message));
        else this.setStatus(`${title}: ${message}`);
    },

    setStatus(text) {
        const el = document.getElementById('cleanup-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => NodeCleanup.init());
