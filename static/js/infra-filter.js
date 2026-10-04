/**
 * Infrastructure tag filter (Permanent / Event), shared by every page.
 *
 * Tags are independent flags on a node (is_permanent, is_event), like
 * is_selected. Filter values:
 *   all             - no filtering
 *   permanent       - tagged Permanent
 *   event           - tagged Event
 *   permanent-event - tagged Permanent or Event (any tagged infrastructure)
 *   untagged        - neither tag
 * A link matches when either endpoint matches, like the Selected filters.
 */
const InfraFilter = {
    matchesFlags(isPermanent, isEvent, value) {
        switch (value) {
            case 'permanent': return isPermanent;
            case 'event': return isEvent;
            case 'permanent-event': return isPermanent || isEvent;
            case 'untagged': return !isPermanent && !isEvent;
            default: return true;
        }
    },

    matchesNode(node, value) {
        return this.matchesFlags(!!(node && node.is_permanent), !!(node && node.is_event), value);
    },

    // Map of node name -> node, from graph nodes (`id`) or database nodes
    // (`name`). Names missing from the map count as untagged.
    lookup(nodes) {
        const map = new Map();
        for (const node of nodes || []) {
            const name = node.id || node.name;
            if (name) map.set(name, node);
        }
        return map;
    },

    nameMatches(lookup, name, value) {
        if (!value || value === 'all') return true;
        return this.matchesNode(lookup.get(name), value);
    },

    linkMatches(lookup, source, target, value) {
        if (!value || value === 'all') return true;
        return this.nameMatches(lookup, source, value) || this.nameMatches(lookup, target, value);
    }
};
