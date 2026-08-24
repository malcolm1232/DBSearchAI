import type { Metadata } from "next";
import { sans, serif, mono } from "@/lib/fonts";
import { SiteNav } from "@/components/site-nav";
import { SiteFooter } from "@/components/site-footer";
import "./globals.css";

const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || "https://dbsearch.ai";
// Matches the H1 (#419). The tab and every shared link carried the old headline
// otherwise. The em dash also went, per house style.
const SITE_TITLE = "DBSearch.AI - Talk to your databases. Ask your company anything.";
const SITE_DESCRIPTION =
  "Permission-faithful enterprise knowledge search that runs inside your own cloud. Self-host free, or managed on Azure.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: SITE_TITLE,
  description: SITE_DESCRIPTION,
  openGraph: {
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    url: SITE_URL,
    siteName: "DBSearch.AI",
    type: "website",
    locale: "en_US",
  },
  twitter: {
    card: "summary_large_image",
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
  },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${sans.variable} ${serif.variable} ${mono.variable}`}
    >
      <body className="min-h-dvh bg-bg font-sans text-fg antialiased">
        <SiteNav />
        {children}
        <SiteFooter />
      </body>
    </html>
  );
}
