/**
 * Operator notes on nodes, links and sites (/api/notes, notes.py), as one
 * widget mounted wherever people troubleshoot: node page, Nodes detail
 * panel, network side panel, RF Stats link diagnosis and Channel Map radio
 * panel. Also keeps a cache of pinned notes for table indicators, and sets
 * chart.$noteMarkers (drawn by the changeMarkers plugin in changes.js) for
 * notes that carry an event time. Note text is free text: always escaped.
 */
const Notes = {
    pinned: [],
    mounts: new Map(),
    seq: 0,

    // ------------------------------------------------------------ data --

    query(opts) {
        const q = new URLSearchParams();
        if (opts.node) q.set('node', opts.node);
        if (opts.peer) q.set('peer', opts.peer);
        if (opts.site) q.set('site', opts.site);
        q.set('limit', opts.limit || 200);
        return fetch(`/api/notes?${q}`).then(r => r.json()).then(d => d.notes || []);
    },

    async loadPinned() {
        try {
            const data = await fetch('/api/notes?pinned=1&limit=2000').then(r => r.json());
            this.pinned = data.notes || [];
        } catch (e) {
            this.pinned = [];
        }
        document.dispatchEvent(new CustomEvent('notes:pinned'));
        return this.pinned;
    },

    /** Pinned node and link notes touching a node (site notes need the site map). */
    pinnedFor(name) {
        return this.pinned.filter(n => n.node_name === name || n.peer_node === name);
    },

    /** Small pinned-note icon for tables; empty when the node has none. */
    indicator(name) {
        const list = this.pinnedFor(name);
        if (!list.length) return '';
        const title = list.map(n => this.oneLine(n)).join('\n');
        return ` <span class="note-flag" title="${this.esc(title)}">&#128204;${list.length > 1 ? list.length : ''}</span>`;
    },

    oneLine(n) {
        return `${this.targetText(n)}: ${String(n.text).replace(/\s+/g, ' ').slice(0, 140)}`;
    },

    targetText(n) {
        if (n.target_type === 'link') return `link ${n.node_name} ↔ ${n.peer_node}`;
        if (n.target_type === 'site') return `site ${n.site}`;
        return n.node_name;
    },

    // ----------------------------------------------------------- mount --

    /**
     * Render notes into el. opts: {node, peer?, site?, compact?, title?,
     * peers? (datalist of link peers)}. Compact shows pinned + latest few.
     */
    mount(el, opts) {
        if (!el) return;
        const id = el.dataset.notesId || String(++this.seq);
        el.dataset.notesId = id;
        this.mounts.set(id, { el, opts: { ...opts }, showAll: false, editing: null, notes: [] });
        if (!el.dataset.notesBound) {
            el.dataset.notesBound = '1';
            el.addEventListener('click', e => this.onClick(id, e));
            el.addEventListener('change', e => {
                if (e.target.matches('[data-note-field="target"]')) this.syncForm(id);
            });
        }
        this.refresh(id);
    },

    /** Append a titled section to a container and mount notes in it. */
    appendSection(container, opts, cls = 'node-detail-section') {
        if (!container) return null;
        const box = document.createElement('div');
        box.className = `${cls} notes-section`;
        container.appendChild(box);
        this.mount(box, opts);
        return box;
    },

    async refresh(id) {
        const m = this.mounts.get(id);
        if (!m || !m.el.isConnected) {
            this.mounts.delete(id);
            return;
        }
        try {
            m.notes = await this.query(m.opts);
        } catch (e) {
            m.notes = [];
            m.error = true;
        }
        this.render(id);
    },

    refreshAll() {
        for (const id of [...this.mounts.keys()]) this.refresh(id);
        this.loadPinned();
        document.dispatchEvent(new CustomEvent('notes:changed'));
    },

    render(id) {
        const m = this.mounts.get(id);
        if (!m) return;
        const { el, opts } = m;
        const title = opts.title || 'Notes';
        let list = m.notes;
        let more = '';
        if (opts.compact && !m.showAll) {
            const pinned = list.filter(n => n.pinned);
            const rest = list.filter(n => !n.pinned).slice(0, 3);
            if (pinned.length + rest.length < list.length) {
                more = `<button class="btn btn-small btn-secondary" data-note-action="all">Show all ${list.length}</button>`;
            }
            list = [...pinned, ...rest];
        }
        const heading = opts.compact ? 'h4' : 'h3';
        el.innerHTML = `
            <${heading} class="notes-title">${this.esc(title)}${m.notes.length ? ` (${m.notes.length})` : ''}
                <button class="btn btn-small btn-primary" data-note-action="new">Add note</button></${heading}>
            <div class="notes-form-slot"></div>
            ${m.error ? '<p class="log-empty">Notes failed to load.</p>' : ''}
            ${list.length ? `<ul class="notes-list">${list.map(n => this.renderItem(n, m)).join('')}</ul>`
                : (m.error ? '' : '<p class="form-hint">No notes yet.</p>')}
            ${more}`;
        if (m.formOpen) this.openForm(id);
    },

    renderItem(n, m) {
        if (m.editing === n.id) {
            return `<li class="note-item note-editing" data-note-id="${n.id}">
                <textarea class="form-input note-edit-text" rows="3">${this.esc(n.text)}</textarea>
                <div class="note-edit-row">
                    <label>When <input type="datetime-local" class="form-input note-edit-time" step="60"
                        value="${this.esc((n.event_time || '').slice(0, 16).replace(' ', 'T'))}"></label>
                    <button class="btn btn-small btn-primary" data-note-action="save-edit" data-note-id="${n.id}">Save</button>
                    <button class="btn btn-small btn-secondary" data-note-action="cancel-edit">Cancel</button>
                </div></li>`;
        }
        const ctx = m.opts;
        // Show what the note is attached to unless it's the thing being viewed.
        const own = n.target_type === 'node' && n.node_name === ctx.node && !ctx.peer;
        const target = own ? '' : `<span class="note-target note-target-${this.esc(n.target_type)}">${this.esc(this.targetText(n))}</span>`;
        const when = n.event_time ? `<span class="note-when" title="When it happened">${this.esc(n.event_time.slice(0, 16))}</span>` : '';
        const meta = `added ${this.esc((n.created_at || '').slice(0, 16))}${n.author ? ' by ' + this.esc(n.author) : ''}` +
            (n.updated_at && n.updated_at !== n.created_at ? ` · edited ${this.esc(n.updated_at.slice(0, 16))}` : '');
        return `<li class="note-item${n.pinned ? ' note-pinned' : ''}" data-note-id="${n.id}">
            <div class="note-head">${n.pinned ? '<span class="note-pin" title="Pinned">&#128204;</span>' : ''}${target}${when}</div>
            <div class="note-text">${this.esc(n.text)}</div>
            <div class="note-meta">${meta}
                <span class="note-actions">
                    <button class="btn-link" data-note-action="pin" data-note-id="${n.id}">${n.pinned ? 'Unpin' : 'Pin'}</button>
                    <button class="btn-link" data-note-action="edit" data-note-id="${n.id}">Edit</button>
                    <button class="btn-link note-del" data-note-action="delete" data-note-id="${n.id}">Delete</button>
                </span></div>
        </li>`;
    },

    renderForm(m) {
        const o = m.opts;
        const opt = (v, label, sel) => `<option value="${v}" ${sel ? 'selected' : ''}>${this.esc(label)}</option>`;
        const targets = o.peer
            ? opt('link', `Link ${o.node} ↔ ${o.peer}`, true) + opt('node', `Node ${o.node}`) +
              opt('node-peer', `Node ${o.peer}`) + opt('site', `Site of ${o.node}`)
            : (o.node ? opt('node', `This node (${o.node})`, true) + opt('link', 'A link of this node') +
               opt('site', `Site of ${o.node}`) : '') + (o.site ? opt('site', `Site ${o.site}`, !o.node) : '');
        const peers = (o.peers || []).map(p => `<option value="${this.esc(p)}">`).join('');
        const listId = `note-peers-${this.esc(m.el.dataset.notesId)}`;
        return `<div class="note-form">
            <div class="note-form-row">
                <label>On <select class="form-input" data-note-field="target">${targets}</select></label>
                <label class="note-peer-field" hidden>Peer <input type="text" class="form-input" data-note-field="peer"
                    list="${listId}" placeholder="other end"></label>
                <datalist id="${listId}">${peers}</datalist>
                <label>When (optional) <input type="datetime-local" class="form-input" data-note-field="time" step="60"
                    title="When the thing in the note happened; shown as a marker on charts"></label>
                <label class="header-checkbox"><input type="checkbox" data-note-field="pinned"> Pin</label>
            </div>
            <textarea class="form-input" data-note-field="text" rows="2" maxlength="2000"
                placeholder="e.g. tilted 2° down on 10/20 · call KF9MT before touching this"></textarea>
            <div class="note-form-row">
                <button class="btn btn-small btn-primary" data-note-action="save">Save note</button>
                <button class="btn btn-small btn-secondary" data-note-action="cancel">Cancel</button>
            </div></div>`;
    },

    openForm(id) {
        const m = this.mounts.get(id);
        const slot = m?.el.querySelector('.notes-form-slot');
        if (!slot) return;
        m.formOpen = true;
        if (!slot.firstChild) slot.innerHTML = this.renderForm(m);
        this.syncForm(id);
    },

    syncForm(id) {
        const m = this.mounts.get(id);
        const target = m?.el.querySelector('[data-note-field="target"]')?.value;
        const peerField = m?.el.querySelector('.note-peer-field');
        if (peerField) peerField.hidden = !(target === 'link' && !m.opts.peer);
    },

    async onClick(id, e) {
        const btn = e.target.closest('[data-note-action]');
        const m = this.mounts.get(id);
        if (!btn || !m) return;
        const action = btn.dataset.noteAction;
        const noteId = Number(btn.dataset.noteId);
        const note = m.notes.find(n => n.id === noteId);
        if (action === 'new') {
            if (m.formOpen) {
                m.formOpen = false;
                m.el.querySelector('.notes-form-slot').innerHTML = '';
            } else {
                this.openForm(id);
                m.el.querySelector('[data-note-field="text"]')?.focus();
            }
        } else if (action === 'cancel') {
            m.formOpen = false;
            m.el.querySelector('.notes-form-slot').innerHTML = '';
        } else if (action === 'all') {
            m.showAll = true;
            this.render(id);
        } else if (action === 'save') {
            await this.save(id, btn);
        } else if (action === 'pin' && note) {
            await this.send(`/api/notes/${note.id}`, 'PATCH', { pinned: !note.pinned });
        } else if (action === 'edit' && note) {
            m.editing = note.id;
            this.render(id);
        } else if (action === 'cancel-edit') {
            m.editing = null;
            this.render(id);
        } else if (action === 'save-edit' && note) {
            const li = btn.closest('.note-item');
            const text = li.querySelector('.note-edit-text').value.trim();
            if (!text) return this.toast('error', 'Note', 'Enter some text');
            const time = li.querySelector('.note-edit-time').value;
            m.editing = null;
            await this.send(`/api/notes/${note.id}`, 'PATCH', { text, event_time: time ? time.replace('T', ' ') : null });
        } else if (action === 'delete' && note) {
            if (!window.confirm(`Delete this note?\n\n${this.oneLine(note)}`)) return;
            await this.send(`/api/notes/${note.id}`, 'DELETE');
        }
    },

    async save(id, btn) {
        const m = this.mounts.get(id);
        const field = name => m.el.querySelector(`[data-note-field="${name}"]`);
        const text = (field('text')?.value || '').trim();
        if (!text) return this.toast('error', 'Note', 'Enter some text');
        const target = field('target')?.value || 'node';
        const o = m.opts;
        const body = { text, pinned: field('pinned')?.checked || false };
        const time = field('time')?.value;
        if (time) body.event_time = time.replace('T', ' ');
        if (target === 'link') {
            body.target_type = 'link';
            body.node_name = o.node;
            body.peer_node = o.peer || (field('peer')?.value || '').trim();
            if (!body.peer_node) return this.toast('error', 'Note', 'Enter the other end of the link');
        } else if (target === 'node-peer') {
            Object.assign(body, { target_type: 'node', node_name: o.peer });
        } else if (target === 'site') {
            Object.assign(body, { target_type: 'site', node_name: o.node || null, site: o.node ? null : o.site });
        } else {
            Object.assign(body, { target_type: 'node', node_name: o.node });
        }
        btn.disabled = true;
        const ok = await this.send('/api/notes', 'POST', body);
        btn.disabled = false;
        if (ok) {
            m.formOpen = false;
            this.toast('success', 'Note saved', this.oneLine(ok.note || { ...body, target_type: target }));
        }
    },

    async send(url, method, body) {
        try {
            const resp = await fetch(url, {
                method, headers: { 'Content-Type': 'application/json' },
                body: body ? JSON.stringify(body) : undefined
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'Request failed');
            this.refreshAll();
            return data;
        } catch (error) {
            this.toast('error', 'Note', error.message);
            return null;
        }
    },

    // ---------------------------------------------------------- charts --

    /** Notes with an event time on a node (or a pair) as chart markers. */
    async markCharts(charts, node, peer) {
        try {
            const list = (await this.query({ node, peer, limit: 500 })).filter(n => n.event_time);
            const markers = list.map(n => ({
                t: new Date(n.event_time.replace(' ', 'T')).getTime(),
                label: `note: ${String(n.text).replace(/\s+/g, ' ').slice(0, 40)}`
            }));
            (charts || []).forEach(ch => {
                if (!ch) return;
                ch.$noteMarkers = markers;
                ch.update('none');
            });
        } catch (e) { /* markers are optional */ }
    },

    // ----------------------------------------------------------- utils --

    toast(type, title, msg) {
        if (typeof NodePage !== 'undefined' && NodePage.showToast) return NodePage.showToast(type, title, msg);
        if (typeof showToast === 'function') return showToast(type, title, msg);
        console.log(title, msg);
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', async () => {
    Notes.loadPinned();
    // Node page: the full notes section, with the node's link peers offered.
    const box = document.getElementById('node-notes');
    if (!box || !window.NODE_NAME) return;
    const node = window.NODE_NAME;
    let peers = [];
    try {
        const data = await fetch(`/api/nodes/detail/${encodeURIComponent(node)}`).then(r => r.json());
        peers = [...new Set((data.links || []).map(l => (l.source_node === node ? l.target_node : l.source_node)))].sort();
    } catch (e) { /* peers are a convenience */ }
    Notes.mount(box, { node, peers, title: 'Notes' });
});
