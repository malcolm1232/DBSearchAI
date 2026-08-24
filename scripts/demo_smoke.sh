#!/usr/bin/env bash
# Instant-demo smoke: one-command demo up -> seeded, query-able, slide-locator citation.
# Requires a running Docker daemon. Exits non-zero on any failed assertion. Tears down at the end.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.demo.yml"

echo "[1/5] bringing up demo stack (auto-pulls qwen2.5:0.5b + nomic-embed-text on first run)..."
$COMPOSE up -d --build

echo "[2/5] waiting for API health on :8080 (model pull + seed embed can take a few min first time)..."
ok=""
for i in $(seq 1 120); do
  if curl -sf http://localhost:8080/health >/dev/null 2>&1; then echo "  healthy"; ok=1; break; fi
  sleep 3
done
[ -z "$ok" ] && { echo "FAIL: api never healthy"; $COMPOSE logs --tail=40 api; $COMPOSE down; exit 1; }

echo "[3/5] building + uploading a 3-slide deck as alice..."
python3 - <<'PY'
from pptx import Presentation
p=Presentation(); b=p.slide_layouts[6]
for t in ["Company overview and mission","Financial results: revenue grew forty percent year over year","Hiring plan and open roles"]:
    p.slides.add_slide(b).shapes.add_textbox(0,0,100,100).text_frame.text=t
p.save("/tmp/demo_smoke_deck.pptx")
PY
MIME="application/vnd.openxmlformats-officedocument.presentationml.presentation"
curl -sf -X POST http://localhost:8080/admin/upload -H "X-DBSearch-User: alice" \
  -F "file=@/tmp/demo_smoke_deck.pptx;type=$MIME" -F "acl=all-staff" -F "title=Smoke Deck" >/dev/null

echo "[4/5] querying + asserting slide-2 citation..."
RESP=$(curl -sf -X POST http://localhost:8080/search -H "X-DBSearch-User: alice" \
  -H "Content-Type: application/json" \
  -d '{"question":"what were the financial results and revenue growth"}')
echo "  response: $RESP"
echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);\
import sys as s;\
ok=any((c.get('locator') or {}).get('kind')=='slide' and (c.get('locator') or {}).get('n')==2 for c in d.get('citations',[]));\
s.exit(0 if ok else 1)" || { echo 'FAIL: no slide-2 citation'; $COMPOSE down; exit 1; }
echo "  PASS: citation carries slide n=2"

echo "[5/5] tearing down..."
$COMPOSE down
echo "DEMO SMOKE PASSED"
