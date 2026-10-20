/**
 * Weekly Report section of the Reports tab (weekly_report.py): saved weekly
 * summaries (worst links/nodes, new problems, infrastructure verdicts,
 * changes, capacity, single points of failure, weather), Generate now, and
 * which notification channels the scheduled report is sent on. Sending uses
 * the channels configured under Diagnostics > Notifications.
 */
const WeeklyReportPanel = {
    initialized: false,
    loaded: false,
    data: null,
    CHANNELS: ['email', 'discord', 'ntfy', 'webhook'],

    init() {
        if (this.initialized) return;
        if (!document.getElementById('wr-list')) return;
        this.initialized = true;
        document.getElementById('wr-generate')?.addEventListener('click', () => this.generate());
        document.getElementById('wr-list').addEventListener('click', (event) => {
            const btn = event.target.closest('button[data-wr]');
            if (!btn) return;
            const id = Number(btn.dataset.id);
            if (btn.dataset.wr === 'view') this.view(id);
            if (btn.dataset.wr === 'send') this.send(id, btn);
            if (btn.dataset.wr === 'delete') this.remove(id);
        });
        document.getElementById('wr-channels').addEventListener('change', () => this.saveChannels());
        document.getElementById('wr-copy')?.addEventListener('click', () => {
            const text = document.getElementById('wr-text').textContent;
            navigator.clipboard?.writeText(text).then(() => this.setStatus('Copied.'), () => this.setStatus('Copy failed.'));
        });
    },

    load() {
        this.init();
        if (!this.initialized || this.loaded) return;
        this.loaded = true;
        this.fetch();
    },

    async fetch() {
        try {
            this.data = await fetch('/api/weekly-reports').then(r => r.json());
            this.render();
        } catch (error) {
            document.getElementById('wr-list').innerHTML = '<p class="log-empty">Failed to load.</p>';
        }
    },

    render() {
        const d = this.data;
        const sch = d.schedule;
        document.getElementById('wr-hint').textContent = sch.enabled
            ? `Built every ${sch.day} at ${sch.time} for the 7 days before.` : 'Scheduled report is off (WEEKLY_REPORT_ENABLED).';
        document.getElementById('wr-channels').innerHTML = 'Send on: ' + this.CHANNELS.map(c =>
            `<label class="checkbox-label"><input type="checkbox" value="${c}" ${d.channels.includes(c) ? 'checked' : ''}> ${c}</label>`).join(' ')
            + ' <span class="form-hint">(channels set up under Diagnostics &gt; Notifications)</span>';
        const el = document.getElementById('wr-list');
        if (!d.reports.length) {
            el.innerHTML = '<p class="log-empty">No reports yet. Generate one now.</p>';
            return;
        }
        el.innerHTML = `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Week</th><th>Built</th><th>Trigger</th><th>Sent</th><th></th></tr></thead>
            <tbody>${d.reports.map(r => `<tr><td>${this.esc(r.start.slice(0, 10))} – ${this.esc(r.end.slice(0, 10))}</td>
                <td>${this.esc(r.created_at.slice(0, 16))}</td><td>${this.esc(r.trigger)}</td>
                <td>${Object.entries(r.sent || {}).map(([c, v]) => `<span class="diag-badge infra-badge-${v === 'sent' ? 'ok' : 'warning'}" title="${this.esc(v)}">${this.esc(c)}</span>`).join(' ') || '-'}</td>
                <td class="rw-actions"><button class="btn btn-small btn-secondary" data-wr="view" data-id="${r.id}">View</button>
                    <button class="btn btn-small btn-secondary" data-wr="send" data-id="${r.id}" title="Send on the checked channels">Send</button>
                    <button class="btn btn-small btn-secondary" data-wr="delete" data-id="${r.id}">Delete</button></td></tr>`).join('')}</tbody></table></div>`;
    },

    checked() {
        return [...document.querySelectorAll('#wr-channels input:checked')].map(i => i.value);
    },

    async saveChannels() {
        await fetch('/api/weekly-reports/channels', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channels: this.checked() })
        });
        this.setStatus('Saved channels for the scheduled report.');
    },

    async generate() {
        const btn = document.getElementById('wr-generate');
        btn.disabled = true;
        this.setStatus('Building (infrastructure review included, may take a while)...');
        try {
            const d = await fetch('/api/weekly-reports', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ send: false })
            }).then(r => r.json());
            await this.fetch();
            this.view(d.id);
            this.setStatus('Report saved. It was not sent; press Send to send it.');
        } catch (error) {
            this.setStatus('Failed to build the report.');
        } finally {
            btn.disabled = false;
        }
    },

    async view(id) {
        const r = await fetch(`/api/weekly-reports/${id}`).then(x => x.json());
        document.getElementById('wr-view').classList.remove('hidden');
        document.getElementById('wr-text').textContent = r.text;
    },

    async send(id, btn) {
        const channels = this.checked();
        if (!channels.length) {
            this.setStatus('Tick at least one channel first.');
            return;
        }
        if (!confirm(`Send this report on ${channels.join(', ')}?`)) return;
        btn.disabled = true;
        try {
            const d = await fetch(`/api/weekly-reports/${id}/send`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ channels })
            }).then(r => r.json());
            this.setStatus(Object.entries(d.sent || {}).map(([c, v]) => `${c}: ${v}`).join(' · '));
            this.fetch();
        } finally {
            btn.disabled = false;
        }
    },

    async remove(id) {
        if (!confirm('Delete this saved report?')) return;
        await fetch(`/api/weekly-reports/${id}`, { method: 'DELETE' });
        document.getElementById('wr-view').classList.add('hidden');
        this.fetch();
    },

    setStatus(text) {
        const el = document.getElementById('wr-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

window.ReportPanels = window.ReportPanels || [];
window.ReportPanels.push(WeeklyReportPanel);
