import type { MetadataRoute } from "next";

// Required by output: "export" - emitted once at build time.
export const dynamic = "force-static";

const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || "https://dbsearch.ai";

const ROUTES = [
  "",
  "/product",
  "/security",
  "/architecture",
  "/architecture/query",
  "/self-host",
  "/pricing",
  "/demo",
  "/privacy",
  "/terms",
] as const;

export default function sitemap(): MetadataRoute.Sitemap {
  return ROUTES.map((route) => ({
    url: `${SITE_URL}${route}`,
    lastModified: new Date(),
    changeFrequency: "weekly",
    priority: route === "" ? 1 : 0.8,
  }));
}
