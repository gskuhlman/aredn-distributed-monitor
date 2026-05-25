/**
 * AREDN Network Monitor - Network Visualization
 * Uses vis.js for interactive network graph
 */

// Global state
let network = null;
let nodesDataset = null;
let edgesDataset = null;
let socket = null;
let selectedNode = null;
let knownNodes = new Set();  // Track known nodes for drop detection
let isInitialLoad = true;    // Track if this is the first load (for physics)
let droppedNodes = new Map(); // Track dropped nodes with timestamp: {nodeId: {timestamp, nodeData}}
let isPinging = false;       // Track if continuous ping is active
let pingTargetNode = null;   // Track which node is being pinged
let pingIntervalId = null;   // Interval ID for REST API ping

// How long to keep dropped nodes visible (15 minutes in milliseconds)
const DROPPED_NODE_VISIBILITY_MS = 15 * 60 * 1000;

// LocalStorage key for node positions
const POSITIONS_STORAGE_KEY = 'aredn_node_positions';

/**
 * Save node positions to localStorage
 */
function saveNodePositions() {
    const positions = {};
    nodesDataset.forEach(node => {
        if (node.x !== undefined && node.y !== undefined) {
            positions[node.id] = { x: node.x, y: node.y };
        }
    });
    localStorage.setItem(POSITIONS_STORAGE_KEY, JSON.stringify(positions));
}

/**
 * Load node positions from localStorage
 */
function loadNodePositions() {
    try {
        const stored = localStorage.getItem(POSITIONS_STORAGE_KEY);
        return stored ? JSON.parse(stored) : {};
    } catch (e) {
        console.error('Error loading positions:', e);
        return {};
    }
}

// DOM elements
const networkContainer = document.getElementById('network-container');
const sidePanel = document.getElementById('side-panel');
const settingsPanel = document.getElementById('settings-panel');
const logPanel = document.getElementById('log-panel');
const panelTitle = document.getElementById('panel-title');
const panelContent = document.getElementById('panel-content');
const logContent = document.getElementById('log-content');
const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');

// Event log state
let eventLog = [];

// Countdown timer state
let lastScanTime = null;
let pollInterval = 30;  // Default poll interval in seconds
let countdownInterval = null;

// Node colors
const NODE_COLORS = {
    normal: {
        background: '#97C2FC',
        border: '#2B7CE9',
        highlight: { background: '#D2E5FF', border: '#2B7CE9' }
    },
    firmwareMismatch: {
        background: '#FFA500',
        border: '#FF6600',
        highlight: { background: '#FFD280', border: '#FF6600' }
    },
    supernode: {
        background: '#9B59B6',
        border: '#8E44AD',
        highlight: { background: '#D7BDE2', border: '#8E44AD' }
    },
    dropped: {
        background: '#CCCCCC',
        border: '#e74c3c',
        highlight: { background: '#DDDDDD', border: '#e74c3c' }
    },
    inactive: {
        background: '#E0E0E0',
        border: '#999999',
        highlight: { background: '#EEEEEE', border: '#999999' }
    }
};

// vis.js network options
const networkOptions = {
    nodes: {
        shape: 'dot',
        size: 25,
        font: {
            size: 12,
            color: '#333',
            multi: 'html'
        },
        borderWidth: 2,
        shadow: true,
        color: NODE_COLORS.normal,
        fixed: {
            x: false,
            y: false
        }
    },
    edges: {
        width: 2,
        smooth: {
            type: 'continuous',
            roundness: 0.5
        },
        shadow: true
    },
    physics: {
        enabled: true,
        barnesHut: {
            gravitationalConstant: -3000,
            centralGravity: 0.3,
            springLength: 200,
            springConstant: 0.04,
            damping: 0.09,
            avoidOverlap: 0.5
        },
        stabilization: {
            enabled: true,
            iterations: 150,
            updateInterval: 25
        }
    },
    interaction: {
        hover: true,
        tooltipDelay: 200,
        zoomView: true,
        dragNodes: true
    }
};

/**
 * Initialize the network visualization
 */
function initNetwork() {
    nodesDataset = new vis.DataSet([]);
    edgesDataset = new vis.DataSet([]);

    const data = {
        nodes: nodesDataset,
        edges: edgesDataset
    };

    network = new vis.Network(networkContainer, data, networkOptions);

    // Event handlers
    network.on('click', handleNetworkClick);
    network.on('hoverNode', handleNodeHover);
    network.on('blurNode', handleNodeBlur);

    // Disable physics after initial stabilization and save positions
    network.on('stabilized', function() {
        if (isInitialLoad) {
            console.log('Initial layout stabilized, disabling physics');
            network.setOptions({ physics: { enabled: false } });
            isInitialLoad = false;

            // Save all positions after initial layout
            const allPositions = network.getPositions();
            for (const nodeId in allPositions) {
                nodesDataset.update({
                    id: nodeId,
                    x: allPositions[nodeId].x,
                    y: allPositions[nodeId].y
                });
            }
            saveNodePositions();
        }
    });

    // Temporarily unfix node when dragging starts so it can be moved
    network.on('dragStart', function(params) {
        if (params.nodes.length > 0) {
            const nodeId = params.nodes[0];
            nodesDataset.update({
                id: nodeId,
                fixed: { x: false, y: false }
            });
        }
    });

    // Save node position after drag and fix it in place
    network.on('dragEnd', function(params) {
        if (params.nodes.length > 0) {
            const nodeId = params.nodes[0];
            const positions = network.getPositions([nodeId]);

            // Update node with position AND fixed property to prevent movement
            nodesDataset.update({
                id: nodeId,
                x: positions[nodeId].x,
                y: positions[nodeId].y,
                fixed: { x: true, y: true }
            });

            // Save to localStorage
            saveNodePositions();
        }
    });
}

/**
 * Handle click on network
 */
function handleNetworkClick(params) {
    if (params.nodes.length > 0) {
        const nodeId = params.nodes[0];
        showNodeDetails(nodeId);
    } else {
        hidePanel();
    }
}

/**
 * Handle node hover
 */
function handleNodeHover(params) {
    networkContainer.style.cursor = 'pointer';
}

/**
 * Handle node blur (mouse leave)
 */
function handleNodeBlur(params) {
    networkContainer.style.cursor = 'default';
}

/**
 * Show node details in side panel
 */
async function showNodeDetails(nodeId) {
    // Stop any active ping if switching to a different node
    if (isPinging && pingTargetNode !== nodeId) {
        stopNodePing();
    }

    selectedNode = nodeId;
    panelTitle.textContent = nodeId;

    try {
        const response = await fetch(`/api/node/${encodeURIComponent(nodeId)}`);
        if (!response.ok) throw new Error('Node not found');

        const data = await response.json();
        renderNodeDetails(data);
        sidePanel.classList.remove('hidden');

    } catch (error) {
        console.error('Error fetching node details:', error);
        panelContent.innerHTML = '<p class="error">Failed to load node details</p>';
        sidePanel.classList.remove('hidden');
    }
}

/**
 * Render node details in panel
 */
function renderNodeDetails(data) {
    const node = data.node;
    const services = data.services || [];
    const links = data.links || [];

    // Determine if this is the currently pinging node
    const isCurrentlyPinging = isPinging && pingTargetNode === node.name;

    let html = `
        <div class="node-info">
            <h3>Node Information</h3>
            <table class="info-table">
                <tr><td>Name:</td><td>${node.name}</td></tr>
                <tr><td>IP:</td><td>${node.ip || 'N/A'}</td></tr>
                <tr><td>Model:</td><td>${node.model || 'Unknown'}</td></tr>
                <tr><td>Firmware:</td><td>${node.firmware_version || 'Unknown'}</td></tr>
                <tr><td>Description:</td><td>${node.description || 'N/A'}</td></tr>
                <tr><td>First Seen:</td><td>${formatDate(node.first_seen)}</td></tr>
                <tr><td>Last Seen:</td><td>${formatDate(node.last_seen)}</td></tr>
            </table>
            <div class="node-panel-actions">
                <a class="btn btn-secondary" href="/nodes/${encodeURIComponent(node.name)}">Full Node Info</a>
            </div>
            <div class="node-ping-section">
                <button id="ping-btn" class="btn ${isCurrentlyPinging ? 'btn-danger' : 'btn-primary'}"
                        onclick="toggleNodePing('${node.name}')" ${!node.ip ? 'disabled' : ''}>
                    ${isCurrentlyPinging ? 'Stop Ping' : 'Ping'}
                </button>
                <div id="ping-output" class="ping-output ${isCurrentlyPinging ? '' : 'hidden'}"></div>
            </div>
        </div>
    `;

    if (node.lat && node.lon) {
        html += `
            <div class="node-location">
                <h3>Location</h3>
                <p>Lat: ${node.lat}, Lon: ${node.lon}</p>
            </div>
        `;
    }

    if (links.length > 0) {
        html += `
            <div class="node-links">
                <h3>Links (${links.length})</h3>
                <table class="info-table">
                    <tr><th>Node</th><th>Type</th><th>Quality</th><th>SNR</th><th>Actions</th></tr>
        `;

        for (const link of links) {
            const otherNode = link.source_node === node.name ? link.target_node : link.source_node;
            const qualityClass = getQualityClass(link.quality);
            html += `
                <tr>
                    <td><a href="#" onclick="showNodeDetails('${otherNode}'); return false;">${otherNode}</a></td>
                    <td>${link.link_type}</td>
                    <td class="${qualityClass}">${link.quality}%</td>
                    <td>${link.snr || 'N/A'}</td>
                    <td>
                        <button class="btn btn-small btn-secondary" onclick="runLinkedNodeTest('${node.name}', '${otherNode}', 'ping', this)">Ping</button>
                        <button class="btn btn-small btn-secondary" onclick="runLinkedNodeTest('${node.name}', '${otherNode}', 'iperf', this)">iPerf3</button>
                    </td>
                </tr>
            `;
        }

        html += '</table></div>';
    }

    // Add iPerf3 as a built-in service for all AREDN nodes
    const allServices = [...services];
    if (node.ip) {
        allServices.push({
            name: 'iPerf3 (built-in)',
            link: null,
            builtin: true
        });
    }

    if (allServices.length > 0) {
        html += `
            <div class="node-services">
                <h3>Services (${allServices.length})</h3>
                <ul class="services-list">
        `;

        for (const service of allServices) {
            const icon = getServiceIcon(service.name);
            const iconHtml = icon ? `<span class="service-icon">${icon}</span> ` : '';
            if (service.link) {
                html += `<li>${iconHtml}<a href="${service.link}" target="_blank">${service.name}</a></li>`;
            } else if (service.builtin && service.name.includes('iPerf')) {
                html += `<li class="builtin-service">${iconHtml}${service.name}</li>`;
            } else {
                html += `<li>${iconHtml}${service.name}</li>`;
            }
        }

        html += '</ul></div>';
    }

    panelContent.innerHTML = html;
}

async function runLinkedNodeTest(source, target, testType, button) {
    const originalText = button ? button.textContent : '';
    if (button) {
        button.disabled = true;
        button.textContent = testType === 'ping' ? 'Pinging...' : 'Testing...';
    }

    try {
        const response = await fetch(
            `/api/rf-stats/test/${encodeURIComponent(source)}/${encodeURIComponent(target)}?type=${testType}`,
            { method: 'POST' }
        );
        const result = await response.json();
        if (result.success) {
            showToast('success', testType === 'ping' ? 'Ping Complete' : 'iPerf Complete', `${source} -> ${target}`);
        } else {
            showToast('error', 'Test Failed', result.error || 'Unknown error');
        }
    } catch (error) {
        console.error('Linked node test failed:', error);
        showToast('error', 'Test Failed', 'Network error');
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = originalText;
        }
    }
}

/**
 * Get CSS class for quality value
 */
function getQualityClass(quality) {
    if (quality > 70) return 'quality-good';
    if (quality > 40) return 'quality-poor';
    return 'quality-bad';
}

/**
 * Get service icon based on service name
 */
function getServiceIcon(serviceName) {
    const name = serviceName.toLowerCase();

    // Phone/VOIP services
    if (name.includes('phone') || name.includes('voip') || name.includes('sip') ||
        name.includes('x1') || name.includes('extension') || name.includes('direct ip')) {
        return '&#128222;';  // Phone icon
    }

    // MeshChat services
    if (name.includes('meshchat') || name.includes('chat')) {
        return '&#128172;';  // Chat bubble icon
    }

    // PBX services
    if (name.includes('pbx') || name.includes('asterisk') || name.includes('freepbx')) {
        return '&#9742;';  // Telephone icon
    }

    // Camera/Video services
    if (name.includes('camera') || name.includes('cam') || name.includes('video') ||
        name.includes('stream') || name.includes('webcam')) {
        return '&#127909;';  // Camera icon
    }

    // Weather services
    if (name.includes('weather') || name.includes('weewx')) {
        return '&#127780;';  // Sun behind cloud
    }

    // Winlink services
    if (name.includes('winlink')) {
        return '&#9993;';  // Envelope icon
    }

    // iPerf3 service (built into AREDN nodes)
    if (name.includes('iperf')) {
        return '&#128200;';  // Chart icon (speed test)
    }

    return null;  // No icon
}

/**
 * Format date string
 */
function formatDate(dateStr) {
    if (!dateStr) return 'N/A';
    const date = new Date(dateStr);
    return date.toLocaleString();
}

/**
 * Show a toast notification
 */
function showToast(type, title, message, duration = 10000) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const icons = {
        warning: '&#9888;',    // Warning triangle
        error: '&#10060;',     // Red X
        info: '&#8505;',       // Info symbol
        success: '&#10004;'    // Checkmark
    };

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <div class="toast-content">
            <div class="toast-title">${title}</div>
            <div class="toast-message">${message}</div>
        </div>
        <button class="toast-close" onclick="this.parentElement.remove()">&times;</button>
    `;

    container.appendChild(toast);

    // Auto-remove after duration
    if (duration > 0) {
        setTimeout(() => {
            toast.style.animation = 'fadeOut 0.3s ease';
            setTimeout(() => toast.remove(), 300);
        }, duration);
    }
}

/**
 * Hide the side panel
 */
function hidePanel() {
    // Stop any active ping when closing panel
    if (isPinging) {
        stopNodePing();
    }
    sidePanel.classList.add('hidden');
    selectedNode = null;
}

/**
 * Toggle continuous ping to a node
 */
function toggleNodePing(nodeName) {
    if (isPinging && pingTargetNode === nodeName) {
        stopNodePing();
    } else {
        startNodePing(nodeName);
    }
}

/**
 * Start continuous ping to a node using REST API
 */
function startNodePing(nodeName) {
    // Stop any existing ping first
    if (pingIntervalId) {
        clearInterval(pingIntervalId);
        pingIntervalId = null;
    }

    isPinging = true;
    pingTargetNode = nodeName;

    // Update button state
    const pingBtn = document.getElementById('ping-btn');
    if (pingBtn) {
        pingBtn.textContent = 'Stop Ping';
        pingBtn.classList.remove('btn-primary');
        pingBtn.classList.add('btn-danger');
    }

    // Show and clear output area
    const pingOutput = document.getElementById('ping-output');
    if (pingOutput) {
        pingOutput.classList.remove('hidden');
        pingOutput.innerHTML = '<div class="ping-line ping-info">Starting ping...</div>';
    }

    // Function to perform a single ping via REST API
    const doPing = async () => {
        if (!isPinging || pingTargetNode !== nodeName) {
            return;
        }

        try {
            const response = await fetch(`/api/ping/${encodeURIComponent(nodeName)}`, {
                method: 'POST'
            });
            const data = await response.json();

            if (!isPinging || pingTargetNode !== nodeName) {
                return; // Stopped while waiting for response
            }

            handlePingResult(data);
        } catch (error) {
            console.error('Ping error:', error);
            handlePingResult({
                success: false,
                ip: nodeName,
                error: error.message
            });
        }
    };

    // Start immediate ping, then repeat every 1 second
    doPing();
    pingIntervalId = setInterval(doPing, 1000);
}

/**
 * Stop continuous ping
 */
function stopNodePing() {
    // Clear the ping interval
    if (pingIntervalId) {
        clearInterval(pingIntervalId);
        pingIntervalId = null;
    }

    isPinging = false;
    pingTargetNode = null;

    // Update button state
    const pingBtn = document.getElementById('ping-btn');
    if (pingBtn) {
        pingBtn.textContent = 'Ping';
        pingBtn.classList.remove('btn-danger');
        pingBtn.classList.add('btn-primary');
    }

    // Add stopped message to output
    const pingOutput = document.getElementById('ping-output');
    if (pingOutput) {
        const stopLine = document.createElement('div');
        stopLine.className = 'ping-line ping-info';
        stopLine.textContent = '--- Ping stopped ---';
        pingOutput.appendChild(stopLine);
    }
}

/**
 * Handle ping result from server
 */
function handlePingResult(data) {
    const pingOutput = document.getElementById('ping-output');
    if (!pingOutput) return;

    const line = document.createElement('div');
    line.className = 'ping-line';

    if (data.success) {
        line.classList.add('ping-success');
        line.textContent = `Reply from ${data.ip}: time=${data.time}ms`;
    } else {
        line.classList.add('ping-fail');
        line.textContent = `Request timed out (${data.ip})`;
    }

    pingOutput.appendChild(line);

    // Keep only last 50 lines
    while (pingOutput.children.length > 50) {
        pingOutput.removeChild(pingOutput.firstChild);
    }

    // Auto-scroll to bottom
    pingOutput.scrollTop = pingOutput.scrollHeight;
}

/**
 * Update network with new data
 */
function updateNetwork(data) {
    if (!data || !data.nodes || !data.edges) return;

    const currentNodeIds = nodesDataset.getIds();
    const newNodeIds = data.nodes.map(n => n.id);
    const savedPositions = loadNodePositions();
    const now = Date.now();

    // Check for nodes that came back online (were dropped but now active)
    for (const node of data.nodes) {
        if (droppedNodes.has(node.id)) {
            console.log(`Node ${node.id} came back online`);
            droppedNodes.delete(node.id);
        }
    }

    // Detect newly dropped nodes (were known but no longer in the network)
    const newlyDroppedNodes = [];
    for (const nodeId of knownNodes) {
        if (!newNodeIds.includes(nodeId) && !droppedNodes.has(nodeId)) {
            newlyDroppedNodes.push(nodeId);
        }
    }

    // Mark newly dropped nodes and keep them visible
    for (const nodeId of newlyDroppedNodes) {
        showToast('warning', 'Node Dropped', `${nodeId} is no longer responding`);

        // Get existing node data to preserve position
        const existingNode = nodesDataset.get(nodeId);
        if (existingNode) {
            droppedNodes.set(nodeId, {
                timestamp: now,
                nodeData: existingNode
            });

            // Update node appearance to show it's dropped
            nodesDataset.update({
                id: nodeId,
                color: NODE_COLORS.dropped,
                label: existingNode.label + '\n(offline)',
                opacity: 0.6
            });
        }
        knownNodes.delete(nodeId);
    }

    // Remove expired dropped nodes (older than 15 minutes)
    for (const [nodeId, data] of droppedNodes.entries()) {
        if (now - data.timestamp > DROPPED_NODE_VISIBILITY_MS) {
            console.log(`Removing expired dropped node: ${nodeId}`);
            droppedNodes.delete(nodeId);
            nodesDataset.remove(nodeId);
        }
    }

    // Detect new nodes
    const newNodes = [];
    for (const nodeId of newNodeIds) {
        if (!knownNodes.has(nodeId)) {
            newNodes.push(nodeId);
            knownNodes.add(nodeId);
        }
    }

    // Show info for new nodes (after initial load)
    if (currentNodeIds.length > 0) {
        for (const nodeId of newNodes) {
            showToast('success', 'Node Discovered', `${nodeId} joined the network`);
        }
    } else {
        // Initial load - just add all to known nodes
        for (const nodeId of newNodeIds) {
            knownNodes.add(nodeId);
        }
    }

    // Only remove nodes that are not in the dropped list
    const nodesToRemove = currentNodeIds.filter(id =>
        !newNodeIds.includes(id) && !droppedNodes.has(id)
    );
    nodesDataset.remove(nodesToRemove);

    // Add or update nodes with appropriate coloring
    for (const node of data.nodes) {
        if (node.is_inactive) {
            // Inactive node (not polled recently but has links)
            node.color = NODE_COLORS.inactive;
            node.label = node.label + '\n(inactive)';
            node.opacity = 0.7;
        } else if (node.is_supernode) {
            node.color = NODE_COLORS.supernode;
            node.size = 35;  // Make supernodes larger
        } else if (node.firmware_mismatch) {
            node.color = NODE_COLORS.firmwareMismatch;
        } else {
            node.color = NODE_COLORS.normal;
        }

        if (currentNodeIds.includes(node.id)) {
            // Preserve existing position and fixed state if node was manually positioned
            const existingNode = nodesDataset.get(node.id);
            if (existingNode && existingNode.x !== undefined) {
                node.x = existingNode.x;
                node.y = existingNode.y;
                // Preserve fixed property if it was set
                if (existingNode.fixed) {
                    node.fixed = existingNode.fixed;
                }
            }
            nodesDataset.update(node);
        } else {
            // New node - check if we have a saved position from localStorage
            if (savedPositions[node.id]) {
                node.x = savedPositions[node.id].x;
                node.y = savedPositions[node.id].y;
                // Fix position since it was manually saved
                node.fixed = { x: true, y: true };
            }
            nodesDataset.add(node);
        }
    }

    // Update edges
    edgesDataset.clear();
    for (let i = 0; i < data.edges.length; i++) {
        const edge = data.edges[i];
        edge.id = `${edge.from}-${edge.to}`;
        edgesDataset.add(edge);
    }

    // Update footer stats
    updateStats(data.nodes.length, data.edges.length);
}

/**
 * Update statistics display
 */
function updateStats(nodeCount, linkCount, lastScan, maxDepth) {
    document.getElementById('footer-nodes').textContent = nodeCount;
    document.getElementById('footer-links').textContent = linkCount;
    document.getElementById('node-count').textContent = nodeCount;
    document.getElementById('link-count').textContent = linkCount;

    if (maxDepth !== undefined) {
        document.getElementById('footer-depth').textContent = maxDepth;
    }

    if (lastScan) {
        const formattedDate = formatDate(lastScan);
        document.getElementById('footer-last-scan').textContent = formattedDate;
        document.getElementById('last-scan').textContent = formattedDate;

        // Update countdown timer
        lastScanTime = new Date(lastScan);
        startCountdown();
    }
}

/**
 * Start or restart the countdown timer
 */
function startCountdown() {
    // Clear existing interval
    if (countdownInterval) {
        clearInterval(countdownInterval);
    }

    // Update immediately
    updateCountdown();

    // Then update every second
    countdownInterval = setInterval(updateCountdown, 1000);
}

/**
 * Update the countdown display
 */
function updateCountdown() {
    const countdownElement = document.getElementById('footer-countdown');

    if (!lastScanTime) {
        countdownElement.textContent = '--';
        return;
    }

    const now = new Date();
    const nextScan = new Date(lastScanTime.getTime() + (pollInterval * 1000));
    const remaining = Math.max(0, Math.floor((nextScan - now) / 1000));

    if (remaining <= 0) {
        countdownElement.textContent = 'Soon...';
    } else {
        const minutes = Math.floor(remaining / 60);
        const seconds = remaining % 60;
        if (minutes > 0) {
            countdownElement.textContent = `${minutes}m ${seconds}s`;
        } else {
            countdownElement.textContent = `${seconds}s`;
        }
    }
}

/**
 * Set connection status indicator
 */
function setStatus(connected, message) {
    statusIndicator.className = 'status-indicator ' + (connected ? 'connected' : 'disconnected');
    statusText.textContent = message || (connected ? 'Connected' : 'Disconnected');
}

/**
 * Initialize Socket.IO connection
 */
function initSocket() {
    socket = io();

    socket.on('connect', () => {
        console.log('Connected to server');
        setStatus(true, 'Connected');
    });

    socket.on('disconnect', () => {
        console.log('Disconnected from server');
        setStatus(false, 'Disconnected');
    });

    socket.on('status', (data) => {
        if (data.is_scanning) {
            setStatus(true, 'Scanning...');
        }
    });

    socket.on('network_update', (data) => {
        console.log('Received network update:', data);
        updateNetwork(data);
    });

    socket.on('scan_started', (data) => {
        setStatus(true, 'Scanning...');
        document.getElementById('scan-btn').disabled = true;
        // Show scanning in countdown
        document.getElementById('footer-countdown').textContent = 'Scanning...';
        // Update scan status in footer
        const scanStatus = document.getElementById('footer-scan-status');
        if (scanStatus) {
            scanStatus.textContent = 'Scanning network...';
            scanStatus.className = 'scan-status scanning';
        }
        if (countdownInterval) {
            clearInterval(countdownInterval);
            countdownInterval = null;
        }
    });

    socket.on('scan_complete', (data) => {
        console.log('Scan complete:', data);
        setStatus(true, 'Connected');
        document.getElementById('scan-btn').disabled = false;

        // Update scan status in footer
        const scanStatus = document.getElementById('footer-scan-status');
        if (scanStatus && data.result) {
            const nodes = data.result.nodes_found || 0;
            const links = data.result.links_found || 0;
            scanStatus.textContent = `Scan complete: ${nodes} nodes, ${links} links`;
            scanStatus.className = 'scan-status idle';
        }

        if (data.network) {
            updateNetwork(data.network);
        }

        if (data.result && data.result.timestamp) {
            updateStats(
                data.result.nodes_found,
                data.result.links_found,
                data.result.timestamp,
                data.result.max_depth_reached
            );
        }
    });

    socket.on('scan_error', (data) => {
        console.error('Scan error:', data);
        setStatus(true, 'Scan failed');
        document.getElementById('scan-btn').disabled = false;
        showToast('error', 'Scan Failed', data.error || 'An error occurred during the network scan');
    });

    // Handle starting node unreachable error
    socket.on('starting_node_error', (data) => {
        console.error('Starting node error:', data);
        showToast('error', 'Starting Node Unreachable',
            data.error || 'Cannot connect to the starting node. Check your network connection and settings.');
    });

    // Handle link drop notifications
    socket.on('link_dropped', (data) => {
        console.log('Link dropped:', data);
        showToast('warning', 'Link Dropped',
            `Connection lost between ${data.source} and ${data.target}`);
    });

    // Handle node inactive notifications
    socket.on('node_inactive', (data) => {
        console.log('Node inactive:', data);
        showToast('warning', 'Node Inactive',
            `${data.node} has gone offline`);
    });

    // Handle new events for real-time log updates
    socket.on('new_event', (data) => {
        console.log('New event:', data);
        addEventToLog(data);
    });

    // Setup RF Stats socket handlers if available
    if (typeof setupRFStatsSocketHandlers === 'function') {
        setupRFStatsSocketHandlers(socket);
    }

    // Note: Ping now uses REST API with setInterval instead of WebSocket
    // The ping_result, ping_started, ping_stopped, and ping_error handlers
    // have been removed as they are no longer needed.
}

/**
 * Request a network scan
 */
function requestScan() {
    if (socket && socket.connected) {
        socket.emit('request_scan');
    } else {
        // Fallback to REST API
        fetch('/api/scan', { method: 'POST' })
            .then(response => response.json())
            .then(data => console.log('Scan triggered:', data))
            .catch(error => console.error('Scan error:', error));
    }
}

/**
 * Load and display settings
 */
async function loadSettings() {
    try {
        const response = await fetch('/api/settings');
        const settings = await response.json();

        document.getElementById('starting-node').value = settings.starting_node || '';
        document.getElementById('show-tunnels').checked = settings.show_tunnels === 'true';
        document.getElementById('max-depth').value = settings.max_depth || 5;
        document.getElementById('auto-scan').checked = settings.auto_scan !== 'false';
        document.getElementById('poll-interval').value = settings.poll_interval || 30;
        document.getElementById('link-remove-after').value = settings.link_remove_after_minutes || 60;
        document.getElementById('new-node-days').value = settings.new_node_days || 30;
        document.getElementById('database-retention-days').value = settings.database_retention_days || 90;

        // Update poll interval for countdown
        if (settings.poll_interval) {
            pollInterval = settings.poll_interval;
        }

    } catch (error) {
        console.error('Error loading settings:', error);
    }
}

/**
 * Save settings
 */
async function saveSettings() {
    const startingNode = document.getElementById('starting-node').value.trim();
    const showTunnels = document.getElementById('show-tunnels').checked;
    const maxDepth = parseInt(document.getElementById('max-depth').value) || 5;
    const autoScan = document.getElementById('auto-scan').checked;
    const newPollInterval = parseInt(document.getElementById('poll-interval').value) || 30;
    const linkRemoveAfter = parseInt(document.getElementById('link-remove-after').value) || 60;
    const newNodeDays = parseInt(document.getElementById('new-node-days').value) || 30;
    const databaseRetentionDays = parseInt(document.getElementById('database-retention-days').value) || 90;

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                starting_node: startingNode,
                show_tunnels: showTunnels,
                max_depth: maxDepth,
                auto_scan: autoScan,
                poll_interval: newPollInterval,
                link_remove_after_minutes: linkRemoveAfter,
                new_node_days: newNodeDays,
                database_retention_days: databaseRetentionDays
            })
        });

        if (response.ok) {
            // Update local poll interval
            pollInterval = newPollInterval;
            alert('Settings saved successfully. A new scan will apply the changes.');
            settingsPanel.classList.add('hidden');
            // Trigger a new scan to apply the changes
            requestScan();
        } else {
            alert('Failed to save settings');
        }

    } catch (error) {
        console.error('Error saving settings:', error);
        alert('Failed to save settings');
    }
}

/**
 * Toggle settings panel
 */
function toggleSettings() {
    if (settingsPanel.classList.contains('hidden')) {
        loadSettings();
        settingsPanel.classList.remove('hidden');
        sidePanel.classList.add('hidden');
    } else {
        settingsPanel.classList.add('hidden');
    }
}

/**
 * Event type icons and labels
 */
const EVENT_ICONS = {
    'node_discovered': { icon: '&#10133;', label: 'Node Discovered', class: 'event-info' },
    'node_offline': { icon: '&#10060;', label: 'Node Offline', class: 'event-warning' },
    'node_online': { icon: '&#10004;', label: 'Node Reconnected', class: 'event-success' },
    'link_new': { icon: '&#128279;', label: 'New Link', class: 'event-info' },
    'link_dropped': { icon: '&#128280;', label: 'Link Dropped', class: 'event-warning' },
    'link_removed': { icon: '&#128465;', label: 'Link Removed', class: 'event-info' },
    'link_restored': { icon: '&#128279;', label: 'Link Restored', class: 'event-success' },
    'frequency_change': { icon: '&#128246;', label: 'Frequency Change', class: 'event-warning' }
};

/**
 * Toggle event log panel
 */
function toggleLog() {
    if (logPanel.classList.contains('hidden')) {
        loadEventLog();
        logPanel.classList.remove('hidden');
        sidePanel.classList.add('hidden');
        settingsPanel.classList.add('hidden');
    } else {
        logPanel.classList.add('hidden');
    }
}

/**
 * Load event log from server
 */
async function loadEventLog() {
    try {
        const response = await fetch('/api/events?limit=200');
        const events = await response.json();
        eventLog = events;
        renderEventLog();
    } catch (error) {
        console.error('Error loading event log:', error);
    }
}

/**
 * Render event log with filters
 */
function renderEventLog() {
    const showNodes = document.getElementById('filter-nodes').checked;
    const showLinks = document.getElementById('filter-links').checked;
    const showFreq = document.getElementById('filter-freq').checked;
    const nodeSearch = (document.getElementById('log-node-search')?.value || '').toLowerCase();

    const nodeEvents = ['node_discovered', 'node_offline', 'node_online'];
    const linkEvents = ['link_new', 'link_dropped', 'link_removed', 'link_restored'];
    const freqEvents = ['frequency_change'];

    const filtered = eventLog.filter(event => {
        if (nodeEvents.includes(event.event_type) && !showNodes) return false;
        if (linkEvents.includes(event.event_type) && !showLinks) return false;
        if (freqEvents.includes(event.event_type) && !showFreq) return false;
        if (nodeSearch) {
            const haystack = `${event.node_name || ''} ${event.details || ''} ${event.event_type || ''}`.toLowerCase();
            if (!haystack.includes(nodeSearch)) return false;
        }
        return true;
    });

    if (filtered.length === 0) {
        logContent.innerHTML = '<p class="log-empty">No events to display</p>';
        return;
    }

    let html = '';
    for (const event of filtered) {
        const eventInfo = EVENT_ICONS[event.event_type] || { icon: '&#8226;', label: event.event_type, class: 'event-info' };
        const timestamp = formatLogTimestamp(event.timestamp);

        html += `
            <div class="log-entry ${eventInfo.class}">
                <span class="log-icon">${eventInfo.icon}</span>
                <div class="log-details">
                    <div class="log-header">
                        <span class="log-type">${eventInfo.label}</span>
                        <span class="log-time">${timestamp}</span>
                    </div>
                    <div class="log-node">${event.node_name || ''}</div>
                    <div class="log-message">${event.details || ''}</div>
                </div>
            </div>
        `;
    }

    logContent.innerHTML = html;
}

/**
 * Format timestamp for log display
 */
function formatLogTimestamp(timestamp) {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    return date.toLocaleString();
}

/**
 * Add new event to log (called from WebSocket)
 */
function addEventToLog(event) {
    console.log('Adding event to log:', event);

    // Add to beginning of array
    eventLog.unshift({
        event_type: event.type,
        node_name: event.node,
        details: event.details,
        severity: event.severity,
        timestamp: new Date().toISOString()
    });

    // Keep only last 200 events in memory
    if (eventLog.length > 200) {
        eventLog.pop();
    }

    // Re-render if log panel is visible
    if (logPanel && !logPanel.classList.contains('hidden')) {
        renderEventLog();
        // Scroll to top to show new event
        if (logContent) {
            logContent.scrollTop = 0;
        }
    }
}

/**
 * Clear the displayed event log and remove dropped nodes from display
 * Note: This does NOT clear events from the database
 */
function clearDisplayedLog() {
    // Clear the in-memory event log
    eventLog = [];

    // Remove all dropped nodes from the network visualization
    for (const [nodeId, data] of droppedNodes.entries()) {
        console.log(`Clearing dropped node from display: ${nodeId}`);
        nodesDataset.remove(nodeId);
    }
    droppedNodes.clear();

    // Re-render the empty log
    renderEventLog();

    showToast('info', 'Log Cleared', 'Displayed events and offline nodes have been cleared');
}

/**
 * Initialize event listeners
 */
function initEventListeners() {
    // Scan button
    document.getElementById('scan-btn').addEventListener('click', requestScan);

    // Log button
    document.getElementById('log-btn').addEventListener('click', toggleLog);

    // Settings button
    document.getElementById('settings-btn').addEventListener('click', toggleSettings);

    // Close panel buttons
    document.getElementById('close-panel').addEventListener('click', hidePanel);
    document.getElementById('close-settings').addEventListener('click', () => {
        settingsPanel.classList.add('hidden');
    });
    document.getElementById('close-log').addEventListener('click', () => {
        logPanel.classList.add('hidden');
    });

    // Save settings button
    document.getElementById('save-settings').addEventListener('click', saveSettings);

    // Reset positions button
    document.getElementById('reset-positions').addEventListener('click', resetNodePositions);

    // Log filter checkboxes
    document.getElementById('filter-nodes').addEventListener('change', renderEventLog);
    document.getElementById('filter-links').addEventListener('change', renderEventLog);
    document.getElementById('filter-freq').addEventListener('change', renderEventLog);
    document.getElementById('log-node-search').addEventListener('input', renderEventLog);

    // Clear log button
    document.getElementById('clear-log-btn').addEventListener('click', clearDisplayedLog);
}

/**
 * Reset all node positions and re-run physics layout
 */
function resetNodePositions() {
    if (!confirm('Reset all node positions? This will re-run the automatic layout.')) {
        return;
    }

    // Clear localStorage
    localStorage.removeItem(POSITIONS_STORAGE_KEY);

    // Clear fixed positions from all nodes
    nodesDataset.forEach(node => {
        nodesDataset.update({
            id: node.id,
            x: undefined,
            y: undefined,
            fixed: { x: false, y: false }
        });
    });

    // Re-enable physics temporarily to re-layout
    isInitialLoad = true;
    network.setOptions({ physics: { enabled: true } });
}

/**
 * Load initial data
 */
async function loadInitialData() {
    try {
        // Load settings first to get poll interval
        const settingsResponse = await fetch('/api/settings');
        const settings = await settingsResponse.json();
        if (settings.poll_interval) {
            pollInterval = settings.poll_interval;
        }

        const response = await fetch('/api/network');
        const data = await response.json();
        updateNetwork(data);

        // Also get status
        const statusResponse = await fetch('/api/status');
        const status = await statusResponse.json();
        const maxDepth = status.last_result ? status.last_result.max_depth_reached : 0;
        updateStats(status.node_count, status.link_count, status.last_scan, maxDepth);

    } catch (error) {
        console.error('Error loading initial data:', error);
    }
}

/**
 * Main initialization
 */
document.addEventListener('DOMContentLoaded', () => {
    console.log('Initializing AREDN Network Monitor');

    initNetwork();
    initSocket();
    initEventListeners();
    loadInitialData();
});
