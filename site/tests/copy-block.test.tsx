import { render, screen, fireEvent } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { CopyBlock } from "@/components/copy-block";

test("copies its code to the clipboard", () => {
  const writeText = vi.fn();
  Object.assign(navigator, { clipboard: { writeText } });
  render(<CopyBlock code="docker compose up -d --build" />);
  fireEvent.click(screen.getByRole("button", { name: /copy/i }));
  expect(writeText).toHaveBeenCalledWith("docker compose up -d --build");
});
