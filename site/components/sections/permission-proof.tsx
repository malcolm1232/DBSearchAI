import Link from "next/link";
import { SectionHeading } from "@/components/sections/section-heading";

const QUESTION = "What is Project Falcon's valuation?";

const OUTCOMES = [
  {
    who: "Alice",
    role: "deal team",
    answer: "Falcon is valued at $340M as of the Q3 committee pack.",
    cited: true,
    note: "Cleared for the deal room, so the answer comes back cited.",
  },
  {
    who: "Bob",
    role: "all staff",
    answer: "Nothing you have access to about that.",
    cited: false,
    // Deliberately not the hero's "never retrieved" phrasing: the same point
    // stated twice in the same words makes the page read as repeating itself.
    note: "Not cleared for the deal room, so those documents never entered his result set at all.",
  },
] as const;

/**
 * Static, server-rendered proof of the product's core claim: one index, one
 * question, two identities, two different answers.
 *
 * Deliberately not the live demo - that lives on /product, and putting a fetch
 * here would mean a loading or error state on the landing page. The copy is
 * kept identical to live-demo.tsx's real preset question and empty state, so a
 * visitor who clicks through sees exactly what this section promised.
 */
export function PermissionProof() {
  return (
    <section className="border-y border-border bg-surface-muted">
      <div className="mx-auto max-w-6xl px-6 py-20 lg:py-32">
        <SectionHeading
          kicker="Permission-faithful"
          title="One question. Two people. Two answers."
          sub="The same index, asked by two identities, at the same moment."
        />

        <p className="mx-auto mt-12 max-w-xl text-center font-display text-2xl text-fg">
          &ldquo;{QUESTION}&rdquo;
        </p>

        <div className="mt-12 grid gap-px overflow-hidden rounded-xl border border-border bg-border sm:grid-cols-2">
          {/*
            The answer carries a two-line floor so the internal hairline lands
            at the same height in both panels. Aligning from the top rather
            than bottom-pinning the note: the notes are different lengths, so
            a bottom pin aligns the panel feet but leaves the rules ragged.
          */}
          {OUTCOMES.map((outcome) => (
            <div key={outcome.who} className="bg-surface p-8">
              <div className="flex items-baseline gap-2">
                <span className="text-base font-medium text-fg">
                  {outcome.who}
                </span>
                <span className="text-micro font-mono text-fg-muted">
                  {outcome.role}
                </span>
              </div>

              <p
                className={
                  outcome.cited
                    ? "mt-6 min-h-[3.75rem] text-lg leading-relaxed text-fg"
                    : "mt-6 min-h-[3.75rem] text-lg leading-relaxed text-fg-muted"
                }
              >
                {outcome.answer}
                {outcome.cited && (
                  <sup className="ml-0.5 font-mono text-[0.6em] text-accent">
                    1
                  </sup>
                )}
              </p>

              <p className="mt-6 border-t border-border pt-5 text-sm leading-relaxed text-fg-muted">
                {outcome.note}
              </p>
            </div>
          ))}
        </div>

        <p className="mt-10 text-center text-sm text-fg-muted">
          <Link
            href="/product"
            className="rounded-sm text-fg underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fg"
          >
            Try it yourself
          </Link>{" "}
          against the sample corpus.
        </p>
      </div>
    </section>
  );
}
