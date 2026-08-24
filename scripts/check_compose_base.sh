#!/usr/bin/env bash
# Asserts the base compose wires the auto-pull init service correctly.
set -euo pipefail
cd "$(dirname "$0")/.."
CFG="$(docker compose -f docker-compose.yml config)"
echo "$CFG" | grep -q "ollama-init:" || { echo "FAIL: no ollama-init service"; exit 1; }
echo "$CFG" | grep -q "service_completed_successfully" || { echo "FAIL: api not gated on ollama-init completion"; exit 1; }
echo "$CFG" | grep -q "ollama pull" || { echo "FAIL: ollama-init doesn't pull models"; exit 1; }
echo "$CFG" | grep -q 'pull ""' && { echo "FAIL: ollama-init pulls empty model name"; exit 1; }
echo "$CFG" | grep -q "nomic-embed-text" || { echo "FAIL: embed model not set"; exit 1; }
echo "PASS: base compose auto-pull wired"
