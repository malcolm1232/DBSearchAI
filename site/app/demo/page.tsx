import type { Metadata } from "next";
import { Check } from "lucide-react";
import { DemoForm } from "@/components/demo-form";

export const metadata: Metadata = {
  title: "Book a demo - DBSearch.AI",
  description:
    "See DBSearch answer questions over your own documents - permission-trimmed, cited, and running inside your tenant. Book a short call to walk through it.",
};

const VALUE_POINTS = [
  "Answers are trimmed to what the asker is actually permitted to see - enforced at query time against your live directory, not a stale snapshot.",
  "It runs inside your own cloud tenant, so your document content never has to leave your environment to get an answer.",
  "Every answer is cited back to the source document, so you can verify it instead of taking the model's word for it.",
] as const;

export default function DemoPage() {
  return (
    <main>
      <section className="vault-grid border-b border-border">
        <div className="mx-auto max-w-6xl px-6 py-20 lg:py-28">
          <div className="grid gap-16 lg:grid-cols-2 lg:items-start">
            <div className="max-w-xl">
              <p className="text-micro font-mono text-fg-muted">
                Book a demo
              </p>
              <h1 className="mt-3 font-display text-display-2 text-fg">
                See DBSearch answer your own documents.
              </h1>
              <p className="mt-6 text-lg text-fg-muted">
                Bring a handful of real documents to the call. We&apos;ll show
                you how DBSearch answers questions over them - with the same
                permission checks and citations your users would see in
                production.
              </p>

              <ul className="mt-10 flex flex-col gap-5">
                {VALUE_POINTS.map((point) => (
                  <li key={point} className="flex items-start gap-3">
                    <Check
                      className="mt-0.5 h-5 w-5 shrink-0 text-accent"
                      aria-hidden="true"
                    />
                    <span className="text-sm text-fg-muted">{point}</span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="rounded-lg border border-border bg-surface p-6 sm:p-8">
              <h2 className="text-lg font-semibold text-fg">
                Request a demo
              </h2>
              <p className="mt-2 text-sm text-fg-muted">
                Tell us a bit about your team and we&apos;ll get back to you
                to schedule a time.
              </p>
              <div className="mt-8">
                <DemoForm />
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>
  );
}
