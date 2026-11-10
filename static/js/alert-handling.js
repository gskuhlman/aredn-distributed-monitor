/**
 * Notifications page, below the channels and rules: Suppressions
 * (suppressions.py: mute one node / link / event types with a reason and a
 * review date; overdue ones flagged), Alerts sent with acknowledgement
 * (acks.py) and Escalation settings (repeat unacknowledged critical alerts,
 * then send them to a second channel).
 */
const AlertHandling = {
    init() {
        if (!document.getElementById('sup-list')) return;
        document.getElementById('sup-add').addEventListener('click', () => this.addSuppression());
        document.getElementById('sup-ended').addEventListener('change', () => this.loadSuppressions());
        document.getElementById('al-open').addEventListener('change', () => this.loadAlerts());
        document.getElementById('esc-save').addEventListener('click', () => this.saveEscalation());
        const d = new Date(Date.now() + 30 * 86400e3);
        document.getElementById('sup-review').value =
            `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        R7.onShow('notifications', null, () => { this.loadSuppressions(); this.loadAlerts(); this.loadEscalation(); });
    },

    async loadSuppressions() {
        const el = document.getElementById('sup-list');
        try {
            const d = await R7.json('/api/suppressions' + (document.getElementById('sup-ended').checked ? '?all=1' : ''));
            document.getElementById('sup-banner').innerHTML = d.overdue
                ? `<div class="overdue-banner">${d.overdue} suppression${d.overdue > 1 ? 's are' : ' is'} past the review date: keep (new date) or end ${d.overdue > 1 ? 'them' : 'it'}.</div>` : '';
            el.innerHTML = d.suppressions.length ? `<table class="info-table"><thead><tr><th>Node / link</th><th>Events</th><th>Reason</th>
                <th>Review by</th><th>By</th><th></th></tr></thead><tbody>${d.suppressions.map(s => `
                <tr${s.overdue ? ' class="rpt-cause-warning"' : ''}><td>${R7.node(s.node_name)}${s.peer_node ? ' ↔ ' + R7.node(s.peer_node) : ''}</td>
                    <td>${s.event_types.length ? s.event_types.map(R7.esc).join(', ') : 'all'}</td><td>${R7.esc(s.reason)}</td>
                    <td>${R7.esc(s.review_date)} ${!s.active ? R7.badge('ended', 'closed') : s.overdue ? R7.badge('overdue', 'warning') : `<small>${s.days_to_review} d</small>`}</td>
                    <td>${R7.esc(s.author || '')}</td>
                    <td>${s.active ? `<input type="date" class="form-input" data-sup-date="${s.id}">
                        <button class="btn btn-secondary btn-small" data-sup-keep="${s.id}">Keep until</button>
                        <button class="btn btn-secondary btn-small" data-sup-end="${s.id}">End</button>` : R7.esc(s.end_note || '')}</td></tr>`).join('')}
                </tbody></table>` : '<p class="log-empty">No suppressions. Alerts for every node are sent by the rules above.</p>';
            el.querySelectorAll('[data-sup-keep]').forEach(b => b.addEventListener('click', () => this.patchSuppression(b.dataset.supKeep,
                { review_date: el.querySelector(`[data-sup-date="${b.dataset.supKeep}"]`).value })));
            el.querySelectorAll('[data-sup-end]').forEach(b => b.addEventListener('click', () => this.patchSuppression(b.dataset.supEnd, { end: true })));
        } catch (e) { el.innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`; }
    },

    async addSuppression() {
        const msg = document.getElementById('sup-msg');
        const body = {
            node_name: document.getElementById('sup-node').value.trim(),
            peer_node: document.getElementById('sup-peer').value.trim() || null,
            event_types: document.getElementById('sup-types').value.split(/[\s,]+/).filter(Boolean),
            reason: document.getElementById('sup-reason').value.trim(),
            review_date: document.getElementById('sup-review').value,
        };
        try {
            await R7.json('/api/suppressions', { method: 'POST', body });
            msg.textContent = 'Suppression added.';
            ['sup-node', 'sup-peer', 'sup-types', 'sup-reason'].forEach(id => { document.getElementById(id).value = ''; });
            this.loadSuppressions();
        } catch (e) { msg.textContent = e.message; }
    },

    async patchSuppression(id, body) {
        try {
            await R7.json(`/api/suppressions/${id}`, { method: 'PATCH', body });
            this.loadSuppressions();
        } catch (e) { document.getElementById('sup-msg').textContent = e.message; }
    },

    async loadAlerts() {
        const el = document.getElementById('al-list');
        try {
            const open = document.getElementById('al-open').checked;
            const d = await R7.json(`/api/alerts?hours=168${open ? '&open=1' : ''}`);
            document.getElementById('al-count').textContent = d.unacked ? `${d.unacked} unacknowledged` : '';
            el.innerHTML = d.alerts.length ? `<table class="info-table"><thead><tr><th>Sent</th><th>Alert</th><th>Channels</th>
                <th>Repeats</th><th>Acknowledged</th></tr></thead><tbody>${d.alerts.map(a => `
                <tr><td>${R7.hm(a.sent_at)}</td><td>${R7.badge(a.severity || 'warning')} ${R7.esc(a.line || a.title)}</td>
                    <td>${a.channels.map(R7.esc).join(', ')}</td><td>${a.repeats || 0}${a.escalated_at ? ' · escalated' : ''}</td>
                    <td>${a.acked ? `${R7.esc(a.acked_by)} ${R7.hm(a.acked_at)}${a.ack_note ? `<br><small>${R7.esc(a.ack_note)}</small>` : ''}`
                        : `<button class="btn btn-primary btn-small" data-ack="${a.id}">Acknowledge</button>`}</td></tr>`).join('')}</tbody></table>`
                : '<p class="log-empty">No alerts sent in the last 7 days.</p>';
            el.querySelectorAll('[data-ack]').forEach(b => b.addEventListener('click', async () => {
                await R7.json(`/api/alerts/${b.dataset.ack}/ack`, { method: 'POST', body: {} });
                this.loadAlerts();
            }));
        } catch (e) { el.innerHTML = `<p class="log-empty">${R7.esc(e.message)}</p>`; }
    },

    async loadEscalation() {
        try {
            const s = await R7.json('/api/notifications/settings');
            const e = s.config.escalation || {};
            document.getElementById('esc-enabled').checked = !!e.enabled;
            document.getElementById('esc-sev').value = e.min_severity || 'critical';
            document.getElementById('esc-repeat').value = e.repeat_minutes ?? 30;
            document.getElementById('esc-max').value = e.max_repeats ?? 3;
            document.getElementById('esc-after').value = e.escalate_minutes ?? 60;
            document.getElementById('esc-base').value = e.ack_base_url || '';
            document.getElementById('esc-channels').innerHTML = s.channels.map(c => `<label class="checkbox-label">
                <input type="checkbox" value="${R7.esc(c)}" ${(e.channels || []).includes(c) ? 'checked' : ''}> ${R7.esc(c)}</label>`).join(' ');
        } catch (err) { document.getElementById('esc-msg').textContent = err.message; }
    },

    async saveEscalation() {
        const msg = document.getElementById('esc-msg');
        const escalation = {
            enabled: document.getElementById('esc-enabled').checked,
            min_severity: document.getElementById('esc-sev').value,
            repeat_minutes: Number(document.getElementById('esc-repeat').value),
            max_repeats: Number(document.getElementById('esc-max').value),
            escalate_minutes: Number(document.getElementById('esc-after').value),
            ack_base_url: document.getElementById('esc-base').value.trim(),
            channels: [...document.querySelectorAll('#esc-channels input:checked')].map(i => i.value),
        };
        try {
            await R7.json('/api/notifications/settings', { method: 'POST', body: { escalation } });
            msg.textContent = 'Saved.';
        } catch (e) { msg.textContent = e.message; }
    },
};

AlertHandling.init();
