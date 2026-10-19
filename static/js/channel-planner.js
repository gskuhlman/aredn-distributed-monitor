/**
 * Channel planner panel on the Channel Map tab: suggested channels for a
 * site or a list of radios from POST /api/channel-planner. All scoring lives
 * in channel_planner.py; this renders the ranked plans (old -> new per radio,
 * reasons, remaining issues, Channel Map what-if delta, spectrum before and
 * after) and hands a plan to the map's what-if. Nothing is saved unless the
 * operator asks for a maintenance window / change-log entries for a plan.
 */
const ChannelPlanner = {
    initialized: false,
    result: null,

    init() {
        if (this.initialized) return;
        const box = document.getElementById('cp-box');
        if (!box) return;
        this.initialized = true;
        box.addEventListener('toggle', () => {
            document.querySelector('.cm-side')?.classList.toggle('cm-side-wide', box.open);
            setTimeout(() => ChannelMap.map?.invalidateSize(), 50);
            if (box.open) this.loadSites();
        });
        document.getElementById('cp-run')?.addEventListener('click', () => this.run());
        document.getElementById('cp-target')?.addEventListener('change', () => this.syncTarget());
        document.getElementById('cp-results')?.addEventListener('click', (event) => {
            const btn = event.target.closest('button[data-cp-action]');
            if (!btn) return;
            const plan = this.planAt(btn.dataset.cpPlan);
            if (!plan) return;
            if (btn.dataset.cpAction === 'map') this.showOnMap(plan);
            if (btn.dataset.cpAction === 'copy') this.copyText(plan);
            if (btn.dataset.cpAction === 'schedule') this.schedule(plan, btn);
        });
    },

    async loadSites() {
        const select = document.getElementById('cp-site');
        if (!select || select.options.length) return;
        try {
            const data = await (await fetch('/api/nodes/infrastructure')).json();
            const codes = new Set(Object.values(data.infrastructure_nodes || {}).filter(Boolean));
            Object.keys(data.sites || {}).forEach(c => codes.add(c));
            select.innerHTML = [...codes].sort().map(c =>
                `<option value="${this.esc(c)}">${this.esc(c)}${data.sites?.[c] ? ' – ' + this.esc(data.sites[c]) : ''}</option>`).join('');
            if (codes.has('COL')) select.value = 'COL';
        } catch (error) {
            console.error('Planner site list error:', error);
        }
    },

    syncTarget() {
        const bySite = document.getElementById('cp-target').value === 'site';
        document.getElementById('cp-site').hidden = !bySite;
        document.getElementById('cp-radios').hidden = bySite;
    },

    request() {
        const bySite = document.getElementById('cp-target').value === 'site';
        const widths = [...document.querySelectorAll('.cp-width:checked')].map(el => Number(el.value));
        const low = Number(document.getElementById('cp-band-low').value);
        const high = Number(document.getElementById('cp-band-high').value);
        const body = {
            band: [low, high],
            widths: document.getElementById('cp-keep-width').checked ? null : widths,
            locked: document.getElementById('cp-locked').value,
            move_clients: document.getElementById('cp-move-clients').checked,
            idle_off: document.getElementById('cp-idle-off')?.checked !== false,
            top: Number(document.getElementById('cp-top').value) || 3
        };
        if (bySite) body.site = document.getElementById('cp-site').value;
        else body.radios = document.getElementById('cp-radios').value;
        const compare = document.getElementById('cm-whatif-text')?.value.trim();
        if (compare && document.getElementById('cp-compare').checked) body.compare = compare;
        return body;
    },

    async run() {
        const button = document.getElementById('cp-run');
        const el = document.getElementById('cp-results');
        const body = this.request();
        if (Array.isArray(body.widths) && !body.widths.length) {
            el.innerHTML = '<p class="log-empty">Pick at least one width, or keep current widths.</p>';
            return;
        }
        button.disabled = true;
        this.setStatus('Planning...');
        try {
            const resp = await fetch('/api/channel-planner', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
            const data = await resp.json();
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Planning failed')}</p>`;
                this.setStatus('');
                return;
            }
            this.result = data;
            this.render();
            this.setStatus(`${data.plans.length} plan(s) from ${data.distinct_optima} candidates · ${data.elapsed_ms} ms`);
        } catch (error) {
            console.error('Channel planner error:', error);
            el.innerHTML = '<p class="log-empty">Planning failed.</p>';
            this.setStatus('');
        } finally {
            button.disabled = false;
        }
    },

    planAt(key) {
        if (!this.result) return null;
        return key === 'compare' ? this.result.compare : this.result.plans[Number(key)];
    },

    render() {
        const d = this.result;
        const el = document.getElementById('cp-results');
        const notes = (d.notes || []).map(n => `<li>${this.esc(n)}</li>`).join('');
        const groups = d.groups.length;
        el.innerHTML = `
            <p class="form-hint">${groups} channel group(s) in ${d.band.low}–${d.band.high} MHz.
                Radios in a group share one channel${d.move_clients ? ' (sectors with their RF-linked clients, PtP ends together)' : ''}.
                Lower score is better.</p>
            ${notes ? `<ul class="diag-evidence">${notes}</ul>` : ''}
            ${d.turn_off?.length ? `<p class="node-warning">Suggested off (every plan below includes this):
                ${d.turn_off.map(t => `<strong>${this.esc(t.name)}</strong> (now ${t.frequency}/${t.chanbw || '20?'})`).join(', ')}
                · ${this.esc(d.turn_off[0].reason)}.</p>` : ''}
            ${this.renderPlan(d.current, 'Current channels', null)}
            ${d.plans.map((p, i) => this.renderPlan(p, `Plan ${i + 1}`, String(i))).join('')}
            ${d.compare ? this.renderPlan(d.compare, 'Your what-if plan', 'compare') : ''}`;
    },

    renderPlan(p, title, key) {
        const c = p.counts;
        const w = p.what_if;
        const delta = w ? `<div class="cp-delta">Channel Map, whole area: critical ${w.baseline_counts.critical} → ${w.counts.critical},
            warning ${w.baseline_counts.warning} → ${w.counts.warning}, info ${w.baseline_counts.info} → ${w.counts.info}
            · ${w.resolved.length} resolved, ${w.introduced.length} new
            ${w.introduced.length ? `<ul class="diag-evidence">${w.introduced.map(x =>
                `<li><span class="diag-badge infra-badge-${this.esc(x.severity)}">${this.esc(x.severity)}</span> ${this.esc(x.summary)}</li>`).join('')}</ul>` : ''}
            </div>` : '';
        const extra = [];
        if (p.split_groups?.length) extra.push(`splits ${p.split_groups.map(g => g.join(' + ')).join('; ')} onto different channels`);
        if (p.outside_band?.length) extra.push(`outside the 5650–5925 MHz band: ${p.outside_band.join(', ')}`);
        const actions = key == null ? '' : `<div class="cm-whatif-actions">
            <button class="btn btn-primary btn-small" data-cp-action="map" data-cp-plan="${key}">Show on map</button>
            <button class="btn btn-secondary btn-small" data-cp-action="copy" data-cp-plan="${key}">Copy as what-if</button>
            ${key !== 'compare' && p.changes ? `<button class="btn btn-secondary btn-small" data-cp-action="schedule" data-cp-plan="${key}"
                title="Create a maintenance window for the changed radios and a change-log entry per radio">Schedule…</button>` : ''}
            </div>`;
        return `<details class="diag-details cp-plan" ${key === '0' ? 'open' : ''}>
            <summary>${this.esc(title)} · score ${p.cost} · ${p.changes} radio(s) changed ·
                <span class="cp-counts">${c.critical} critical, ${c.warning} warning, ${c.info} info,
                ${c.same_channel} shared-channel${c.link_break ? `, <strong>${c.link_break} broken link(s)</strong>` : ''}</span></summary>
            ${extra.length ? `<p class="node-warning">${this.esc(extra.join('; '))}</p>` : ''}
            ${actions}
            ${this.renderSpectrum(p)}
            ${this.renderTable(p)}
            ${delta}
            ${this.renderGroups(p)}
        </details>`;
    },

    renderTable(p) {
        const rows = p.radios.map(r => {
            const o = r.old, n = r.new;
            const oldText = o.frequency != null ? `${o.frequency}/${o.chanbw || '20?'} <small>ch ${o.channel ?? '?'}</small>` : '-';
            if (r.off) {
                return `<tr class="cp-changed"><td>${this.esc(r.name)}${r.is_infrastructure ? ' <small>(infra)</small>' : ''}</td>
                    <td>${oldText}</td><td>→</td><td><strong>off</strong></td><td><small>no RF links</small></td></tr>`;
            }
            return `<tr class="${r.changed ? 'cp-changed' : ''}">
                <td>${this.esc(r.name)}${r.is_infrastructure ? ' <small>(infra)</small>' : ''}</td>
                <td>${oldText}</td>
                <td>${r.changed ? '→' : '='}</td>
                <td><strong>${n.frequency}/${n.chanbw}</strong> <small>ch ${n.channel}</small></td>
                <td><small>${n.low}–${n.high} MHz · ch ${n.low_channel}–${n.high_channel}</small></td></tr>`;
        }).join('');
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table cp-table">
            <thead><tr><th>Radio</th><th>Now (MHz)</th><th></th><th>Plan (MHz)</th><th>Span</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    },

    renderGroups(p) {
        return `<details class="diag-details"><summary>Why these channels</summary>${p.groups.map(g => {
            const issues = g.issues.slice(0, 6).map(i => {
                const label = i.rule === 'same_dish' ? 'same channel as a backbone dish aimed here'
                    : i.rule ? 'under 20 MHz from infrastructure at another site'
                    : i.relation === 'link_break' ? 'link breaks'
                    : i.relation === 'same' ? 'shares channel' : (i.severity || i.relation);
                const where = i.colocated ? 'same mast' : i.distance_m != null ? `${(i.distance_m / 1000).toFixed(1)} km` : '';
                const gap = i.gap_mhz == null || i.relation === 'same' ? ''
                    : i.gap_mhz < 0 ? `${-i.gap_mhz} MHz overlap` : `${i.gap_mhz} MHz gap`;
                return `<li><strong>${this.esc(label)}</strong>: ${this.esc(i.a)} ↔ ${this.esc(i.b)} ${[gap, where].filter(Boolean).map(x => '· ' + this.esc(x)).join(' ')}</li>`;
            }).join('');
            return `<div class="cp-group"><div><strong>${g.frequency}/${g.chanbw} MHz (ch ${g.channel})</strong>:
                ${this.esc(g.members.join(', '))}</div>
                <ul class="diag-evidence">${g.reasons.map(r => `<li>${this.esc(r)}</li>`).join('')}</ul>
                ${issues ? `<ul class="diag-evidence cp-issues">${issues}</ul>` : ''}</div>`;
        }).join('')}</details>`;
    },

    /** Before/after occupied spectrum of every planned radio (old bar faded above the new one). */
    renderSpectrum(p) {
        const radios = p.radios.filter(r => r.new.frequency != null);
        if (!radios.length) return '';
        const lo = Math.min(...radios.map(r => Math.min(r.new.low, r.old.low ?? r.new.low)), this.result.band.low);
        const hi = Math.max(...radios.map(r => Math.max(r.new.high, r.old.high ?? r.new.high)), this.result.band.high);
        const min = Math.floor((lo - 5) / 10) * 10, max = Math.ceil((hi + 5) / 10) * 10;
        const W = 640, labelW = 170, rowH = 18, top = 6, H = top + radios.length * rowH + 22;
        const x = f => labelW + (f - min) / (max - min) * (W - labelW - 10);
        const palette = ['#2e86c1', '#28b463', '#af7ac5', '#e67e22', '#16a085', '#c0392b', '#7f8c8d', '#d4ac0d', '#1abc9c', '#8e44ad'];
        let ticks = '';
        const step = (max - min) > 200 ? 50 : 20;
        for (let f = Math.ceil(min / step) * step; f <= max; f += step) {
            ticks += `<line x1="${x(f)}" x2="${x(f)}" y1="${top}" y2="${H - 18}" class="spec-grid"/>
                <text x="${x(f)}" y="${H - 5}" class="spec-tick">${f}</text>`;
        }
        const band = `<rect x="${x(this.result.band.low)}" y="${top}" width="${x(this.result.band.high) - x(this.result.band.low)}"
            height="${H - 18 - top}" class="cp-band"/>`;
        const wifi = (ChannelMap.data?.wifi_bands || []).filter(b => b.busy === 'heavy').map(b =>
            `<rect x="${x(Math.max(b.low, min))}" y="${top}" width="${Math.max(0, x(Math.min(b.high, max)) - x(Math.max(b.low, min)))}"
                height="${H - 18 - top}" class="cp-wifi"><title>${this.esc(b.label)}</title></rect>`).join('');
        const bars = radios.map((r, i) => {
            const y = top + i * rowH;
            const color = palette[r.group % palette.length];
            const old = r.old.low != null && r.changed
                ? `<rect x="${x(r.old.low)}" y="${y + 2}" width="${Math.max(2, x(r.old.high) - x(r.old.low))}" height="5"
                    rx="1" fill="#95a5a6"><title>${this.esc(r.name)} now: ${r.old.low}–${r.old.high} MHz</title></rect>` : '';
            return `<text x="${labelW - 6}" y="${y + 12}" class="spec-label">${this.esc(r.name)}</text>${old}
                <rect x="${x(r.new.low)}" y="${y + 7}" width="${Math.max(2, x(r.new.high) - x(r.new.low))}" height="${rowH - 9}"
                  rx="2" fill="${color}" fill-opacity="0.85">
                  <title>${this.esc(r.name)} plan: ${r.new.low}–${r.new.high} MHz (ch ${r.new.channel})</title></rect>`;
        }).join('');
        return `<div class="spec-wrap"><svg viewBox="0 0 ${W} ${H}" class="spec-chart" role="img"
            aria-label="Planned spectrum per radio">${band}${wifi}${ticks}${bars}</svg>
            <p class="form-hint">Thin grey bar = current channel, colored bar = planned (same color = one group sharing a channel).
            Shaded: allowed band; orange tint: heavy WiFi (ch 149–165).</p></div>`;
    },

    showOnMap(plan) {
        const box = document.getElementById('cm-whatif-text');
        box.value = this.planText(plan);
        document.getElementById('cm-whatif-box').open = true;
        ChannelMap.runWhatIf();
    },

    planText(plan) {
        return plan.radios.map(r => `${r.name} ${r.new.frequency} ${r.new.chanbw}`).join('\n');
    },

    async copyText(plan) {
        const text = this.planText(plan);
        try {
            await navigator.clipboard.writeText(text);
            this.setStatus('Copied what-if lines to the clipboard');
        } catch (error) {
            document.getElementById('cm-whatif-text').value = text;
            document.getElementById('cm-whatif-box').open = true;
            this.setStatus('Copied into the what-if box');
        }
    },

    /** Operator-triggered: one maintenance window for the changed radios
     * (their RF peers included) plus a change-log entry per radio. */
    async schedule(plan, button) {
        const changed = plan.radios.filter(r => r.changed);
        const hours = Number(document.getElementById('cp-maint-hours').value) || 2;
        if (!changed.length) return;
        if (!window.confirm(`Create a ${hours} h maintenance window starting now for ${changed.length} radio(s) ` +
                `and add ${changed.length} change-log entries? No radio settings are changed.`)) return;
        button.disabled = true;
        try {
            const note = `Channel plan${this.result.site ? ' for ' + this.result.site : ''}: ` +
                changed.map(r => r.off ? `${r.name} off` : `${r.name} ${r.new.frequency}/${r.new.chanbw}`).join(', ');
            const resp = await fetch('/api/maintenance', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scope: 'nodes', nodes: changed.map(r => r.name),
                    duration_minutes: hours * 60, note: note.slice(0, 500), include_rf_peers: true })
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || 'maintenance window failed');
            let logged = 0;
            for (const r of changed) {
                const o = r.old;
                const res = await fetch('/api/changes', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ node_name: r.name, category: 'channel',
                        description: `Channel plan: ${o.frequency ?? '?'}/${o.chanbw ?? '?'} MHz (ch ${o.channel ?? '?'}) → ` +
                            (r.off ? 'radio off (no RF links)' : `${r.new.frequency}/${r.new.chanbw} MHz (ch ${r.new.channel})`) })
                });
                if (res.ok) logged++;
            }
            this.setStatus(`Maintenance window #${data.window?.id ?? ''} created; ${logged} change-log entries added`);
        } catch (error) {
            console.error('Schedule plan error:', error);
            this.setStatus(`Schedule failed: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    },

    setStatus(text) {
        const el = document.getElementById('cp-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => ChannelPlanner.init());
