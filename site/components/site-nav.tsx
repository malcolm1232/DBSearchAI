"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, X } from "lucide-react";
import { NAV_LINKS, DOCS_URL, APP_URL, START_URL, SIGN_IN_URL } from "@/lib/nav";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

function NavLink({
  href,
  label,
  isActive,
  onClick,
  mobile,
}: {
  href: string;
  label: string;
  isActive: boolean;
  onClick?: () => void;
  mobile?: boolean;
}) {
  return (
    <Link
      href={href}
      aria-current={isActive ? "page" : undefined}
      onClick={onClick}
      className={cn(
        "rounded-sm text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fg",
        isActive ? "text-fg" : "text-fg-muted hover:text-fg",
        mobile && "block min-h-11 py-2.5"
      )}
    >
      {label}
    </Link>
  );
}

function DocsLink() {
  return (
    <a
      href={DOCS_URL}
      target="_blank"
      rel="noreferrer"
      className="rounded-sm text-sm font-medium text-fg-muted transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fg"
    >
      Docs ↗
    </a>
  );
}

function OpenAppLink({ mobile }: { mobile?: boolean }) {
  return (
    <a
      href={APP_URL}
      className={cn(
        "rounded-sm text-sm font-medium text-fg-muted transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fg",
        mobile && "block min-h-11 py-2.5"
      )}
    >
      Open app ↗
    </a>
  );
}

export function SiteNav() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  return (
    <header className="sticky top-0 z-50 border-b border-border bg-bg/85 backdrop-blur-sm">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-6">
        <Link
          href="/"
          className="rounded-sm font-display text-xl text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fg"
        >
          DBSearch<span className="text-accent">.AI</span>
        </Link>

        <nav
          className="hidden items-center gap-6 md:flex"
          aria-label="Primary"
        >
          {NAV_LINKS.map((link) => (
            <NavLink
              key={link.href}
              href={link.href}
              label={link.label}
              isActive={pathname === link.href}
            />
          ))}
          <DocsLink />
          <OpenAppLink />
        </nav>

        <div className="hidden items-center gap-2 md:flex">
          {/* #386: returning users had no door - the page never said "Sign in". */}
          <Button asChild variant="quiet">
            <a href={SIGN_IN_URL}>Sign in</a>
          </Button>
          <Button asChild variant="quiet">
            <Link href="/demo">Book a demo</Link>
          </Button>
          <Button asChild variant="pill" arrow>
            <a href={START_URL}>Self-host free</a>
          </Button>
        </div>

        <button
          type="button"
          className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md p-2 text-fg-muted transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fg md:hidden"
          aria-label="Toggle menu"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? <X className="size-6" /> : <Menu className="size-6" />}
        </button>
      </div>

      {open && (
        <nav
          className="border-t border-border px-6 pb-6 md:hidden"
          aria-label="Primary mobile"
        >
          <div className="flex flex-col gap-4 pt-4">
            {NAV_LINKS.map((link) => (
              <NavLink
                key={link.href}
                href={link.href}
                label={link.label}
                isActive={pathname === link.href}
                onClick={() => setOpen(false)}
                mobile
              />
            ))}
            <DocsLink />
            <OpenAppLink mobile />
            <Button asChild variant="quiet">
              <a href={SIGN_IN_URL} onClick={() => setOpen(false)}>
                Sign in
              </a>
            </Button>
            <Button asChild variant="quiet">
              <Link href="/demo" onClick={() => setOpen(false)}>
                Book a demo
              </Link>
            </Button>
            <Button asChild variant="pill" arrow>
              <a href={START_URL} onClick={() => setOpen(false)}>
                Self-host free
              </a>
            </Button>
          </div>
        </nav>
      )}
    </header>
  );
}
