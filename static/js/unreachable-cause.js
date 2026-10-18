/**
 * Shared block explaining why a node was unreachable (unreachable.py), shown
 * on the node page and the Nodes tab detail panel. `cause` is the
 * `unreachable_cause` field of /api/nodes/full and /api/nodes/detail.
 */
const UnreachableCause = {
    render(cause, esc) {
        if (!cause) return '';
        const evidence = (cause.evidence || []).length
            ? `<ul>${cause.evidence.map(e => `<li>${esc(e)}</li>`).join('')}</ul>` : '';
        const status = cause.explained
            ? 'Explained: not counted as unreachable in reports.'
            : 'Counted as unreachable in reports.';
        const hint = cause.can_exclude
            ? ' Excluding it from scanning (scan boundary "exclude") removes this noise.' : '';
        return `<div class="unreach-cause rpt-cause-${esc(cause.severity)}">
            <strong>Unreachable: ${esc(cause.cause_label)}</strong>
            <small>(${esc(cause.failed_polls)} failed poll(s), ${esc(cause.probe_fail)} failed probe(s);
            last answered ${esc(cause.last_success || 'never')})</small>
            <div>${esc(cause.detail)} ${esc(status)}${esc(hint)}</div>${evidence}
        </div>`;
    }
};
