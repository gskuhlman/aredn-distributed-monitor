/**
 * Ghost Trackers section on the Diagnostics tab (ghost_trackers.py): RF
 * trackers that can't be what they claim. "Left out" ones (radio off, cabled
 * pair) are already excluded from Mesh Health, Link Watch, the Channel Map,
 * Site Matrix and Sector Airtime; the rest are for a person to check.
 */
const GhostTrackers = {
    initialized: false,

    init() {
        if (this.initialized || !document.getElementById('gt-report')) return;
        this.initialized = true;
        document.getElementById('gt-run')?.addEventListener('click', () => this.load());
        document.getElementById('gt-report').addEventListener('click', (event) => {
            const btn = event.target.closest('button[data-a]');
            if (!btn) return;
            document.querySelector('.tab-btn[data-tab="rf-stats"]')?.click();
            setTimeout(() => { if (typeof RFStats !== 'undefined') RFStats.selectLink(btn.dataset.a, btn.dataset.b); }, 300);
        });
    },

    async load() {
        this.init();
        const el = document.getElementById('gt-report');
        this.setStatus('Loading...');
        try {
            const d = await (await fetch('/api/reports/ghost-trackers')).json();
            if (!d.ghosts.length) {
                el.innerHTML = '<p class="log-empty">No ghost or suspicious RF trackers.</p>';
            } else {
                el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
                    <thead><tr><th>Tracker at</th><th>Claims to hear</th><th>Why</th><th>LQ / SNR</th><th>Handling</th><th></th></tr></thead>
                    <tbody>${d.ghosts.map(g => `<tr>
                        <td>${this.esc(g.source)}</td><td>${this.esc(g.target)}</td>
                        <td>${g.notes.map(n => this.esc(n)).join('<br>')}</td>
                        <td>${g.quality ?? '-'}% / ${g.snr ?? '-'} dB</td>
                        <td><span class="diag-badge infra-badge-${g.exclude ? 'info' : 'warning'}">${g.exclude ? 'left out of RF checks' : 'check by hand'}</span></td>
                        <td><button class="btn btn-small btn-secondary" data-a="${this.esc(g.source)}" data-b="${this.esc(g.target)}">Diagnose</button></td>
                    </tr>`).join('')}</tbody></table></div>`;
            }
            this.setStatus(`${d.ghosts.length} tracker(s), ${d.excluded} left out of RF checks · ${d.generated_at}`);
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Failed to load.</p>';
            this.setStatus('Failed to load.');
        }
    },

    setStatus(text) {
        const el = document.getElementById('gt-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => GhostTrackers.init());
