#!/bin/bash
set -euo pipefail
# AREDN Distributed Monitor Setup Script
# Run this to install system dependencies and start CouchDB

echo "=== AREDN Monitor Setup ==="

# Install iperf3
echo "Installing iperf3..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y iperf3
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y iperf3
elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y iperf3
else
    echo "WARNING: Could not install iperf3 automatically. Please install it manually."
fi

# Start CouchDB via Docker if available
if command -v docker >/dev/null 2>&1; then
    echo "Starting CouchDB container..."
    if ! docker ps --format '{{.Names}}' | grep -q '^couchdb$'; then
        docker run -d \
            --name couchdb \
            -p 5984:5984 \
            -e COUCHDB_USER=admin \
            -e COUCHDB_PASSWORD=password \
            -v couchdb_data:/opt/couchdb/data \
            --restart unless-stopped \
            couchdb:latest 2>/dev/null || {
                echo "WARNING: Failed to start CouchDB container. You may need to add your user to the 'docker' group, or start the Docker service."
                echo "  sudo usermod -aG docker \$USER"
                echo "  sudo systemctl start docker"
            }
    else
        echo "CouchDB container already running."
    fi
else
    echo "WARNING: Docker not found. CouchDB must be installed manually for distributed mode."
    echo "  Ubuntu/Debian: sudo apt-get install -y couchdb"
    echo "  Or install Docker: https://docs.docker.com/engine/install/"
fi

echo ""
echo "Setup complete. Next steps:"
echo "  1. Copy aredn-monitor.service to /etc/systemd/system/ and start it."
echo "  2. Or run: python app.py"
