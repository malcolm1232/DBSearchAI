import { render, screen } from "@testing-library/react";
import { test, expect } from "vitest";
import { PermissionProof } from "@/components/sections/permission-proof";

test("shows the same question asked by two identities", () => {
  render(<PermissionProof />);
  expect(screen.getByText("Alice")).toBeInTheDocument();
  expect(screen.getByText("Bob")).toBeInTheDocument();
});

test("asks one question, not two", () => {
  render(<PermissionProof />);
  expect(screen.getByText(/Project Falcon's valuation/)).toBeInTheDocument();
});

test("Bob is refused rather than given a redacted answer", () => {
  render(<PermissionProof />);
  expect(
    screen.getByText(/Nothing you have access to about that/i)
  ).toBeInTheDocument();
});

test("links out to the interactive demo rather than duplicating it", () => {
  render(<PermissionProof />);
  expect(screen.getByRole("link", { name: /try it/i })).toHaveAttribute(
    "href",
    "/product"
  );
});
