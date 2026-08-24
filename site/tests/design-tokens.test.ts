import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, test, expect } from "vitest";
import { contrastRatio } from "@/lib/contrast";

const CSS = readFileSync(resolve(process.cwd(), "app/globals.css"), "utf8");

/** Pull a `--color-x: #hex;` declaration out of globals.css. */
function token(name: string): string {
  const match = CSS.match(new RegExp(`--color-${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!match) throw new Error(`token --color-${name} not found in globals.css`);
  return match[1].toLowerCase();
}

describe("design tokens", () => {
  test("palette matches the spec exactly", () => {
    expect(token("bg")).toBe("#faf9f7");
    expect(token("surface")).toBe("#ffffff");
    expect(token("surface-muted")).toBe("#f2f0ec");
    expect(token("fg")).toBe("#16161a");
    expect(token("fg-muted")).toBe("#6b6b73");
    expect(token("border")).toBe("#e4e2dd");
    expect(token("accent")).toBe("#15803d");
    expect(token("accent-hover")).toBe("#166534");
    expect(token("accent-bright")).toBe("#22c55e");
    expect(token("on-ink-muted")).toBe("#9a9aa2");
    expect(token("destructive")).toBe("#b91c1c");
  });

  test.each([
    ["fg", 17],
    ["fg-muted", 4.5],
    ["accent", 4.5],
    ["destructive", 4.5],
  ])("%s clears %s:1 on paper", (name, min) => {
    expect(contrastRatio(token(name as string), token("bg"))).toBeGreaterThanOrEqual(
      min as number
    );
  });

  test("text on the inverted band clears AA", () => {
    // The ink band paints `fg` as its background and `bg` as its text.
    expect(contrastRatio(token("fg"), token("bg"))).toBeGreaterThanOrEqual(4.5);
    expect(
      contrastRatio(token("fg"), token("on-ink-muted"))
    ).toBeGreaterThanOrEqual(4.5);
  });

  test("light-mode color-scheme is declared", () => {
    expect(CSS).toMatch(/color-scheme:\s*light/);
  });
});
