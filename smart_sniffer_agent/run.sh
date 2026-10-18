#!/usr/bin/with-contenv bashio
# ==============================================================================
# SMART Sniffer Agent — App Startup Script
# ==============================================================================
set -e

APP_VERSION="0.3.1"

# ── Read configuration ───────────────────────────────────────────────────────
PORT=$(bashio::config 'port')
TOKEN=$(bashio::config 'token')
SCAN_INTERVAL=$(bashio::config 'scan_interval')
MOCK_PORT=$(bashio::config 'mock_port')
INGRESS_PORT=8099

# Replica files: the shipped ones are in the image, the user's own in /share
# so they survive updates and can be dropped in over Samba or SSH.
SHIPPED_REPLICAS="/opt/replicas"
USER_REPLICAS="/share/smart-sniffer/replicas"

# ── Validate configuration ───────────────────────────────────────────────────
# The replica agent always runs, so the two ports must always differ.
if [ "${PORT}" = "${MOCK_PORT}" ]; then
    bashio::log.fatal "Replica agent port (${MOCK_PORT}) cannot be the same as agent port (${PORT})"
    bashio::exit.nok
fi

# ── Preflight checks ────────────────────────────────────────────────────────
bashio::log.info "Running preflight checks..."

# Check smartctl exists
if ! command -v smartctl &> /dev/null; then
    bashio::log.warning "smartctl not found! SMART data collection will not work."
    bashio::log.warning "This should not happen — smartmontools is installed in the container image."
else
    # Check smartctl version (need 7.0+ for --json support)
    SMARTCTL_VERSION=$(smartctl --version | head -1 | sed -n 's/.*smartctl \([0-9]*\.[0-9]*\).*/\1/p' || echo "unknown")
    [ -z "${SMARTCTL_VERSION}" ] && SMARTCTL_VERSION="unknown"
    bashio::log.info "smartctl version: ${SMARTCTL_VERSION}"

    # Check smartctl permissions by doing a dry scan
    if smartctl --scan --json > /dev/null 2>&1; then
        DRIVE_COUNT=$(smartctl --scan --json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(len(data.get('devices', [])))
except:
    print(0)
" 2>/dev/null || echo "0")
        bashio::log.info "Drives detected by smartctl: ${DRIVE_COUNT}"

        # Try to actually read the first detected drive to check permissions
        if [ "${DRIVE_COUNT}" -gt 0 ] 2>/dev/null; then
            FIRST_DRIVE=$(smartctl --scan --json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    devs = data.get('devices', [])
    print(devs[0]['name'] if devs else '')
except:
    print('')
" 2>/dev/null || echo "")

            if [ -n "${FIRST_DRIVE}" ]; then
                SMART_OUTPUT=$(smartctl --json -i "${FIRST_DRIVE}" 2>&1 || true)
                if echo "${SMART_OUTPUT}" | grep -q "Operation not permitted"; then
                    bashio::log.warning "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    bashio::log.warning "DRIVE ACCESS BLOCKED"
                    bashio::log.warning "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    bashio::log.warning "smartctl cannot open ${FIRST_DRIVE}."
                    bashio::log.warning "SMART Sniffer needs direct hardware access"
                    bashio::log.warning "to read drive health data."
                    bashio::log.warning ""
                    bashio::log.warning "To fix: Go to Settings → Apps → SMART Sniffer"
                    bashio::log.warning "→ turn OFF 'Protection mode', then restart."
                    bashio::log.warning ""
                    bashio::log.warning "Why? Protection mode restricts hardware access."
                    bashio::log.warning "SMART monitoring requires raw drive I/O, the"
                    bashio::log.warning "same access used by smartmontools and Scrutiny."
                    bashio::log.warning "Our code is open source: github.com/DAB-LABS"
                    bashio::log.warning "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                else
                    bashio::log.info "Drive access: OK (${FIRST_DRIVE})"
                fi
            fi
        fi
    else
        DRIVE_COUNT="?"
        bashio::log.warning "smartctl --scan failed. This may be a permissions issue."
        bashio::log.warning "The app needs SYS_RAWIO privilege to access host drives."
        bashio::log.warning "Check that the app has the required privileges in its configuration."
    fi
fi

# ── Update smartmontools drive database ──────────────────────────────────────
# drivedb.h maps drive models to their SMART attribute definitions. The version
# bundled with Alpine may be months old — updating it improves attribute
# interpretation for uncommon drives. Persisted to /data so it survives rebuilds.
DRIVEDB_PERSIST="/data/drivedb.h"
DRIVEDB_SYSTEM="/var/lib/smartmontools/drivedb/drivedb.h"

# Restore persisted copy if available (faster than downloading every restart)
if [ -f "${DRIVEDB_PERSIST}" ]; then
    cp "${DRIVEDB_PERSIST}" "${DRIVEDB_SYSTEM}" 2>/dev/null || true
fi

# Try to fetch latest from upstream — fail silently if no internet
# --no-verify: skip GPG signature check (gnupg not installed; HTTPS provides transport security)
if update-smart-drivedb --no-verify >/dev/null 2>&1; then
    cp "${DRIVEDB_SYSTEM}" "${DRIVEDB_PERSIST}" 2>/dev/null || true
    bashio::log.info "drivedb.h: updated from upstream"
elif [ -f "${DRIVEDB_PERSIST}" ]; then
    bashio::log.info "drivedb.h: using persisted copy"
else
    bashio::log.info "drivedb.h: using bundled version"
fi

# ── Resolve HA hostname for unique mDNS name ─────────────────────────────────
# On multi-HA networks, each instance needs a unique mDNS service name.
# Query the Supervisor API for the real HA hostname (set in Settings → System).
# Falls back gracefully — the agent uses its container hostname if this fails.
HA_HOSTNAME=""
MDNS_NAME=""

# Try bashio's native helper first (handles Supervisor auth automatically),
# then fall back to raw curl if bashio isn't available.
if HA_HOSTNAME=$(bashio::host.hostname 2>/dev/null) && [ -n "${HA_HOSTNAME}" ]; then
    MDNS_NAME="smartha-${HA_HOSTNAME}"
    bashio::log.info "mDNS name: ${MDNS_NAME} (from HA hostname: ${HA_HOSTNAME})"
elif [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    HA_HOSTNAME=$(curl -sSL -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
        http://supervisor/host/info 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('data', {}).get('hostname', ''))
except:
    print('')
" 2>/dev/null || echo "")

    if [ -n "${HA_HOSTNAME}" ]; then
        MDNS_NAME="smartha-${HA_HOSTNAME}"
        bashio::log.info "mDNS name: ${MDNS_NAME} (from HA hostname: ${HA_HOSTNAME})"
    else
        bashio::log.warning "Could not resolve HA hostname — mDNS will use container hostname"
    fi
else
    bashio::log.warning "Supervisor API not available — mDNS will use container hostname"
fi

# ── Find the mounts to measure for disk space ────────────────────────────────
# The agent measures disk space only for the filesystems its config lists.
# mounts.py writes that config from the mounts Home Assistant gives the app:
# the data disk (/data, also after Move data disk) and USB drives under
# /media. It is a snapshot, so a
# drive plugged in later shows after an app restart. If it fails, the agent
# starts without the config and disk space is off.
AGENT_CONFIG="/data/agent-config.yaml"
FS_COUNT=0
if MOUNT_LINES="$(python3 /opt/mounts.py --out "${AGENT_CONFIG}" 2>&1)"; then
    while IFS= read -r line; do
        [ -n "${line}" ] || continue
        bashio::log.info "Disk space: ${line}"
        FS_COUNT=$((FS_COUNT + 1))
    done <<< "${MOUNT_LINES}"
else
    bashio::log.warning "Could not list the mounts. Disk space is off."
    bashio::log.warning "${MOUNT_LINES}"
    AGENT_CONFIG=""
fi

# ── Start the real Go agent ──────────────────────────────────────────────────
bashio::log.info "Starting SMART Sniffer agent on port ${PORT}..."

AGENT_ARGS="--port=${PORT} --scan-interval=${SCAN_INTERVAL}s"
if [ -n "${AGENT_CONFIG}" ]; then
    AGENT_ARGS="${AGENT_ARGS} --config=${AGENT_CONFIG}"
fi
if [ -n "${MDNS_NAME}" ]; then
    # Only pass --mdns-name if the agent binary supports it (v0.4.28+)
    if /usr/bin/smartha-agent --help 2>&1 | grep -q "mdns-name"; then
        AGENT_ARGS="${AGENT_ARGS} --mdns-name=${MDNS_NAME}"
    else
        bashio::log.warning "Agent binary does not support --mdns-name (needs v0.4.28+). Using default mDNS name."
        MDNS_NAME=""
    fi
fi
if bashio::var.has_value "${TOKEN}"; then
    AGENT_ARGS="${AGENT_ARGS} --token=${TOKEN}"
    AUTH_STATUS="enabled"
else
    AUTH_STATUS="disabled"
fi

/usr/bin/smartha-agent ${AGENT_ARGS} &
AGENT_PID=$!

# ── Start the replica agent ──────────────────────────────────────────────────
# The replica agent serves the replicas (drives that are not real) to the
# Control Center and, once added, to the integration. It always runs. It is
# started with --no-mdns: advertising it would show every user a discovered
# card for a fake agent.
if ! mkdir -p "${USER_REPLICAS}" 2>/dev/null; then
    bashio::log.warning "Cannot create ${USER_REPLICAS}. Is /share mounted? Saving and uploading replicas is off."
fi

bashio::log.info "Starting the replica agent on port ${MOCK_PORT}..."
# The replica agent comes from the smart-sniffer release (see the
# Dockerfile). Each option is passed only when that version supports it, so
# an older one still starts.
MOCK_HELP="$(python3 /opt/mock-agent.py --help 2>&1 || true)"
MOCK_ARGS="--port=${MOCK_PORT} --no-mdns"
if echo "${MOCK_HELP}" | grep -q -- "--data-dir"; then
    MOCK_ARGS="${MOCK_ARGS} --data-dir /data"
else
    bashio::log.warning "Replica agent does not support --data-dir. Replica edits will not survive a restart."
fi
if echo "${MOCK_HELP}" | grep -q -- "--user-replicas"; then
    MOCK_ARGS="${MOCK_ARGS} --replicas ${SHIPPED_REPLICAS} --user-replicas ${USER_REPLICAS}"
else
    bashio::log.warning "Replica agent does not read replica files. Starting it with its built-in drives."
    MOCK_ARGS="${MOCK_ARGS} --preload sata_hdd,sata_ssd,nvme,usb_blocked"
fi
python3 /opt/mock-agent.py ${MOCK_ARGS} &
MOCK_PID=$!
SHIPPED_COUNT=$(find "${SHIPPED_REPLICAS}" -maxdepth 1 -name '*.json' ! -name 'scenarios.json' 2>/dev/null | wc -l)
USER_COUNT=$(find "${USER_REPLICAS}" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l)

# ── Start the web UI proxy server ────────────────────────────────────────────
bashio::log.info "Starting web UI proxy on port ${INGRESS_PORT}..."

# SUPERVISOR_TOKEN is already in this environment (with-contenv) and the
# proxy inherits it; it is never passed on the command line.
# The container's hostname is the name the integration connects to: the
# app's slug with dashes, for example local-smart-sniffer-agent when the app
# is installed as a local app. The page shows it and Add uses it.
AGENT_HOST="$(hostname 2>/dev/null || true)"
PROXY_ARGS="--port=${INGRESS_PORT} --agent-port=${PORT} --mock-port=${MOCK_PORT}"
PROXY_ARGS="${PROXY_ARGS} --app-version=${APP_VERSION} --data-dir=/data"
if [ -n "${AGENT_HOST}" ]; then
    PROXY_ARGS="${PROXY_ARGS} --agent-host=${AGENT_HOST}"
else
    bashio::log.warning "Could not read the container hostname. The Control Center will show the default host."
fi
PROXY_ARGS="${PROXY_ARGS} --mdns-name=${MDNS_NAME}"
if bashio::var.has_value "${TOKEN}"; then
    PROXY_ARGS="${PROXY_ARGS} --token=${TOKEN}"
fi

python3 /opt/web/proxy.py ${PROXY_ARGS} &
WEBUI_PID=$!

# ── Startup summary ─────────────────────────────────────────────────────────
bashio::log.info "─────────────────────────────────────────"
bashio::log.info "SMART Sniffer App v${APP_VERSION}"
bashio::log.info "Agent: running on port ${PORT}"
bashio::log.info "Integration host: ${AGENT_HOST:-unknown}:${PORT}"
bashio::log.info "mDNS: ${MDNS_NAME:-default (container hostname)}"
bashio::log.info "Auth: ${AUTH_STATUS}"
bashio::log.info "Replicas: agent on port ${MOCK_PORT}, ${SHIPPED_COUNT} shipped, ${USER_COUNT} in ${USER_REPLICAS}"
bashio::log.info "Integration rules: $(cat /opt/ssrules/VERSION 2>/dev/null || echo 'not found')"
bashio::log.info "Web UI: http://localhost:${INGRESS_PORT} (via ingress)"
bashio::log.info "Drives detected: ${DRIVE_COUNT:-0}"
bashio::log.info "Disk space: ${FS_COUNT} mounts measured"
bashio::log.info "─────────────────────────────────────────"

# ── Wait for processes — restart on critical failure ─────────────────────────
wait_and_monitor() {
    while true; do
        # Check if the real agent is still running
        if ! kill -0 "${AGENT_PID}" 2>/dev/null; then
            bashio::log.fatal "SMART Sniffer agent (PID ${AGENT_PID}) has died!"
            bashio::exit.nok
        fi

        # Check if the web UI proxy is still running
        if ! kill -0 "${WEBUI_PID}" 2>/dev/null; then
            bashio::log.fatal "Web UI proxy (PID ${WEBUI_PID}) has died!"
            bashio::exit.nok
        fi

        # The replica agent: log and carry on, the real agent matters more
        if [ -n "${MOCK_PID}" ]; then
            if ! kill -0 "${MOCK_PID}" 2>/dev/null; then
                bashio::log.warning "Replica agent (PID ${MOCK_PID}) has died. Replicas will be unavailable."
                bashio::log.warning "The real agent continues to run. Restart the app to restore the replicas."
                MOCK_PID=""  # Stop checking
            fi
        fi

        sleep 5
    done
}

wait_and_monitor
