import Link from "next/link";
import type { Metadata } from "next";
import {
  ShieldCheck,
  Lock,
  Check,
  X,
  Eye,
  Server,
  FileX,
  KeyRound,
  Cpu,
  ShieldOff,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { SectionHeading } from "@/components/sections/section-heading";
import { CtaBand } from "@/components/sections/cta-band";
import { START_URL } from "@/lib/nav";

export const metadata: Metadata = {
  title: "Security - DBSearch.AI",
  description:
    "How DBSearch.AI keeps your documents inside your tenant: a control/data-plane split, a content-rejecting uplink, Entra-native ACLs enforced at query time, in-tenant model hosting, and zero content egress - no exceptions.",
};

const PROMISES = [
  {
    icon: Eye,
    title: "We never see your data",
    body: "Document content - text, file bytes, embeddings derived from content - never crosses out of your tenant. We operate the control plane; you keep the data plane.",
  },
  {
    icon: ShieldCheck,
    title: "We never return an unauthorized result",
    body: "Every result is filtered against the asker's live directory permissions before it reaches the screen. The filter is mandatory and default-deny - no feature can bypass it.",
  },
  {
    icon: Server,
    title: "It runs inside your own tenant",
    body: "Indexing, retrieval, and answer generation all execute in your cloud subscription (or fully air-gapped). Nothing about your knowledge base lives on our infrastructure.",
  },
] as const;

const LAWS = [
  {
    icon: Lock,
    title: "Control-plane / data-plane split",
    body: "Your documents, embeddings, index, and LLM inference all run in the data plane - your tenant. The control plane we operate receives metadata and telemetry only, never document content.",
  },
  {
    icon: FileX,
    title: "A content-rejecting uplink",
    body: "The only channel between your tenant and DBSearch.AI is a narrow, schema-enforced uplink. It physically cannot carry document text or file bytes - only counts, health, and version metadata cross it.",
  },
  {
    icon: KeyRound,
    title: "Entra-native ACLs, enforced at query time",
    body: "Permissions are re-checked against live Entra group membership on every query - not a stale snapshot taken at indexing time. Access revoked five minutes ago is enforced five minutes later.",
  },
  {
    icon: Cpu,
    title: "In-tenant model hosting",
    body: "The LLM that reads your documents and writes your answers runs inside your cloud, or fully air-gapped with no outbound network path at all. Your prompts and your data meet the model on your infrastructure, not ours.",
  },
  {
    icon: ShieldOff,
    title: "Zero content egress",
    body: "This isn't a policy promise layered on top of a system that could technically leak - it's an architectural constraint. There is no code path by which document content can leave your tenant.",
  },
] as const;

const CAN_SEE = [
  "Document and connector counts",
  "Index and service health, uptime",
  "Software version and deployment metadata",
  "Aggregate usage (e.g. queries per day)",
  "Connector sync status and error codes",
] as const;

const CANNOT_SEE = [
  "Document contents or file bodies",
  "Any text extracted from a customer file",
  "Embeddings derived from document content",
  "Query text or the answers shown to users",
  "Individual users' document-level permissions",
] as const;

export default function SecurityPage() {
  return (
    <main>
      {/* Hero */}
      <section className="vault-grid border-b border-border">
        <div className="mx-auto max-w-3xl px-6 py-20 text-center lg:py-28">
          <p className="text-micro font-mono text-fg-muted">
            Security
          </p>
          <h1 className="mt-3 font-display text-display-2 text-fg">
            Built so we never have to be trusted with your data
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg text-fg-muted">
            DBSearch.AI runs inside your own cloud tenant. Document content
            never reaches us, and every result is re-checked against your
            live directory permissions before a user ever sees it - by
            architecture, not by policy.
          </p>
          <div className="mt-8 flex flex-wrap items-center justify-center gap-4">
            <Button asChild variant="pill">
              <Link href="/demo">Book a demo</Link>
            </Button>
            <Button asChild variant="quiet">
              <a href={START_URL}>Self-host free</a>
            </Button>
          </div>
        </div>
      </section>

      {/* The three promises */}
      <section className="mx-auto max-w-6xl px-6 py-24">
        <SectionHeading
          kicker="The promises"
          title="Three commitments the architecture enforces"
          sub="These aren't things we ask you to trust us on - they're constraints built into how the product is deployed."
        />

        <div className="mt-16 grid gap-6 lg:grid-cols-3">
          {PROMISES.map(({ icon: Icon, title, body }) => (
            <div
              key={title}
              className="rounded-lg border border-border bg-surface p-8"
            >
              <Icon className="h-6 w-6 text-accent" aria-hidden="true" />
              <h3 className="mt-4 text-lg font-semibold text-fg">{title}</h3>
              <p className="mt-2 text-sm text-fg-muted">{body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* The LAWs in plain language */}
      <section className="border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="How it works"
            title="The invariants, in plain language"
            sub="Every one of these is a hard rule in the codebase, not a best-effort guideline - a feature that would violate one gets redesigned, not shipped."
          />

          <div className="mt-16 grid gap-6 sm:grid-cols-2">
            {LAWS.map(({ icon: Icon, title, body }) => (
              <div
                key={title}
                className="flex gap-4 rounded-lg border border-border bg-bg p-6"
              >
                <Icon
                  className="mt-0.5 h-5 w-5 shrink-0 text-accent"
                  aria-hidden="true"
                />
                <div>
                  <h3 className="text-base font-semibold text-fg">{title}</h3>
                  <p className="mt-2 text-sm text-fg-muted">{body}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* What the control plane can and cannot see */}
      <section className="mx-auto max-w-6xl px-6 py-24">
        <SectionHeading
          kicker="Transparency"
          title="What the control plane can and cannot see"
          sub="We operate the control plane - the piece that keeps your deployment healthy and licensed. Here is the complete list of what it has access to, and what it never touches."
        />

        <div
          aria-label="What the control plane can and cannot see"
          className="mt-16 grid gap-6 sm:grid-cols-2"
        >
          <div
            className="rounded-lg border border-accent/40 bg-surface p-6"
          >
            <p
              className="flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-widest text-accent"
            >
              <Check className="h-4 w-4" aria-hidden="true" />
              Can see - metadata
            </p>
            <ul className="mt-5 space-y-3">
              {CAN_SEE.map((item) => (
                <li
                  key={item}
                  className="flex items-start gap-3 text-sm text-fg"
                >
                  <Check
                    className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                    aria-hidden="true"
                  />
                  {item}
                </li>
              ))}
            </ul>
          </div>

          <div
            className="rounded-lg border border-destructive/40 bg-surface p-6"
          >
            <p
              className="flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-widest text-destructive"
            >
              <X className="h-4 w-4" aria-hidden="true" />
              Cannot see - content
            </p>
            <ul className="mt-5 space-y-3">
              {CANNOT_SEE.map((item) => (
                <li
                  key={item}
                  className="flex items-start gap-3 text-sm text-fg"
                >
                  <X
                    className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
                    aria-hidden="true"
                  />
                  {item}
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      {/* Compliance posture */}
      <section className="border-t border-border bg-surface">
        <div className="mx-auto max-w-4xl px-6 py-24">
          <SectionHeading
            kicker="Compliance"
            title="Compliance posture"
            sub="We'd rather tell you exactly where we are than show you a badge we haven't earned."
          />

          <div className="mt-16 grid gap-6 sm:grid-cols-2">
            <div className="rounded-lg border border-border bg-bg p-6">
              <div className="flex items-center justify-between">
                <h3 className="text-base font-semibold text-fg">SOC 2</h3>
                <span className="rounded-full border border-border px-3 py-1 font-mono text-xs uppercase tracking-wide text-fg-muted">
                  Planned
                </span>
              </div>
              <p className="mt-3 text-sm text-fg-muted">
                We are not SOC 2 certified today. The control/data-plane split
                and default-deny permission model are designed from the
                ground up to support your own SOC 2 audit - since your
                documents never leave your environment, most of the audit
                surface stays inside your existing controls.
              </p>
            </div>

            <div className="rounded-lg border border-border bg-bg p-6">
              <div className="flex items-center justify-between">
                <h3 className="text-base font-semibold text-fg">
                  ISO 27001
                </h3>
                <span className="rounded-full border border-border px-3 py-1 font-mono text-xs uppercase tracking-wide text-fg-muted">
                  Roadmap
                </span>
              </div>
              <p className="mt-3 text-sm text-fg-muted">
                ISO 27001 certification for DBSearch.AI as an organization is
                on our roadmap, not yet achieved. In the meantime, in-tenant
                deployment means our architecture is designed to sit inside
                the ISO-scoped environment you already operate and audit.
              </p>
            </div>
          </div>

          <p className="mx-auto mt-8 max-w-2xl text-center text-sm text-fg-muted">
            Ask us for our current security architecture documentation and
            open-source codebase during your evaluation - we&apos;d rather
            you verify the control/data-plane split yourself than take our
            word for it. Or walk the boundary in the actual code on the{" "}
            <Link
              href="/architecture"
              className="text-fg underline underline-offset-4 hover:text-accent"
            >
              Architecture
            </Link>{" "}
            page.
          </p>
        </div>
      </section>

      <CtaBand />
    </main>
  );
}
