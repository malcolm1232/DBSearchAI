# Security policy

DBSearch.AI's central promise is that it never returns a result the person asking is not
already authorised to see. A bug that breaks that promise is the most serious kind of bug this
project can have, and we would much rather hear about it from you than from a customer.

## Reporting a vulnerability

Preferably use GitHub's private vulnerability reporting: go to the **Security** tab of this
repository and choose **Report a vulnerability**. That keeps the whole exchange private until a
fix ships, and it reaches the maintainers directly.

If you would rather not use GitHub, email **security@dbsearch.ai**.

Please do not open a public issue for anything that could be used to read data belonging to
someone else.

What helps most, roughly in order:

- what you did, concretely enough to repeat — the request, the identity you were signed in as,
  and the identity whose data you reached;
- what you expected instead;
- the deployment mode: self-host from this repo, in-tenant, or the hosted demo at dbsearch.ai;
- the commit or image you were running.

You will get an acknowledgement within **3 working days** and an assessment within **10**. This
is a small project, so those are honest targets rather than a contractual SLA. If you have not
heard back by then, please chase it rather than assuming it was seen.

Please give us **90 days** before publishing, or less if we agree a shorter window. We will
credit you in the fix commit and release notes unless you would rather stay anonymous.

## What is in scope

Anything reachable in this repository, especially:

- **Permission trimming (LAW 2).** Any path where a caller receives content, a citation, a
  store name, a routing explanation, or an error that distinguishes "you may not see this" from
  "this does not exist", for data they are not entitled to.
- **Identity.** Anything that lets a caller act as another user, keep access after logout or
  revocation, or have someone else's stored credential used on their behalf.
- **Tenant and workspace isolation.** Any crossing between accounts, tenants or per-owner
  document partitions.
- **The control/data-plane boundary (LAW 1).** Any path that moves document content into the
  control plane's uplink.
- **Sharing.** Share links that outlive a revocation, widen beyond what was granted, or expose
  documents that were never selected.
- **Stored credentials.** Anything that reads a secret back out, logs it, or returns it to a
  client.

## What is not in scope

- **The hosted demo at dbsearch.ai is a demo.** It runs on our infrastructure with a
  third-party chat provider, seeded with sample data. Please do not put confidential material
  there, and do not report "the operator can see demo data" — that is documented, not a defect.
- **`DBSEARCH_DEV_AUTH=1` trusting the `X-DBSearch-User` header.** That switch exists to make
  local development possible, is off by default, and the documentation says plainly that
  turning it on hands every document to anyone who can reach the port. Turning it on and then
  reaching the port is the documented behaviour of the switch.
- Findings that require an attacker to already be an operator of the deployment, or to already
  hold the session-signing key.
- Missing hardening headers, rate-limit tuning, or scanner output with no demonstrated impact.
- Denial of service through sheer volume.

## Known-weak areas

We would rather point at these than have you spend time discovering them:

- The **control plane runs in-process** today. The boundary validator is real and tested, but
  there is no network separation or mTLS deployed, so LAW 1 is enforced by a code boundary
  rather than by topology.
- **Ingestion is not authenticated** in the self-host edition.
- Open work is tracked publicly on the issue tracker, including permission and scoping issues
  we already know about. Please still report anything you find — a duplicate costs us nothing.
