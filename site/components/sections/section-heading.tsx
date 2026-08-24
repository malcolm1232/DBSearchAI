export interface SectionHeadingProps {
  kicker: string;
  title: string;
  sub?: string;
  align?: "left" | "center";
}

/**
 * Reusable section header: mono uppercase kicker in muted ink, a serif display
 * h2, and an optional sub-paragraph. Used by every marketing section on the
 * page. The kicker moved off accent green so that green stays a signal rather
 * than a decoration.
 */
export function SectionHeading({
  kicker,
  title,
  sub,
  align = "center",
}: SectionHeadingProps) {
  return (
    <div
      className={
        align === "center"
          ? "mx-auto max-w-2xl text-center"
          : "max-w-2xl text-left"
      }
    >
      <p className="text-micro font-mono text-fg-muted">{kicker}</p>
      <h2 className="mt-4 font-display text-display-2 text-fg">{title}</h2>
      {sub && <p className="mt-4 text-lg text-fg-muted">{sub}</p>}
    </div>
  );
}
