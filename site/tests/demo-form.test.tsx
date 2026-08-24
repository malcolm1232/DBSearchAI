import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { test, expect, vi, afterEach } from "vitest";
import { DemoForm } from "@/components/demo-form";

afterEach(() => {
  vi.unstubAllGlobals();
});

function fillValidFields() {
  fireEvent.change(screen.getByLabelText(/name/i), {
    target: { value: "Ada Lovelace" },
  });
  fireEvent.change(screen.getByLabelText(/work email/i), {
    target: { value: "ada@example.com" },
  });
  fireEvent.change(screen.getByLabelText(/company/i), {
    target: { value: "Analytical Engines Inc" },
  });
}

test("shows an inline error below an invalid email on blur", async () => {
  render(<DemoForm />);

  const emailInput = screen.getByLabelText(/work email/i);
  fireEvent.change(emailInput, { target: { value: "nope" } });
  fireEvent.blur(emailInput);

  const error = await screen.findByRole("alert");
  expect(error).toHaveTextContent(/valid email/i);
});

test("disables submit and shows a spinner while sending", async () => {
  let resolveFetch: (value: unknown) => void = () => {};
  const fetchPromise = new Promise((resolve) => {
    resolveFetch = resolve;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(() => fetchPromise),
  );

  render(<DemoForm />);
  fillValidFields();

  const submitButton = screen.getByRole("button", { name: /request a demo/i });
  fireEvent.click(submitButton);

  await waitFor(() => expect(submitButton).toBeDisabled());
  expect(
    screen.getByRole("status", { name: /submitting/i }),
  ).toBeInTheDocument();

  resolveFetch({ ok: true, status: 200 });
  await waitFor(() => expect(submitButton).not.toBeInTheDocument());
});

test("shows a success state after a successful submit", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, status: 200 }),
  );

  render(<DemoForm />);
  fillValidFields();

  fireEvent.click(screen.getByRole("button", { name: /request a demo/i }));

  expect(
    await screen.findByText(/thanks.*we.ll be in touch/i),
  ).toBeInTheDocument();
});
