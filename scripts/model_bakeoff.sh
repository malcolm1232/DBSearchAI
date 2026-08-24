#!/bin/bash
# #496 - chat-model bake-off over both golden packs.
#
# For each model: restart the rig with OLLAMA_CHAT_MODEL swapped, then per pack run one
# DISCARDED warm run + three stamped scored runs (the #483/#491 discipline). Stamps are
# bake_<model-slug>_<pack>_{warm,a,b,c}; baselines already key on chat_model, so runs
# never collide. Sequential on purpose: two models inferring at once on a 16GB machine
# is how phantom regressions get minted.
#
#   ./scripts/model_bakeoff.sh qwen2.5-coder:7b qwen3:8b            # the small rung
#   ./scripts/model_bakeoff.sh qwen2.5:14b phi4                     # the stretch rung
set -u
cd "$(dirname "$0")/.."
LOG_DIR="${TMPDIR:-/tmp}/bakeoff_logs"; mkdir -p "$LOG_DIR"

run_pack () {                     # $1=pack path  $2=stamp prefix
  for suffix in warm a b c; do
    echo "--- $2_${suffix} $(date +%H:%M:%S)"
    PYTHONPATH=src python3 scripts/golden_runner.py --pack "$1" \
      --base http://127.0.0.1:8099 --profile semantic --auth dev --full \
      --stamp "$2_${suffix}" 2>&1 | tail -3
  done
}

for MODEL in "$@"; do
  SLUG=$(echo "$MODEL" | tr -c 'a-zA-Z0-9' '_' | sed 's/_*$//')
  echo "=== MODEL $MODEL $(date +%H:%M:%S) ==="
  lsof -ti :8099 | xargs kill 2>/dev/null; sleep 2
  PYTHONPATH=src SELFHOST_BACKEND=memory-ollama-embed \
    OLLAMA_EMBED_MODEL=nomic-embed-text OLLAMA_CHAT_MODEL="$MODEL" \
    DBSEARCH_MAX_BODY_BYTES=60000000 DBSEARCH_RATE_LIMIT=0 \
    nohup python3 -m uvicorn dbsearch.server.app:app --port 8099 \
      > "$LOG_DIR/server_${SLUG}.log" 2>&1 &
  for _ in $(seq 60); do
    grep -q "Uvicorn running" "$LOG_DIR/server_${SLUG}.log" 2>/dev/null && break
    sleep 1
  done
  run_pack "eval_fixtures/golden_pack_real"    "bake_${SLUG}_sql"
  run_pack "unstructured documents/doc_pack"   "bake_${SLUG}_doc"
done
lsof -ti :8099 | xargs kill 2>/dev/null
echo "=== BAKE-OFF DONE $(date +%H:%M:%S) ==="
