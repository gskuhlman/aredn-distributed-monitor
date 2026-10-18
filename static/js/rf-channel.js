/**
 * Shared rendering of a node's RF channel (centre, width, low/high edge and
 * channel numbers) and of each connected peer's channel relation. The values
 * come from the server (infra_audit.rf_span_info / annotate_link_rf).
 */
const RfChannel = {
    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    mhz(v) {
        return Number.isInteger(v) ? String(v) : Number(v).toFixed(1).replace(/\.0$/, '');
    },

    /** "5905 MHz · 20 MHz wide (5895–5915 MHz)" */
    frequencyText(span, fallback) {
        if (!span) return this.esc(fallback || 'N/A');
        const width = span.chanbw ? `${this.mhz(span.chanbw)} MHz wide` : 'width unknown, 20 MHz assumed';
        const off = span.rf_status === 'off' ? ' · <em>radio off</em>' : '';
        return `${this.mhz(span.frequency)} MHz · ${width} (${this.mhz(span.low)}–${this.mhz(span.high)} MHz)${off}`;
    },

    /** "181 (spans ch 179–183)" */
    channelText(span, fallback) {
        if (!span || span.channel == null) return this.esc(fallback || 'N/A');
        const edges = span.low_channel != null && span.high_channel != null
            ? ` (spans ch ${span.low_channel}–${span.high_channel})` : '';
        return `${this.esc(span.channel)}${edges}`;
    },

    /** Compact cell for a peer in a links table. */
    peerCell(rf) {
        if (!rf) return 'N/A';
        const ch = rf.channel != null ? `ch ${this.esc(rf.channel)} · ` : '';
        const width = rf.chanbw ? `${this.mhz(rf.chanbw)} MHz` : '20? MHz';
        return `${ch}${this.mhz(rf.frequency)} MHz / ${width}<br><small>${this.mhz(rf.low)}–${this.mhz(rf.high)} MHz` +
            (rf.low_channel != null ? ` · ch ${rf.low_channel}–${rf.high_channel}` : '') + '</small>';
    },

    relationCell(rf) {
        if (!rf || !rf.relation || rf.relation === 'unknown') return '';
        const gap = rf.gap_mhz;
        const note = rf.relation === 'same' ? 'same channel'
            : gap < 0 ? `${this.mhz(-gap)} MHz overlap`
            : `${this.mhz(gap)} MHz gap`;
        return `<span class="rel-badge rel-${rf.relation}">${rf.relation}</span> <small>${note}</small>`;
    },

    rowClass(rf) {
        return rf && ['overlap', 'adjacent', 'near'].includes(rf.relation) ? `rel-row rel-row-${rf.relation}` : '';
    },

    /** Warning line listing peers on overlapping/adjacent channels. */
    summary(links, peerOf) {
        const hits = links.filter(l => l.peer_rf && ['overlap', 'adjacent'].includes(l.peer_rf.relation));
        if (!hits.length) return '';
        const names = hits.map(l => `${this.esc(peerOf(l))} (${l.peer_rf.relation})`).join(', ');
        return `<p class="node-warning">${hits.length} connected node(s) on overlapping or adjacent channels: ${names}.
            Overlap = channels share spectrum; adjacent = less than 10 MHz between channel edges.</p>`;
    }
};
