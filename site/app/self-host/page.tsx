import Link from "next/link";
import type { Metadata } from "next";
import { Check, X, Server, HardDrive, Cpu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CopyBlock } from "@/components/copy-block";
import { SectionHeading } from "@/components/sections/section-heading";
import { CtaBand } from "@/components/sections/cta-band";
import { GITHUB_URL } from "@/lib/nav";

export const metadata: Metadata = {
  title: "Self-host - DBSearch.AI",
  description:
    "Run the free, open source edition of DBSearch.AI on your own box: Docker Compose, pgvector, and a local LLaMA model. No account, no data leaving your machine.",
};

const QUICKSTART_STEPS = [
  {
    label: "Build and start the stack",
    code: "docker compose up -d --build && ./scripts/ollama_pull.sh",
  },
  {
    label: "Confirm it's healthy",
    code: "curl localhost:8080/health",
  },
] as const;

const OPEN_CORE_FEATURES = [
  { label: "Free, forever", included: true },
  { label: "Runs entirely on your own box", included: true },
  { label: "pgvector + local LLaMA model", included: true },
  { label: "Community support (GitHub issues)", included: true },
  { label: "Managed SSO setup", included: false },
  { label: "SLA-backed uptime", included: false },
] as const;

const MANAGED_FEATURES = [
  { label: "Hosted for you on Azure", included: true },
  { label: "Managed SSO setup", included: true },
  { label: "SLA-backed uptime", included: true },
  { label: "Dedicated support", included: true },
  { label: "pgvector + local LLaMA model", included: true },
] as const;

const REQUIREMENTS = [
  {
    icon: Server,
    name: "Docker",
    body: "Docker Engine with the Compose plugin - that's the whole runtime dependency.",
  },
  {
    icon: HardDrive,
    name: "Disk space",
    body: "Room for the local LLaMA model weights plus your indexed documents and embeddings.",
  },
  {
    icon: Cpu,
    name: "GPU (optional)",
    body: "A GPU speeds up local inference for the LLaMA model; CPU-only works, just slower.",
  },
] as const;

export default function SelfHostPage() {
  return (
    <main>
      {/* Hero */}
      <section className="vault-grid border-b border-border">
        <div className="mx-auto max-w-3xl px-6 py-20 text-center lg:py-28">
          <p className="text-micro font-mono text-fg-muted">
            Self-host
          </p>
          <h1 className="mt-3 font-display text-display-2 text-fg">
            Run DBSearch on your own box. Free.
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg text-fg-muted">
            The open source edition is free and runs entirely on your own
            infrastructure - pgvector for retrieval, a local LLaMA model for
            synthesis, and nothing sent off-box. No account required.
          </p>

          <div className="mx-auto mt-10 max-w-xl">
            <CopyBlock code="docker compose up -d --build && ./scripts/ollama_pull.sh" />
          </div>

          <div className="mt-8 flex flex-wrap items-center justify-center gap-4">
            <Button asChild variant="pill">
              <a href={GITHUB_URL} target="_blank" rel="noreferrer">
                <Server className="h-4 w-4" aria-hidden="true" />
                View on GitHub
              </a>
            </Button>
            <Button asChild variant="quiet">
              <Link href="/demo">Book a demo</Link>
            </Button>
          </div>
        </div>
      </section>

      {/* Quickstart */}
      <section className="mx-auto max-w-4xl px-6 py-24">
        <SectionHeading
          kicker="Quickstart"
          title="Two commands to a running instance"
          sub="Clone the repo, then run these from its root."
          align="center"
        />

        <ol className="mx-auto mt-12 max-w-2xl space-y-6">
          {QUICKSTART_STEPS.map((step, i) => (
            <li key={step.code}>
              <div className="flex items-center gap-3">
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface-muted font-mono text-xs text-fg-muted">
                  {i + 1}
                </span>
                <p className="text-sm font-medium text-fg">{step.label}</p>
              </div>
              <div className="mt-3 pl-9">
                <CopyBlock code={step.code} />
              </div>
            </li>
          ))}
        </ol>
      </section>

      {/* Open-core vs Managed Azure */}
      <section className="border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Compare"
            title="Open-core vs. Managed Azure"
            sub="Same permission-faithful retrieval either way. Choose where it runs."
          />

          <div className="mt-16 grid gap-6 lg:grid-cols-2">
            <div className="rounded-lg border border-border bg-bg p-8">
              <p className="text-micro font-mono text-fg-muted">
                Open-core
              </p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                Free, self-hosted
              </h3>
              <ul className="mt-6 space-y-3">
                {OPEN_CORE_FEATURES.map((f) => (
                  <li
                    key={f.label}
                    className="flex items-start gap-3 text-sm text-fg-muted"
                  >
                    {f.included ? (
                      <Check
                        className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                        aria-hidden="true"
                      />
                    ) : (
                      <X
                        className="mt-0.5 h-4 w-4 shrink-0 text-fg-muted/50"
                        aria-hidden="true"
                      />
                    )}
                    <span className={f.included ? "text-fg" : ""}>
                      {f.label}
                    </span>
                  </li>
                ))}
              </ul>
              <div className="mt-8">
                <Button asChild variant="pill" className="w-full">
                  <a href={GITHUB_URL} target="_blank" rel="noreferrer">
                    View on GitHub
                  </a>
                </Button>
              </div>
            </div>

            <div className="rounded-lg border border-border bg-surface-muted p-8">
              <p className="font-mono text-xs font-medium uppercase tracking-widest text-fg-muted">
                Managed Azure
              </p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                Hosted, for teams
              </h3>
              <ul className="mt-6 space-y-3">
                {MANAGED_FEATURES.map((f) => (
                  <li
                    key={f.label}
                    className="flex items-start gap-3 text-sm text-fg-muted"
                  >
                    {f.included ? (
                      <Check
                        className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                        aria-hidden="true"
                      />
                    ) : (
                      <X
                        className="mt-0.5 h-4 w-4 shrink-0 text-fg-muted/50"
                        aria-hidden="true"
                      />
                    )}
                    <span className={f.included ? "text-fg" : ""}>
                      {f.label}
                    </span>
                  </li>
                ))}
              </ul>
              <div className="mt-8">
                <Button asChild variant="quiet" className="w-full">
                  <Link href="/demo">Book a demo</Link>
                </Button>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Requirements */}
      <section className="mx-auto max-w-6xl px-6 py-24">
        <SectionHeading
          kicker="Requirements"
          title="What you need to run it"
          sub="A single Docker host is enough to get started."
        />

        <div className="mt-16 grid gap-6 sm:grid-cols-3">
          {REQUIREMENTS.map(({ icon: Icon, name, body }) => (
            <div
              key={name}
              className="rounded-lg border border-border bg-surface p-6"
            >
              <Icon className="h-6 w-6 text-accent" aria-hidden="true" />
              <h3 className="mt-4 text-base font-semibold text-fg">{name}</h3>
              <p className="mt-2 text-sm text-fg-muted">{body}</p>
            </div>
          ))}
        </div>
      </section>

      <CtaBand />
    </main>
  );
}
