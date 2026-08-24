#!/bin/bash
# The gates owed for 260805's afternoon work: #500 (chunk cap), #503 (same-store split),
# #504 (carry-source literal repair), plus the review fixes on top of them.
#
# Same shape as model_bakeoff.sh, which survived the external stopper all day: ONE rig on
# llama3.1:8b (the measured baseline model - the bake settled that nothing beats it on
# SQL), warm run discarded, three stamped scored runs per pack.
#
# Compare PER ITEM against:
#   SQL: real_pack_cap_sql3_a/b/c = 32/38    doc: real_pack_cap_doc2_a/b/c = 26/33
set -u
cd "$(dirname "$0")/.."
LOG_DIR="${TMPDIR:-/tmp}/gate_260805b"; mkdir -p "$LOG_DIR"

run_pack () {                     # $1=pack path  $2=stamp prefix
  for suffix in warm a b c; do
    echo "--- $2_${suffix} $(date +%H:%M:%S)"
    PYTHONPATH=src python3 scripts/golden_runner.py --pack "$1" \
      --base http://127.0.0.1:8099 --profile semantic --auth dev --full \
      --stamp "$2_${suffix}" 2>&1 | tail -3
  done
}

echo "=== GATE 260805b $(date +%H:%M:%S) ==="
lsof -ti :8099 | xargs kill 2>/dev/null; sleep 2
PYTHONPATH=src SELFHOST_BACKEND=memory-ollama-embed \
  OLLAMA_EMBED_MODEL=nomic-embed-text OLLAMA_CHAT_MODEL=llama3.1:8b \
  DBSEARCH_MAX_BODY_BYTES=60000000 DBSEARCH_RATE_LIMIT=0 \
  nohup python3 -m uvicorn dbsearch.server.app:app --port 8099 \
    > "$LOG_DIR/server.log" 2>&1 &
for _ in $(seq 60); do
  grep -q "Uvicorn running" "$LOG_DIR/server.log" 2>/dev/null && break
  sleep 1
done

# Doc pack FIRST: #500 is the change this whole cycle was built around.
run_pack "unstructured documents/doc_pack"   "gate2_doc"
run_pack "eval_fixtures/golden_pack_real"    "gate2_sql"

lsof -ti :8099 | xargs kill 2>/dev/null
echo "=== GATE 260805b DONE $(date +%H:%M:%S) ==="
