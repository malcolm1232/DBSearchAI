import Link from "next/link";
import { NAV_LINKS, DOCS_URL, GITHUB_URL } from "@/lib/nav";

type FooterLink = { label: string; href: string; external?: boolean };
type FooterColumn = { heading: string; links: FooterLink[] };

const FOOTER_COLUMNS: FooterColumn[] = [
  {
    heading: "Product",
    links: NAV_LINKS.map((link) => ({ label: link.label, href: link.href })),
  },
  {
    heading: "Security & Trust",
    links: [
      { label: "Security", href: "/security" },
      { label: "Docs", href: DOCS_URL, external: true },
    ],
  },
  {
    heading: "Open source",
    links: [
      { label: "GitHub", href: GITHUB_URL, external: true },
      { label: "Self-host", href: "/self-host" },
    ],
  },
  {
    heading: "Legal",
    links: [
      { label: "Privacy", href: "/privacy" },
      { label: "Terms", href: "/terms" },
    ],
  },
];

export function SiteFooter() {
  return (
    <footer className="border-t border-border bg-bg">
      <div className="mx-auto grid max-w-6xl grid-cols-1 gap-8 px-6 py-12 sm:grid-cols-2 lg:grid-cols-4">
        {FOOTER_COLUMNS.map((column) => (
          <div key={column.heading} className="flex flex-col gap-3">
            <h3 className="text-micro font-mono text-fg-muted">
              {column.heading}
            </h3>
            {/*
              gap-0.5 rather than gap-2: the links now carry their own py-1.5
              so each one clears the 24px minimum target size, and the padding
              supplies the visual rhythm the gap used to.
            */}
            <ul className="flex flex-col gap-0.5">
              {column.links.map((link) => (
                <li key={link.label}>
                  {link.external ? (
                    <a
                      href={link.href}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-block rounded-sm py-1.5 text-sm text-fg-muted transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fg"
                    >
                      {link.label}
                    </a>
                  ) : (
                    <Link
                      href={link.href}
                      className="inline-block rounded-sm py-1.5 text-sm text-fg-muted transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fg"
                    >
                      {link.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <div className="border-t border-border px-6 py-6">
        <p className="mx-auto max-w-6xl text-xs text-fg-muted">
          © 2026 DBSearch.AI · Permission-faithful enterprise search
        </p>
      </div>
    </footer>
  );
}
