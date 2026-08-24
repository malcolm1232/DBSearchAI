#!/bin/bash
# #500 CONTROL: the same doc pack with the chunk cap DISABLED, so the cap's effect is
# measured against itself rather than inferred. gate2_doc_* ran at cap=3 (27/33, real
# movement +B-007 -A-003); this is the counterfactual.
set -u
cd "$(dirname "$0")/.."
LOG_DIR="${TMPDIR:-/tmp}/control_cap0"; mkdir -p "$LOG_DIR"
lsof -ti :8099 | xargs kill 2>/dev/null; sleep 2
PYTHONPATH=src SELFHOST_BACKEND=memory-ollama-embed \
  OLLAMA_EMBED_MODEL=nomic-embed-text OLLAMA_CHAT_MODEL=llama3.1:8b \
  DBSEARCH_MAX_BODY_BYTES=60000000 DBSEARCH_RATE_LIMIT=0 \
  DBSEARCH_SYNTH_CHUNK_CAP=0 \
  nohup python3 -m uvicorn dbsearch.server.app:app --port 8099 > "$LOG_DIR/server.log" 2>&1 &
for _ in $(seq 60); do
  grep -q "Uvicorn running" "$LOG_DIR/server.log" 2>/dev/null && break; sleep 1
done
for suffix in warm a; do
  echo "--- cap0_doc_${suffix} $(date +%H:%M:%S)"
  PYTHONPATH=src python3 scripts/golden_runner.py --pack "unstructured documents/doc_pack" \
    --base http://127.0.0.1:8099 --profile semantic --auth dev --full \
    --stamp "cap0_doc_${suffix}" 2>&1 | tail -3
done
lsof -ti :8099 | xargs kill 2>/dev/null
echo "=== CONTROL DONE $(date +%H:%M:%S) ==="
