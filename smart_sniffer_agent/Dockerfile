# Home Assistant base image, pinned for build stability.
# Published as a multi-platform manifest, so a single FROM covers both
# supported architectures (linux/amd64 and linux/arm64) -- per-arch base
# images are no longer needed.
#
# This replaces the former `ARG BUILD_FROM` / `FROM ${BUILD_FROM}` pair fed
# by build.yaml. Supervisor stopped supplying BUILD_FROM by default in
# 2026.04.0, so relying on it would break the build once build.yaml is gone.
# See https://developers.home-assistant.io/blog/2026/04/02/builder-migration
FROM ghcr.io/home-assistant/base:3.24-2026.08.0

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
# Architecture is detected from the running container via uname -m, which works
# for local builds, Supervisor builds, and CI alike without needing a build arg.
# CACHE_BUST changes with each app version to force a fresh binary download.
ARG CACHE_BUST=1
RUN ARCH=$(uname -m) && \
    case "$ARCH" in \
        x86_64)  GO_ARCH="amd64" ;; \
        aarch64) GO_ARCH="arm64" ;; \
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
