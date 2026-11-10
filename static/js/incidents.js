/**
 * Incidents page (menu Overview → Incidents, incidents.py): automatic
 * incidents (starting node lost, site outage with power / network cause,
 * nodes cut off behind a backbone link, several nodes down together) and
 * investigations opened by hand. List with status filter, a record per
 * incident (symptoms, suspected cause, current action, owner contacted,
 * resolution, status), timeline with notes, and linked changes / notes.
 */
const Incidents = {
    data: null,
    current: null,
    pendingId: null,   // set by links elsewhere (Status ticker) before the page opens

    init() {
        if (!document.getElementById('inc-list')) return;
        document.getElementById('inc-status-filter').addEventListener('change', () => this.load());
        document.getElementById('inc-refresh').addEventListener('click', () => this.load());
        document.getElementById('inc-new').addEventListener('click', () => this.create());
        R7.onShow('incidents', null, () => this.load());
    },

    async load() {
        const status = document.getElementById('inc-status-filter').value;
        const el = document.getElementById('inc-list');
        try {
            this.data = await R7.json(`/api/incidents?days=90${status ? '&status=' + encodeURIComponent(status) : ''}`);
            this.renderList();
            if (this.pendingId) { this.open(this.pendingId); this.pendingId = null; }
        } catch (e) {
            el.innerHTML = `<p class="log-empty">Could not load incidents: ${R7.esc(e.message)}</p>`;
        }
    },

    renderList() {
        const el = document.getElementById('inc-list');
        const c = this.data.counts || {};
        document.getElementById('inc-counts').textContent =
            `${c.open || 0} open · ${c.monitoring || 0} monitoring · ${c.resolved || 0} resolved · ${c.closed || 0} closed`;
        const rows = this.data.incidents || [];
        el.innerHTML = rows.length ? `<table class="info-table"><thead><tr><th>#</th><th>Status</th><th>Opened</th>
            <th>Incident</th><th>Kind</th><th>Nodes</th><th>Updated</th></tr></thead><tbody>${rows.map(i => `
            <tr class="clickable" data-id="${i.id}"><td>${i.id}</td><td>${R7.badge(i.status)}</td><td>${R7.hm(i.opened_at)}</td>
                <td><strong>${R7.esc(i.title)}</strong>${i.root_cause ? `<br><small>${R7.esc(i.root_cause)}</small>` : ''}</td>
                <td>${R7.esc(i.kind.replace('_', ' '))}${i.auto ? ' <small>(auto)</small>' : ''}</td>
                <td>${(i.nodes || []).length}</td><td>${R7.hm(i.updated_at)}</td></tr>`).join('')}</tbody></table>`
            : '<p class="log-empty">No incidents in this view. Automatic ones open when the starting node is lost, a whole site goes down, or several nodes drop together.</p>';
        el.querySelectorAll('tr[data-id]').forEach(tr => tr.addEventListener('click', () => this.open(Number(tr.dataset.id))));
    },

    async create() {
        const title = document.getElementById('inc-new-title').value.trim();
        const status = document.getElementById('inc-new-status');
        if (!title) { status.textContent = 'Give the incident a title.'; return; }
        try {
            const res = await R7.json('/api/incidents', { method: 'POST', body: {
                title, symptoms: document.getElementById('inc-new-symptoms').value,
                nodes: document.getElementById('inc-new-nodes').value.split(/[\s,]+/).filter(Boolean) } });
            document.getElementById('inc-new-title').value = '';
            document.getElementById('inc-new-symptoms').value = '';
            document.getElementById('inc-new-nodes').value = '';
            status.textContent = `Opened #${res.incident.id}.`;
            await this.load();
            this.open(res.incident.id);
        } catch (e) { status.textContent = e.message; }
    },

    FIELDS: [['symptoms', 'Symptoms'], ['suspected_cause', 'Suspected cause'], ['current_action', 'Current action'],
        ['owner_contacted', 'Owner contacted'], ['resolution', 'Resolution'], ['owner', 'Handled by']],

    async open(id) {
        const el = document.getElementById('inc-detail');
        try {
            const i = await R7.json(`/api/incidents/${id}`);
            this.current = i;
            el.innerHTML = `<div class="diag-section"><h3>#${i.id} ${R7.esc(i.title)} ${R7.badge(i.status)}</h3>
                <p class="form-hint">${R7.esc(i.kind.replace('_', ' '))}${i.auto ? ' (automatic)' : ''} · opened ${R7.esc(i.opened_at)}
                    ${i.resolved_at ? ' · resolved ' + R7.esc(i.resolved_at) : ''}${i.site ? ' · site ' + R7.esc(i.site) : ''}
                    ${i.link_a ? ` · link ${R7.esc(i.link_a)} ↔ ${R7.esc(i.link_b)}` : ''}</p>
                ${i.root_cause ? `<p><strong>Root cause:</strong> ${R7.esc(i.root_cause)}</p>` : ''}
                ${(i.nodes || []).length ? `<p><strong>Nodes:</strong> ${i.nodes.map(R7.node).join(', ')}</p>` : ''}
                <div class="mh-controls"><label>Status <select id="inc-ed-status" class="form-input">${['open', 'monitoring', 'resolved', 'closed']
                    .map(s => `<option ${s === i.status ? 'selected' : ''}>${s}</option>`).join('')}</select></label></div>
                <div class="inc-fields">${this.FIELDS.map(([k, label]) => `<label>${label}
                    <textarea class="form-input" rows="2" data-field="${k}">${R7.esc(i[k] || '')}</textarea></label>`).join('')}</div>
                <div class="mh-controls"><button id="inc-save" class="btn btn-primary">Save changes</button>
                    <button id="inc-delete" class="btn btn-secondary btn-small">Delete</button><span id="inc-ed-msg" class="form-hint"></span></div>
                <h4>Timeline</h4>
                <ul class="inc-timeline">${(i.timeline || []).map(t => `<li><time>${R7.hm(t.timestamp)}</time>${R7.esc(t.text)}
                    ${t.author ? `<small> — ${R7.esc(t.author)}</small>` : ''}</li>`).join('')}</ul>
                <div class="mh-controls"><input id="inc-note-text" class="form-input" placeholder="Add a note to the timeline" style="flex:1">
                    <button id="inc-note-add" class="btn btn-secondary btn-small">Add note</button></div>
                <h4>Linked changes and notes</h4>
                ${(i.links || []).length ? `<ul>${i.links.map(l => `<li>${R7.esc(l.ref_type)} #${l.ref_id}: ${R7.esc(l.timestamp || '')}
                    ${R7.esc(l.node_name || '')} ${R7.esc(l.description || '')}
                    <button class="btn btn-secondary btn-small" data-unlink="${R7.esc(l.ref_type)}:${l.ref_id}">Unlink</button></li>`).join('')}</ul>`
                    : '<p class="form-hint">None linked.</p>'}
                <div class="mh-controls"><select id="inc-link-type" class="form-input"><option value="change">Change #</option><option value="note">Note #</option></select>
                    <input id="inc-link-id" type="number" class="form-input lp-num" placeholder="id">
                    <button id="inc-link-add" class="btn btn-secondary btn-small">Link</button></div></div>`;
            document.getElementById('inc-save').addEventListener('click', () => this.save());
            document.getElementById('inc-delete').addEventListener('click', () => this.remove());
            document.getElementById('inc-note-add').addEventListener('click', () => this.note());
            document.getElementById('inc-link-add').addEventListener('click', () => this.link(
                document.getElementById('inc-link-type').value, Number(document.getElementById('inc-link-id').value)));
            el.querySelectorAll('[data-unlink]').forEach(b => b.addEventListener('click', () => {
                const [t, rid] = b.dataset.unlink.split(':');
                this.link(t, Number(rid), true);
            }));
            el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } catch (e) {
            el.innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`;
        }
    },

    async save() {
        const body = { status: document.getElementById('inc-ed-status').value };
        document.querySelectorAll('#inc-detail [data-field]').forEach(t => { body[t.dataset.field] = t.value; });
        try {
            await R7.json(`/api/incidents/${this.current.id}`, { method: 'PATCH', body });
            await this.load();
            this.open(this.current.id);
        } catch (e) { document.getElementById('inc-ed-msg').textContent = e.message; }
    },

    async note() {
        const text = document.getElementById('inc-note-text').value.trim();
        if (!text) return;
        try {
            await R7.json(`/api/incidents/${this.current.id}/notes`, { method: 'POST', body: { text } });
            this.open(this.current.id);
        } catch (e) { document.getElementById('inc-ed-msg').textContent = e.message; }
    },

    async link(refType, refId, remove = false) {
        if (!refId) return;
        try {
            await R7.json(`/api/incidents/${this.current.id}/links`, { method: 'POST', body: { ref_type: refType, ref_id: refId, remove } });
            this.open(this.current.id);
        } catch (e) { document.getElementById('inc-ed-msg').textContent = e.message; }
    },

    async remove() {
        if (!confirm(`Delete incident #${this.current.id} and its timeline? This cannot be undone.`)) return;
        try {
            await R7.json(`/api/incidents/${this.current.id}`, { method: 'DELETE' });
            this.current = null;
            document.getElementById('inc-detail').innerHTML = '';
            this.load();
        } catch (e) { document.getElementById('inc-ed-msg').textContent = e.message; }
    },
};

Incidents.init();
