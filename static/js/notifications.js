/**
 * Notifications section (Diagnostics tab): channels, anti-spam timing, mute,
 * rules and the send log, backed by /api/notifications/*. Matching, cooldown,
 * flap hold and digest all happen server-side in notify.py; secrets come back
 * masked and are only sent when the operator types a new value.
 */
const Notifications = {
    loaded: false,
    data: null,
    rules: [],
    editingId: null,

    CHANNEL_FIELDS: {
        email: [['host', 'SMTP host'], ['port', 'Port', 'number'], ['tls', 'Security', 'tls'],
                ['user', 'Username'], ['password', 'Password', 'secret'], ['from', 'From'], ['to', 'To (comma separated)']],
        discord: [['webhook_url', 'Webhook URL', 'secret']],
        ntfy: [['server', 'Server'], ['topic', 'Topic'], ['token', 'Access token', 'secret'], ['priority', 'Priority', 'priority']],
        webhook: [['url', 'URL'], ['token', 'Bearer token', 'secret']]
    },
    CHANNEL_LABELS: { email: 'Email (SMTP)', discord: 'Discord', ntfy: 'ntfy', webhook: 'Webhook (JSON POST)' },

    init() {
        const section = document.getElementById('notify-section');
        if (!section) return;
        section.addEventListener('toggle', () => { if (section.open && !this.loaded) this.load(); });
        this.refreshBadge();
    },

    async api(url, options = {}) {
        const resp = await fetch(url, {
            headers: { 'Content-Type': 'application/json' }, ...options,
            body: options.body ? JSON.stringify(options.body) : undefined
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        return data;
    },

    async refreshBadge() {
        try {
            const d = await this.api('/api/notifications/settings');
            this.setBadge(d.status);
        } catch (e) { /* section still works when opened */ }
    },

    setBadge(status) {
        const el = document.getElementById('notify-badge');
        if (!el || !status) return;
        const text = !status.enabled ? 'off' : status.muted ? 'muted' : status.quiet_now ? 'quiet hours' : 'on';
        el.textContent = text;
        el.className = `notify-badge notify-${text.replace(' ', '-')}`;
    },

    async load() {
        const el = document.getElementById('notify-panel');
        try {
            const [settings, rules] = await Promise.all([
                this.api('/api/notifications/settings'), this.api('/api/notifications/rules')]);
            this.data = settings;
            this.rules = rules.rules || [];
            this.loaded = true;
            this.render();
            this.loadLog();
        } catch (error) {
            el.innerHTML = `<p class="log-empty">Could not load notifications: ${this.esc(error.message)}</p>`;
        }
    },

    render() {
        const c = this.data.config;
        const s = this.data.status;
        this.setBadge(s);
        const qh = c.quiet_hours || {};
        document.getElementById('notify-panel').innerHTML = `
            <div class="notify-grid">
                <fieldset class="notify-box">
                    <legend>General</legend>
                    <label class="checkbox-label"><input type="checkbox" id="nt-enabled" ${c.enabled ? 'checked' : ''}> Send notifications</label>
                    <label>Digest (seconds) <input type="number" min="0" id="nt-digest" class="form-input" value="${c.digest_seconds}"></label>
                    <small class="form-hint">Alerts within this window go out as one message. 0 = send each immediately.</small>
                    <label>Cooldown (minutes) <input type="number" min="0" id="nt-cooldown" class="form-input" value="${c.cooldown_minutes}"></label>
                    <small class="form-hint">Same rule + same node/link + same event type is sent at most once per cooldown.</small>
                    <label>Flap hold (minutes) <input type="number" min="0" id="nt-flap" class="form-input" value="${c.flap_minutes}"></label>
                    <small class="form-hint">Problems (offline, dropped, lost...) wait this long; if they recover in time you get one "flapped" note instead of two alerts. 0 = no hold.</small>
                </fieldset>
                <fieldset class="notify-box">
                    <legend>Quiet hours &amp; mute</legend>
                    <label class="checkbox-label"><input type="checkbox" id="nt-qh-enabled" ${qh.enabled ? 'checked' : ''}> Quiet hours</label>
                    <div class="notify-row">
                        <label>From <input type="time" id="nt-qh-start" class="form-input" value="${this.esc(qh.start || '22:00')}"></label>
                        <label>to <input type="time" id="nt-qh-end" class="form-input" value="${this.esc(qh.end || '07:00')}"></label>
                    </div>
                    <label class="checkbox-label"><input type="checkbox" id="nt-qh-critical" ${qh.allow_critical ? 'checked' : ''}> Still send critical alerts</label>
                    <div class="notify-mute">
                        <strong>Mute:</strong> <span id="nt-mute-status">${s.muted ? `until ${this.esc(c.mute_until)}` : 'not muted'}</span>
                        <div class="notify-row">
                            <button class="btn btn-small btn-secondary" data-mute="60">1 h</button>
                            <button class="btn btn-small btn-secondary" data-mute="240">4 h</button>
                            <button class="btn btn-small btn-secondary" data-mute="720">12 h</button>
                            <button class="btn btn-small btn-secondary" data-mute="1440">24 h</button>
                            <button class="btn btn-small btn-secondary" data-mute="0">Unmute</button>
                        </div>
                    </div>
                </fieldset>
            </div>
            <div class="notify-grid">${this.data.channels.map(ch => this.renderChannel(ch, c.channels[ch])).join('')}</div>
            <div class="mh-controls">
                <button id="nt-save" class="btn btn-primary">Save notification settings</button>
                <span id="nt-status" class="form-hint"></span>
            </div>
            <small class="form-hint">Passwords, tokens and the Discord webhook URL are stored only in this monitor's local database
                (or NOTIFY_* environment variables) and are never shown again. Leave a saved secret as ******** to keep it; clear the box to remove it.</small>

            <h4 class="notify-h">Rules</h4>
            <div id="nt-rules">${this.renderRules()}</div>
            <div id="nt-rule-form">${this.renderRuleForm()}</div>

            <h4 class="notify-h">Sent / suppressed log <button id="nt-log-refresh" class="btn btn-small btn-secondary">Refresh</button></h4>
            <div id="nt-log"><p class="log-empty">Loading...</p></div>`;
        this.bind();
    },

    renderChannel(name, ch) {
        const fields = this.CHANNEL_FIELDS[name].map(([key, label, kind]) => {
            const id = `nt-${name}-${key}`;
            let input;
            if (kind === 'tls') {
                input = `<select id="${id}" class="form-input">${['starttls', 'ssl', 'none'].map(v =>
                    `<option value="${v}" ${ch[key] === v ? 'selected' : ''}>${v === 'starttls' ? 'STARTTLS' : v === 'ssl' ? 'SSL/TLS' : 'None'}</option>`).join('')}</select>`;
            } else if (kind === 'priority') {
                input = `<select id="${id}" class="form-input">${['min', 'low', 'default', 'high', 'urgent'].map(v =>
                    `<option value="${v}" ${ch[key] === v ? 'selected' : ''}>${v}</option>`).join('')}</select>`;
            } else if (kind === 'secret') {
                const env = ch[`${key}_from_env`] ? ' <small class="form-hint">(set by environment)</small>' : '';
                input = `<input type="password" autocomplete="new-password" id="${id}" class="form-input"
                    value="${this.esc(ch[key] || '')}" placeholder="${ch[`${key}_set`] ? '(saved)' : ''}"
                    ${ch[`${key}_from_env`] ? 'disabled' : ''}>${env}`;
            } else {
                input = `<input type="${kind === 'number' ? 'number' : 'text'}" id="${id}" class="form-input" value="${this.esc(ch[key] ?? '')}">`;
            }
            return `<label>${label} ${input}</label>`;
        }).join('');
        return `<fieldset class="notify-box">
            <legend><label class="checkbox-label"><input type="checkbox" id="nt-${name}-enabled" ${ch.enabled ? 'checked' : ''}>
                ${this.CHANNEL_LABELS[name]}</label></legend>
            ${fields}
            <button class="btn btn-small btn-secondary" data-test="${name}">Send test</button>
            <span class="form-hint" id="nt-test-${name}"></span>
        </fieldset>`;
    },

    renderRules() {
        if (!this.rules.length) return '<p class="log-empty">No rules. Add one below.</p>';
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>On</th><th>Rule</th><th>Events</th><th>Min severity</th><th>Scope</th><th>Channels</th><th></th></tr></thead>
            <tbody>${this.rules.map(r => `<tr>
                <td><input type="checkbox" data-rule-toggle="${r.id}" ${r.enabled ? 'checked' : ''}></td>
                <td>${this.esc(r.name)}</td>
                <td>${r.event_types.map(t => this.esc(t)).join(', ')}</td>
                <td>${this.esc(r.min_severity)}</td>
                <td>${this.esc(r.scope)}${r.scope === 'nodes' ? ': ' + this.esc(r.nodes.join(', ')) : ''}</td>
                <td>${r.channels.length ? this.esc(r.channels.join(', ')) : 'all enabled'}</td>
                <td><button class="btn btn-small btn-secondary" data-rule-edit="${r.id}">Edit</button>
                    <button class="btn btn-small btn-secondary" data-rule-delete="${r.id}">Delete</button></td>
            </tr>`).join('')}</tbody></table></div>`;
    },

    renderRuleForm(rule = null) {
        const r = rule || { name: '', event_types: [], min_severity: 'info', scope: 'any', nodes: [], channels: [] };
        const types = this.data.event_types.map(t => `<label class="checkbox-label notify-chip">
            <input type="checkbox" name="nt-rule-type" value="${t}" ${r.event_types.includes(t) ? 'checked' : ''}> ${t}</label>`).join('');
        const chans = this.data.channels.map(c => `<label class="checkbox-label notify-chip">
            <input type="checkbox" name="nt-rule-channel" value="${c}" ${r.channels.includes(c) ? 'checked' : ''}> ${c}</label>`).join('');
        return `<fieldset class="notify-box notify-rule-form">
            <legend>${rule ? `Edit rule: ${this.esc(rule.name)}` : 'Add rule'}</legend>
            <label>Name <input type="text" id="nt-rule-name" class="form-input" value="${this.esc(r.name)}"></label>
            <div><strong>Event types</strong><div class="notify-chips">${types}</div></div>
            <div class="notify-row">
                <label>Min severity <select id="nt-rule-severity" class="form-input">${this.data.severities.map(s =>
                    `<option ${r.min_severity === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label>
                <label>Scope <select id="nt-rule-scope" class="form-input">${this.data.scopes.map(s =>
                    `<option value="${s}" ${r.scope === s ? 'selected' : ''}>${s === 'nodes' ? 'specific nodes' : s}</option>`).join('')}</select></label>
            </div>
            <label>Nodes (for specific nodes; comma separated) <input type="text" id="nt-rule-nodes" class="form-input" value="${this.esc(r.nodes.join(', '))}"></label>
            <div><strong>Channels</strong> <small class="form-hint">none ticked = every enabled channel</small><div class="notify-chips">${chans}</div></div>
            <small class="form-hint">Recovery events (online, restored, cleared) are only sent for problems that were alerted.
                A link matches a node scope when either end is in it.</small>
            <div class="mh-controls">
                <button id="nt-rule-save" class="btn btn-primary btn-small">${rule ? 'Update rule' : 'Add rule'}</button>
                ${rule ? '<button id="nt-rule-cancel" class="btn btn-secondary btn-small">Cancel</button>' : ''}
                <span id="nt-rule-status" class="form-hint"></span>
            </div>
        </fieldset>`;
    },

    bind() {
        document.getElementById('nt-save').addEventListener('click', () => this.save());
        document.querySelectorAll('#notify-panel [data-mute]').forEach(b =>
            b.addEventListener('click', () => this.mute(Number(b.dataset.mute))));
        document.querySelectorAll('#notify-panel [data-test]').forEach(b =>
            b.addEventListener('click', () => this.test(b.dataset.test, b)));
        document.getElementById('nt-log-refresh').addEventListener('click', () => this.loadLog());
        this.bindRules();
    },

    bindRules() {
        document.querySelectorAll('#nt-rules [data-rule-toggle]').forEach(cb =>
            cb.addEventListener('change', () => {
                const rule = this.rules.find(r => r.id === Number(cb.dataset.ruleToggle));
                this.saveRule({ ...rule, enabled: cb.checked });
            }));
        document.querySelectorAll('#nt-rules [data-rule-edit]').forEach(b =>
            b.addEventListener('click', () => {
                const rule = this.rules.find(r => r.id === Number(b.dataset.ruleEdit));
                this.editingId = rule.id;
                document.getElementById('nt-rule-form').innerHTML = this.renderRuleForm(rule);
                this.bindRuleForm();
                document.getElementById('nt-rule-form').scrollIntoView({ block: 'nearest' });
            }));
        document.querySelectorAll('#nt-rules [data-rule-delete]').forEach(b =>
            b.addEventListener('click', () => this.deleteRule(Number(b.dataset.ruleDelete))));
        this.bindRuleForm();
    },

    bindRuleForm() {
        document.getElementById('nt-rule-save')?.addEventListener('click', () => {
            const form = document.getElementById('nt-rule-form');
            const rule = {
                id: this.editingId || undefined,
                name: document.getElementById('nt-rule-name').value,
                event_types: [...form.querySelectorAll('input[name="nt-rule-type"]:checked')].map(i => i.value),
                min_severity: document.getElementById('nt-rule-severity').value,
                scope: document.getElementById('nt-rule-scope').value,
                nodes: document.getElementById('nt-rule-nodes').value.split(',').map(s => s.trim()).filter(Boolean),
                channels: [...form.querySelectorAll('input[name="nt-rule-channel"]:checked')].map(i => i.value),
                enabled: this.editingId ? this.rules.find(r => r.id === this.editingId)?.enabled : true
            };
            this.saveRule(rule, true);
        });
        document.getElementById('nt-rule-cancel')?.addEventListener('click', () => this.resetRuleForm());
    },

    resetRuleForm() {
        this.editingId = null;
        document.getElementById('nt-rule-form').innerHTML = this.renderRuleForm();
        this.bindRuleForm();
    },

    async saveRule(rule, fromForm = false) {
        const status = document.getElementById('nt-rule-status');
        try {
            await this.api('/api/notifications/rules', { method: 'POST', body: rule });
            this.rules = (await this.api('/api/notifications/rules')).rules || [];
            document.getElementById('nt-rules').innerHTML = this.renderRules();
            if (fromForm) this.resetRuleForm();
            this.bindRules();
        } catch (error) {
            if (status) status.textContent = error.message;
        }
    },

    async deleteRule(id) {
        try {
            await this.api(`/api/notifications/rules/${id}`, { method: 'DELETE' });
            this.rules = this.rules.filter(r => r.id !== id);
            if (this.editingId === id) this.resetRuleForm();
            document.getElementById('nt-rules').innerHTML = this.renderRules();
            this.bindRules();
        } catch (error) {
            document.getElementById('nt-rule-status').textContent = error.message;
        }
    },

    collect() {
        const val = id => document.getElementById(id)?.value;
        const checked = id => !!document.getElementById(id)?.checked;
        const channels = {};
        for (const [name, fields] of Object.entries(this.CHANNEL_FIELDS)) {
            channels[name] = { enabled: checked(`nt-${name}-enabled`) };
            for (const [key, , kind] of fields) {
                const el = document.getElementById(`nt-${name}-${key}`);
                if (!el || (kind === 'secret' && el.disabled)) continue;
                channels[name][key] = el.value;
            }
        }
        return {
            enabled: checked('nt-enabled'),
            digest_seconds: val('nt-digest'), cooldown_minutes: val('nt-cooldown'), flap_minutes: val('nt-flap'),
            quiet_hours: { enabled: checked('nt-qh-enabled'), start: val('nt-qh-start'), end: val('nt-qh-end'),
                           allow_critical: checked('nt-qh-critical') },
            channels
        };
    },

    async save() {
        const status = document.getElementById('nt-status');
        status.textContent = 'Saving...';
        try {
            const d = await this.api('/api/notifications/settings', { method: 'POST', body: this.collect() });
            this.data.config = d.config;
            this.data.status = d.status;
            this.render();
            document.getElementById('nt-status').textContent = 'Saved.';
        } catch (error) {
            status.textContent = error.message;
        }
    },

    async mute(minutes) {
        try {
            const d = await this.api('/api/notifications/settings', { method: 'POST', body: { mute_until: minutes || null } });
            this.data.config.mute_until = d.config.mute_until;
            this.data.status = d.status;
            document.getElementById('nt-mute-status').textContent = d.status.muted ? `until ${d.config.mute_until}` : 'not muted';
            this.setBadge(d.status);
        } catch (error) {
            document.getElementById('nt-status').textContent = error.message;
        }
    },

    async test(channel, button) {
        const out = document.getElementById(`nt-test-${channel}`);
        button.disabled = true;
        out.textContent = 'Sending... (save first if you changed this channel)';
        try {
            await this.api('/api/notifications/test', { method: 'POST', body: { channel } });
            out.textContent = 'Sent.';
        } catch (error) {
            out.textContent = `Failed: ${error.message}`;
        } finally {
            button.disabled = false;
            this.loadLog();
        }
    },

    async loadLog() {
        const el = document.getElementById('nt-log');
        if (!el) return;
        try {
            const log = (await this.api('/api/notifications/log?limit=100')).log || [];
            el.innerHTML = log.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
                <thead><tr><th>Time</th><th>Status</th><th>Channel</th><th>Rule</th><th>Title</th><th>Events</th><th>Reason / error</th></tr></thead>
                <tbody>${log.map(r => `<tr class="notify-log-${this.esc(r.status)}">
                    <td>${this.esc(r.timestamp)}</td><td>${this.esc(r.status)}</td><td>${this.esc(r.channel || '-')}</td>
                    <td>${this.esc(r.rule || '')}</td><td>${this.esc(r.title || '')}</td>
                    <td>${this.esc(r.event_ids || '')}</td><td>${this.esc(r.error || '')}</td></tr>`).join('')}</tbody>
                </table></div>` : '<p class="log-empty">Nothing sent yet.</p>';
        } catch (error) {
            el.innerHTML = `<p class="log-empty">${this.esc(error.message)}</p>`;
        }
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => Notifications.init());
