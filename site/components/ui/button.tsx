import * as React from "react";
import { cn } from "@/lib/utils";

export type ButtonVariant =
  | "pill"
  | "pill-inverse"
  | "quiet"
  | "primary"
  | "ghost";

const VARIANT_STYLES: Record<ButtonVariant, string> = {
  // The reference's signature control: near-black pill on paper.
  pill: "rounded-full bg-fg text-bg hover:-translate-y-px hover:bg-fg/90",
  // The same pill on the inverted band, where the paper/ink roles swap.
  "pill-inverse":
    "rounded-full bg-bg text-fg hover:-translate-y-px hover:bg-white",
  // A quiet text link that sits beside a pill without competing with it.
  quiet:
    "rounded-full text-fg-muted underline-offset-4 hover:text-fg hover:underline",
  // Retained for the five inner pages not yet redesigned.
  primary: "rounded-md bg-accent text-bg hover:bg-accent-hover",
  ghost: "rounded-md text-fg-muted hover:text-fg",
};

const BASE_STYLES =
  "inline-flex min-h-11 items-center justify-center gap-2 px-5 py-2 text-sm font-medium cursor-pointer transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fg focus-visible:ring-offset-2 focus-visible:ring-offset-bg disabled:pointer-events-none disabled:opacity-50";

interface BaseProps {
  variant?: ButtonVariant;
  className?: string;
  /** Trailing decorative arrow, as on the reference's CTA pills. */
  arrow?: boolean;
}

interface AsChildProps extends BaseProps {
  asChild: true;
  children: React.ReactElement<{
    className?: string;
    children?: React.ReactNode;
  }>;
}

interface AsButtonProps
  extends BaseProps,
    React.ButtonHTMLAttributes<HTMLButtonElement> {
  asChild?: false;
}

export type ButtonProps = AsChildProps | AsButtonProps;

/**
 * Hand-written CVA-free Button (shadcn's default `init`/`add button` pulled
 * in an incompatible oklch design-token system + a @base-ui/react primitive -
 * see task-2-report.md for details). Supports `asChild` so it can render a
 * `next/link` as the interactive element (needed for the nav CTAs).
 *
 * The `arrow` glyph is aria-hidden, so a CTA's accessible name stays the label
 * alone and screen readers never announce a stray arrow.
 */
export function Button({
  variant = "pill",
  className,
  arrow,
  ...props
}: ButtonProps) {
  const classes = cn(BASE_STYLES, VARIANT_STYLES[variant], className);
  const glyph = arrow ? (
    <span aria-hidden="true" className="text-[0.9em] leading-none">
      &#8599;
    </span>
  ) : null;

  if (props.asChild) {
    const { children } = props;
    return React.cloneElement(
      children,
      { className: cn(classes, children.props.className) },
      <>
        {children.props.children}
        {glyph}
      </>
    );
  }

  const { asChild, children, ...rest } = props;
  void asChild;
  return (
    <button className={classes} {...rest}>
      {children}
      {glyph}
    </button>
  );
}
