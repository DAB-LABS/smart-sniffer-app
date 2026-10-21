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
# - python3: the web UI proxy server
# - curl: downloading the Go agent binary and the smart-sniffer tarball
RUN apk add --no-cache \
    smartmontools \
    python3 \
    curl \
    libc6-compat

# The smart-sniffer release this app version is built against. It picks both
# the Go agent binary and the integration's rule modules below, so the app
# always ships the agent of the version it names (never "latest"). SS_TAG is
# bumped with each app release.
ARG SS_TAG=v0.9.0

# Download the Go agent binary from the SS_TAG release.
# Architecture is detected from the running container via uname -m, which works
# for local builds, Supervisor builds, and CI alike without needing a build arg.
# CACHE_BUST changes with each app version to force a fresh binary download.
# The build stops when SS_TAG does not name a release (empty, "latest", a
# branch); test_dockerfile.py fails if the URL is not the SS_TAG release.
ARG CACHE_BUST=1
RUN ARCH=$(uname -m) && \
    case "$ARCH" in \
        x86_64)  GO_ARCH="amd64" ;; \
        aarch64) GO_ARCH="arm64" ;; \
        *)       echo "Unsupported architecture: $ARCH" && exit 1 ;; \
    esac && \
    case "${SS_TAG}" in \
        v[0-9]*) ;; \
        *)       echo "SS_TAG must name a release, got: ${SS_TAG}" && exit 1 ;; \
    esac && \
    RELEASE_URL="https://github.com/DAB-LABS/smart-sniffer/releases/download/${SS_TAG}/smartha-agent-linux-${GO_ARCH}" && \
    echo "Downloading agent binary from: ${RELEASE_URL}" && \
    curl -fSL -o /usr/bin/smartha-agent "${RELEASE_URL}" && \
    chmod +x /usr/bin/smartha-agent

# The integration's rule modules, from the smart-sniffer tag this app version
# is built against (SS_TAG, above; or, while testing, a branch). The proxy runs
# the rules for the page's verdicts. SS_TARBALL can
# point a local build at another copy. Replicas moved into the integration,
# so no replica agent or replica file is taken any more.
#
# attention.py, devstat.py, extract.py, pool_health.py and const.py import
# nothing from Home Assistant. They go in custom_components/smart_sniffer/
# with no package __init__.py of their own; the proxy registers that package
# itself so their relative imports resolve without the integration's
# __init__ (which needs Home Assistant).
ARG SS_TARBALL=https://github.com/DAB-LABS/smart-sniffer/archive/${SS_TAG}.tar.gz
RUN set -e; \
    SRC=/tmp/smart-sniffer; \
    RULES_DIR=/opt/ssrules/custom_components; \
    echo "Downloading smart-sniffer ${SS_TAG} from ${SS_TARBALL}"; \
    mkdir -p "${SRC}" "${RULES_DIR}/smart_sniffer"; \
    curl -fSL -o /tmp/smart-sniffer.tar.gz "${SS_TARBALL}"; \
    tar -xzf /tmp/smart-sniffer.tar.gz -C "${SRC}" --strip-components=1; \
    : > "${RULES_DIR}/__init__.py"; \
    for f in attention devstat extract pool_health const; do \
      cp "${SRC}/custom_components/smart_sniffer/${f}.py" "${RULES_DIR}/smart_sniffer/"; \
    done; \
    rm -rf "${SRC}" /tmp/smart-sniffer.tar.gz; \
    echo "${SS_TAG}" > /opt/ssrules/VERSION; \
    python3 -m py_compile "${RULES_DIR}"/smart_sniffer/*.py

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

# Copy the startup script, and the mount finder it runs for disk space
COPY run.sh /
COPY mounts.py /opt/mounts.py
RUN chmod +x /run.sh

# The HA base image provides ENTRYPOINT ["/init"] (s6-overlay).
# CMD tells s6-overlay what to run as the main service.
CMD [ "/run.sh" ]
