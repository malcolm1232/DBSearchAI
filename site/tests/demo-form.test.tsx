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

/**
 * #962. These three tests all passed while the form POSTed every lead this site ever
 * took to `https://formspree.io/f/PLACEHOLDER_NOT_A_REAL_ENDPOINT`, because they mock
 * `fetch` and never look at where it was called. The component's own docstring said so
 * out loud - "in tests, fetch is mocked, so the exact target URL never matters" - which
 * is true of the mock and catastrophically false of the product.
 *
 * So: assert the TARGET, not just the reaction to the response.
 */
test("posts the lead to our own same-origin endpoint, not a third party", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 202 });
  vi.stubGlobal("fetch", fetchMock);

  render(<DemoForm />);
  fillValidFields();
  fireEvent.click(screen.getByRole("button", { name: /request a demo/i }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalled());

  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/demo-request");
  // Belt and braces: a relative path cannot be a third party, but an absolute URL
  // sneaking back in via an env var would still be caught here.
  expect(String(url)).not.toMatch(/^https?:\/\//);
  expect(String(url)).not.toMatch(/formspree|PLACEHOLDER/i);
  expect(init.method).toBe("POST");

  const sent = JSON.parse(init.body);
  expect(sent.email).toBe("ada@example.com");
  // The honeypot rides along empty for a real human.
  expect(sent.website).toBe("");
});

test("a submit whose honeypot is filled still carries it, so the server can drop it", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 202 });
  vi.stubGlobal("fetch", fetchMock);

  render(<DemoForm />);
  fillValidFields();
  fireEvent.change(screen.getByLabelText(/leave this field empty/i), {
    target: { value: "http://spam.example" },
  });
  fireEvent.click(screen.getByRole("button", { name: /request a demo/i }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  const sent = JSON.parse(fetchMock.mock.calls[0][1].body);
  expect(sent.website).toBe("http://spam.example");
});
