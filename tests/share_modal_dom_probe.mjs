// Drives the REAL ask surface in a real DOM (jsdom) and reports what a user would find in
// the share modal, as JSON on stdout. Used by tests/selftest_606_share_modal_ui.py.
//
// WHY THIS EXISTS. Every other assertion available to a python selftest is a string search
// over a file or a served asset, and this branch has already shipped four tests that were
// green while the page in question was never rendered to anybody. A string in a module is
// not a control on a screen. This mounts the surface the router actually mounts, clicks the
// Share button a user clicks, and reads the resulting DOM - so "there is no add-document
// control" is answered by counting the controls that exist, not by grepping for a name
// somebody might not have used.
//
// It is NOT a browser: no layout, no paint, no CSS. A real-browser pass is still owed.
import { pathToFileURL } from "node:url";

const [, , jsdomPath, askPath] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);

const dom = new JSDOM("<!doctype html><html><body><div id='root'></div></body></html>",
                      { url: "http://localhost/ask" });
const { window } = dom;
for (const k of ["document", "window", "location", "HTMLElement", "Node", "Event",
                 "CustomEvent", "getComputedStyle"]) {
  Object.defineProperty(globalThis, k, { value: window[k], configurable: true, writable: true });
}
const copiedText = [];
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async (t) => { copiedText.push(t); } } },
  configurable: true, writable: true,
});

const CONV_DOCS = {
  documents: [
    { id: "d-handbook", title: "Employee handbook", shareable: true },
    { id: "d-sales", title: "Sales pipeline Q3", shareable: true },
    { id: "d-carol", title: "Carol's memo", shareable: false },
  ],
  // #851: the SOURCES a routed turn drew on, which /shareable now returns beside the
  // documents so the owner decides what this share hands over in ONE place. Two of them, so
  // unticking one can be told apart from unticking all.
  stores: [
    { id: "azure_sql-1", title: "Azure SQL · srv / sales", shareable: true },
    { id: "bigquery-1", title: "BigQuery · analytics", shareable: true },
  ],
  turns: 3,
};
const EXISTING = { shares: [
  { share_id: "s-people", audience: "people", grantee_oid: "acct_beef",
    created_at: "2026-08-01T10:00:00Z", expires_at: null, opens: 0, live: true },
  { share_id: "s-link", audience: "link", grantee_oid: "link:s-link",
    created_at: "2026-08-02T10:00:00Z", expires_at: "2026-08-09T10:00:00Z",
    opens: 4, live: true },
] };
const posted = [];

globalThis.fetch = async (url, opts = {}) => {
  const j = (body, status = 200) => ({
    ok: status < 400, status,
    json: async () => body, text: async () => JSON.stringify(body),
  });
  if (url.startsWith("/ask/suggestions")) {
    return j({ known: true, indexed: true, authorized_docs: 3, examples: [] });
  }
  if (url.startsWith("/conversations/shared-with-me")) {
    return j({ shares: [{ share_id: "s-in", conv_id: "c-from-bob", grantor_oid: "acct_bob",
                          created_at: "2026-08-03T10:00:00Z", live: true }] });
  }
  if (url.endsWith("/transcript")) {
    return j({ own: false, turns: [
      { seq: 0, question: "what does the handbook say?", answer: "It says so.", own: false }] });
  }
  if (url.endsWith("/shareable")) return j(CONV_DOCS);
  if (url.endsWith("/shares") && (opts.method || "GET") === "GET") return j(EXISTING);
  if (url.endsWith("/shares") && opts.method === "POST") {
    const body = JSON.parse(opts.body);
    posted.push(body);
    const out = { share_id: "s-new", audience: body.audience, documents: 1,
                  turns_withheld: 1, created_at: "2026-08-10T10:00:00Z",
                  expires_at: "2026-08-17T10:00:00Z", opens: 0, live: true,
                  grantee_oid: body.audience === "link" ? "link:s-new" : "acct_new" };
    if (body.audience === "link") out.url = "/c/TOKEN-abc123";
    return j(out);
  }
  return j({}, 404);
};

const tick = () => new Promise((r) => setTimeout(r, 0));
const { mountAsk } = await import(pathToFileURL(askPath).href);

const root = window.document.getElementById("root");
mountAsk(root);
await tick(); await tick();

// A user only ever reaches this button after a turn has been answered.
const shareBtn = window.document.getElementById("share-conversation");
shareBtn.style.display = "";
shareBtn.click();
await tick(); await tick(); await tick();

const backdrop = window.document.getElementById("share-modal");
const panel = backdrop.querySelector(".share-modal");
const text = () => panel.textContent;

const inputs = () => [...panel.querySelectorAll("input")].map((i) => ({
  type: i.type, cls: i.className, checked: i.checked, disabled: i.disabled,
  readonly: i.readOnly, value: i.value }));
const buttons = () => [...panel.querySelectorAll("button")].map((b) => b.textContent.trim());

const report = {
  modal_present: !!panel,
  form_text: text(),
  form_inputs: inputs(),
  form_buttons: buttons(),
  doc_rows: [...panel.querySelectorAll("#share-doc-list > *")].map((r) => ({
    text: r.textContent.trim(),
    has_checkbox: !!r.querySelector('input[type="checkbox"]'),
    blocked: r.className.includes("blocked"),
  })),
  count_line: panel.querySelector(".share-doc-count")?.textContent || "",
  share_rows: [...panel.querySelectorAll(".share-row")].map((r) => r.textContent.trim()),
};

// Uncheck the sales pipeline - the whole point of the checklist - and pick the link audience.
const boxes = [...panel.querySelectorAll('input[type="checkbox"]')];
const sales = boxes.find((b) => b.parentElement.textContent.includes("Sales pipeline"));
sales.checked = false;
sales.dispatchEvent(new window.Event("change"));
report.count_after_uncheck = panel.querySelector(".share-doc-count").textContent;

// #851: untick ONE SOURCE. Two are offered, so "unticked one" and "unticked all" are
// distinguishable in what gets posted - a rig where both look the same could not tell a
// working narrow from a broken one that sends every id.
const bq = boxes.find((b) => b.parentElement.textContent.includes("BigQuery"));
if (bq) {
  bq.checked = false;
  bq.dispatchEvent(new window.Event("change"));
}
report.count_after_source_uncheck = panel.querySelector(".share-doc-count").textContent;

const linkRadio = [...panel.querySelectorAll('input[type="radio"]')]
  .find((r) => r.value === "link");
linkRadio.checked = true;
linkRadio.dispatchEvent(new window.Event("change"));
report.email_row_hidden_in_link_mode =
  panel.querySelector(".share-input")?.parentElement.style.display === "none";

panel.querySelector(".share-add").click();
await tick(); await tick(); await tick();

report.posted = posted;
report.link_view_text = panel.textContent;
report.link_field_value = panel.querySelector(".share-link-url")?.value || "";

// The show-once guard. The FIRST dismissal of an uncopied one-time link must not dismiss it,
// whichever route it arrives by - so the first attempt here is the backdrop, which is the
// route owned by the surface rather than by the modal, and the second is the Close button.
// If they consulted two different guards, this pair would not hold.
const close = panel.querySelector(".share-modal-close");
backdrop.dispatchEvent(new window.Event("click"));
report.closed_on_first_backdrop_click = backdrop.innerHTML === "";
report.close_label_after_first_attempt = close.textContent.trim();
report.guard_note = panel.querySelector(".share-guard-note")?.textContent || "";
close.click();
report.closed_on_second_click = backdrop.innerHTML === "";

// ...and after copying, one click is enough.
shareBtn.click();
await tick(); await tick(); await tick();
const panel2 = backdrop.querySelector(".share-modal");
const linkRadio2 = [...panel2.querySelectorAll('input[type="radio"]')]
  .find((r) => r.value === "link");
linkRadio2.checked = true;
linkRadio2.dispatchEvent(new window.Event("change"));
panel2.querySelector(".share-add").click();
await tick(); await tick(); await tick();
const copyBtn = [...panel2.querySelectorAll("button")]
  .find((b) => b.textContent.trim() === "Copy");
copyBtn.click();
await tick(); await tick();
report.copied_text = copiedText;
panel2.querySelector(".share-modal-close").click();
report.closed_after_copy = backdrop.innerHTML === "";

// ---- fix round 1: the two routes that used to skip the guard entirely --------------------
//
// Neither of these is the Close button. Both replace the conversation the modal belongs to,
// and both used to tear the modal down directly - destroying a one-time link that the API
// will never return again, with no warning of any kind.

async function openWithUncopiedLink() {
  shareBtn.style.display = "";
  shareBtn.click();
  await tick(); await tick(); await tick();
  const p = backdrop.querySelector(".share-modal");
  const radio = [...p.querySelectorAll('input[type="radio"]')].find((r) => r.value === "link");
  radio.checked = true;
  radio.dispatchEvent(new window.Event("change"));
  p.querySelector(".share-add").click();
  await tick(); await tick(); await tick();
  if (!p.querySelector(".share-link-url")) throw new Error("no link view to guard");
  return p;
}

// ROUTE A: "New conversation", the button sitting right beside the modal.
{
  const p = await openWithUncopiedLink();
  const newBtn = window.document.getElementById("new-conversation");
  newBtn.click();
  await tick();
  report.reset_closed_modal_on_first_click = backdrop.innerHTML === "";
  report.reset_guard_note = p.querySelector(".share-guard-note")?.textContent || "";
  // The navigation itself must not have happened either: a reset that clears the thread but
  // leaves the modal up would be the same loss with the dialog still on screen.
  report.reset_happened_on_first_click = shareBtn.style.display === "none";
  newBtn.click();
  await tick();
  report.reset_closed_modal_on_second_click = backdrop.innerHTML === "";
  report.reset_happened_on_second_click = shareBtn.style.display === "none";
}

// ROUTE B: clicking a conversation somebody shared with you, which replaces this thread.
{
  const p = await openWithUncopiedLink();
  // #631: the shared-with-you rows are .rail-thread buttons now (they moved into the rail
  // slot). Still the same navigation, still guarded by the same teardown.
  const row = window.document.querySelector("#shared-with-you .rail-thread");
  row.click();
  await tick(); await tick();
  report.shared_open_closed_modal_on_first_click = backdrop.innerHTML === "";
  report.shared_open_guard_note = p.querySelector(".share-guard-note")?.textContent || "";
  report.shared_open_happened_on_first_click =
    window.document.getElementById("thread").textContent.includes("what does the handbook say?");
  row.click();
  await tick(); await tick(); await tick();
  report.shared_open_closed_modal_on_second_click = backdrop.innerHTML === "";
  report.shared_open_happened_on_second_click =
    window.document.getElementById("thread").textContent.includes("what does the handbook say?");
}

// ---- the focus trap ----------------------------------------------------------------------
//
// HONEST LIMIT, and it is the reason this reports raw values rather than a verdict: jsdom
// does not implement native tab traversal at all, so it cannot show where focus WOULD have
// gone without the trap. What is proved here is the trap's own contract - Tab at the last
// control wraps to the first, Shift+Tab at the first wraps to the last, and focus starting
// outside is pulled back in - which is exactly the code that stops Shift+Tab reaching the
// "New conversation" button in a real browser. The browser half is still owed.
{
  shareBtn.style.display = "";
  shareBtn.click();
  await tick(); await tick(); await tick();
  const p = backdrop.querySelector(".share-modal");
  const tabbable = [...p.querySelectorAll("button, input")].filter(
    (n) => !n.closest('[style*="display: none"], [style*="display:none"]'));
  const key = (el, shift) => el.dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "Tab", shiftKey: shift, bubbles: true }));
  const where = () => {
    const a = window.document.activeElement;
    return { inside: p.contains(a), id: a?.id || "", cls: a?.className || "",
             text: (a?.textContent || "").trim().slice(0, 24) };
  };
  report.focus_on_open = where();
  const first = tabbable[0], last = tabbable[tabbable.length - 1];
  first.focus(); key(first, true);
  report.focus_after_shift_tab_from_first = where();
  report.shift_tab_landed_on_last = window.document.activeElement === last;
  last.focus(); key(last, false);
  report.focus_after_tab_from_last = where();
  report.tab_landed_on_first = window.document.activeElement === first;
  // Focus parked outside (the very button the old bypass was reachable through) is pulled
  // back into the dialog rather than being allowed to walk the shell.
  window.document.getElementById("new-conversation").focus();
  key(window.document.getElementById("new-conversation"), false);
  report.focus_after_tab_from_outside = where();
}

process.stdout.write(JSON.stringify(report, null, 1));
