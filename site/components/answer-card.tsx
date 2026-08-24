import { cn } from "@/lib/utils";

export interface AnswerCardProps {
  question?: string;
  answer?: string;
  scope?: string;
  className?: string;
}

const DEFAULT_QUESTION = "What is our holiday & expenses policy?";
const DEFAULT_ANSWER = "25 days PTO, receipts filed within 30 days";
const DEFAULT_SCOPE = "Searched 1,721 documents AK is allowed to see";

/**
 * Presentational "High confidence" answer preview, and the calm centrepiece of
 * the home hero. Reused on Home + Product pages, so it takes no state/effects
 * and stays a plain server-renderable component.
 *
 * The shell prompt and the monospace answer body were dropped in the paper
 * redesign: on a light canvas the whole card was reading as a terminal rather
 * than as an answer. Mono is now reserved for the two micro-labels, which is
 * what makes them read as metadata.
 */
export function AnswerCard({
  question = DEFAULT_QUESTION,
  answer = DEFAULT_ANSWER,
  scope = DEFAULT_SCOPE,
  className,
}: AnswerCardProps) {
  return (
    <div
      className={cn(
        "rounded-xl border border-border bg-surface p-6 shadow-[0_1px_2px_rgba(22,22,26,0.04),0_12px_32px_-12px_rgba(22,22,26,0.12)]",
        className
      )}
    >
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className="size-1.5 rounded-full bg-accent-bright"
        />
        <span className="text-micro font-mono text-fg-muted">
          High confidence
        </span>
      </div>

      <p className="mt-5 font-sans text-sm text-fg-muted">{question}</p>

      <p className="mt-2 font-sans text-base leading-relaxed text-fg">
        {answer}
        <sup className="ml-0.5 font-mono text-[0.65em] text-accent">1,2</sup>
      </p>

      <p className="mt-5 border-t border-border pt-4 text-micro font-mono text-fg-muted">
        {scope}
      </p>
    </div>
  );
}
