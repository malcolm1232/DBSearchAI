import type { Metadata } from "next";
import { ChevronDown } from "lucide-react";
import { SectionHeading } from "@/components/sections/section-heading";
import { CtaBand } from "@/components/sections/cta-band";
import { PricingTable } from "@/components/pricing-table";

export const metadata: Metadata = {
  title: "Pricing - DBSearch.AI",
  description:
    "Self-host the open source edition for free, or let us run it for you in your own Azure tenant. Same permission-faithful retrieval either way - no seat counts, no invented percentages.",
};

const FAQS = [
  {
    q: "Where does our data live?",
    a: "It stays in your tenant. On the self-host tier it runs on your own box; on Managed Azure it runs inside your own Azure subscription. We never host or see your document content on our own infrastructure.",
  },
  {
    q: "How fresh are the permission checks?",
    a: "Access is checked at query time against your live Entra (Azure AD) groups - not a cached or stale permissions snapshot taken when a document was indexed. If someone's access is revoked, the next query reflects that immediately.",
  },
  {
    q: "Can it run fully air-gapped?",
    a: "Yes, on the Enterprise tier. Indexing, retrieval, and the local LLaMA model all run with no outbound network dependency, for environments that can't reach the public internet at all.",
  },
  {
    q: "What's the actual difference between self-host and managed?",
    a: "The software is the same permission-faithful retrieval engine either way. Self-host means you run the Docker Compose stack yourself for free, under Apache 2.0. Managed Azure means we operate that same stack for you inside your Azure tenant, with SSO set up for you and a support agreement layered on top.",
  },
] as const;

export default function PricingPage() {
  return (
    <main>
      {/* Hero */}
      <section className="vault-grid border-b border-border">
        <div className="mx-auto max-w-3xl px-6 py-20 text-center lg:py-28">
          <p className="text-micro font-mono text-fg-muted">
            Pricing
          </p>
          <h1 className="mt-3 font-display text-display-2 text-fg">
            Free to self-host. Simple to scale.
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg text-fg-muted">
            Start with the open source edition on your own infrastructure at no
            cost, or let us run it for you inside your own Azure tenant when
            you need SSO, an SLA, or hands-off operations.
          </p>
        </div>
      </section>

      {/* Pricing table */}
      <section className="mx-auto max-w-6xl px-6 py-24">
        <SectionHeading
          kicker="Plans"
          title="Three ways to run it"
          sub="Same permission-faithful retrieval on every tier - the difference is who operates it and where."
        />
        <div className="mt-16">
          <PricingTable />
        </div>
      </section>

      {/* FAQ */}
      <section className="border-t border-border bg-surface">
        <div className="mx-auto max-w-3xl px-6 py-24">
          <SectionHeading
            kicker="FAQ"
            title="Common questions"
            sub="If something below doesn't answer it, book a demo and ask us directly."
          />

          <div className="mt-12 space-y-3">
            {FAQS.map((faq) => (
              <details
                key={faq.q}
                className="group rounded-lg border border-border bg-bg px-6 py-4 open:pb-5 [&:not([open])]:motion-safe:transition-colors"
              >
                <summary className="flex cursor-pointer list-none items-center justify-between gap-4 text-left text-base font-medium text-fg marker:content-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
                  {faq.q}
                  <ChevronDown
                    className="h-4 w-4 shrink-0 text-fg-muted motion-safe:transition-transform motion-safe:duration-200 group-open:rotate-180"
                    aria-hidden="true"
                  />
                </summary>
                <p className="mt-3 text-sm text-fg-muted">{faq.a}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      <CtaBand />
    </main>
  );
}
