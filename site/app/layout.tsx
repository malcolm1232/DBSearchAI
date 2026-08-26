import type { Metadata, Viewport } from "next";
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
  // #961. Declared by hand rather than via Next's app/favicon.ico file convention: that
  // convention reads only the largest frame out of the .ico and emits sizes="256x256",
  // which advertises the 16px tab icon as a 256px one and discards the optically-sized
  // small frames scripts/make_brand_icons.py renders. `sizes: "any"` is what a
  // multi-resolution .ico should say. The files live in public/ (copied verbatim) and
  // are also served at these paths by server/app.py, which is what gets them to the app
  // shell and to a self-hoster with no site/out on disk.
  manifest: "/site.webmanifest",
  icons: {
    icon: [{ url: "/favicon.ico?v=cf883b396d", sizes: "any", type: "image/x-icon" }],
    apple: "/apple-touch-icon.png?v=cf883b396d",
  },
};

// Tints the phone's address bar to the page it is sitting on. Split by scheme because
// the site itself is - paper in light, near-black in dark - and a single value leaves
// a bright bar clamped above a dark page in one of the two.
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#FAF9F7" },
    { media: "(prefers-color-scheme: dark)", color: "#141416" },
  ],
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
