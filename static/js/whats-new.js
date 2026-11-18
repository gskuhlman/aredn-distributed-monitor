/**
 * "What's new" (whats_new.py, TODO #83): click the build number in the header
 * for the finished features, newest first. Items this browser hasn't seen yet
 * are marked new, and the build number gets a dot until the panel is opened
 * (seen item numbers in localStorage; the first visit marks everything seen).
 */
const WhatsNew = {
    KEY: 'whats-new:seen',
    data: null,

    seen() {
        try { return JSON.parse(localStorage.getItem(this.KEY) || 'null'); } catch (e) { return null; }
    },

    remember(numbers) {
        try { localStorage.setItem(this.KEY, JSON.stringify(numbers)); } catch (e) { /* convenience only */ }
    },

    numbers() {
        return this.data.sections.flatMap(s => s.items.map(i => i.number));
    },

    async init() {
        const v = document.querySelector('.app-version');
        if (!v) return;
        v.setAttribute('role', 'button');
        v.tabIndex = 0;
        v.title = "Build number. Click for what's new";
        v.addEventListener('click', () => this.toggle());
        v.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.toggle(); } });
        try {
            this.data = await R7.json('/api/whats-new');
        } catch (e) {
            return;
        }
        const seen = this.seen();
        if (seen === null) this.remember(this.numbers());
        else if (this.numbers().some(n => !seen.includes(n))) v.classList.add('wn-has-new');
    },

    md(text) {
        return R7.esc(text).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\*(.+?)\*/g, '<em>$1</em>');
    },

    toggle() {
        const old = document.getElementById('wn-panel');
        if (old) { old.remove(); return; }
        if (!this.data) return;
        const seen = new Set(this.seen() || []);
        const panel = document.createElement('div');
        panel.id = 'wn-panel';
        panel.className = 'wn-panel';
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-label', "What's new");
        panel.innerHTML = `<div class="wn-head"><h3>What's new <small class="form-hint">build ${R7.esc(this.data.version)}</small></h3>
                <button type="button" class="btn btn-secondary btn-small wn-close">Close</button></div>
            <div class="wn-body">${this.data.sections.map(s => `<h4>${R7.esc(s.title)}</h4><ul>${s.items.map(i =>
                `<li>${seen.size && !seen.has(i.number) ? '<span class="wn-new">new</span> ' : ''}<strong>#${i.number} ${R7.esc(i.title)}</strong>
                    <span class="form-hint">${this.md(i.text)}</span></li>`).join('')}</ul>`).join('')}</div>`;
        document.body.appendChild(panel);
        panel.querySelector('.wn-close').addEventListener('click', () => panel.remove());
        panel.addEventListener('keydown', e => { if (e.key === 'Escape') panel.remove(); });
        panel.querySelector('.wn-close').focus();
        this.remember(this.numbers());
        document.querySelector('.app-version')?.classList.remove('wn-has-new');
    }
};

document.addEventListener('DOMContentLoaded', () => WhatsNew.init());
