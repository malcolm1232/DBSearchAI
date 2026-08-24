import { Button } from "@/components/ui/button";
import { GITHUB_URL, START_URL } from "@/lib/nav";

export function SelfHostBand() {
  return (
    <section className="mx-auto max-w-6xl px-6 py-20 lg:py-32">
      <div className="grid gap-12 lg:grid-cols-2 lg:items-center">
        <div>
          <p className="text-micro font-mono text-fg-muted">Open core</p>
          <h2 className="mt-4 font-display text-display-2 text-fg">
            Run it yourself. Free.
          </h2>
          <p className="mt-5 max-w-md text-lg leading-relaxed text-fg-muted">
            The full open source edition, with the same permission-faithful
            retrieval, running entirely on your own box. Nothing leaves your
            machine.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Button asChild variant="pill" arrow>
              <a href={START_URL}>Self-host free</a>
            </Button>
            <Button asChild variant="quiet">
              <a href={GITHUB_URL} target="_blank" rel="noreferrer">
                View on GitHub
              </a>
            </Button>
          </div>
        </div>

        {/*
          The one place dark is allowed before the CTA band: a command reads as
          a command. `overflow-x-auto` keeps a long line scrolling inside its
          own container at 390px instead of extending the page.
        */}
        <div className="overflow-x-auto rounded-xl bg-fg p-6 font-mono text-sm text-on-ink-muted">
          <p className="whitespace-nowrap">
            <span className="text-accent-bright" aria-hidden="true">
              ${" "}
            </span>
            docker compose up -d --build
          </p>
          <p className="mt-2 whitespace-nowrap">
            <span className="text-accent-bright" aria-hidden="true">
              ${" "}
            </span>
            curl localhost:8080/health
          </p>
        </div>
      </div>
    </section>
  );
}
