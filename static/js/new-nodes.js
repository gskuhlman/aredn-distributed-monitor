/**
 * New Nodes section on the Diagnostics tab (welcome.py): nodes discovered
 * recently, watched for a week, with their setup checklist, how well they
 * answer, their links and a suggested channel. `NewNodes.banner(node, el)`
 * shows the same on the node page while the node is watched.
 */
const NewNodes = {
    initialized: false,

    init() {
        if (this.initialized || !document.getElementById('nn-report')) return;
        this.initialized = true;
        document.getElementById('nn-refresh')?.addEventListener('click', () => this.load());
        document.getElementById('nn-all')?.addEventListener('change', () => this.load());
        document.getElementById('nn-report').addEventListener('click', (event) => this.onClick(event));
    },

    async load() {
        this.init();
        const el = document.getElementById('nn-report');
        const all = document.getElementById('nn-all')?.checked;
        try {
            const d = await (await fetch('/api/welcome' + (all ? '?all=1' : ''))).json();
            el.innerHTML = d.welcomes.length ? d.welcomes.map(w => this.card(w)).join('')
                : `<p class="log-empty">No new nodes being watched. Nodes the scanner discovers are welcomed and watched for ${d.watch_days} days.</p>`;
            this.setStatus(`${d.welcomes.length} node(s)`);
        } catch (error) {
            this.setStatus('Failed to load.');
        }
    },

    card(w) {
        const links = (w.links || []).filter(l => l.status !== 'removed');
        return `<div class="nn-card" data-node="${this.esc(w.node_name)}">
            <div class="nn-head"><a href="/nodes/${encodeURIComponent(w.node_name)}"><strong>${this.esc(w.node_name)}</strong></a>
                <span class="form-hint">discovered ${this.esc(w.discovered_at)} · ${w.watching ? `watched until ${this.esc(w.watch_until.slice(0, 16))}` : 'no longer watched'}</span>
                ${w.score != null ? `<span class="diag-badge infra-badge-${Hygiene.scoreClass(w.score)}">setup ${w.score}/100</span>` : ''}
                ${w.answered_pct != null ? `<span class="form-hint">answered ${w.answered_pct}% of ${w.polls} polls</span>` : ''}</div>
            ${w.fixes?.length ? `<ul class="hy-fixes">${w.fixes.map(f => `<li>${this.esc(f)}</li>`).join('')}</ul>` : ''}
            <div class="form-hint">${links.length ? 'Links: ' + links.map(l => `${this.esc(l.peer)} (${this.esc(l.link_type)}${l.quality != null ? ` ${Math.round(l.quality)}%` : ''})`).join(', ') : 'No links yet.'}</div>
            <div class="nn-suggest">${w.suggestion ? this.esc(w.suggestion.text) : ''}</div>
            <div class="nn-actions">
                ${w.suggestion ? '' : '<button class="btn btn-small btn-secondary" data-nn="suggest">Suggest a channel</button>'}
                ${w.watching ? '<button class="btn btn-small btn-secondary" data-nn="dismiss">Stop watching</button>' : ''}
            </div></div>`;
    },

    async onClick(event) {
        const btn = event.target.closest('button[data-nn]');
        if (!btn) return;
        const card = btn.closest('.nn-card');
        const node = card.dataset.node;
        btn.disabled = true;
        if (btn.dataset.nn === 'suggest') {
            btn.textContent = 'Planning…';
            const w = await (await fetch(`/api/welcome/${encodeURIComponent(node)}`)).json();
            card.querySelector('.nn-suggest').textContent = w.suggestion?.text || 'No suggestion.';
            btn.remove();
        } else if (btn.dataset.nn === 'dismiss') {
            const r = await fetch(`/api/welcome/${encodeURIComponent(node)}/dismiss`, { method: 'POST' });
            if (r.ok) this.load();
            else btn.disabled = false;
        }
    },

    /** Node page banner while the node is watched. */
    async banner(node, el) {
        try {
            const r = await fetch(`/api/welcome/${encodeURIComponent(node)}`);
            if (!r.ok) return;
            const w = await r.json();
            if (!w.watching) return;
            el.hidden = false;
            el.innerHTML = `<strong>New node</strong>, discovered ${this.esc(w.discovered_at)}; watched until ${this.esc(w.watch_until.slice(0, 16))}.
                ${w.suggestion ? ' ' + this.esc(w.suggestion.text) : ''}
                ${w.fixes?.length ? `<ul class="hy-fixes">${w.fixes.map(f => `<li>${this.esc(f)}</li>`).join('')}</ul>` : ''}`;
        } catch (e) { /* banner is optional */ }
    },

    setStatus(text) {
        const el = document.getElementById('nn-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => NewNodes.init());
