import Link from "next/link";
import { Button } from "@/components/ui/button";
import { AnswerCard } from "@/components/answer-card";
import { HowItWorks } from "@/components/sections/how-it-works";
import { PermissionProof } from "@/components/sections/permission-proof";
import { FeatureGrid } from "@/components/sections/feature-grid";
import { SelfHostBand } from "@/components/sections/self-host-band";
import { CtaBand } from "@/components/sections/cta-band";
import { START_URL } from "@/lib/nav";

const TRUST_ITEMS = [
  "Zero content egress",
  "Entra-native ACLs",
  "Every answer cited",
  "Azure or self-host",
] as const;

export default function Home() {
  return (
    <main>
      <section
        data-testid="hero"
        className="mx-auto max-w-6xl px-6 py-20 lg:py-32"
      >
        <div className="grid items-center gap-16 lg:grid-cols-12 lg:gap-10">
          {/* Left: the promise. */}
          <div className="text-center lg:col-span-5 lg:text-left">
            {/*
              Sentence case, and PLURAL: the product routes across six kinds of
              source, so "your database" would read as a single-connection dev
              tool. The permission claim is not in the headline any more; it is
              carried above the fold by the trust facts in the right column and
              by the line under the answer card.
            */}
            <h1 className="font-display text-display-1 text-fg">
              Talk to your databases.
              <br />
              Ask your company anything.
            </h1>

            <div className="mt-10 flex flex-wrap items-center justify-center gap-3 lg:justify-start">
              <Button asChild variant="pill" arrow>
                <a href={START_URL}>Self-host free</a>
              </Button>
              <Button asChild variant="quiet">
                <Link href="/demo">Book a demo</Link>
              </Button>
            </div>
          </div>

          {/* Centre: the proof, as one calm object. */}
          <div className="lg:col-span-4">
            <AnswerCard className="mx-auto max-w-sm" />
            <p className="mx-auto mt-5 max-w-sm text-center text-sm leading-relaxed text-fg-muted lg:text-left">
              3 documents matched that you&apos;re not cleared for. They were
              never retrieved.
            </p>
          </div>

          {/* Right: the plain-language claim, then the facts behind it. */}
          <div className="flex flex-col items-center text-center lg:col-span-3 lg:items-start lg:text-left">
            <p className="max-w-xs text-base leading-relaxed text-fg-muted">
              Permission-faithful enterprise search that runs inside your own
              cloud. Self-host free, or managed on Azure.
            </p>

            <hr className="my-8 w-12 border-t border-border" />

            <ul className="flex flex-col gap-2.5">
              {TRUST_ITEMS.map((item) => (
                <li key={item} className="text-micro font-mono text-fg-muted">
                  {item}
                </li>
              ))}
            </ul>

            {/* The reference's one ornament, carried over deliberately. */}
            <span
              aria-hidden="true"
              className="mt-12 font-display text-3xl leading-none text-fg"
            >
              &#10033;
            </span>
          </div>
        </div>
      </section>

      <HowItWorks />
      <PermissionProof />
      <FeatureGrid />
      <SelfHostBand />
      <CtaBand />
    </main>
  );
}
