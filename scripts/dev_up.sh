#!/usr/bin/env bash
# One command to bring the DBSearch dev canvas back after a restart or a laptop shutdown.
#
#   ./scripts/dev_up.sh          # start the product server, wake the DB, re-seed the demo docs
#   ./scripts/dev_up.sh --down   # stop it
#
# WHY THIS EXISTS. The self-host dev server runs SELFHOST_BACKEND=memory, so a restart drops
# more than people expect. What survives, and what does not:
#
#   survives                              lost on every restart
#   --------                              ---------------------
#   canvas layout (browser localStorage)  uploaded documents        <- this script re-seeds
#   Entra groups + membership (tenant)    composed router catalog   <- canvas re-composes itself
#   Azure SQL data                        token vault (#210)        <- ONE manual click, by design
#                                         group memberships (#266)  <- re-resolve on first request
#                                         SQL memo cache (#254)     <- re-rolls; harmless
#
# So a cold start is: run this, then click "Sign in again" IF you need Azure SQL - it is in
# the account menu now, not the canvas header (#643 removed the canvas's own auth chip). Document
# questions work immediately — since #266 the group lookup no longer waits on that click.
#
# The sign-in genuinely cannot be automated here: the vault holds a per-user delegated refresh
# token, and obtaining one means the user authenticating as themselves. That is the product
# working correctly (queries run AS you), not a gap.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT=8080
LOG="${TMPDIR:-/tmp}/dbsearch-dev-up.log"

if [ "${1:-}" = "--down" ]; then
  pkill -f "uvicorn dbsearch.server.app:app --port $PORT" && echo "stopped" || echo "nothing running"
  exit 0
fi

# Source with BASH, not zsh, and keep every value in .env quoted — an unquoted value with
# spaces/globs makes `. ./.env` try to RUN a word as a command. (Same trap as live_entra_up.sh.)
set -a
. ./.env
[ -f ./scratchpad/sharepoint.env ]     && . ./scratchpad/sharepoint.env
[ -f ./secrets/entra_test_users.env ]  && . ./secrets/entra_test_users.env
set +a

for v in AUTH_TENANT_ID AUTH_CLIENT_ID AUTH_CLIENT_SECRET AZURE_SQL_SERVER AZURE_SQL_PASSWORD; do
  [ -n "${!v:-}" ] || { echo "MISSING $v — see docs/DEPLOY_AZURE.md"; exit 1; }
done
echo "env: required vars present"

# A port already in use means something is ALREADY serving — possibly the browser session you
# are about to test in. Say so rather than nohup'ing a uvicorn that dies on bind and printing
# its pid as though it were healthy.
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT already in use — a server is running. './scripts/dev_up.sh --down' first"
  exit 1
fi

export SELFHOST_BACKEND=memory DBSEARCH_DEV_SEED=1
nohup env PYTHONPATH=src python3 -m uvicorn dbsearch.server.app:app --port $PORT >"$LOG" 2>&1 &
echo "  server starting (pid $!) — log: $LOG"

for i in $(seq 1 30); do
  curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || {
  echo "  server did not come up — tail $LOG"; exit 1; }
echo "  healthy: $(curl -s http://127.0.0.1:$PORT/health)"
echo "  build:   $(curl -s http://127.0.0.1:$PORT/version)"

# dbsampleaw is SERVERLESS: it auto-pauses, and the first connect after a pause fails while it
# wakes (~30-60s). Wake it HERE so the first canvas click is not the one that eats the error.
echo "waking Azure SQL (serverless — up to ~60s if paused)..."
python3 - <<'PY'
import os, sys
try:
    import pyodbc
except ImportError:
    print("  pyodbc missing — skipping wake (the first query will do it, slowly)"); sys.exit(0)
cs = ("DRIVER={ODBC Driver 18 for SQL Server};SERVER=%s;DATABASE=dbsampleaw;UID=%s;PWD=%s;"
      "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60"
      % (os.environ["AZURE_SQL_SERVER"], os.environ.get("AZURE_SQL_USER", ""),
         os.environ["AZURE_SQL_PASSWORD"]))
for attempt in range(5):
    try:
        with pyodbc.connect(cs, timeout=60) as c:
            n = c.cursor().execute("SELECT COUNT(*) FROM SalesLT.Product").fetchone()[0]
        print(f"  AWAKE — SalesLT.Product has {n} rows"); break
    except Exception as e:
        print(f"  attempt {attempt+1}/5: {str(e)[:80]}")
else:
    print("  could not wake it — the first canvas query will retry")
PY

# Re-seed the tiered demo documents. The built-in DBSEARCH_DEMO_SEED corpus is ACL'd to the
# STRING principals all-staff/deal-team, which only match the dev-auth identities — under a real
# Entra sign-in your principals are OIDs and GROUP oids, so those docs would be invisible. These
# are ACL'd to the real entitlement groups instead, which is what #258 set up.
if [ -f ./scripts/seed_demo_docs.py ]; then
  echo "seeding demo documents (ACL'd to the real Entra entitlement groups)..."
  python3 ./scripts/seed_demo_docs.py || echo "  seeding failed — run it by hand to see why"
fi

cat <<EOS

ready. http://localhost:$PORT/canvas

ONE manual step remains, and it is the product working as intended:

  -> open the account menu (top right, your initials) and click "Sign in again"
     on the Microsoft row.

It restores your DELEGATED TOKEN, which only you can mint (#210 — the vault is
in-memory by design). Until you click it, queries against Azure SQL will say
"sign in to query this source"; DOCUMENT questions already work.

Your Entra group memberships and the ACL picker's display names no longer need it —
since #266 they re-resolve on your first request, from the app-only Graph token.

If the canvas ever looks like it is missing a feature, check the build first:
  page:   open devtools console -> DBS_BUILD
  server: curl -s http://127.0.0.1:$PORT/version
A mismatch means a stale cached page (#265 self-heals it; a copy cached before that
shipped needs one Cmd+Shift+R).
EOS
