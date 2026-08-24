import { SectionHeading } from "@/components/sections/section-heading";

const STEPS = [
  {
    number: "01",
    title: "Connect your sources",
    body: "SharePoint, OneDrive, and local file shares connect via Microsoft Graph. ACLs stay intact, and nothing is copied out of your tenant.",
  },
  {
    number: "02",
    title: "Ask in natural language",
    body: "DBSearch.AI retrieves the relevant documents, synthesizes an answer, and cites every claim back to its source.",
  },
  {
    number: "03",
    title: "Answers trimmed to permissions",
    body: "Every result is filtered against the asker's Entra groups before anything is shown, so no document leaks past who's allowed to see it.",
  },
] as const;

export function HowItWorks() {
  return (
    <section className="mx-auto max-w-6xl px-6 py-20 lg:py-32">
      <SectionHeading
        kicker="How it works"
        title="Search that respects the org chart"
        sub="Three steps from tenant to trusted answer."
      />

      <ol className="mt-16 grid gap-10 sm:grid-cols-3 sm:gap-0">
        {STEPS.map((step) => (
          <li
            key={step.number}
            className="border-t border-border pt-6 sm:px-8 sm:first:pl-0 sm:last:pr-0"
          >
            <span className="text-micro font-mono text-fg-muted">
              {step.number}
            </span>
            <h3 className="mt-4 text-lg font-medium text-fg">{step.title}</h3>
            <p className="mt-3 text-sm leading-relaxed text-fg-muted">
              {step.body}
            </p>
          </li>
        ))}
      </ol>
    </section>
  );
}
