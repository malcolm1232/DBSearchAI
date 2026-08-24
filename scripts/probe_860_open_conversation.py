#!/usr/bin/env python3
"""#860 - does clicking a conversation in Recents actually render the thread?

WHY THIS IS A STANDING SCRIPT RATHER THAN A ONE-OFF. #860 was reported from prod with a
precise and alarming signature - the transcript GET returns 200 with real turns and the
surface keeps showing the empty state - and could not be reproduced afterwards. The card is
blocked on a reproduction, and the ONE thing that would reopen it is a click where a
transcript 200 and the empty state coincide. Rebuilding a rig from the card's prose each time
that question comes up is how a defect stays unfalsifiable, so the rig lives here.

WHAT IT MEASURES, rather than infers:

  · every append into the thread, with `isConnected` on the target - a render into a DETACHED
    tree is the failure mode the card hypothesised, and it is invisible in every other
    channel: HTTP 200, no throw, no catch, no console output
  · every innerHTML wipe, with the call stack - so a router remount landing mid-await names
    itself instead of being deduced
  · every /conversations/* fetch, with the call stack - a COUNT cannot tell a mount's list
    load from a click's transcript load, and #860's original network reading turned on
    exactly that ambiguity
  · popstate / hashchange / pushState - the only three things that can drive router.render()
    after boot, so "something remounted" becomes a named trigger or nothing

A GREEN RUN IS ONLY MEANINGFUL BECAUSE THE RIG CAN GO RED. The probe reports
`targetConnected` on every append and a stack on every wipe; if the defect were present, the
detached append prints live=false and the remount prints its render() stack. Do not read a
pass as "the surface works" without that - read it as "the failure signature was looked for
and was absent".

USAGE
    python3 scripts/probe_860_open_conversation.py                 # prod, every Recents row
    python3 scripts/probe_860_open_conversation.py --rows 5        # just the first 5
    python3 scripts/probe_860_open_conversation.py --identity bob
    python3 scripts/probe_860_open_conversation.py --base http://localhost:8080 --local-mint

Exit status is 0 when every row rendered, 1 when any row finished on the empty state - so it
can gate a deploy as well as answer a question.

Preconditions are pw_dbs_auth.py's: ssh alias `dbsprod` reachable (prod), the untracked
secrets/entra_test_users.env populated, and playwright chromium installed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pw_dbs_auth import authed_page, mint_cookie  # noqa: E402

DEFAULT_BASE = "https://dbsearch.ai"

#: Armed BEFORE any page script runs, so the click is already instrumented when it happens.
#: Patching is confined to recording - every hook calls through to the original.
INIT = r"""
(() => {
  const LOG = []; window.__p860 = LOG;
  const t0 = performance.now(); const st = () => +(performance.now() - t0).toFixed(1);
  // Only frames naming our own modules; the patch frames themselves are noise.
  const stack = () => (new Error().stack || "").split("\n").slice(2)
      .filter(l => /static\/js/.test(l))
      .map(l => l.trim().replace(/https?:\/\/[^\/]+\/static\/js\//, "").replace(/\?v=[^:]*/, ""))
      .slice(0, 5);
  const rec = (kind, extra) => LOG.push(Object.assign({t: st(), kind}, extra || {}));
  window.__mark = (m) => rec("MARK", {m});
  const EMPTY = "Search your company knowledge";

  const of = window.fetch;
  window.fetch = function (u) {
    const s = String(u && u.url ? u.url : u);
    if (/\/conversations|\/ask\/suggestions/.test(s))
      rec("fetch", {url: s.replace(/^https?:\/\/[^\/]+/, ""), from: stack()});
    return of.apply(this, arguments);
  };

  const note = (tg, ns) => {
    for (const n of ns) {
      if (!n || (n.nodeType !== 1 && n.nodeType !== 11)) continue;   // 11 = transcriptTurn's fragment
      const tx = n.textContent || "";
      const cl = String((n.className && n.className.baseVal !== undefined)
                        ? n.className.baseVal : (n.className || ""));
      const id = (tg && tg.id) || ""; const tc = String((tg && tg.className) || "");
      if (tx.indexOf(EMPTY) !== -1)
        rec("EMPTY_PAINTED", {into: id || tc, live: !!(tg && tg.isConnected), from: stack()});
      else if (cl.indexOf("chat-empty") !== -1)
        rec("loading", {text: tx.slice(0, 20), live: !!(tg && tg.isConnected)});
      else if (id === "thread" || tc.indexOf("chat-thread") !== -1)
        rec("TURN_APPEND", {live: !!(tg && tg.isConnected), text: tx.slice(0, 40), from: stack()});
    }
  };
  const oA = Element.prototype.append;
  Element.prototype.append = function (...n) { note(this, n); return oA.apply(this, n); };
  const oAC = Node.prototype.appendChild;
  Node.prototype.appendChild = function (n) { note(this, [n]); return oAC.call(this, n); };

  const d = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
  Object.defineProperty(Element.prototype, "innerHTML", {
    configurable: true, enumerable: d.enumerable, get: d.get,
    set: function (v) {
      const id = this.id || ""; const cl = String(this.className || "");
      if (id === "thread" || id === "surface" || /chat-thread|navrail-slot/.test(cl))
        rec("WIPE", {on: id || cl, live: this.isConnected, from: stack()});
      return d.set.call(this, v);
    },
  });

  // The only three things that can drive router.render() after boot. If none of them fires
  // and the surface still remounts, the remount hypothesis is dead rather than unproven.
  addEventListener("popstate", () => rec("popstate"), true);
  addEventListener("hashchange", () => rec("hashchange"), true);
  const oP = history.pushState;
  history.pushState = function () { rec("pushState", {to: arguments[2]}); return oP.apply(this, arguments); };
})();
"""

STATE = r"""(() => {
  const th = document.getElementById("thread");
  const ce = document.querySelector("#thread .chat-empty");
  return {children: th ? th.children.length : null,
          empty: !!ce, emptyText: ce ? (ce.textContent || "").slice(0, 40) : null,
          text: th ? (th.textContent || "").slice(0, 70) : null,
          threads: document.querySelectorAll("#thread").length,
          rows: document.querySelectorAll(".rail-thread").length};
})()"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity", default="jjjj636")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--rows", type=int, default=0, help="0 = every row")
    ap.add_argument("--settle", type=int, default=1500, help="ms to wait after each click")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()

    cookie = mint_cookie(a.identity)
    failures, opened = [], 0

    with authed_page(a.identity, base=a.base, headed=a.headed, cookie=cookie) as (page, _b):
        page.context.add_init_script(INIT)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))

        page.goto(f"{a.base}/ask", wait_until="domcontentloaded")
        page.wait_for_selector(".rail-thread", timeout=20000)
        page.wait_for_timeout(1200)

        total = len(page.query_selector_all(".rail-thread"))
        n = total if a.rows <= 0 else min(a.rows, total)
        print(f"[probe860] {a.base} as {a.identity}: {total} rows, driving {n}\n")

        for i in range(n):
            rows = page.query_selector_all(".rail-thread")
            if i >= len(rows):
                break
            title = (rows[i].get_attribute("title") or "").replace("\n", " | ")[:52]
            page.evaluate(f"window.__mark({json.dumps('row ' + str(i))})")
            rows[i].click()
            page.wait_for_timeout(a.settle)
            s = page.evaluate(STATE)
            ok = bool(s["children"]) and not s["empty"]
            opened += ok
            print(f"  row {i:>3}  {'ok ' if ok else 'FAIL'}  {title!r}")
            if not ok:
                failures.append({"row": i, "title": title, "state": s})
                # The whole point of the card: pair the fetch with the paint for THIS click.
                log = page.evaluate("window.__p860")
                since = []
                for e in reversed(log):
                    since.append(e)
                    if e.get("kind") == "MARK":
                        break
                print(json.dumps(list(reversed(since)), indent=2)[:2500])

        # A detached append is the hypothesised failure and is silent everywhere else.
        detached = page.evaluate(
            "(window.__p860||[]).filter(e=>e.kind==='TURN_APPEND'&&e.live===false).length")
        # COUNT THE CALL, NOT THE APPENDS. One showEmptyState() produces THREE matching
        # appends - the <h2> into the .chat-empty, the .chat-empty into #thread, and the
        # scroller into #surface - because each of those subtrees contains the string. Only
        # the append whose target is #thread is one-per-call, so that is what is counted; the
        # naive total reads as three mounts for one and invites exactly the miscount that put
        # "four /conversations/mine calls" on this card in the first place.
        remounts = page.evaluate(
            "(window.__p860||[]).filter(e=>e.kind==='EMPTY_PAINTED'&&e.into==='thread').length")

    print(f"\n[probe860] opened {opened}/{n}")
    print(f"[probe860] appends into a DETACHED thread: {detached}   "
          f"(non-zero = #860's hypothesised mechanism, reopen the card)")
    print(f"[probe860] showEmptyState() calls: {remounts} "
          f"(exactly 1 per page load is expected; more means the surface remounted)")
    print(f"[probe860] page errors: {errors or 'none'}")
    if failures:
        print(f"\n[probe860] FAILED rows: {[f['row'] for f in failures]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
