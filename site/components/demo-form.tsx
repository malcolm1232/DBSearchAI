"use client";

import * as React from "react";
import { Loader2, CheckCircle2, AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

/**
 * Submission goes to OUR OWN backend, same origin: POST /demo-request.
 *
 * #962. This used to POST to a Formspree-style endpoint read from
 * `NEXT_PUBLIC_FORM_ENDPOINT`, falling back to a hardcoded placeholder URL
 * when that was unset. It was unset on every build, so the placeholder
 * shipped, and every lead this site ever took POSTed to
 * `https://formspree.io/f/PLACEHOLDER_NOT_A_REAL_ENDPOINT` and 404'd. The
 * visitor got "something went wrong, please try again", tried again, and hit
 * the same 404. Nothing was stored and nobody was told.
 *
 * The env var is gone rather than merely set, because a build-time fallback to
 * a URL that cannot work is the whole defect: it fails silently, it fails
 * identically in every environment, and nothing in CI can tell a configured
 * build from an unconfigured one. A same-origin relative path has no
 * configuration to forget - the site is served by the very app that answers
 * this route (see server/app.py), so there is no third party and no CORS.
 *
 * The server stores the lead before it tries to email anyone, so a mail outage
 * can no longer turn a captured lead into a red error message.
 */
const FORM_ENDPOINT = "/demo-request";

type FieldName = "name" | "email" | "company" | "message";

interface FormValues {
  name: string;
  email: string;
  company: string;
  message: string;
}

const INITIAL_VALUES: FormValues = {
  name: "",
  email: "",
  company: "",
  message: "",
};

type FormErrors = Partial<Record<FieldName, string>>;

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function validateField(name: FieldName, value: string): string | undefined {
  const trimmed = value.trim();

  if (name === "email") {
    if (!trimmed) return "Work email is required.";
    if (!EMAIL_PATTERN.test(trimmed)) {
      return "Enter a valid email address.";
    }
    return undefined;
  }

  if (name === "name") {
    if (!trimmed) return "Name is required.";
    return undefined;
  }

  if (name === "company") {
    if (!trimmed) return "Company is required.";
    return undefined;
  }

  // message is optional
  return undefined;
}

type SubmitStatus = "idle" | "submitting" | "success" | "error";

const FIELD_INPUT_CLASSES =
  "min-h-11 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-fg placeholder:text-fg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";

export function DemoForm() {
  const [values, setValues] = React.useState<FormValues>(INITIAL_VALUES);
  const [errors, setErrors] = React.useState<FormErrors>({});
  const [status, setStatus] = React.useState<SubmitStatus>("idle");
  // Honeypot. Never shown, never labelled for a screen reader, never required -
  // so nothing a human uses can fill it, and a bot that fills every input will.
  // The server answers a tripped honeypot with the same 202 as a real submit.
  const [honeypot, setHoneypot] = React.useState("");

  const nameRef = React.useRef<HTMLInputElement>(null);
  const emailRef = React.useRef<HTMLInputElement>(null);
  const companyRef = React.useRef<HTMLInputElement>(null);

  const fieldRefs: Record<
    Extract<FieldName, "name" | "email" | "company">,
    React.RefObject<HTMLInputElement | null>
  > = {
    name: nameRef,
    email: emailRef,
    company: companyRef,
  };

  function handleChange(name: FieldName, value: string) {
    setValues((prev) => ({ ...prev, [name]: value }));
  }

  function handleBlur(name: FieldName, value: string) {
    const error = validateField(name, value);
    setErrors((prev) => ({ ...prev, [name]: error }));
  }

  function validateAll(): FormErrors {
    const nextErrors: FormErrors = {
      name: validateField("name", values.name),
      email: validateField("email", values.email),
      company: validateField("company", values.company),
    };
    (Object.keys(nextErrors) as FieldName[]).forEach((key) => {
      if (!nextErrors[key]) delete nextErrors[key];
    });
    return nextErrors;
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const nextErrors = validateAll();
    setErrors(nextErrors);

    if (Object.keys(nextErrors).length > 0) {
      const firstInvalid = (["name", "email", "company"] as const).find(
        (key) => nextErrors[key],
      );
      if (firstInvalid) {
        fieldRefs[firstInvalid].current?.focus();
      }
      return;
    }

    setStatus("submitting");

    try {
      const response = await fetch(FORM_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ ...values, website: honeypot }),
      });

      if (response.ok) {
        setStatus("success");
      } else {
        setStatus("error");
      }
    } catch {
      setStatus("error");
    }
  }

  if (status === "success") {
    return (
      <div
        className="flex flex-col items-center gap-3 rounded-lg border border-border bg-surface p-8 text-center"
        data-testid="demo-form-success"
      >
        <CheckCircle2 className="h-8 w-8 text-accent" aria-hidden="true" />
        <p className="text-lg font-semibold text-fg">
          Thanks - we&apos;ll be in touch.
        </p>
        <p className="text-sm text-fg-muted">
          We usually respond within one business day.
        </p>
      </div>
    );
  }

  const isSubmitting = status === "submitting";

  return (
    <form
      className="flex flex-col gap-5"
      noValidate
      onSubmit={handleSubmit}
    >
      <div aria-hidden="true" className="absolute left-[-9999px] h-px w-px overflow-hidden">
        <label htmlFor="demo-website">Leave this field empty</label>
        <input
          id="demo-website"
          name="website"
          type="text"
          tabIndex={-1}
          autoComplete="off"
          value={honeypot}
          onChange={(e) => setHoneypot(e.target.value)}
        />
      </div>

      <div className="flex flex-col gap-2">
        <label htmlFor="demo-name" className="text-sm font-medium text-fg">
          Name <span className="text-destructive">*</span>
        </label>
        <input
          id="demo-name"
          name="name"
          type="text"
          autoComplete="name"
          required
          aria-required="true"
          ref={nameRef}
          value={values.name}
          onChange={(e) => handleChange("name", e.target.value)}
          onBlur={(e) => handleBlur("name", e.target.value)}
          aria-invalid={Boolean(errors.name)}
          aria-describedby={errors.name ? "demo-name-error" : undefined}
          className={cn(
            FIELD_INPUT_CLASSES,
            errors.name && "border-destructive",
          )}
        />
        {errors.name && (
          <p
            id="demo-name-error"
            role="alert"
            className="text-sm text-destructive"
          >
            {errors.name}
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <label htmlFor="demo-email" className="text-sm font-medium text-fg">
          Work email <span className="text-destructive">*</span>
        </label>
        <input
          id="demo-email"
          name="email"
          type="email"
          autoComplete="email"
          required
          aria-required="true"
          ref={emailRef}
          value={values.email}
          onChange={(e) => handleChange("email", e.target.value)}
          onBlur={(e) => handleBlur("email", e.target.value)}
          aria-invalid={Boolean(errors.email)}
          aria-describedby={errors.email ? "demo-email-error" : undefined}
          className={cn(
            FIELD_INPUT_CLASSES,
            errors.email && "border-destructive",
          )}
        />
        {errors.email && (
          <p
            id="demo-email-error"
            role="alert"
            className="text-sm text-destructive"
          >
            {errors.email}
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <label htmlFor="demo-company" className="text-sm font-medium text-fg">
          Company <span className="text-destructive">*</span>
        </label>
        <input
          id="demo-company"
          name="company"
          type="text"
          autoComplete="organization"
          required
          aria-required="true"
          ref={companyRef}
          value={values.company}
          onChange={(e) => handleChange("company", e.target.value)}
          onBlur={(e) => handleBlur("company", e.target.value)}
          aria-invalid={Boolean(errors.company)}
          aria-describedby={errors.company ? "demo-company-error" : undefined}
          className={cn(
            FIELD_INPUT_CLASSES,
            errors.company && "border-destructive",
          )}
        />
        {errors.company && (
          <p
            id="demo-company-error"
            role="alert"
            className="text-sm text-destructive"
          >
            {errors.company}
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <label htmlFor="demo-message" className="text-sm font-medium text-fg">
          What are you hoping to search?
        </label>
        <textarea
          id="demo-message"
          name="message"
          rows={4}
          autoComplete="off"
          value={values.message}
          onChange={(e) => handleChange("message", e.target.value)}
          className={cn(FIELD_INPUT_CLASSES, "min-h-24 resize-y")}
        />
      </div>

      {status === "error" && (
        <p role="alert" className="text-sm text-destructive">
          Something went wrong sending your request. Please try again.
        </p>
      )}

      <div className="flex items-center gap-3">
        <Button
          type="submit"
          variant="pill"
          disabled={isSubmitting}
          className="min-h-11"
        >
          {isSubmitting && (
            <Loader2
              className="h-4 w-4 animate-spin"
              aria-hidden="true"
            />
          )}
          {isSubmitting ? "Sending..." : "Request a demo"}
        </Button>
        {isSubmitting && (
          <span role="status" aria-label="Submitting" className="text-sm text-fg-muted">
            <span aria-hidden="true">Sending your request...</span>
          </span>
        )}
        {status === "error" && (
          <span className="flex items-center gap-1 text-sm text-fg-muted">
            <AlertCircle className="h-4 w-4 text-destructive" aria-hidden="true" />
            You can retry above.
          </span>
        )}
      </div>
    </form>
  );
}
