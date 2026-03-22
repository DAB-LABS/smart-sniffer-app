#!/usr/bin/env bash
# setup-dev.sh — Download runtime JS dependencies for local development/testing.
# Run this once before copying smart_sniffer_agent/ to your HA /addons/ folder.
set -euo pipefail

WEB_DIR="smart_sniffer_agent/web"

echo "Downloading Alpine.js 3.x..."
curl -fSL -o "${WEB_DIR}/alpine.min.js" \
  "https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"
echo "  → $(wc -c < "${WEB_DIR}/alpine.min.js") bytes"

echo "Downloading Tailwind CSS 3.4.17..."
curl -fSL -o "${WEB_DIR}/tailwind.min.js" \
  "https://cdn.tailwindcss.com/3.4.17"
echo "  → $(wc -c < "${WEB_DIR}/tailwind.min.js") bytes"

echo ""
echo "Done. You can now copy smart_sniffer_agent/ to your HA /addons/ folder."
