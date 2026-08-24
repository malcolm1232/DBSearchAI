#!/usr/bin/env bash
# Asserts the demo overlay adds dev-auth + seed + the small model, on top of the base.
set -euo pipefail
cd "$(dirname "$0")/.."
CFG="$(docker compose -f docker-compose.yml -f docker-compose.demo.yml config)"
echo "$CFG" | grep -q "DBSEARCH_DEV_AUTH" || { echo "FAIL: demo overlay missing DBSEARCH_DEV_AUTH"; exit 1; }
echo "$CFG" | grep -q "DBSEARCH_DEMO_SEED" || { echo "FAIL: demo overlay missing DBSEARCH_DEMO_SEED"; exit 1; }
echo "$CFG" | grep -q "qwen2.5:0.5b" || { echo "FAIL: demo overlay not using qwen2.5:0.5b"; exit 1; }
# base compose must NOT carry dev-auth
BASE="$(docker compose -f docker-compose.yml config)"
echo "$BASE" | grep -q "DBSEARCH_DEV_AUTH" && { echo "FAIL: base compose leaked dev-auth"; exit 1; }
echo "PASS: demo overlay correct, base stays production-shaped"
