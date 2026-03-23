ARG BUILD_FROM
FROM ${BUILD_FROM}

# Build argument for architecture-specific binary selection
ARG BUILD_ARCH

# Install runtime dependencies
# - smartmontools: smartctl for reading SMART data from host drives
# - python3: mock agent and web UI proxy server
# - py3-pip: for installing Python packages (zeroconf)
# - curl: downloading the Go agent binary from GitHub releases
RUN apk add --no-cache \
    smartmontools \
    python3 \
    py3-pip \
    curl \
    libc6-compat

# Install Python packages needed by the mock agent
# - zeroconf: mDNS advertisement so mock drives appear in HA autodiscovery
RUN pip3 install --no-cache-dir --break-system-packages zeroconf

# Download the Go agent binary from GitHub releases.
# Detect architecture from the running container (works for both local builds
# and CI builds, regardless of whether BUILD_ARCH is set).
RUN ARCH=$(uname -m) && \
    case "$ARCH" in \
        x86_64)  GO_ARCH="amd64" ;; \
        aarch64) GO_ARCH="arm64" ;; \
        armv7l)  GO_ARCH="arm" ;; \
        *)       echo "Unsupported architecture: $ARCH" && exit 1 ;; \
    esac && \
    RELEASE_URL="https://github.com/DAB-LABS/smart-sniffer/releases/latest/download/smartha-agent-linux-${GO_ARCH}" && \
    echo "Downloading agent binary from: ${RELEASE_URL}" && \
    curl -fSL -o /usr/bin/smartha-agent "${RELEASE_URL}" && \
    chmod +x /usr/bin/smartha-agent

# Copy the mock agent
COPY mock-agent.py /opt/mock-agent.py

# Copy the web UI
COPY web/ /opt/web/

# Download Alpine.js and Tailwind CSS if placeholder files are present.
# CI may have already replaced these; this ensures the Docker image is
# self-contained regardless of how it's built.
RUN if head -1 /opt/web/alpine.min.js | grep -q "PLACEHOLDER"; then \
      echo "Downloading Alpine.js 3.x..." && \
      curl -fSL -o /opt/web/alpine.min.js \
        "https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"; \
    fi && \
    if head -1 /opt/web/tailwind.min.js | grep -q "PLACEHOLDER"; then \
      echo "Downloading Tailwind CSS 3.4.17..." && \
      curl -fSL -o /opt/web/tailwind.min.js \
        "https://cdn.tailwindcss.com/3.4.17"; \
    fi

# Copy the startup script
COPY run.sh /
RUN chmod +x /run.sh

# The HA base image provides ENTRYPOINT ["/init"] (s6-overlay).
# CMD tells s6-overlay what to run as the main service.
CMD [ "/run.sh" ]
