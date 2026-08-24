import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static export: the marketing site is served as plain files by the same
  // FastAPI box that runs the product, so no third party sits between a
  // visitor and dbsearch.ai.
  output: "export",
  // Emit product/index.html rather than product.html, so the FastAPI static
  // mount can resolve a clean URL to a directory index.
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
