import { render, screen, within } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import Home from "@/app/page";

vi.mock("next/navigation", () => ({ usePathname: () => "/" }));

test("leads with the new headline", () => {
  render(<Home />);
  const heading = screen.getByRole("heading", { level: 1 });
  expect(heading).toHaveTextContent("Talk to your databases.");
  expect(heading).toHaveTextContent("Ask your company anything.");
});

test("states the permission trim in plain words", () => {
  render(<Home />);
  // Scoped to the hero: the proof section further down makes the same point in
  // its own words, and this assertion is about the hero specifically.
  const hero = screen.getByTestId("hero");
  expect(within(hero).getByText(/never retrieved/i)).toBeInTheDocument();
});

test("keeps the four trust facts", () => {
  render(<Home />);
  for (const fact of [
    /zero content egress/i,
    /entra-native acls/i,
    /every answer cited/i,
    /azure or self-host/i,
  ]) {
    expect(screen.getByText(fact)).toBeInTheDocument();
  }
});

test("offers both calls to action", () => {
  render(<Home />);
  // Names must be the labels alone: the pill's arrow is decorative, and the
  // nav renders the same two CTAs, so these are scoped to the hero region.
  const hero = screen.getByTestId("hero");
  // #410: the primary CTA lands in the Ask workspace. Connecting data is a
  // second step, reachable from the shell's Connectors nav (which goes to the
  // canvas). Plain anchor, not next/link: /ask is served by the app, not by
  // this static export.
  expect(
    within(hero).getByRole("link", { name: "Self-host free" })
  ).toHaveAttribute("href", "/ask");
  expect(
    within(hero).getByRole("link", { name: "Book a demo" })
  ).toHaveAttribute("href", "/demo");
});
