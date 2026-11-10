# AREDN Network Monitor. Run with docker-compose.yml (host networking, so
# *.local.mesh names resolve through the workstation's AREDN DNS and pings,
# traceroutes and SSH leave from its mesh interface). See docs/DOCKER.md.
FROM python:3.12-slim

# ping / traceroute: rf_stats, collector_health, voip MTU checks
# openssh-client: ssh_collect / ssh_traffic (key login only)
# tzdata: every stored and shown time is local Central (local_time.py)
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping traceroute openssh-client tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Everything the app writes lives under /data (one volume to back up).
ENV PYTHONUNBUFFERED=1 \
    TZ=America/Chicago \
    DATABASE_PATH=/data/aredn_monitor.db \
    BACKUP_DIR=/data/backups \
    OFFLINE_MAP_DIR=/data/maps \
    SSH_KEY_PATH=/data/ssh/aredn_monitor_ed25519 \
    SSH_KNOWN_HOSTS=/data/ssh/known_hosts \
    CONNECTIVITY_LOG_PATH=/data/connectivity.log
VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
  CMD python -c "import urllib.request, os; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '5000'), timeout=8)"

CMD ["python", "app.py"]
