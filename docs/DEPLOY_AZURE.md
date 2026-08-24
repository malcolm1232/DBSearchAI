# Phase 1b — Going Real on Azure (Runbook)

> **Just want to try it? One command:**
> ```bash
> ./scripts/connect-azure.sh          # (add --dry-run first to preview, spend nothing)
> ```
> It automates every step below — provision → RBAC → Entra app → (pauses for the one human
> step, admin consent) → pick a SharePoint library → ingest → open the query UI at
> http://localhost:8080. Idempotent/resumable. The manual runbook below is the reference for
> what it does under the hood (and for debugging).

This stands up a real single-tenant data plane and runs a live end-to-end query against
your SharePoint. Steps marked **🔐 you** require interactive auth/consent only you can do
(Anthropic/Claude can't and shouldn't hold your Azure credentials).

> Outcome: `python scripts/smoke_azure.py` ingests a real SharePoint library and answers a
> question with citations, security-trimmed by real Entra group membership (LAW 2 on live data).

---

## 0. Prerequisites (🔐 you)
- An **Azure subscription** with rights to create resources.
- **Azure OpenAI access** enabled on that subscription (in some tenants this needs a one-time
  enablement — check the Azure OpenAI blade).
- Tools: `az` CLI (`az login`), Python 3.10+, and this repo cloned.
- A **SharePoint site** with a document library you can read (put a few PDFs/decks in it).

## 1. Provision the data-plane resources
```bash
az login                                            # 🔐 you
az group create -n dbsearch-acme -l eastus
az deployment group create -g dbsearch-acme \
  -f infra/main.bicep -p namePrefix=dbsacme         # adjust prefix (lowercase, ≤11 chars)
```
Copy the deployment **outputs** (blob/search/aoai/docintel/kv endpoints) into `.env`
(start from `.env.example`). Grab the keys:
```bash
az search admin-key show -g dbsearch-acme --service-name dbsacme-search   # -> AZURE_SEARCH_ADMIN_KEY
az cognitiveservices account keys list -g dbsearch-acme -n dbsacme-aoai   # -> AZURE_OPENAI_API_KEY
```

## 2. Register the Entra app (Graph access) (🔐 you)
The app lets DBSearch read SharePoint content + ACLs and expand users' groups.
```bash
az ad app create --display-name "DBSearch-acme"                           # note the appId
az ad app credential reset --id <appId>                                   # note the password (secret)
```
Then in **Entra admin center → App registrations → DBSearch-acme → API permissions**, add
**Application** permissions for Microsoft Graph and click **Grant admin consent** (🔐 you):
- `Sites.Read.All`  (SharePoint content)
- `Files.Read.All`  (drive items + permissions)
- `GroupMember.Read.All`  (transitive group expansion for security trimming)
- `User.Read.All`

Put `AZURE_TENANT_ID` (directory id), `AZURE_CLIENT_ID` (appId), and `AZURE_CLIENT_SECRET`
(the password) into `.env`.

> Least-privilege note (LAW 3): these are **read-only** scopes. Don't add write scopes.

## 3. Find your SharePoint drive id
```bash
# site id:
az rest --method get --url "https://graph.microsoft.com/v1.0/sites?search=<your-site-name>"
# drive id for that site:
az rest --method get --url "https://graph.microsoft.com/v1.0/sites/<siteId>/drives"
```
Put the document library's `id` into `SHAREPOINT_DRIVE_ID`.

## 4. Install + create the index
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[azure]'
```

## 5. Run the live smoke test
```bash
set -a; source .env; set +a            # load env
export TEST_USER_OID=<an Entra user object id>
python scripts/smoke_azure.py
```
You should see an ingest log, then a cited answer drawn only from documents that user is
allowed to see.

## 6. Verify LAW 2 on real data (the important bit)
Run step 5 twice:
- once as a user who **can** see a restricted document, and
- once as a user who **cannot** (not in its SharePoint group).

The restricted doc must appear in the first run's citations and **never** in the second's.
That's permission-faithful retrieval proven on live SharePoint ACLs.

---

## Known Phase-1b limits (hardened in Phase 3)
- **ACL mapping** covers user/group grants from Graph `permissions`; SharePoint-group nesting,
  sharing links, and external/guest shares need fuller handling. Unmappable grants are
  treated as **deny** (default-deny) — safe, but may under-return until Phase 3.
- **Ingestion** runs as a one-shot drain (the smoke script), not long-running autoscaling
  workers — that's Phase 4. The stage code is identical; only the host changes.
- **Auth** uses client-secret + admin keys for speed. Move to **managed identity + Key Vault**
  (the adapters already accept `DefaultAzureCredential`) before any real customer.
- **No private endpoints / CMK yet** — add before a customer security review (ARCHITECTURE.md §7).
