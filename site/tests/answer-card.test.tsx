import { render, screen } from "@testing-library/react";
import { test, expect } from "vitest";
import { AnswerCard } from "@/components/answer-card";

test("renders the default illustrative answer", () => {
  render(<AnswerCard />);
  expect(screen.getByText(/25 days PTO/)).toBeInTheDocument();
  expect(screen.getByText(/1,721 documents/)).toBeInTheDocument();
});

test("the status dot is decorative and the label carries the meaning", () => {
  const { container } = render(<AnswerCard />);
  expect(screen.getByText(/high confidence/i)).toBeInTheDocument();
  expect(
    container.querySelectorAll("[aria-hidden='true']").length
  ).toBeGreaterThan(0);
});

test("accepts a className so the hero can size it", () => {
  const { container } = render(<AnswerCard className="w-80" />);
  expect(container.firstChild).toHaveClass("w-80");
});

test("overrides the illustrative copy when asked", () => {
  render(
    <AnswerCard
      question="What is Project Falcon's valuation?"
      answer="Falcon is valued at $340M."
      scope="Searched 12 documents"
    />
  );
  expect(screen.getByText(/Falcon is valued/)).toBeInTheDocument();
  expect(screen.queryByText(/25 days PTO/)).not.toBeInTheDocument();
});
