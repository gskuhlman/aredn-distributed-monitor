/**
 * Single Points of Failure section on the Diagnostics tab (spof.py): links
 * and nodes whose loss cuts nodes off from the starting node, ranked by nodes
 * behind x how weak the link is. "Internet down" leaves the tunnels out first.
 * "Show on map" draws the ranked links on the Channel Map (thicker = more
 * nodes behind, coloured by link quality).
 */
const SpofPanel = {
    initialized: false,
    data: null,
    overlay: null,

    init() {
        if (this.initialized) return;
        if (!document.getElementById('spof-report')) return;
        this.initialized = true;
        document.getElementById('spof-run')?.addEventListener('click', () => this.load());
        document.getElementById('spof-no-tunnels')?.addEventListener('change', () => this.load());
        document.getElementById('spof-map')?.addEventListener('click', () => this.showOnMap());
        document.getElementById('spof-report').addEventListener('click', (event) => {
            const btn = event.target.closest('button[data-spof]');
            if (!btn) return;
            const { spof, a, b } = btn.dataset;
            if (spof === 'diagnose') {
                document.querySelector('.tab-btn[data-tab="rf-stats"]')?.click();
                setTimeout(() => { if (typeof RFStats !== 'undefined') RFStats.selectLink(a, b); }, 300);
            } else if (spof === 'path' && typeof PathView !== 'undefined') {
                PathView.open('', b);
            } else if (spof === 'map') {
                this.showOnMap(this.data.links.filter(l => l.a === a && l.b === b));
            }
        });
    },

    async load() {
        this.init();
        const el = document.getElementById('spof-report');
        const noTunnels = document.getElementById('spof-no-tunnels').checked;
        this.setStatus('Loading...');
        try {
            const resp = await fetch(`/api/reports/spof?tunnels=${noTunnels ? 0 : 1}`);
            this.data = await resp.json();
            this.render();
            const c = this.data.counts || {};
            this.setStatus(this.data.error || `${c.links} link(s) and ${c.nodes} node(s) cut nodes off · `
                + `${this.data.nodes_in_scope} polled node(s) reachable from ${this.data.seed}`
                + (noTunnels ? ' without tunnels' : ''));
        } catch (error) {
            el.innerHTML = '<p class="log-empty">Failed to load.</p>';
            this.setStatus('Failed to load.');
        }
    },

    qColor(q, type) {
        if (type === 'DTD') return '#2874a6';
        if (type === 'TUNNEL') return '#8e44ad';
        if (q == null) return '#7f8c8d';
        if (q >= 80) return '#27ae60';
        if (q >= 50) return '#d4ac0d';
        return '#c0392b';
    },

    behind(list, n = 8) {
        const shown = list.slice(0, n).map(x => this.esc(x)).join(', ');
        return list.length > n
            ? `<details><summary>${shown}, … (${list.length})</summary>${list.map(x => this.esc(x)).join(', ')}</details>` : shown;
    },

    render() {
        const el = document.getElementById('spof-report');
        const d = this.data;
        if (d.error) {
            el.innerHTML = `<p class="log-empty">${this.esc(d.error)}</p>`;
            return;
        }
        const links = d.links.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Risk</th><th>Link</th><th>Type</th><th>Quality now / median</th><th>Nodes behind</th><th>Cut off</th><th></th></tr></thead>
            <tbody>${d.links.map(l => `<tr>
                <td><span class="hs-score" style="background:${l.risk >= 5 ? '#c0392b' : l.risk >= 1 ? '#e67e22' : '#95a5a6'}">${l.risk}</span></td>
                <td>${this.esc(l.a)} ↔ ${this.esc(l.b)}${l.infrastructure ? ' <small class="hs-infra">infra</small>' : ''}</td>
                <td>${this.esc(l.types.join(', '))}</td>
                <td>${l.type === 'RF' ? `<span style="color:${this.qColor(l.quality, 'RF')}">${l.quality ?? '?'}%</span> / ${l.median_quality ?? '?'}%` : '-'}</td>
                <td><strong>${l.behind}</strong>${l.behind_infra ? ` <small>(${l.behind_infra} infra)</small>` : ''}</td>
                <td class="spof-behind">${this.behind(l.nodes_behind)}</td>
                <td class="rw-actions">${l.type === 'RF' ? `<button class="btn btn-small btn-secondary" data-spof="diagnose" data-a="${this.esc(l.a)}" data-b="${this.esc(l.b)}">Diagnose</button>` : ''}
                    <button class="btn btn-small btn-secondary" data-spof="path" data-a="${this.esc(l.a)}" data-b="${this.esc(l.far_end)}">Path</button>
                    ${l.a_pos && l.b_pos ? `<button class="btn btn-small btn-secondary" data-spof="map" data-a="${this.esc(l.a)}" data-b="${this.esc(l.b)}">Map</button>` : ''}</td>
                </tr>`).join('')}</tbody></table></div>` : '<p class="log-empty">No single link cuts anything off.</p>';
        const nodes = d.nodes.length ? `<div class="table-scroll-wrapper"><table class="info-table diag-table">
            <thead><tr><th>Node</th><th>Nodes behind</th><th>Cut off if it fails</th></tr></thead>
            <tbody>${d.nodes.map(n => `<tr><td>${this.esc(n.name)}${n.infrastructure ? ' <small class="hs-infra">infra</small>' : ''}</td>
                <td><strong>${n.behind}</strong>${n.behind_infra ? ` <small>(${n.behind_infra} infra)</small>` : ''}</td>
                <td class="spof-behind">${this.behind(n.nodes_behind)}</td></tr>`).join('')}</tbody></table></div>`
            : '<p class="log-empty">No single node cuts anything off.</p>';
        el.innerHTML = `<h4>Links <small class="form-hint">risk = nodes behind × weakness (1 − quality for RF, 0.5 tunnel, 0.1 DtD)</small></h4>${links}
            <h4>Nodes</h4>${nodes}`;
    },

    async showOnMap(rows) {
        if (!this.data) await this.load();
        rows = rows || this.data.links;
        rows = rows.filter(l => l.a_pos && l.b_pos);
        if (!rows.length) {
            this.setStatus('None of these links have both ends located.');
            return;
        }
        document.querySelector('.tab-btn[data-tab="channel-map"]')?.click();
        for (let i = 0; i < 100 && !(typeof ChannelMap !== 'undefined' && ChannelMap.map); i++) {
            await new Promise(r => setTimeout(r, 100));
        }
        if (typeof ChannelMap === 'undefined' || !ChannelMap.map || typeof L === 'undefined') return;
        if (this.overlay) this.overlay.remove();
        this.overlay = L.layerGroup().addTo(ChannelMap.map);
        const max = Math.max(...rows.map(l => l.behind), 1);
        const pts = [];
        rows.forEach(l => {
            const weight = 3 + 9 * l.behind / max;
            L.polyline([l.a_pos, l.b_pos], { color: this.qColor(l.quality, l.type), weight, opacity: 0.85 })
                .bindPopup(`<strong>${this.esc(l.a)} ↔ ${this.esc(l.b)}</strong><br>${this.esc(l.types.join(', '))}
                    ${l.quality != null ? ` · quality ${l.quality}%` : ''}<br>${l.behind} node(s) behind
                    ${l.behind_infra ? ` (${l.behind_infra} infrastructure)` : ''}: ${this.esc(l.nodes_behind.slice(0, 12).join(', '))}
                    ${l.nodes_behind.length > 12 ? '…' : ''}<br>risk ${l.risk}`)
                .addTo(this.overlay);
            pts.push(l.a_pos, l.b_pos);
        });
        const clear = L.control({ position: 'topright' });
        clear.onAdd = () => {
            const div = L.DomUtil.create('div', 'leaflet-bar spof-clear');
            div.innerHTML = '<a href="#" title="Remove the single-point-of-failure overlay">✕ SPOF</a>';
            L.DomEvent.on(div, 'click', (e) => {
                L.DomEvent.preventDefault(e);
                this.overlay?.remove();
                this.overlay = null;
                clear.remove();
            });
            return div;
        };
        if (this.clearControl) this.clearControl.remove();
        this.clearControl = clear.addTo(ChannelMap.map);
        ChannelMap.map.fitBounds(pts, { padding: [40, 40], maxZoom: 13 });
    },

    setStatus(text) {
        const el = document.getElementById('spof-status');
        if (el) el.textContent = text;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

document.addEventListener('DOMContentLoaded', () => SpofPanel.init());
