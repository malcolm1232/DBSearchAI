"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

export interface CopyBlockProps {
  code: string;
  className?: string;
}

/**
 * Click-to-copy command block. Mono text on `bg-surface` with a copy button
 * that swaps the Lucide `Copy` icon for `Check` for ~1.5s after a successful
 * `navigator.clipboard.writeText`.
 */
export function CopyBlock({ code, className }: CopyBlockProps) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    if (!navigator?.clipboard?.writeText) {
      return;
    }

    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div
      className={cn(
        "flex items-center justify-between gap-4 rounded-lg border border-border bg-surface px-4 py-3 font-mono text-sm text-fg-muted",
        className
      )}
    >
      <code className="overflow-x-auto whitespace-pre">{code}</code>
      <button
        type="button"
        aria-label="Copy"
        onClick={handleCopy}
        className="inline-flex h-11 w-11 shrink-0 cursor-pointer items-center justify-center rounded-md text-fg-muted transition-colors hover:bg-surface-muted hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        {copied ? (
          <Check className="h-4 w-4 text-accent" aria-hidden="true" />
        ) : (
          <Copy className="h-4 w-4" aria-hidden="true" />
        )}
      </button>
    </div>
  );
}
