import Link from "next/link";
import { cn } from "@/lib/utils";

/* The Architecture section's chapter switcher: both chapters visible at all
 * times, one active. An ink segment marks "you are here" (green stays a
 * signal); the inactive segment is a quiet link. Server component - the
 * active chapter is a build-time fact of the page that renders it. */
const CHAPTERS = [
  { key: "laws", href: "/architecture", num: "Chapter 1", name: "The laws" },
  {
    key: "query",
    href: "/architecture/query",
    num: "Chapter 2",
    name: "Life of a query",
  },
] as const;

export function ChapterTabs({ active }: { active: "laws" | "query" }) {
  return (
    <nav aria-label="Architecture chapters" className="mt-5">
      <ul className="inline-flex max-w-full overflow-hidden rounded-full border border-border">
        {CHAPTERS.map(({ key, href, num, name }, i) => {
          const isActive = key === active;
          return (
            <li key={key} className={cn("flex", i > 0 && "border-l border-border")}>
              <Link
                href={href}
                aria-current={isActive ? "page" : undefined}
                className={cn(
                  "px-4 py-2 font-mono text-xs whitespace-nowrap transition-colors",
                  isActive
                    ? "bg-fg text-bg"
                    : "text-fg-muted hover:bg-surface hover:text-fg",
                )}
              >
                <span className="hidden sm:inline">{num} · </span>
                {name}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
