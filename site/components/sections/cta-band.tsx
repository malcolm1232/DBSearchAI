import Link from "next/link";
import { Button } from "@/components/ui/button";
import { START_URL } from "@/lib/nav";

/**
 * The page's one deliberate contrast beat, mirroring the inverted band the
 * reference design uses near the foot of its page. Also rendered at the foot
 * of Product, Security, Pricing and Self-host.
 */
export function CtaBand() {
  return (
    <section className="bg-fg">
      <div className="mx-auto flex max-w-4xl flex-col items-center gap-10 px-6 py-24 text-center lg:py-32">
        {/*
          Explicit break at the sentence boundary rather than trusting a
          max-width: left to wrap, the line broke after "your data" and
          orphaned "yours." on its own line. Suppressed below sm, where the
          headline wraps several ways anyway.
        */}
        <h2 className="max-w-2xl font-display text-display-2 text-bg">
          Bring search to your knowledge.
          <br className="hidden sm:inline" />{" "}
          Keep your data yours.
        </h2>
        <div className="flex flex-wrap items-center justify-center gap-3">
          <Button asChild variant="pill-inverse" arrow>
            <a href={START_URL}>Self-host free</a>
          </Button>
          <Button
            asChild
            variant="quiet"
            className="text-on-ink-muted hover:text-bg"
          >
            <Link href="/demo">Book a demo</Link>
          </Button>
        </div>
      </div>
    </section>
  );
}
