#!/usr/bin/env bash
# Asserts the docs describe the one-command flow + the demo overlay.
set -euo pipefail
cd "$(dirname "$0")/.."
grep -q "docker-compose.demo.yml" README.md || { echo "FAIL: README missing demo overlay flow"; exit 1; }
grep -q "docker-compose.demo.yml" docs/SELFHOST.md || { echo "FAIL: SELFHOST missing demo overlay flow"; exit 1; }
grep -qi "optional" scripts/ollama_pull.sh || { echo "FAIL: ollama_pull.sh not marked optional"; exit 1; }
echo "PASS: docs describe one-command + demo overlay"
