/**
 * Diagnostics page: every check on one page. Nothing is copied; while the
 * page is open the sections themselves are moved in (each leaves a
 * placeholder behind) from Status (Mesh Health, Link Watch, Node Uptime,
 * New Nodes, Service Checks), Radio & RF, Routing, Setup, the Link Stability
 * part of Links and Channel Planning > WiFi Survey. Opening any other page
 * puts them back first, so those pages and every element id keep working.
 *
 *   DiagnosticsPage.shows(tab, sub)   true while that page's sub-page is on Diagnostics
 *                                     (R7.onShow uses it to load sections shown here)
 */
const DiagnosticsPage = {
    GROUPS: [
        ['health', '#status-view [data-diag="health"]'],
        ['radio', '#radio-view .subpage'],
        ['routing', '#routing-view .subpage'],
        ['setup', '#setup-view .subpage'],
        ['stability', '#rf-stability'],
        ['wifi', '#planning-view .subpage[data-sub="wifi"]'],
    ],
    moved: [],      // [element, placeholder, tab, sub]

    init() {
        document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => btn.addEventListener('click', () => {
            if (btn.dataset.tab === 'diagnostics') this.open();
            else this.restore();
        }));
        document.querySelectorAll('#diagnostics-view .dg-jump-btn').forEach(btn => btn.addEventListener('click', () => {
            document.getElementById(`dg-${btn.dataset.dg}`)?.scrollIntoView({ block: 'start', behavior: 'smooth' });
        }));
    },

    gather() {
        if (this.moved.length) return;
        for (const [key, sel] of this.GROUPS) {
            const target = document.getElementById(`dg-${key}`);
            if (!target) continue;
            document.querySelectorAll(sel).forEach(el => {
                const tab = el.closest('.view-container')?.id.replace(/-view$/, '');
                const ph = document.createComment(`diagnostics: ${key}`);
                el.before(ph);
                target.append(el);
                this.moved.push([el, ph, tab, el.dataset.sub || null]);
            });
        }
    },

    restore() {
        for (const [el, ph] of this.moved) ph.replaceWith(el);
        this.moved = [];
    },

    shows(tab, sub) {
        return !!sub && document.getElementById('diagnostics-view')?.classList.contains('active')
            && this.moved.some(m => m[2] === tab && m[3] === sub);
    },

    open() {
        this.gather();
        // What these sections' own pages load when they are shown.
        if (typeof DiagnosticsModule !== 'undefined') { DiagnosticsModule.init(); DiagnosticsModule.load({ health: true }); }
        if (typeof LinkWatch !== 'undefined') LinkWatch.init();
        if (typeof NodeUptime !== 'undefined') NodeUptime.load();
        if (typeof NewNodes !== 'undefined' && !NewNodes.loaded) { NewNodes.loaded = true; NewNodes.load(); }
        if (typeof ServiceChecks !== 'undefined' && !ServiceChecks.data) ServiceChecks.load();
        if (typeof InfraReview !== 'undefined') InfraReview.init();
        if (typeof WifiSurvey !== 'undefined' && !WifiSurvey.loaded) { WifiSurvey.loaded = true; WifiSurvey.load(); }
        // Sub-page loaders registered with R7.onShow (tunnels, degradation, noise…).
        for (const [, , tab, sub] of this.moved) {
            if (sub) document.dispatchEvent(new CustomEvent('subpage-shown', { detail: { tab, sub, diagnostics: true } }));
        }
    }
};

document.addEventListener('DOMContentLoaded', () => DiagnosticsPage.init());
