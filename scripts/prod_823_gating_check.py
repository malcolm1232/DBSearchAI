"""#823 - the PROD proof that adding a source is gated on sign-in and on the provider link.

The jsdom tests prove the RULE against a stubbed /auth/me. This proves the WIRING: a real
deployment, the real /auth/me, and the two states that actually exist on prod right now.

  layer 1 - a visitor with no cookie at all: every provider offers sign-in, none offers a
            service. (On prod an anonymous visitor is in demo mode, and /router/demo answers
            401 for them anyway, so a palette that looked live was offering what the server
            refuses.)
  layer 2 - bob, signed in: every cloud provider must match his REAL link state, read from
            /auth/me rather than assumed. Today that state is mixed (entra vaulted during the
            interactive sign-in in #822, google and aws not), so one run exercises both
            branches: Azure and Microsoft 365 open, Google and AWS offer "Connect your <x>
            account". Files & Local offers its services either way, needing only an account.
            The run fails if the identity is one-sided, because a gate that never opens and a
            gate that never closes both pass a one-branch check.

Read entirely from the DOM, so it is cheap and repeatable. The pixel judgment is a separate
Claude-in-Chrome pass; this is the assertion.

    python scripts/prod_823_gating_check.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pw_dbs_auth import DEFAULT_BASE, DEFAULT_SSH_HOST, authed_pages  # noqa: E402

CLOUD = {"azure": "Azure", "google": "Google Cloud", "aws": "AWS", "m365": "Microsoft 365"}
# The vault idp each rail group needs, mirroring the canvas PROVIDERS table. Azure and
# Microsoft 365 share entra, which is why link state is read per idp, not per group.
IDP = {"azure": "entra", "google": "google", "aws": "aws", "m365": "entra"}
FILES = "Files & Local"

# Open each provider row and report what its flyout offers. Mirrors the jsdom probe, but
# against the deployed bundle: rows are found by the label a person reads.
_PROBE = """
(labels) => {
  const out = {};
  for (const label of labels) {
    const row = [...document.querySelectorAll('#rail .prov')]
      .find(r => r.querySelector('.pn b')?.textContent.trim() === label);
    if (!row) { out[label] = {present:false}; continue; }
    row.dispatchEvent(new MouseEvent('click', {bubbles:true}));
    const menu = document.getElementById('provmenu');
    const cta = menu.querySelector('.gate-cta');
    out[label] = {
      present: true,
      svcCount: menu.querySelectorAll('.svc').length,
      ctaText: cta ? cta.textContent.trim() : null,
      gateText: menu.querySelector('.gate-msg')?.textContent.trim() || null,
    };
  }
  return out;
}
"""


def _probe(page, base: str) -> dict:
    page.goto(f"{base}/canvas", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)          # boot resolves /auth/me before the rail paints
    return page.evaluate(_PROBE, list(CLOUD.values()) + [FILES])


def main() -> int:
    ap = argparse.ArgumentParser(description="#823 prod gating check")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    failures = []

    with authed_pages(("bob",), base=base, ssh_host=args.ssh_host) as pages:
        bob = pages["bob"]

        # ---- layer 1: no cookie at all -------------------------------------------------
        anon = bob.context.browser.new_context().new_page()
        try:
            rows = _probe(anon, base)
            for label, r in rows.items():
                if not r.get("present"):
                    failures.append(f"[anon] the {label} row is missing from the rail")
                    continue
                if r["svcCount"]:
                    failures.append(
                        f"[anon] {label} offers {r['svcCount']} services to a visitor with no "
                        "account, so the canvas offers what the server refuses")
                if not (r["ctaText"] or "").lower().startswith("sign in"):
                    failures.append(f"[anon] {label} offers no sign-in affordance: {r}")
            if not failures:
                print(f"[layer 1] anonymous: all {len(rows)} providers offer sign in, none "
                      "offers a service")
        finally:
            anon.context.close()

        # ---- layer 2: bob, signed in, with whatever he has actually linked --------------
        # Expectations come from the LIVE /auth/me, never from a hardcoded belief about what
        # bob has vaulted. The first version of this check assumed "bob has linked nothing"
        # from a handover note and reported a defect that did not exist: he vaulted an entra
        # refresh token during the real interactive sign-in in #822. Reading the truth also
        # makes one run cover BOTH branches, since bob has entra linked and google/aws not.
        me = bob.request.get(f"{base}/auth/me").json()
        linked = set(me.get("linked") or [])
        assert me.get("signed_in"), f"bob is not signed in, so layer 2 proves nothing: {me}"
        print(f"[layer 2] bob linked={sorted(linked) or 'nothing'}")
        rows = _probe(bob, base)
        for key, label in CLOUD.items():
            r = rows[label]
            if IDP[key] in linked:
                if not r["svcCount"]:
                    failures.append(
                        f"[bob] {label} withholds its services although {IDP[key]} IS linked")
                if r["ctaText"]:
                    failures.append(f"[bob] {label} nags to connect an already-linked "
                                    f"provider: {r['ctaText']!r}")
            else:
                if r["svcCount"]:
                    failures.append(
                        f"[bob] {label} offers {r['svcCount']} services with {IDP[key]} NOT "
                        "linked, so every one of them composes to a credential error")
                if "connect" not in (r["ctaText"] or "").lower():
                    failures.append(
                        f"[bob] {label} does not offer to connect: {r['ctaText']!r}")
        gated = [k for k in CLOUD if IDP[k] not in linked]
        open_ = [k for k in CLOUD if IDP[k] in linked]
        if not gated or not open_:
            failures.append(
                f"this identity exercises only one branch (gated={gated}, open={open_}), so "
                "the run cannot show the gate discriminating; use one with a mixed link state")
        files = rows[FILES]
        if not files["svcCount"]:
            failures.append(
                "[bob] Files & Local is gated for a signed-in user, so a hosted user who has "
                "linked no cloud cannot add ANY source")
        if files["ctaText"]:
            failures.append(f"[bob] Files & Local nags for a link it does not need: {files}")
        if not failures:
            print("[layer 2] each cloud provider matches bob's real link state, and "
                  "Files & Local offers its services:")
            for key, label in CLOUD.items():
                state = "linked" if IDP[key] in linked else "not linked"
                offer = rows[label]["ctaText"] or f"{rows[label]['svcCount']} services"
                print(f"          {label} ({IDP[key]}, {state}): {offer}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("\nPASS - #823 proved on prod: both gating layers, real /auth/me, real bundle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
