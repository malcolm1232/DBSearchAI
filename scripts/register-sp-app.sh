#!/usr/bin/env bash
#
# register-sp-app.sh — one-time setup for the in-app 'Add SharePoint' connector (card #148).
#
# Registers DBSearch ONCE as a MULTI-TENANT Entra app (web redirect + read-only Graph app
# permissions) and prints the SP_CONNECTOR_* env the server needs. After this, ANY customer
# just clicks 'Add SharePoint' in the UI and consents to this app in THEIR tenant — no
# per-customer app. Run it in YOUR (the DBSearch operator's) Azure tenant.
#
#   ./scripts/register-sp-app.sh                                   # localhost redirect
#   ./scripts/register-sp-app.sh --redirect https://app.example.com/connectors/sharepoint/callback
#   ./scripts/register-sp-app.sh --dry-run
#
# Requires: az (logged in), jq.
#
set -euo pipefail

APP_NAME="${APP_NAME:-DBSearch SharePoint Connector}"
REDIRECT="${REDIRECT:-http://localhost:8080/connectors/sharepoint/callback}"
DRY_RUN=0

GRAPH_APP="00000003-0000-0000-c000-000000000000"
GRAPH_PERMS=(
  "Sites.Read.All=332a536c-c7ef-4017-ab91-336970924f0d"
  "Files.Read.All=01d4889c-1287-42c6-ac1f-5d1e02578ef6"
  "GroupMember.Read.All=98830695-27a2-44f7-8c18-0c3ebc9698f6"
  "User.Read.All=df021288-bdef-4463-88db-98f22de89214"
)

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; D=$'\033[2m'; N=$'\033[0m'; else B=; G=; D=; N=; fi
die() { echo "✗ $*" >&2; exit 1; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "${D}[dry-run] $*${N}"; else eval "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --redirect) REDIRECT="$2"; shift;;
    --name) APP_NAME="$2"; shift;;
    --dry-run) DRY_RUN=1;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown arg: $1";;
  esac
  shift
done

command -v az >/dev/null || die "az CLI not found"
command -v jq >/dev/null || die "jq not found"
az account show >/dev/null 2>&1 || die "run 'az login' first"

echo "${B}▶ Registering multi-tenant app '${APP_NAME}'${N}  (redirect: ${REDIRECT})"

# find-or-create, multi-tenant (AzureADMultipleOrgs) with the web redirect
APP_ID="$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv 2>/dev/null || true)"
if [ -z "${APP_ID:-}" ] || [ "$APP_ID" = "null" ]; then
  if [ "$DRY_RUN" = 1 ]; then echo "${D}[dry-run] az ad app create --display-name '$APP_NAME' --sign-in-audience AzureADMultipleOrgs --web-redirect-uris '$REDIRECT'${N}"; APP_ID="<app-id>"; else
    APP_ID="$(az ad app create --display-name "$APP_NAME" \
      --sign-in-audience AzureADMultipleOrgs \
      --web-redirect-uris "$REDIRECT" --query appId -o tsv)"; fi
  echo "${G}✓${N} created app ${APP_ID}"
else
  run "az ad app update --id '$APP_ID' --sign-in-audience AzureADMultipleOrgs --web-redirect-uris '$REDIRECT'"
  echo "${G}✓${N} app exists — ensured multi-tenant + redirect (${APP_ID})"
fi

# service principal + read-only Graph app permissions
run "az ad sp create --id '$APP_ID' -o none 2>/dev/null || true"
for entry in "${GRAPH_PERMS[@]}"; do
  perm="${entry%%=*}"; rid="${entry#*=}"
  run "az ad app permission add --id '$APP_ID' --api '$GRAPH_APP' --api-permissions '${rid}=Role' -o none 2>/dev/null || true"
  echo "${G}✓${N} Graph app permission: ${perm}"
done

# client secret
if [ "$DRY_RUN" = 1 ]; then SECRET="<secret>"; echo "${D}[dry-run] az ad app credential reset --id $APP_ID${N}"; else
  SECRET="$(az ad app credential reset --id "$APP_ID" --query password -o tsv)"; fi

cat <<EOF

${B}Done.${N} Set these on the DBSearch server, then restart it:

  export SP_CONNECTOR_CLIENT_ID='${APP_ID}'
  export SP_CONNECTOR_CLIENT_SECRET='${SECRET}'
  export SP_CONNECTOR_REDIRECT_URI='${REDIRECT}'

Notes:
  • This app is registered ONCE (here). Customers do NOT create an app — they click
    'Add SharePoint' in the UI and a Global Admin in THEIR tenant grants consent.
  • The redirect URI above MUST exactly match SP_CONNECTOR_REDIRECT_URI and the server's
    public URL. For a deployed instance re-run with --redirect https://<host>/connectors/sharepoint/callback
  • To test end-to-end you need a real SharePoint tenant (e.g. an M365 E5 trial). See
    docs/SHAREPOINT_CONNECTOR.md.
EOF
