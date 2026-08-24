import { SectionHeading } from "@/components/sections/section-heading";

const FEATURES = [
  {
    title: "No content leaves your cloud",
    body: "Runs entirely inside your own Azure tenant or self-hosted infrastructure, so your documents never transit a third-party service.",
  },
  {
    title: "ACLs honored at query time",
    body: "Permissions are re-checked against the asker's Entra groups on every query, not just at index time.",
  },
  {
    title: "Cited, verifiable answers",
    body: "Every answer links back to the exact source document, so you can verify before you trust.",
  },
  {
    title: "Azure or fully self-host",
    body: (
      <>
        Deploy managed on Azure, or run the open source edition yourself with{" "}
        <code className="rounded bg-surface-muted px-1.5 py-0.5 font-mono text-xs text-fg">
          docker compose
        </code>
        .
      </>
    ),
  },
] as const;

/**
 * The four trust claims, as hairline-topped columns rather than bordered
 * cards, and without icons: four green glyphs were decoration rather than
 * information, and green is reserved for signal in the paper system.
 *
 * Two columns rather than four. At `lg` the four-column version gave each body
 * roughly 22 characters per line, well under a readable measure.
 */
export function FeatureGrid() {
  return (
    <section className="mx-auto max-w-6xl px-6 py-20 lg:py-32">
      <SectionHeading
        kicker="Why teams trust it"
        title="Built for firms that can't afford a leak"
      />

      <div className="mt-16 grid gap-x-16 gap-y-12 sm:grid-cols-2">
        {FEATURES.map(({ title, body }) => (
          <div key={title} className="border-t border-border pt-6">
            <h3 className="text-lg font-medium text-fg">{title}</h3>
            <p className="mt-3 text-sm leading-relaxed text-fg-muted">{body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
