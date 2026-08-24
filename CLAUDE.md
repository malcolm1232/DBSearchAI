# DBSearch.AI

**Before designing or building anything in this repo:**

1. **Read [`SKILL.md`](./SKILL.md) first** — it is the canonical architecture spec (the LAWs).
2. **Run the Architecture-Correctness Gate** (SKILL.md §1) on every change. If any check
   fails or you're unsure, STOP and redesign. A feature that violates a LAW is redesigned,
   never merged "for now."
3. Detail lives in [`docs/`](./docs/). Keep `SKILL.md` lean — push how-to into `docs/`.
   **Any UI work starts at [`docs/DESIGN_SYSTEM.md`](./docs/DESIGN_SYSTEM.md)** — palette,
   type, layout rules, the deploy trap, and the verification bar.
4. Any big or one-way-door decision gets an ADR in [`docs/ADR/`](./docs/ADR/) before coding.

Locked: **Azure-first · consulting wedge · control/data-plane split.** Never let customer
document content leave the customer tenant. Never return a result a user isn't authorized
to see.
