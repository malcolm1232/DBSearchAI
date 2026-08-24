# tests/e2e_880_ingest_modal.py
"""End-to-end, in a real browser: the SharePoint ingest modal OUTLIVES the job (#880), and
the picker opens instantly and reads as a choice (#879).

    python3 tests/e2e_880_ingest_modal.py

WHY THIS IS A BROWSER TEST AND NOT AN API ONE. Every endpoint this exercises was already
correct before #880: POST /connectors/sharepoint/finish returned a job_id, GET
/ingest/jobs/{id} reported every phase and the terminal status. The whole defect lived in
what the SURFACE did with them - it treated the 202 "queued" as "finished", closed itself,
and wrote an absent field onto the node. An API drive cannot see any of that.

WHY THE GRAPH AND JOB ROUTES ARE INTERCEPTED rather than run for real. The properties under
test are about TIME: the modal must still be on screen while the job is mid-flight, the
libraries must not be awaited before the dialog paints, and the count must be re-read only
once the job reports terminal. A real crawl finishes when it finishes; holding it at
"fetching" long enough to assert against is not something a test can ask of it. So the job
state machine is driven from here, through the REAL 202-then-poll contract the server
publishes, and every line of canvas.js under test is the shipped one.

The four properties are asserted separately on purpose (#880 named them separately):
  P1  the modal outlives the job
  P2  it shows real phases, not a spinner - including `skipping`, which used to blank it
  P3  completion is stated in words and dismissed by the USER, not by the code
  P4  the node's count is re-read from the finished job
"""
import json
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dbsearch.server.app import app  # noqa: E402
from _e2eserver import serving  # noqa: E402

HOST, PORT = "127.0.0.1", 8091
BASE = f"http://{HOST}:{PORT}"

TENANT = "contoso-tid"

# Seven libraries all called "Documents", four of them SharePoint/Viva plumbing - the shape of
# the owner's own tenant, which is what made him ask "since when i have so many docs?".
DRIVES = [
    {"siteId": "s1", "siteName": "QuantifyMe.AI", "driveId": "d1", "driveName": "Documents",
     "web": "https://contoso.sharepoint.com/sites/QuantifyMe", "system": False},
    {"siteId": "s2", "siteName": "Deal Team", "driveId": "d2", "driveName": "Documents",
     "web": "https://contoso.sharepoint.com/sites/DealTeam", "system": False},
    {"siteId": "s3", "siteName": "contentTypeHub", "driveId": "d3", "driveName": "Documents",
     "web": "https://contoso.sharepoint.com/sites/cth", "system": True},
    {"siteId": "s4", "siteName": "allcompany", "driveId": "d4", "driveName": "Documents",
     "web": "https://contoso.sharepoint.com/sites/ac", "system": True},
]

# The job, driven from here. `phase` walks the pipeline the runner really emits, `skipping`
# included - it is in this list because STEP_OF["skipping"] used to be undefined, so paint(-1)
# cleared every dot and a resumed crawl looked like it had restarted from nothing.
PHASES = ["discovering", "fetching", "skipping", "extracting", "embedding", "indexing"]
job = {"status": "running", "phase": "discovering", "done": 0, "total": 5, "skipped": 1}
throttle = {"on": False}   # when True the job route answers 429, as prod did
job_hits = {"n": 0}        # how often the surface actually asked
sources_doc_count = {"n": 0}


def _install_routes(page):
    def j(route, payload, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    page.route("**/connectors/sharepoint/status*", lambda r: j(r, {
        "configured": True, "connected": [{"tenant": TENANT}]}))
    page.route("**/admin/sources*", lambda r: j(r, [{
        "source_id": f"sharepoint:{TENANT}", "kind": "sharepoint",
        "display_name": "SharePoint", "doc_count": sources_doc_count["n"],
        "last_sync_at": None, "status": "ok"}]))
    # Deliberately slow: the dialog must be usable long before this resolves. 1.5s stands in
    # for the 11.2s measured on prod.
    page.route("**/connectors/sharepoint/drives*",
               lambda r: (page.wait_for_timeout(1500), j(r, DRIVES))[-1])
    page.route("**/connectors/sharepoint/finish*", lambda r: j(r, {
        "status": "ingesting", "source_id": f"sharepoint:{TENANT}", "tenant": TENANT,
        "drive_id": "d1", "job_id": "job-880", "job_status": "queued"}, status=202))
    def _job(route):
        job_hits["n"] += 1
        if throttle["on"]:
            return route.fulfill(status=429, headers={"retry-after": "5"},
                                 content_type="application/json",
                                 body=json.dumps({"detail": "too many requests"}))
        j(route, {"job_id": "job-880", "source_id": f"sharepoint:{TENANT}",
                  "status": job["status"], "phase": job["phase"], "docs_done": job["done"],
                  "docs_total": job["total"], "docs_skipped": job["skipped"], "error": ""})
    page.route("**/ingest/jobs/job-880*", _job)


def main():
    print("SharePoint ingest modal E2E (#880 the modal outlives the job, #879 it opens fast):")
    srv = serving(app, HOST, PORT)
    results = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # 1440 is the design system's own desktop reference (s10). At the
            # default 800x600 the seeded node lands under the nav rail, which is
            # its own question - carded, not silently worked around here.
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            _install_routes(page)
            page.goto(BASE + "/canvas", wait_until="domcontentloaded")

            # ---- #879: the dialog paints BEFORE the libraries arrive ---------------------
            page.wait_for_selector(".sp-pick", timeout=15000)
            # A seeded node lands wherever the layout puts it, which at this viewport is under
            # the right-hand inspector. Use the product's own "fit all sources in view" control
            # rather than clicking through an overlay - a real user reaches for the same button,
            # and forcing the click would hide a genuine hit-testing problem if one ever appears.
            page.click("#zoomFit")
            page.wait_for_timeout(500)
            page.click(".sp-pick")
            # 400ms is well inside the 1500ms the drives call takes. If the link field is
            # here, the open path did not await the enumeration.
            page.wait_for_selector("#spLink", timeout=400)
            assert page.is_visible("#spLink"), "the folder-link field waited on the library list"
            assert "Finding the libraries" in page.inner_text(".sp-list"), \
                f"the wait is not explained: {page.inner_text('.sp-list')!r}"
            results.append("#879 the dialog opens with the link field usable, libraries lazy")

            # ---- #879: rows say WHICH site, and system sites are behind a disclosure -----
            page.wait_for_selector(".sp-drive", timeout=8000)
            labels = page.eval_on_selector_all(".sp-drive b", "els => els.map(e => e.textContent)")
            assert labels == ["QuantifyMe.AI — Documents", "Deal Team — Documents"], labels
            assert page.is_visible(".sp-sysmore"), "system sites were dropped instead of folded"
            assert "2 system sites" in page.inner_text(".sp-sysmore"), page.inner_text(".sp-sysmore")
            page.click(".sp-sysmore")
            page.wait_for_selector(".sp-sys .sp-drive")
            assert len(page.query_selector_all(".sp-sys .sp-drive")) == 2
            results.append("#879 rows read '<site> — <library>'; system sites fold, never vanish")

            # ---- P1: the modal outlives the 202 -----------------------------------------
            page.click(".sp-drive .sp-ingest")
            page.wait_for_selector(".sp-progress", timeout=5000)
            for ph in PHASES:
                job["phase"] = ph
                job["done"] = PHASES.index(ph) + 1
                # Wait for the SURFACE to reflect the phase rather than sleeping a fixed
                # time: the poll interval is a tuning decision (it moved from 600ms to
                # 1200ms when prod showed the throttling), and a test that encodes it
                # silently stops observing every phase the moment someone changes it.
                page.wait_for_function(
                    "n => { const e = document.querySelector('.sp-prog-label');"
                    "       return e && e.textContent.includes(n); }",
                    arg=str(PHASES.index(ph) + 1) + "/5", timeout=6000)
                assert page.is_visible("#spPicker.show"), \
                    f"THE #880 DEFECT: the modal closed during phase {ph!r}"
                assert page.is_visible(".sp-progress"), f"progress vanished during {ph!r}"
                seen = page.inner_text(".sp-prog-label")
                # P2: a real phase word, and never the raw vocabulary of the runner.
                assert seen.strip(), f"no phase reported at {ph!r}"
                lit = page.eval_on_selector_all(
                    ".sp-step.active, .sp-step.done", "els => els.length")
                assert lit >= 1, f"phase {ph!r} lit no step ({seen!r}) - STEP_OF is blind to it"
                if ph == "skipping":
                    assert "kipping" in seen, f"`skipping` rendered as {seen!r}"
            results.append("#880 P1 the modal is still open through every phase of the job")
            results.append("#880 P2 each phase lights a step - `skipping` included")

            # ---- a 429 is "slow down", never "the job is gone" ---------------------------
            # This is the defect that reached the owner on prod: /ingest/jobs matched the
            # "/ingest" costly prefix, the poller spent the per-IP budget in 18 seconds, and
            # the modal announced "That ingest did not finish" about a crawl that finished
            # with 5 documents. The server half is METER_EXEMPT; this is the client half,
            # because a throttled poller must survive a busy box regardless.
            # Measured over a window long enough to SEPARATE the two behaviours. A first
            # version of this only asserted "no failure shown" over 6 seconds, and it passed
            # with the backoff deleted - the un-backed-off poller simply had not yet reached
            # its miss threshold. A guard that cannot tell the fix from its absence is not a
            # guard, so this counts the requests the surface actually makes.
            job_hits["n"] = 0
            throttle["on"] = True
            page.wait_for_timeout(12000)
            throttled_hits = job_hits["n"]
            assert not page.query_selector(".sp-done-bad"), \
                "a 429 was reported to the user as a failed ingest"
            assert page.is_visible(".sp-progress"), "a 429 tore down the progress panel"
            # 12s at the 1200ms baseline is ~10 polls with no backoff. Backing off must cut
            # that materially, or the surface is still hammering a server asking it to stop.
            assert throttled_hits <= 7, (
                f"the poller made {throttled_hits} requests across 12s of 429s - it is not "
                "backing off, so it keeps hammering a server that asked it to slow down")
            throttle["on"] = False
            results.append(f"#880 a throttled poll backs off ({throttled_hits} requests in "
                           "12s of 429s) and never calls the job failed")

            # ---- P3 + P4: terminal state -------------------------------------------------
            sources_doc_count["n"] = 5          # the crawl commits its count, then goes terminal
            job["status"] = "succeeded"
            job["phase"] = "done"
            page.wait_for_selector(".sp-done", timeout=6000)
            done_text = page.inner_text(".sp-done")
            assert "ingested" in done_text.lower(), done_text
            assert "5 documents" in done_text, f"the count is not stated in words: {done_text!r}"
            page.wait_for_timeout(900)
            assert page.is_visible("#spPicker.show"), \
                "THE #880 DEFECT: the code dismissed the completed run itself"
            results.append("#880 P3 completion is stated in words and left on screen")

            # The SHAREPOINT node specifically - the canvas seeds others, and asserting on
            # whichever happens to be first would be a test that passes for the wrong reason.
            node_text = page.eval_on_selector_all(".node", """els => {
              const n = els.find(e => /sharepoint/i.test(e.textContent));
              return n ? n.textContent : "(no sharepoint node)"; }""")
            assert "5 docs" in node_text, f"P4: the node did not re-read the count: {node_text!r}"
            results.append("#880 P4 the node's count is re-read from the finished job")

            page.click(".sp-done-ok")
            page.wait_for_selector("#spPicker.show", state="hidden", timeout=4000)
            results.append("#880 P3 and the person closes it")
            browser.close()
    finally:
        srv.stop()

    for r in results:
        print(f"  PASS  {r}")
    print("\nSHAREPOINT INGEST MODAL E2E PASSED.")


if __name__ == "__main__":
    main()
