#!/usr/bin/env bash
#
# connect-azure.sh — one command to stand up DBSearch.AI on YOUR Azure and query YOUR
# SharePoint, permission-trimmed by real Entra groups, in a browser.
#
# It automates everything a script can: provision (Bicep) → grant the RBAC roles the code
# needs → register the Entra app → (pause for the ONE human step: admin consent) → let you
# pick a SharePoint library → ingest it → launch the query UI at http://localhost:8080.
#
# Idempotent + resumable: re-run it any time; each phase skips what's already done. Nothing
# leaves your tenant — generation is your in-tenant Azure OpenAI (LAW 1).
#
#   ./scripts/connect-azure.sh                 # do it (interactive pickers)
#   ./scripts/connect-azure.sh --dry-run       # print the az commands, spend nothing
#   ./scripts/connect-azure.sh -g my-rg -l westus -p mydbs
#   ./scripts/connect-azure.sh --drive-id <id> --test-users <oid1>,<oid2>   # non-interactive
#
# Testing as a FRESH customer? Log into the new account and pin its subscription so this
# never touches your other one:
#   az login                                   # sign in with the NEW account
#   ./scripts/connect-azure.sh --free -s "<new subscription name or id>"
# --free swaps the two cost-driver SKUs to free tiers (AI Search Free + Service Bus Basic +
# Doc Intelligence F0; AOAI stays pay-per-call) so the trial runs at ~$0. Limits: one Free
# search + one F0 DocIntel per subscription; Free search = 50MB/3 indexes (fine for a demo).
# (The tenant must have SharePoint/M365 to index — a blank Azure sub has none. A free
#  Microsoft 365 Developer tenant gives you SharePoint + sample docs + Global Admin, free.)
#
# Requires: az (logged in or it'll prompt), python3, and this repo. ~$85/mo idle while up
# (or ~$0 with --free) — tear down with:  az group delete -n <resource-group> --yes --no-wait
#
set -euo pipefail

# ── config (override via flags/env) ───────────────────────────────────────────────────────
RG="${RG:-dbsearch-trial}"
LOCATION="${LOCATION:-eastus}"
PREFIX="${PREFIX:-dbstrial}"          # lowercase, ≤11 chars (Azure name limits)
APP_NAME="${APP_NAME:-DBSearch-trial}"
PORT="${PORT:-8080}"
SUBSCRIPTION="${SUBSCRIPTION:-}"      # pin the target sub so we never touch the wrong account
FREE=0                                # --free → Bicep dev=true (Search Free + SB Basic + DocIntel F0) ≈ $0
DRY_RUN=0
DRIVE_ID="${DRIVE_ID:-}"
TEST_USERS="${TEST_USERS:-}"
SITE_SEARCH="${SITE_SEARCH:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

# well-known Microsoft Graph application-permission role ids (stable GUIDs).
# Plain "name=guid" strings (not an associative array) so this runs on macOS's bash 3.2 too.
GRAPH_APP="00000003-0000-0000-c000-000000000000"
GRAPH_PERMS=(
  "Sites.Read.All=332a536c-c7ef-4017-ab91-336970924f0d"
  "Files.Read.All=01d4889c-1287-42c6-ac1f-5d1e02578ef6"
  "GroupMember.Read.All=98830695-27a2-44f7-8c18-0c3ebc9698f6"
  "User.Read.All=df021288-bdef-4463-88db-98f22de89214"
)
RBAC_ROLES=("Storage Blob Data Contributor" "Azure Service Bus Data Owner" "Cognitive Services User")

# ── ui helpers ────────────────────────────────────────────────────────────────────────────
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'; else B=; G=; Y=; R=; D=; N=; fi
step() { echo; echo "${B}▶ $*${N}"; }
ok()   { echo "${G}✓${N} $*"; }
warn() { echo "${Y}⚠${N} $*"; }
die()  { echo "${R}✗ $*${N}" >&2; exit 1; }
# run: execute, or just print when --dry-run (for mutating az commands)
run()  { if [ "$DRY_RUN" = 1 ]; then echo "${D}[dry-run] $*${N}"; else eval "$@"; fi; }

usage() { sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1;;
    --free) FREE=1;;
    -g|--resource-group) RG="$2"; shift;;
    -s|--subscription) SUBSCRIPTION="$2"; shift;;
    -l|--location) LOCATION="$2"; shift;;
    -p|--prefix) PREFIX="$2"; shift;;
    --drive-id) DRIVE_ID="$2"; shift;;
    --test-users) TEST_USERS="$2"; shift;;
    --site) SITE_SEARCH="$2"; shift;;
    -h|--help) usage;;
    *) die "unknown arg: $1 (try --help)";;
  esac
  shift
done
[ ${#PREFIX} -le 11 ] || die "prefix '$PREFIX' too long (Azure limit ≤11 chars)"

# ── 0. preflight ──────────────────────────────────────────────────────────────────────────
preflight() {
  step "Preflight"
  command -v az >/dev/null || die "azure CLI 'az' not found — install it first"
  command -v python3 >/dev/null || die "python3 not found"
  command -v jq >/dev/null || die "jq not found (brew install jq / apt install jq)"
  if ! az account show >/dev/null 2>&1; then
    warn "not logged in to Azure — launching 'az login' (🔐 only you can do this)"
    [ "$DRY_RUN" = 1 ] && { echo "${D}[dry-run] az login${N}"; } || az login >/dev/null
  fi
  # Pin the target subscription so we can NEVER provision into the wrong account. When you're
  # testing as a fresh customer, pass -s "<new sub name or id>" (or export SUBSCRIPTION=...).
  if [ -n "$SUBSCRIPTION" ]; then
    run "az account set --subscription '$SUBSCRIPTION'"
  fi
  TENANT_ID="$(az account show --query tenantId -o tsv 2>/dev/null || echo '<tenant>')"
  SUB="$(az account show --query name -o tsv 2>/dev/null || echo '<subscription>')"
  SUB_ID="$(az account show --query id -o tsv 2>/dev/null || echo '<sub-id>')"
  ok "Azure: ${B}${SUB}${N} (${SUB_ID}, tenant ${TENANT_ID})  ·  RG=${RG} prefix=${PREFIX} loc=${LOCATION}"
  # Safety gate: on a REAL run, confirm this is the account you intend before spending money.
  if [ "$DRY_RUN" != 1 ]; then
    read -r -p "  Provision into THIS subscription? [y/N] " _c; case "$_c" in y|Y) ;; *) die "aborted — switch with 'az account set -s <sub>' or pass -s"; esac
  fi
}

# ── 1. provision (idempotent) ─────────────────────────────────────────────────────────────
provision() {
  step "Provision data-plane resources (Bicep)"
  if az group show -n "$RG" >/dev/null 2>&1; then
    ok "resource group ${RG} already exists — reusing"
  else
    run "az group create -n '$RG' -l '$LOCATION' -o none"
    ok "created resource group ${RG}"
  fi
  local devparam=""; [ "$FREE" = 1 ] && devparam="dev=true"
  run "az deployment group create -g '$RG' -n dbsearch -f '$REPO_ROOT/infra/main.bicep' -p namePrefix='$PREFIX' $devparam -o none"
  if [ "$FREE" = 1 ]; then
    ok "Bicep applied in ${B}FREE mode${N} (AI Search Free · Service Bus Basic · Doc Intelligence F0 · AOAI pay-per-call ≈ \$0)"
  else
    ok "Bicep deployment applied (Blob · Service Bus · AI Search · Azure OpenAI · Doc Intelligence · Key Vault)"
  fi
}

# capture deployment outputs into shell vars
load_outputs() {
  [ "$DRY_RUN" = 1 ] && return 0
  local o; o="$(az deployment group show -g "$RG" -n dbsearch --query properties.outputs -o json)"
  BLOB_URL="$(echo "$o"     | jq -r .blobAccountUrl.value)"
  SB_NS="$(echo "$o"        | jq -r .servicebusNamespace.value)"
  SEARCH_EP="$(echo "$o"    | jq -r .searchEndpoint.value)"
  AOAI_EP="$(echo "$o"      | jq -r .aoaiEndpoint.value)"
  DOCINTEL_EP="$(echo "$o"  | jq -r .docintelEndpoint.value)"
  KV_URL="$(echo "$o"       | jq -r .keyvaultUrl.value)"
  EMBED_DEP="$(echo "$o"    | jq -r .embeddingDeployment.value)"
  CHAT_DEP="$(echo "$o"     | jq -r .chatDeployment.value)"
}

# ── 2. RBAC — grant the running identity the roles the code's DefaultAzureCredential needs ──
grant_rbac() {
  step "Grant RBAC roles to your identity (Bicep grants none — this is the #11 gap)"
  local me scope; me="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || echo '<you>')"
  scope="$(az group show -n "$RG" --query id -o tsv 2>/dev/null || echo '<rg>')"
  for role in "${RBAC_ROLES[@]}"; do
    run "az role assignment create --assignee '$me' --role '$role' --scope '$scope' -o none 2>/dev/null || true"
    ok "role: ${role}"
  done
}

# ── 3. Entra app (find-or-create) + Graph permissions ─────────────────────────────────────
entra_app() {
  step "Register the Entra app (read-only Graph access)"
  APP_ID="$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv 2>/dev/null || true)"
  if [ -z "${APP_ID:-}" ] || [ "$APP_ID" = "null" ]; then
    if [ "$DRY_RUN" = 1 ]; then echo "${D}[dry-run] az ad app create --display-name '$APP_NAME'${N}"; APP_ID="<app-id>"; else
      APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"; fi
    ok "created app ${APP_NAME} (${APP_ID})"
  else
    ok "app ${APP_NAME} already exists (${APP_ID})"
  fi
  # client secret
  if [ "$DRY_RUN" = 1 ]; then CLIENT_SECRET="<secret>"; echo "${D}[dry-run] az ad app credential reset --id $APP_ID${N}"; else
    CLIENT_SECRET="$(az ad app credential reset --id "$APP_ID" --query password -o tsv)"; fi
  # ensure a service principal exists (needed for role grants + consent)
  run "az ad sp create --id '$APP_ID' -o none 2>/dev/null || true"
  # add the four Graph application permissions
  for entry in "${GRAPH_PERMS[@]}"; do
    local perm="${entry%%=*}" rid="${entry#*=}"
    run "az ad app permission add --id '$APP_ID' --api '$GRAPH_APP' --api-permissions '${rid}=Role' -o none 2>/dev/null || true"
    ok "requested Graph app permission: ${perm}"
  done
}

# ── 4. admin consent — the ONE step no script can do for you ───────────────────────────────
admin_consent() {
  step "Admin consent (🔐 Global Admin) — the only human step"
  # try to grant it automatically (works if you're a Global Admin); else fall back to the URL.
  if [ "$DRY_RUN" = 1 ]; then echo "${D}[dry-run] az ad app permission admin-consent --id $APP_ID${N}"; return 0; fi
  if az ad app permission admin-consent --id "$APP_ID" 2>/dev/null; then
    ok "admin consent granted automatically (you're a Global Admin)"
    sleep 10   # let the grant propagate before we call Graph
    return 0
  fi
  warn "couldn't auto-consent (you may not be a Global Admin, or it needs the portal)."
  echo
  echo "  ${B}Open this URL, sign in as a Global Admin, and click ${G}Accept${N}${B}:${N}"
  echo "  ${B}https://login.microsoftonline.com/${TENANT_ID}/adminconsent?client_id=${APP_ID}${N}"
  echo
  read -r -p "  Press Enter once consent is granted... " _
  ok "continuing"
}

# graph client-credentials token (used to browse sites/drives/users)
graph_token() {
  curl -s -X POST "https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/token" \
    -d "client_id=${APP_ID}" -d "scope=https://graph.microsoft.com/.default" \
    -d "client_secret=${CLIENT_SECRET}" -d "grant_type=client_credentials" | jq -r '.access_token // empty'
}

# GET a Graph URL with the app token. Retries while admin consent propagates (it can take
# ~30-60s after Accept). Always prints a JSON body; callers check for .error / .value.
graph_get() {
  local url="$1" tok body code i
  for i in 1 2 3 4 5 6; do
    tok="$(graph_token)"
    if [ -z "$tok" ]; then body='{"error":{"code":"noToken","message":"no Graph token — client secret / app not ready"}}'; sleep 8; continue; fi
    body="$(curl -s -H "Authorization: Bearer $tok" "$url")"
    if echo "$body" | jq -e '.error' >/dev/null 2>&1; then
      code="$(echo "$body" | jq -r '.error.code // empty')"
      case "$code" in
        Authorization_RequestDenied|AccessDenied|Forbidden|InvalidAuthenticationToken|unauthorized_client|noToken)
          [ "$i" -lt 6 ] && { sleep 10; continue; };;   # consent likely still propagating
      esac
    fi
    break
  done
  echo "$body"
}

# fail with the real Graph error + consent guidance (instead of a raw jq crash)
graph_fail() {
  local resp="$1" what="$2"
  warn "Graph error while $what: $(echo "$resp" | jq -r '(.error.code // "unknown") + " — " + (.error.message // "")')"
  echo "  ${D}This almost always means admin consent isn't effective yet. Confirm a Global Admin of"
  echo "  tenant ${TENANT_ID} clicked ${B}Accept${N}${D} on the consent URL, then wait ~30s and re-run"
  echo "  (it's idempotent). If you are NOT a Global Admin, someone who is must grant consent.${N}"
  die "cannot continue until the Graph permissions are consented"
}

# ── 5. pick a SharePoint library + test users ─────────────────────────────────────────────
pick_sharepoint() {
  step "Pick a SharePoint library to index"
  if [ -n "$DRIVE_ID" ]; then ok "using --drive-id ${DRIVE_ID}"; return 0; fi
  if [ "$DRY_RUN" = 1 ]; then DRIVE_ID="<drive-id>"; ok "(dry-run) would list libraries and prompt"; return 0; fi
  local q="${SITE_SEARCH:-*}"
  local resp; resp="$(graph_get "https://graph.microsoft.com/v1.0/sites?search=${q}")"
  echo "$resp" | jq -e '.error' >/dev/null 2>&1 && graph_fail "$resp" "listing SharePoint sites"
  [ "$(echo "$resp" | jq '.value | length')" -gt 0 ] 2>/dev/null || die \
    "no SharePoint sites found in tenant ${TENANT_ID} (search='${q}'). This tenant may have no SharePoint/M365 content yet — create a site + document library first, or pass --site <name>."
  local sites; sites="$(echo "$resp" | jq -c '.value[] | {id,name,web:.webUrl}')"
  echo "  Sites found:"; local i=0; local -a SITE_IDS=()
  while IFS= read -r s; do i=$((i+1)); SITE_IDS+=("$(echo "$s" | jq -r .id)")
    printf "    %d) %s  ${D}%s${N}\n" "$i" "$(echo "$s" | jq -r .name)" "$(echo "$s" | jq -r .web)"; done <<< "$sites"
  local sc; read -r -p "  Pick a site [1-$i]: " sc; local site="${SITE_IDS[$((sc-1))]}"
  local dresp; dresp="$(graph_get "https://graph.microsoft.com/v1.0/sites/${site}/drives")"
  echo "$dresp" | jq -e '.error' >/dev/null 2>&1 && graph_fail "$dresp" "listing document libraries"
  [ "$(echo "$dresp" | jq '.value | length')" -gt 0 ] 2>/dev/null || die "that site has no document libraries — pick another site or add a library."
  local drives; drives="$(echo "$dresp" | jq -c '.value[] | {id,name}')"
  echo "  Libraries:"; i=0; local -a DRIVE_IDS=()
  while IFS= read -r d; do i=$((i+1)); DRIVE_IDS+=("$(echo "$d" | jq -r .id)")
    printf "    %d) %s\n" "$i" "$(echo "$d" | jq -r .name)"; done <<< "$drives"
  local dc; read -r -p "  Pick a library [1-$i]: " dc; DRIVE_ID="${DRIVE_IDS[$((dc-1))]}"
  ok "library drive: ${DRIVE_ID}"
}

pick_test_users() {
  step "Pick test users (to show the permission contrast)"
  if [ -n "$TEST_USERS" ]; then ok "using --test-users ${TEST_USERS}"; return 0; fi
  if [ "$DRY_RUN" = 1 ]; then TEST_USERS="<oid-allowed>,<oid-denied>"; ok "(dry-run) would list users and prompt"; return 0; fi
  local uresp; uresp="$(graph_get "https://graph.microsoft.com/v1.0/users?\$top=25&\$select=id,displayName,userPrincipalName")"
  echo "$uresp" | jq -e '.error' >/dev/null 2>&1 && graph_fail "$uresp" "listing users"
  [ "$(echo "$uresp" | jq '.value | length')" -gt 0 ] 2>/dev/null || die "no users returned — check User.Read.All consent."
  local users; users="$(echo "$uresp" | jq -c '.value[] | {id,name:.displayName,upn:.userPrincipalName}')"
  echo "  ${D}Pick TWO: one who's IN the restricted group, one who is NOT.${N}"
  local i=0; local -a UIDS=()
  while IFS= read -r u; do i=$((i+1)); UIDS+=("$(echo "$u" | jq -r .id)")
    printf "    %d) %s  ${D}%s${N}\n" "$i" "$(echo "$u" | jq -r .name)" "$(echo "$u" | jq -r .upn)"; done <<< "$users"
  local a b; read -r -p "  Allowed user  [1-$i]: " a; read -r -p "  Denied user   [1-$i]: " b
  TEST_USERS="${UIDS[$((a-1))]},${UIDS[$((b-1))]}"
  ok "test users: ${TEST_USERS}"
}

# ── 6. write .env ─────────────────────────────────────────────────────────────────────────
write_env() {
  step "Write .env"
  if [ "$DRY_RUN" = 1 ]; then ok "(dry-run) would write $ENV_FILE"; return 0; fi
  local search_key aoai_key
  search_key="$(az search admin-key show -g "$RG" --service-name "${PREFIX}-search" --query primaryKey -o tsv)"
  aoai_key="$(az cognitiveservices account keys list -g "$RG" -n "${PREFIX}-aoai" --query key1 -o tsv)"
  cat > "$ENV_FILE" <<EOF
# generated by connect-azure.sh — DBSearch.AI live Azure trial ($(az account show --query name -o tsv))
DBSEARCH_BACKEND=azure
DBSEARCH_TENANT_ID=trial
DBSEARCH_EMBEDDING_DIM=1536
AZURE_BLOB_ACCOUNT_URL=${BLOB_URL}
AZURE_BLOB_CONTAINER=dbsearch
AZURE_SERVICEBUS_NAMESPACE=${SB_NS}
AZURE_SEARCH_ENDPOINT=${SEARCH_EP}
AZURE_SEARCH_INDEX=chunks
AZURE_SEARCH_ADMIN_KEY=${search_key}
AZURE_OPENAI_ENDPOINT=${AOAI_EP}
AZURE_OPENAI_API_KEY=${aoai_key}
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=${EMBED_DEP}
AZURE_OPENAI_CHAT_DEPLOYMENT=${CHAT_DEP}
AZURE_DOCINTEL_ENDPOINT=${DOCINTEL_EP}
AZURE_KEYVAULT_URL=${KV_URL}
AZURE_TENANT_ID=${TENANT_ID}
AZURE_CLIENT_ID=${APP_ID}
AZURE_CLIENT_SECRET=${CLIENT_SECRET}
SHAREPOINT_DRIVE_ID=${DRIVE_ID}
AZURE_TEST_USER_OIDS=${TEST_USERS}
EOF
  chmod 600 "$ENV_FILE"
  ok "wrote ${ENV_FILE} (chmod 600 — contains secrets, gitignored)"
}

# ── 7. install deps + ingest + launch the query UI ────────────────────────────────────────
launch() {
  step "Install, ingest, and launch the query UI"
  if [ "$DRY_RUN" = 1 ]; then
    ok "(dry-run) would: create venv → pip install -e '.[azure,server]' → python scripts/azure_ingest.py → uvicorn on :$PORT"
    return 0
  fi
  cd "$REPO_ROOT"
  [ -d .venv ] || python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -q -e '.[azure,server]'   # azure SDKs + openai (AOAI) + fastapi/uvicorn (the UI)
  set -a; # shellcheck disable=SC1090
  source "$ENV_FILE"; set +a
  export SELFHOST_BACKEND=azure PYTHONPATH="$REPO_ROOT/src"
  python scripts/azure_ingest.py
  ok "starting the query server on http://localhost:${PORT} (Ctrl-C to stop)"
  echo "  ${D}Ask e.g. \"what proposals have we written?\" and flip the user switcher to see LAW-2 trimming.${N}"
  ( sleep 2; (command -v open >/dev/null && open "http://localhost:${PORT}") || (command -v xdg-open >/dev/null && xdg-open "http://localhost:${PORT}") || true ) &
  exec uvicorn dbsearch.server.app:app --host 127.0.0.1 --port "$PORT"
}

# ── run ───────────────────────────────────────────────────────────────────────────────────
preflight
provision
load_outputs
grant_rbac
entra_app
admin_consent
pick_sharepoint
pick_test_users
write_env
launch
