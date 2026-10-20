/**
 * Node page "Power Advisor" (TODO #11): predicted SNR at every RF neighbour
 * after a TX power cut on this radio, from /api/power-advisor/<node>. All
 * thresholds and estimates live in power_advisor.py; this renders the cut
 * slider, the neighbour table, the near-far benefit and an SNR-vs-cut chart.
 * Opened prefilled from near-far findings via ?power_cut=N#power-advisor.
 */
const PowerAdvisor = {
    nodeName: window.NODE_NAME,
    base: null,      // response without cut_db: curve + suggested cut
    data: null,      // response for the chosen cut
    chart: null,
    timer: null,

    init() {
        if (!document.getElementById('pa-panel')) return;
        this.load();
    },

    async load() {
        const el = document.getElementById('pa-panel');
        try {
            const resp = await fetch(`/api/power-advisor/${encodeURIComponent(this.nodeName)}`);
            const data = await resp.json();
            if (!resp.ok) {
                el.innerHTML = `<p class="log-empty">${this.esc(data.error || 'Power advisor unavailable')}</p>`;
                return;
            }
            this.base = data;
            if (data.refused || !data.curve?.length) {
                this.data = data;
                this.render();
                return;
            }
            const asked = Number(new URLSearchParams(location.search).get('power_cut'));
            const cut = Number.isFinite(asked) && asked > 0 ? Math.min(asked, data.thresholds.max_cut_db) : data.suggested_cut_db;
            await this.fetchCut(cut);
            if (location.hash === '#power-advisor') document.getElementById('power-advisor')?.scrollIntoView();
        } catch (error) {
            console.error('Power advisor error:', error);
            el.innerHTML = '<p class="log-empty">Power advisor failed to load.</p>';
        }
    },

    async fetchCut(cut) {
        const resp = await fetch(`/api/power-advisor/${encodeURIComponent(this.nodeName)}?cut_db=${encodeURIComponent(cut)}`);
        const data = await resp.json();
        if (!resp.ok) return;
        this.data = data;
        this.render();
    },

    onSlide(value) {
        document.getElementById('pa-cut-value').textContent = `${value} dB`;
        clearTimeout(this.timer);
        this.timer = setTimeout(() => this.fetchCut(Number(value)), 150);
    },

    render() {
        const d = this.data;
        const el = document.getElementById('pa-panel');
        if (d.refused) {
            const alts = (d.alternatives || []).map(a =>
                `<li><a href="/nodes/${encodeURIComponent(a.node)}?power_cut=${encodeURIComponent(a.target_cut_db)}#power-advisor">${this.esc(a.node)}</a>:
                heard here at SNR ${this.esc(a.snr_at_us)} dB${a.signal_at_us != null ? `, ${this.esc(a.signal_at_us)} dBm` : ''}
                ${a.target_cut_db ? ` · about ${this.esc(a.target_cut_db)} dB above the 30 dB target` : ''}</li>`).join('');
            el.innerHTML = `<p class="node-warning">${this.esc(d.reason)}</p>
                ${alts ? `<p>Neighbours this radio hears (their power can be cut instead):</p><ul class="diag-evidence">${alts}</ul>` : ''}`;
            return;
        }
        if (!d.curve?.length) {
            el.innerHTML = `<p class="log-empty">${this.esc(d.reason || 'No RF neighbours hear this radio.')}</p>`;
            return;
        }
        const t = d.thresholds;
        const max = t.max_cut_db;
        const slider = document.getElementById('pa-cut');
        if (!slider) {
            el.innerHTML = `
                <div class="pa-controls">
                    <label>TX power cut <input type="range" id="pa-cut" min="0" max="${max}" step="1" value="${d.cut_db}"></label>
                    <strong id="pa-cut-value">${this.esc(d.cut_db)} dB</strong>
                    <span class="form-hint">Largest safe cut: <strong>${this.esc(this.base.suggested_cut_db)} dB</strong> ·
                        comfortable: <strong>${this.esc(this.base.comfortable_cut_db)} dB</strong>
                        (weakest intended peer stays near ${this.esc(t.target_snr)} dB)</span>
                </div>
                <div id="pa-body"></div>
                <div class="pa-chart-wrap"><canvas id="pa-chart"></canvas></div>
                <p class="form-hint">"ok" = median SNR after the cut ≥ ${this.esc(t.min_snr)} + ${this.esc(t.margin_db)} dB margin and the
                    fade level (10th percentile) ≥ ${this.esc(t.min_snr)} dB (AREDN LQM's default minimum SNR); "marginal" = median still
                    ≥ ${this.esc(t.min_snr)} dB; "lost" = below it, where LQM would block the link. Bitrates are estimates
                    (measured rate scaled by an 802.11ac MCS table). ${(d.notes || []).map(n => this.esc(n)).join(' ')}
                    Data as of ${this.esc(d.as_of)}, last ${this.esc(d.hours)} h.</p>`;
            document.getElementById('pa-cut').addEventListener('input', e => this.onSlide(e.target.value));
            this.renderChart();
        }
        document.getElementById('pa-body').innerHTML = this.renderTable() + this.renderNearFar();
    },

    renderTable() {
        const rows = this.data.neighbours.map(n => {
            const rate = n.rate != null
                ? `${this.esc(n.rate)} → ${n.rate_after != null ? this.esc(n.rate_after) : '?'} Mbps` : '-';
            return `<tr class="pa-${this.esc(n.status)}">
                <td><a href="/nodes/${encodeURIComponent(n.node)}">${this.esc(n.node)}</a>
                    ${n.intended ? '' : ' <small>(not an intended peer)</small>'}</td>
                <td>${n.snr != null ? this.esc(n.snr) : '-'} <small>fade ${n.snr_fade != null ? this.esc(n.snr_fade) : '-'}</small></td>
                <td><strong>${n.snr_after != null ? this.esc(n.snr_after) : '-'}</strong>
                    <small>fade ${n.fade_after != null ? this.esc(n.fade_after) : '-'}</small></td>
                <td><span class="pa-badge pa-badge-${this.esc(n.status)}">${this.esc(n.status)}</span></td>
                <td>${rate}</td>
                <td><small>${this.esc(n.samples)} samples · ${this.esc(n.source)}</small></td></tr>`;
        }).join('');
        return `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Neighbour hearing this radio</th><th>SNR now (dB)</th><th>After cut (dB)</th>
            <th>Status</th><th>Est. bitrate</th><th>Data</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    },

    renderNearFar() {
        const nf = this.data.near_far || [];
        if (!nf.length) return '<p class="form-hint">No receiver hears this radio loudly enough to count as near-far.</p>';
        return `<h3 class="subsection-heading">Near-far benefit</h3><ul class="diag-evidence">${nf.map(x => `<li>
            <strong>${this.esc(x.receiver)}</strong> hears this radio at ${this.esc(x.snr)} dB${x.signal != null ? ` (${this.esc(x.signal)} dBm)` : ''}
            → ${this.esc(x.snr_after)} dB${x.signal_after != null ? ` (${this.esc(x.signal_after)} dBm)` : ''}
            ${x.hot_after ? '· still loud' : '· no longer loud'}
            ${x.weakest ? `· gap to its weakest neighbour ${this.esc(x.weakest.node)} ${this.esc(x.gap_db)} → ${this.esc(x.gap_after_db)} dB` : '· it has no other RF neighbours'}
        </li>`).join('')}</ul>`;
    },

    renderChart() {
        const canvas = document.getElementById('pa-chart');
        if (!canvas || typeof Chart === 'undefined') return;
        const curve = this.base.curve;
        const names = Object.keys(curve[0].snr);
        const colors = ['#2e86c1', '#c0392b', '#27ae60', '#8e44ad', '#d68910', '#16a085', '#7f8c8d'];
        const t = this.base.thresholds;
        const flat = (y, label, color) => ({ label, data: curve.map(c => ({ x: c.cut_db, y })), borderColor: color,
            borderDash: [6, 4], pointRadius: 0, borderWidth: 1 });
        if (this.chart) this.chart.destroy();
        this.chart = new Chart(canvas, {
            type: 'line',
            data: { datasets: [
                ...names.map((n, i) => ({ label: n, data: curve.map(c => ({ x: c.cut_db, y: c.snr[n] })),
                    borderColor: colors[i % colors.length], pointRadius: 0, borderWidth: 2 })),
                flat(t.min_snr + t.margin_db, `ok (${t.min_snr + t.margin_db} dB)`, '#27ae60'),
                flat(t.min_snr, `LQM minimum (${t.min_snr} dB)`, '#c0392b')
            ] },
            options: { responsive: true, maintainAspectRatio: false, animation: false,
                scales: { x: { type: 'linear', min: 0, max: t.max_cut_db, title: { display: true, text: 'TX power cut (dB)' } },
                          y: { title: { display: true, text: 'Predicted SNR at neighbour (dB)' } } } }
        });
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => PowerAdvisor.init());
