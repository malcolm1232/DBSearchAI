// Drives the REAL "Your data" surface in a real DOM (jsdom) and reports what an owner would
// find in the Shared section, as JSON on stdout. Used by tests/selftest_607_shared_surface.py.
//
// WHY THIS EXISTS, and it is the same reason tests/share_modal_dom_probe.mjs exists: every
// other assertion available to a python selftest is a string search over a file or a served
// asset, and this branch has already shipped four tests that were green while the page in
// question was never rendered to anybody. A string in a module is not a row on a screen.
// This mounts the surface the router mounts, reads the rows, clicks Edit, unchecks a document
// and clicks Save - so "the edit dialog can only narrow" is answered by counting the controls
// that exist and reading the request that goes out, not by grepping for a name.
//
// It is NOT a browser: no layout, no paint, no CSS. A real-browser pass is still owed.
import { pathToFileURL } from "node:url";

const [, , jsdomPath, adminPath] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);

const dom = new JSDOM("<!doctype html><html><body><div id='root'></div></body></html>",
                      { url: "http://localhost/admin" });
const { window } = dom;
for (const k of ["document", "window", "location", "HTMLElement", "Node", "Event",
                 "CustomEvent", "getComputedStyle"]) {
  Object.defineProperty(globalThis, k, { value: window[k], configurable: true, writable: true });
}
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } }, configurable: true, writable: true,
});
Object.defineProperty(globalThis, "crypto", {
  value: { randomUUID: () => "c-probe" }, configurable: true, writable: true,
});

// Two shares of one thread, one per audience - the whole point of the section is that both
// live in one list, because to the owner they are the same act with two doorways.
const SHARES = { shares: [
  { share_id: "s-people", conv_id: "c-hr", audience: "people", grantee_oid: "acct_beef",
    created_at: "2026-08-01T10:00:00Z", expires_at: null, opens: 0, last_open_at: null,
    live: true, turn_cutoff: 2,
    first_question: "how many weeks of severance does the Hamburg clause pay?",
    scope: [{ id: "d-hh", title: "Hamburg severance policy" },
            { id: "d-lis", title: "Lisbon carryover policy" }],
    questions_asked: 0 },
  { share_id: "s-link", conv_id: "c-hr", audience: "link", grantee_oid: "link:s-link",
    created_at: "2026-08-02T10:00:00Z", expires_at: "2026-08-09T10:00:00Z",
    opens: 4, last_open_at: "2026-08-08T09:00:00Z", live: true, turn_cutoff: 2,
    first_question: "how many weeks of severance does the Hamburg clause pay?",
    scope: [{ id: "d-hh", title: "Hamburg severance policy" },
            { id: "d-lis", title: "Lisbon carryover policy" }],
    questions_asked: 2 },
] };

const patched = [];
let sharesCall = 0;

globalThis.fetch = async (url, opts = {}) => {
  const j = (body, status = 200) => ({
    ok: status < 400, status,
    json: async () => body, text: async () => JSON.stringify(body),
  });
  if (url.startsWith("/config")) return j({ operator: false, users: [] });
  if (url.startsWith("/auth/me")) {
    return j({ oid: "acct_bob", name: "Bob", signed_in: true });
  }
  if (url.startsWith("/admin/documents")) return j([]);
  if (url.startsWith("/me/questions")) return j([]);
  if (url.startsWith("/shares/mine")) { sharesCall += 1; return j(SHARES); }
  if (url.endsWith("/questions")) {
    return j({ visitors: 2, questions: [
      { question: "VISITOR-Q1 how many weeks?", asked_at: "2026-08-08T09:01:00Z", visitor: 1 },
      { question: "VISITOR-Q2 and in Lisbon?", asked_at: "2026-08-08T09:02:00Z", visitor: 2 },
    ] });
  }
  if (opts.method === "PATCH" && url.includes("/scope")) {
    patched.push({ url, body: JSON.parse(opts.body) });
    return j({ share_id: "s-link", removed: 1, documents: 1, turn_cutoff: 1, live: true });
  }
  if (opts.method === "DELETE") return j({ revoked: "s-people", grants_dropped: 2 });
  return j({}, 404);
};

const tick = () => new Promise((r) => setTimeout(r, 0));
const { mountAdmin } = await import(pathToFileURL(adminPath).href);

const root = window.document.getElementById("root");
await mountAdmin(root);
for (let i = 0; i < 6; i += 1) await tick();

const section = window.document.getElementById("admin-shared");
const rows = [...section.querySelectorAll(".shared-row")];
const report = {
  section_present: !!section,
  section_title: [...window.document.querySelectorAll(".admin-panel-title")]
    .map((n) => n.textContent.trim()),
  rows: rows.map((r) => ({
    name: r.querySelector(".shared-name")?.textContent || "",
    audience: r.querySelector(".doc-audience")?.textContent || "",
    when: r.querySelector(".shared-when")?.textContent || "",
    scope: r.querySelector(".shared-scope")?.textContent || "",
    asked: r.querySelector(".shared-asked")?.textContent || "",
    buttons: [...r.querySelectorAll("button")].map((b) => b.textContent.trim()),
  })),
};

// ---- [View] opens the question log ---------------------------------------------------------
const linkRow = rows[1];
linkRow.querySelector(".shared-view").click();
await tick(); await tick();
report.log_text = linkRow.querySelector(".shared-drawer").textContent;

// ---- [Edit] reopens the share modal, in edit mode -------------------------------------------
linkRow.querySelector(".shared-edit").click();
await tick();
const host = window.document.getElementById("shared-modal");
const panel = host.querySelector(".share-modal");
report.edit_modal_present = !!panel;
report.edit_title = panel.querySelector(".share-modal-title")?.textContent || "";
report.edit_text = panel.textContent;
report.edit_inputs = [...panel.querySelectorAll("input")].map((i) => i.type);
report.edit_buttons = [...panel.querySelectorAll("button")].map((b) => b.textContent.trim());
report.edit_doc_rows = [...panel.querySelectorAll("#share-doc-list > *")].map((r) => ({
  text: r.textContent.trim(),
  has_checkbox: !!r.querySelector('input[type="checkbox"]'),
}));
report.edit_count_line = panel.querySelector(".share-doc-count")?.textContent || "";

// Uncheck one document, which is the only interaction this dialog has, and save.
const boxes = [...panel.querySelectorAll('input[type="checkbox"]')];
const lisbon = boxes.find((b) => b.parentElement.textContent.includes("Lisbon"));
lisbon.checked = false;
lisbon.dispatchEvent(new window.Event("change"));
report.edit_count_after_uncheck = panel.querySelector(".share-doc-count").textContent;

const sharesCallsBeforeSave = sharesCall;
panel.querySelector(".share-save").click();
await tick(); await tick(); await tick();
report.patched = patched;
report.reread_after_save = sharesCall > sharesCallsBeforeSave;

// ---- the keyboard contract of an aria-modal dialog ------------------------------------------
//
// #607 review round 1, Finding 3: this dialog declared `aria-modal="true"` and implemented
// neither Escape nor a focus trap, because both live in mountAsk's closure and nothing here
// inherited them. HONEST LIMIT, same as share_modal_dom_probe.mjs: jsdom implements no native
// tab traversal, so this cannot show where focus would have gone WITHOUT the trap. What it
// proves is the trap's own contract - which is the code that stops the escape in a browser.
{
  const rows2 = [...window.document.querySelectorAll(".shared-row")];
  rows2[1].querySelector(".shared-edit").click();
  await tick();
  const p = host.querySelector(".share-modal");
  report.edit_aria_modal = p.getAttribute("aria-modal");
  const where = () => {
    const a = window.document.activeElement;
    return { inside: p.contains(a), text: (a?.textContent || "").trim().slice(0, 24) };
  };
  report.edit_focus_on_open = where();

  const key = (el, k, shift) => el.dispatchEvent(new window.KeyboardEvent("keydown", {
    key: k, shiftKey: !!shift, bubbles: true }));
  const tabbable = [...p.querySelectorAll("button, input")].filter(
    (n) => !n.closest('[style*="display: none"], [style*="display:none"]'));
  const first = tabbable[0], last = tabbable[tabbable.length - 1];
  first.focus(); key(first, "Tab", true);
  report.edit_shift_tab_landed_on_last = window.document.activeElement === last;
  last.focus(); key(last, "Tab", false);
  report.edit_tab_landed_on_first = window.document.activeElement === first;
  // Focus parked on the page BEHIND the dialog is pulled back in rather than allowed to walk.
  const outside = window.document.querySelector(".shared-revoke");
  outside.focus();
  key(outside, "Tab", false);
  report.edit_focus_after_tab_from_outside = where();

  // Escape closes it. Dispatched on the document, which is where a real keystroke lands when
  // focus is anywhere on the page.
  key(window.document, "Escape", false);
  report.edit_closed_on_escape = host.innerHTML === "";

  // ...and so does a click on the backdrop, but a click INSIDE the panel must not.
  rows2[1].querySelector(".shared-edit").click();
  await tick();
  const p2 = host.querySelector(".share-modal");
  p2.dispatchEvent(new window.Event("click", { bubbles: true }));
  report.edit_survives_click_inside_panel = host.innerHTML !== "";
  host.dispatchEvent(new window.Event("click"));
  report.edit_closed_on_backdrop_click = host.innerHTML === "";
}

process.stdout.write(JSON.stringify(report, null, 1));
