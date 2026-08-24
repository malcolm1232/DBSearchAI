// src/dbsearch/server/static/js/ui/motion.js
// Pop-in a freshly-rendered node. Respects prefers-reduced-motion via the CSS @media rule
// (the class is a no-op animation when reduced motion is on).
export function popIn(node) {
  node.classList.add("pop-in");
  node.addEventListener("animationend", () => node.classList.remove("pop-in"), { once: true });
  return node;
}
