import Link from "next/link";
import type { Metadata } from "next";
import { Cloud, HardDrive, FolderSync, Plug, Link2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AnswerCard } from "@/components/answer-card";
import { SectionHeading } from "@/components/sections/section-heading";
import { CANVAS_URL, START_URL } from "@/lib/nav";
import { CtaBand } from "@/components/sections/cta-band";

export const metadata: Metadata = {
  title: "Product - DBSearch.AI",
  description:
    "How DBSearch.AI works: connectors over Microsoft Graph, permission-trimming at query time against live Entra groups, cited answers, and a control/data-plane split that keeps your documents inside your tenant.",
};

const CONNECTORS = [
  {
    icon: Cloud,
    name: "SharePoint",
    body: "Sites and document libraries, connected read-only via Microsoft Graph.",
  },
  {
    icon: FolderSync,
    name: "OneDrive",
    body: "Personal and shared drives, indexed with the same Graph permissions model.",
  },
  {
    icon: HardDrive,
    name: "Local file shares",
    body: "On-prem or self-hosted shares, indexed in place without copying files off-box.",
  },
] as const;

const CITATION_POINTS = [
  "Every synthesized answer carries inline citation markers, like [1] and [2] above.",
  "Each marker deep-links to the exact source document - not just a folder or site.",
  "If a document can't be re-verified, the answer isn't shown as high confidence.",
] as const;

export default function ProductPage() {
  return (
    <main>
      {/* Hero */}
      <section className="vault-grid border-b border-border">
        <div className="mx-auto max-w-3xl px-6 py-20 text-center lg:py-28">
          <p className="text-micro font-mono text-fg-muted">
            Product
          </p>
          {/*
            #420: aligned to the home page's voice - verb-first and plural. The
            old line was the only noun-fragment headline on the site, said
            "document" singular when the product routes across six kinds of
            source, and used "allowed to see" where the rest of the copy says
            "cleared for".
          */}
          <h1 className="mt-3 font-display text-display-2 text-fg">
            Ask once.
            <br />
            Search every source you&apos;re cleared for.
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg text-fg-muted">
            DBSearch.AI connects to the places your company already stores
            knowledge, retrieves and cites the relevant documents, and
            re-checks permissions against your live directory before a single
            result is shown - all inside your own cloud.
          </p>
          <div className="mt-8 flex flex-wrap items-center justify-center gap-4">
            <Button asChild variant="pill">
              <a href={START_URL}>Self-host free</a>
            </Button>
            <Button asChild variant="quiet">
              <Link href="/demo">Book a demo</Link>
            </Button>
          </div>
        </div>
      </section>

      {/* Try it live */}
      <section className="border-b border-border">
        <div className="mx-auto max-w-6xl px-6 py-20 lg:py-28">
          <p className="text-micro font-mono text-fg-muted">
            Live demo
          </p>
          <h2 className="mt-3 text-3xl font-semibold tracking-display text-fg sm:text-4xl">
            Try it live
          </h2>
          <p className="mt-4 max-w-2xl text-fg-muted">
            Ask the seeded demo corpus as two different people. Alice is on the deal
            team; Bob is not. Ask about Project Falcon and watch the answer change
            with who is asking, which is the same permission trim your users get in
            production.
          </p>
          <div className="mt-10 max-w-2xl">
            <AnswerCard />
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <Button asChild variant="pill" arrow>
                <a href={CANVAS_URL}>Open the live demo</a>
              </Button>
              <span className="text-sm text-fg-muted">
                Six stores, two identities, real permission trimming.
              </span>
            </div>
          </div>
        </div>
      </section>

      {/* Connectors */}
      <section className="mx-auto max-w-6xl px-6 py-24">
        <SectionHeading
          kicker="Connectors"
          title="Plug into where your knowledge already lives"
          sub="SharePoint, OneDrive, and local file shares connect through Microsoft Graph, with ACLs intact - nothing is copied out of your tenant."
        />

        <div className="mt-16 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {CONNECTORS.map(({ icon: Icon, name, body }) => (
            <div
              key={name}
              className="rounded-lg border border-border bg-surface p-6"
            >
              <Icon className="h-6 w-6 text-accent" aria-hidden="true" />
              <h3 className="mt-4 text-base font-semibold text-fg">{name}</h3>
              <p className="mt-2 text-sm text-fg-muted">{body}</p>
            </div>
          ))}

          <div className="rounded-lg border border-dashed border-border bg-surface-muted p-6">
            <Plug className="h-6 w-6 text-fg-muted" aria-hidden="true" />
            <h3 className="mt-4 text-base font-semibold text-fg">
              Built on ports
            </h3>
            <p className="mt-2 text-sm text-fg-muted">
              Every source connects through the same stable connector
              interface - a &quot;port&quot; - so more sources can be added
              without changing how permissions are enforced.
            </p>
          </div>
        </div>
      </section>

      {/* Permission-trimming at query time */}
      <section className="border-y border-border bg-surface">
        <div className="mx-auto grid max-w-6xl gap-12 px-6 py-24 lg:grid-cols-2 lg:items-center">
          <div>
            <p className="text-micro font-mono text-fg-muted">
              Query-time enforcement
            </p>
            <h2 className="mt-3 text-3xl font-semibold tracking-display text-fg sm:text-4xl">
              Permissions are checked when you ask - not just when it&apos;s
              indexed
            </h2>
            <p className="mt-4 max-w-md text-lg text-fg-muted">
              Before any result reaches the screen, it&apos;s filtered against
              the asker&apos;s live Entra group membership. Access that was
              revoked yesterday is enforced today - no stale index to leak a
              document someone shouldn&apos;t see.
            </p>
          </div>

          <div className="flex justify-center lg:justify-end">
            <div className="w-full max-w-md">
              <AnswerCard />
            </div>
          </div>
        </div>
      </section>

      {/* Cited, verifiable answers */}
      <section className="mx-auto max-w-6xl px-6 py-24">
        <SectionHeading
          kicker="Verifiable by design"
          title="Cited, verifiable answers"
          sub="Search results are only useful if you can trust them. Every answer traces back to where it came from."
        />

        <div className="mx-auto mt-16 max-w-3xl">
          <ul className="space-y-4">
            {CITATION_POINTS.map((point) => (
              <li
                key={point}
                className="flex items-start gap-3 rounded-lg border border-border bg-surface p-5"
              >
                <Link2
                  className="mt-0.5 h-5 w-5 shrink-0 text-accent"
                  aria-hidden="true"
                />
                <p className="text-sm text-fg-muted">{point}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* Architecture overview */}
      <section className="border-t border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Architecture"
            title="Control plane and data plane, cleanly split"
            sub="The control plane only ever sees metadata - document counts, health, and version information. Your documents, their contents, and the answers generated from them never leave your tenant."
          />

          <div className="mt-16">
            <div className="grid gap-6 lg:grid-cols-2">
              <div className="rounded-lg border border-border bg-bg p-8">
                <p className="text-micro font-mono text-fg-muted">
                  Data plane - inside your tenant
                </p>
                <h3 className="mt-2 text-lg font-semibold text-fg">
                  Where your documents live
                </h3>
                <ul className="mt-4 space-y-2 text-sm text-fg-muted">
                  <li>Documents, embeddings, and the search index</li>
                  <li>Entra group membership checks</li>
                  <li>Retrieval and answer synthesis</li>
                </ul>
              </div>

              <div className="rounded-lg border border-border bg-surface-muted p-8">
                <p className="font-mono text-xs font-medium uppercase tracking-widest text-fg-muted">
                  Control plane - DBSearch.AI
                </p>
                <h3 className="mt-2 text-lg font-semibold text-fg">
                  What we operate
                </h3>
                <ul className="mt-4 space-y-2 text-sm text-fg-muted">
                  <li>Document counts</li>
                  <li>Health and uptime</li>
                  <li>Version and deployment metadata</li>
                </ul>
              </div>
            </div>

            <div
              className="mt-6 flex items-center gap-3"
              aria-hidden="true"
            >
              <span className="hidden h-px flex-1 bg-border sm:block" />
              <span className="rounded-full border border-border px-3 py-1 text-center font-mono text-xs uppercase tracking-wide text-fg-muted">
                Tenant boundary - no document content crosses
              </span>
              <span className="hidden h-px flex-1 bg-border sm:block" />
            </div>
          </div>
        </div>
      </section>

      <CtaBand />
    </main>
  );
}
