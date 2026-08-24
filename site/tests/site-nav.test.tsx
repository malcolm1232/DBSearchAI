import { render, screen } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { SiteNav } from "@/components/site-nav";

vi.mock("next/navigation", () => ({ usePathname: () => "/security" }));

test("marks the current route active", () => {
  render(<SiteNav />);
  expect(screen.getByRole("link", { name: "Security" })).toHaveAttribute(
    "aria-current",
    "page"
  );
});

test("shows the primary self-host CTA", () => {
  render(<SiteNav />);
  expect(
    screen.getByRole("link", { name: /self-host free/i })
  ).toBeInTheDocument();
});

test("Open app links same-origin, never at the visitor's localhost", () => {
  render(<SiteNav />);
  const link = screen.getByRole("link", { name: /open app/i });
  // #401: the site is statically exported and served by the same box as the app,
  // so this must be a plain path. The old default was http://127.0.0.1:8090, which
  // is harmless in `npm run dev` and gets BAKED INTO THE HTML by a static export -
  // shipping production links that point at the visitor's own machine. It did.
  expect(link).toHaveAttribute("href", "/app");
  expect(link.getAttribute("href")).not.toMatch(/^https?:\/\/(127\.0\.0\.1|localhost)/);
});
