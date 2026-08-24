import Link from "next/link";
import { Check, X, Server, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { GITHUB_URL, START_URL } from "@/lib/nav";

interface Tier {
  id: string;
  icon: typeof Server;
  eyebrow: string;
  name: string;
  price: string;
  priceNote: string;
  badge?: string;
  description: string;
  features: string[];
  // `external` opens a new tab. `app` is a same-tab link into the product, which
  // is served by the FastAPI box rather than by this static export, so it must
  // bypass next/link.
  cta: { label: string; href: string; external?: boolean; app?: boolean };
  variant: "pill" | "quiet";
  emphasized?: boolean;
  premium?: boolean;
}

const TIERS: Tier[] = [
  {
    id: "open-core",
    icon: Server,
    eyebrow: "Open-core",
    name: "Free / self-host",
    price: "Free",
    priceNote: "forever, self-hosted",
    badge: "Most popular for evaluators",
    description:
      "Run the full open source edition on your own box. No account, no data leaving your machine.",
    features: [
      "Runs entirely on your own infrastructure",
      "pgvector + local LLaMA model",
      "Permission-faithful retrieval",
      "Community support (GitHub issues)",
    ],
    cta: { label: "Self-host free", href: START_URL, app: true },
    variant: "pill",
    emphasized: true,
  },
  {
    id: "managed-azure",
    icon: ShieldCheck,
    eyebrow: "Managed",
    name: "Managed Azure",
    price: "Custom",
    priceNote: "hosted in your Azure tenant",
    description:
      "We run it for you, inside your own Azure tenant - with SSO set up for you and a service agreement.",
    features: [
      "Hosted in your Azure tenant",
      "SSO (Microsoft Entra / Google)",
      "SLA-backed service agreement",
      "Email support",
    ],
    cta: { label: "Book a demo", href: "/demo" },
    variant: "quiet",
  },
  {
    id: "enterprise",
    icon: ShieldCheck,
    eyebrow: "Enterprise",
    name: "Enterprise",
    price: "Talk to us",
    priceNote: "custom deployment",
    description:
      "For organizations that need air-gapped deployment, dedicated support, and a custom SLA.",
    features: [
      "Air-gapped deployment supported",
      "SSO (Microsoft Entra / Google)",
      "Dedicated support",
      "Custom SLA",
    ],
    cta: { label: "Book a demo", href: "/demo" },
    variant: "quiet",
    premium: true,
  },
];

const COMPARISON_ROWS: { label: string; values: [boolean, boolean, boolean] }[] = [
  { label: "Runs on your own box", values: [true, false, false] },
  { label: "Hosted in your Azure tenant", values: [false, true, false] },
  { label: "Air-gapped deployment", values: [false, false, true] },
  { label: "pgvector + local LLaMA model", values: [true, true, true] },
  { label: "Permission-faithful retrieval", values: [true, true, true] },
  { label: "Managed SSO setup", values: [false, true, true] },
  { label: "SLA-backed support", values: [false, true, true] },
  { label: "Dedicated support", values: [false, false, true] },
  { label: "Community support (GitHub issues)", values: [true, true, true] },
] as const;

/**
 * Three pricing tiers as cards (self-host / managed Azure / enterprise),
 * followed by a feature-comparison list. No invented dollar figures, seat
 * counts, or SLA percentages anywhere here - the product has no public list
 * price, so tiers 2 and 3 read "Custom" / "Talk to us".
 */
export function PricingTable() {
  return (
    <div>
      <div className="grid gap-6 lg:grid-cols-3 lg:items-start">
        {TIERS.map((tier) => (
          <div
            key={tier.id}
            className={cn(
              "flex h-full flex-col rounded-lg border p-8",
              tier.emphasized
                ? "border-accent bg-surface"
                : "border-border bg-surface",
              tier.premium && "bg-surface-muted",
            )}
          >
            {tier.badge && (
              <span className="mb-4 inline-flex w-fit items-center rounded-full border border-border bg-surface-muted px-3 py-1 text-micro font-mono text-fg-muted">
                {tier.badge}
              </span>
            )}

            <p className="text-micro font-mono text-fg-muted">
              {tier.eyebrow}
            </p>
            <h3 className="mt-2 text-xl font-semibold text-fg">{tier.name}</h3>
            <p className="mt-3 text-sm text-fg-muted">{tier.description}</p>

            <div className="mt-6 flex items-baseline gap-2">
              <span className="font-mono text-3xl font-semibold tabular-nums text-fg">
                {tier.price}
              </span>
              <span className="text-sm text-fg-muted">{tier.priceNote}</span>
            </div>

            <ul className="mt-6 flex-1 space-y-3">
              {tier.features.map((feature) => (
                <li
                  key={feature}
                  className="flex items-start gap-3 text-sm text-fg-muted"
                >
                  <Check
                    className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                    aria-hidden="true"
                  />
                  <span>{feature}</span>
                </li>
              ))}
            </ul>

            <div className="mt-8">
              <Button asChild variant={tier.variant} className="w-full">
                {tier.cta.external ? (
                  <a href={tier.cta.href} target="_blank" rel="noreferrer">
                    {tier.cta.label}
                  </a>
                ) : tier.cta.app ? (
                  <a href={tier.cta.href}>{tier.cta.label}</a>
                ) : (
                  <Link href={tier.cta.href}>{tier.cta.label}</Link>
                )}
              </Button>
            </div>
          </div>
        ))}
      </div>

      {/* Feature comparison */}
      <div className="mt-20">
        <h3 className="text-lg font-semibold text-fg">Compare features</h3>
        <div className="mt-6 overflow-x-auto">
          <table className="w-full min-w-[560px] border-collapse text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="py-3 pr-4 text-left font-medium text-fg-muted">
                  Feature
                </th>
                {TIERS.map((tier) => (
                  <th
                    key={tier.id}
                    className="px-4 py-3 text-left font-medium text-fg"
                  >
                    {tier.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {COMPARISON_ROWS.map((row) => (
                <tr key={row.label} className="border-b border-border/60">
                  <td className="py-3 pr-4 text-fg-muted">{row.label}</td>
                  {row.values.map((included, i) => (
                    <td key={TIERS[i].id} className="px-4 py-3">
                      {included ? (
                        <Check
                          className="h-4 w-4 text-accent"
                          aria-hidden="true"
                        />
                      ) : (
                        <X
                          className="h-4 w-4 text-fg-muted/50"
                          aria-hidden="true"
                        />
                      )}
                      <span className="sr-only">
                        {included ? "Included" : "Not included"}
                      </span>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <p className="mt-6 text-center text-xs text-fg-muted">
        Want to see the source first?{" "}
        <a
          href={GITHUB_URL}
          target="_blank"
          rel="noreferrer"
          className="rounded-sm text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          View DBSearch.AI on GitHub
        </a>
        .
      </p>
    </div>
  );
}
