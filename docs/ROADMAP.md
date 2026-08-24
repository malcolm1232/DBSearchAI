# Roadmap — Architecturally Correct from Day 1, Built in Slices

> The point of this order: the **architecture is correct from Phase 0**, so each later phase is
> additive (a config/adapter change), never a rewrite. Each phase lists the LAWs it must satisfy
> — the Gate (SKILL.md §1) is run against them.

## Phase 0 — Foundations (no feature code until this exists)  — ✅ DONE
- Repo + module layout; **ports/adapters skeleton** (every `*Port` from `ARCHITECTURE.md` §2).
- **IaC** (Bicep/Terraform) for the data-plane module — a tenant is `terraform apply`.
- **Boundary-contract schema** (`boundary.schema.json`) + the wire validator that enforces it.
- `SKILL.md` (LAWs + Gate), this roadmap, and the ADRs.
- **Satisfies/sets up:** LAW 1, 6, 7. *Exit test:* skeleton compiles; wire rejects a payload
  carrying document text.

## Phase 1 — Prove the spine (1 connector, 1 tenant, manual deploy)  — ✅ DONE
### Phase 1b — Real Azure adapters
- **🟢 PROVEN on live Azure (the retrieval core):** deployed the Bicep to a real
  subscription and ran `scripts/smoke_azure_search.py` — real Azure OpenAI embeddings
  (text-embedding-3-small) + real **Azure AI Search security-trim** + real gpt-4.1-mini
  cited answers. LAW 2 holds on live infra: a user off the deal team is denied the
  confidential doc (the LLM can't answer because the trim removed it from context).
  Live run caught two real bugs: AI Search forbids '#' in doc keys (now base64url-encoded)
  and reserves Service Bus names ending in '-sb' (renamed to '-bus'); gpt-4o-mini was
  deprecated (now gpt-4.1-mini).
- **🟡 Coded + compile-checked, not yet wired live:** Blob + Service Bus (used in-memory
  for the smoke run; need RBAC roles for the logged-in identity), the Graph SharePoint
  connector + Entra identity (need the Entra app + admin consent — the 🔐 steps in
  `docs/DEPLOY_AZURE.md`), Doc Intelligence OCR.
- SharePoint (Graph) → Service Bus → parse/OCR → chunk+embed → Azure AI Search.
- Query API: hybrid retrieve → **security-trim** → rerank → Azure OpenAI **cited answer**.
- **Satisfies:** LAW 2, 3, 4. *Exit test:* end-to-end cited answer **and** the cross-user
  permission test passes (user A cannot retrieve user B's private doc).

## Phase 2 — Control plane (minimal)  — ✅ DONE (in-memory; mTLS wire is Phase 2b)
- Central **release push** + signed in-tenant agent; **metering** (counters) + **health**.
- Onboard a **2nd tenant** without hand-holding.
- **Satisfies:** LAW 1, 5, 8. *Exit test:* two isolated tenants; control plane shows health/cost
  with **zero** customer content having crossed the wire (audit the boundary log).

## Phase 3 — Connector framework hardening
- Robust **incremental sync** + delta cursors, **ACL freshness**/revocation propagation, OCR for
  hard PDFs, dead-letter handling. Add Teams/Outlook + Confluence/Jira.
- **Satisfies:** LAW 2, 3. *Exit test:* revoke a permission at the source → result disappears
  within one sync cycle.

## Phase 4 — Scale & answer quality
- Independent **autoscaling** per stage; reranking; an **answer-quality eval harness**
  (retrieval precision/recall + answer faithfulness, run in CI). Admin console.
- **Satisfies:** LAW 4, 6, 8. *Exit test:* a multi-TB crawl runs without degrading live queries;
  eval scores tracked over time.

## Phase 5 — Portability & editions
- **AWS adapter** (SQS/S3/OpenSearch/Bedrock/IAM) behind the existing ports — **no core change**.
- **Air-gapped edition** = `uplink.enabled = false`. Azure Marketplace listing.
- **Satisfies:** LAW 7. *Exit test:* the same core runs on AWS by swapping adapters only;
  air-gapped tenant runs with the wire off.

## Guardrails for every phase
- Run the **Gate** (SKILL.md §1) before building each slice.
- Vertical slices end-to-end; don't build a layer ahead of a working spine.
- Any one-way-door decision → an **ADR** first.
