#!/usr/bin/with-contenv bashio
# ==============================================================================
# SMART Sniffer Agent — App Startup Script
# ==============================================================================
set -e

APP_VERSION="0.2.8"

# ── Read configuration ───────────────────────────────────────────────────────
PORT=$(bashio::config 'port')
TOKEN=$(bashio::config 'token')
SCAN_INTERVAL=$(bashio::config 'scan_interval')
MOCK_MODE=$(bashio::config 'mock_mode')
MOCK_PORT=$(bashio::config 'mock_port')
INGRESS_PORT=8099

# ── Validate configuration ───────────────────────────────────────────────────
if bashio::var.true "${MOCK_MODE}" && [ "${PORT}" = "${MOCK_PORT}" ]; then
    bashio::log.fatal "Mock port (${MOCK_PORT}) cannot be the same as agent port (${PORT})"
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

# ── Start the real Go agent ──────────────────────────────────────────────────
bashio::log.info "Starting SMART Sniffer agent on port ${PORT}..."

AGENT_ARGS="--port=${PORT} --scan-interval=${SCAN_INTERVAL}s"
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

# ── Start the mock agent (if enabled) ───────────────────────────────────────
MOCK_PID=""
MOCK_STATUS="disabled"
MOCK_DRIVE_COUNT="0"

if bashio::var.true "${MOCK_MODE}"; then
    bashio::log.info "Starting mock agent on port ${MOCK_PORT}..."
    python3 /opt/mock-agent.py \
        --port="${MOCK_PORT}" \
        --data-dir /data \
        --preload sata_hdd,sata_ssd,nvme,usb_blocked &
    MOCK_PID=$!
    MOCK_STATUS="enabled on port ${MOCK_PORT}"
    MOCK_DRIVE_COUNT="4"  # preloaded count
fi

# ── Start the web UI proxy server ────────────────────────────────────────────
bashio::log.info "Starting web UI proxy on port ${INGRESS_PORT}..."

python3 /opt/web/proxy.py \
    --port="${INGRESS_PORT}" \
    --agent-port="${PORT}" \
    --mock-port="${MOCK_PORT}" &
WEBUI_PID=$!

# ── Startup summary ─────────────────────────────────────────────────────────
bashio::log.info "─────────────────────────────────────────"
bashio::log.info "SMART Sniffer App v${APP_VERSION}"
bashio::log.info "Agent: running on port ${PORT}"
bashio::log.info "mDNS: ${MDNS_NAME:-default (container hostname)}"
bashio::log.info "Auth: ${AUTH_STATUS}"
bashio::log.info "Mock: ${MOCK_STATUS}"
bashio::log.info "Web UI: http://localhost:${INGRESS_PORT} (via ingress)"
bashio::log.info "Drives detected: ${DRIVE_COUNT:-0}"
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

        # Check mock agent only if it was started
        if bashio::var.true "${MOCK_MODE}" && [ -n "${MOCK_PID}" ]; then
            if ! kill -0 "${MOCK_PID}" 2>/dev/null; then
                bashio::log.warning "Mock agent (PID ${MOCK_PID}) has died. Mock drives will be unavailable."
                bashio::log.warning "The real agent continues to run. Restart the app to restore mock mode."
                MOCK_PID=""  # Stop checking
            fi
        fi

        sleep 5
    done
}

wait_and_monitor
