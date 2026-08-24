// src/dbsearch/server/static/js/ui/modal.js
//
// The keyboard contract of a dialog, in ONE place, because there are now two screens that open
// one: the share modal on Ask (surfaces/ask.js) and the edit dialog in the Shared section on
// "Your data" (surfaces/admin.js). DESIGN_SYSTEM s6's rule - exactly one definition of anything
// that appears on more than one screen - and here it is a safety property rather than a visual
// one, which is why it is extracted rather than copied.
//
// WHAT `aria-modal="true"` PROMISES AND WHO KEEPS IT. That attribute tells a screen reader the
// rest of the page is inert. Nothing enforces it: without a trap a keyboard user Shift+Tabs
// straight out of the panel onto whatever sits behind it, and on Ask that is "New conversation",
// one of the two routes that used to destroy an uncopied one-time link. A dialog that declares
// `aria-modal` and implements neither Escape nor a trap is making a promise in markup that its
// code does not keep - which is what the Shared section's edit dialog was doing when this file
// was written (#607 review round 1, Finding 3: it inherited nothing, because the trap lived
// inside `mountAsk`'s closure).

// A control the user can actually reach. Ordered as the DOM is, which is the order Tab follows.
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), '
  + 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function focusablesIn(root) {
  return [...root.querySelectorAll(FOCUSABLE)].filter(
    // A control inside a hidden row is not reachable by eye, so it must not be reachable by
    // Tab either - a focus ring landing on nothing is its own defect. (The share modal hides
    // its email row in link mode exactly this way.)
    (n) => !n.closest('[style*="display: none"], [style*="display:none"]'));
}

/** Keep Tab inside `root`. Call from a keydown handler when `event.key === "Tab"`.
 *
 *  Wraps in both directions, and ALSO pulls focus back when it is parked outside the dialog
 *  entirely - that last case is not decoration, it is the one that fires when the page behind
 *  still holds focus because the dialog opened without moving it.
 */
export function trapFocus(root, e) {
  const items = focusablesIn(root);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const active = root.ownerDocument.activeElement;
  const outside = !root.contains(active);
  if (e.shiftKey && (outside || active === first)) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && (outside || active === last)) {
    e.preventDefault(); first.focus();
  }
}

/** Give a backdrop the three dismissal routes every dialog in this product must answer:
 *  Escape, a click on the backdrop itself, and Tab (which is a dismissal route only in the
 *  sense that without a trap it walks the user out of the dialog and into the page behind).
 *
 *  `isOpen()` is asked on every keystroke rather than remembered, because the caller owns the
 *  dialog's lifetime and a cached boolean here would be a second opinion about whether
 *  something is on screen. `onDismiss()` is the caller's ONE teardown - never a local one -
 *  so a dialog holding something unrecoverable can refuse, exactly as the share modal's
 *  copy-link guard does.
 *
 *  Returns nothing to unbind: both listeners are keyed on `isOpen()` and cost nothing when the
 *  dialog is down, which is the same shape `mountAsk` has always used. A surface that mounts
 *  once per page load has nothing to leak.
 */
export function wireModalHost(backdrop, { isOpen, onDismiss }) {
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) onDismiss();
  });
  backdrop.ownerDocument.addEventListener("keydown", (e) => {
    if (!isOpen()) return;
    if (e.key === "Escape") { onDismiss(); return; }
    if (e.key === "Tab") trapFocus(backdrop, e);
  });
}

/** Move focus into the dialog, or the trap has nothing to hold: a Tab from the page behind
 *  would otherwise walk the shell before it ever reached the panel. */
export function focusFirstIn(root) {
  const first = focusablesIn(root)[0];
  if (first) first.focus();
  return first || null;
}
