import { render, screen } from "@testing-library/react";
import { test, expect } from "vitest";
import { Button } from "@/components/ui/button";

test("renders a pill with a full radius", () => {
  render(<Button variant="pill">Self-host free</Button>);
  expect(screen.getByRole("button")).toHaveClass("rounded-full");
});

test("the arrow affordance is decorative, not announced", () => {
  render(
    <Button variant="pill" arrow>
      Self-host free
    </Button>
  );
  // The accessible name must be the label alone, with no stray glyph.
  expect(
    screen.getByRole("button", { name: "Self-host free" })
  ).toBeInTheDocument();
});

test("renders the arrow when asChild wraps a link", () => {
  render(
    <Button asChild variant="pill" arrow>
      <a href="/self-host">Self-host free</a>
    </Button>
  );
  const link = screen.getByRole("link", { name: "Self-host free" });
  expect(link).toHaveClass("rounded-full");
  expect(link.textContent).toContain("↗");
});

test("keeps the legacy variants the inner pages still use", () => {
  render(<Button variant="primary">Legacy</Button>);
  expect(screen.getByRole("button")).toHaveClass("bg-accent");
});
