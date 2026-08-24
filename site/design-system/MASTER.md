> # SUPERSEDED (2026-07-29)
>
> **This file documents the dark "Vault" palette, which no longer exists in any surface.**
> Every token below (`#0F172A` background, `#22C55E` accent, dark-only, Inter headings)
> was replaced by the warm-paper system.
>
> **The canonical reference is [`docs/DESIGN_SYSTEM.md`](../../docs/DESIGN_SYSTEM.md).**
>
> Kept only as a record of what the site used to be. Do not build against it.

---

# Design System Master File

> **LOGIC:** When building a specific page, first check `design-system/pages/[page-name].md`.
> If that file exists, its rules **override** this Master file.
> If not, strictly follow the rules below.

---

**Project:** DBSearch.AI
**Generated:** 2026-07-01 13:38:03
**Category:** Developer Tool / IDE

---

## Global Rules

> **NOTE (reconciled 2026-07-01):** The generator's raw output (Primary/Secondary/Ring
> roles) is superseded below by the **Vault palette from Global Constraints** — those
> values are locked and win over anything the generator produced. Tailwind theme keys
> use these exact names: `bg`, `surface`, `surface-muted`, `fg`, `fg-muted`, `border`,
> `accent`, `accent-hover`, `destructive`.

### Color Palette (Vault — locked, verbatim from Global Constraints)

| Tailwind Key | Hex | CSS Variable | Role |
|---|---|---|---|
| `bg` | `#0F172A` | `--color-bg` | Page background |
| `surface` | `#1E293B` | `--color-surface` | Card / panel surface |
| `surface-muted` | `#272F42` | `--color-surface-muted` | Muted / nested surface |
| `fg` | `#F8FAFC` | `--color-fg` | Primary text (on dark) |
| `fg-muted` | `#94A3B8` | `--color-fg-muted` | Secondary / muted text |
| `border` | `#475569` | `--color-border` | Hairlines, dividers, input borders |
| `accent` | `#22C55E` | `--color-accent` | Primary CTA / brand green |
| `accent-hover` | `#16A34A` | `--color-accent-hover` | Accent hover/active state |
| `destructive` | `#EF4444` | `--color-destructive` | Errors, destructive actions |

**Color Notes:** Code dark + run green. Dark-only for v1 (no light mode).

### Typography

- **Heading Font (sans):** Inter — weights 300–700, `next/font/google`, `display: swap`, CSS var `--font-sans`
- **Body Font (sans):** Inter
- **Monospace:** JetBrains Mono — weights 400,500, `next/font/google`, `display: swap`, CSS var `--font-mono` (code, data, technical labels)
- **Mood:** dark, cinematic, technical, precision, clean, premium, developer, professional, high-end utility
- **Google Fonts:** [Inter](https://fonts.google.com/specimen/Inter) + [JetBrains Mono](https://fonts.google.com/specimen/JetBrains+Mono)
- **Letter spacing (display):** `-0.02em` for large display/heading text

**Fonts are loaded via `next/font/google` in `lib/fonts.ts`, not a CSS `@import`** (avoids render-blocking font requests and gets automatic self-hosting/optimization).

### Spacing Variables

| Token | Value | Usage |
|-------|-------|-------|
| `--space-xs` | `4px` / `0.25rem` | Tight gaps |
| `--space-sm` | `8px` / `0.5rem` | Icon gaps, inline spacing |
| `--space-md` | `16px` / `1rem` | Standard padding |
| `--space-lg` | `24px` / `1.5rem` | Section padding |
| `--space-xl` | `32px` / `2rem` | Large gaps |
| `--space-2xl` | `48px` / `3rem` | Section margins |
| `--space-3xl` | `64px` / `4rem` | Hero padding |

### Shadow Depths

| Level | Value | Usage |
|-------|-------|-------|
| `--shadow-sm` | `0 1px 2px rgba(0,0,0,0.05)` | Subtle lift |
| `--shadow-md` | `0 4px 6px rgba(0,0,0,0.1)` | Cards, buttons |
| `--shadow-lg` | `0 10px 15px rgba(0,0,0,0.1)` | Modals, dropdowns |
| `--shadow-xl` | `0 20px 25px rgba(0,0,0,0.15)` | Hero images, featured cards |

---

## Component Specs

**All component specs use Vault dark tokens (no light-theme values). See Color Palette above for token definitions.**

### Buttons

```css
/* Primary Button — Accent green on dark bg */
.btn-primary {
  background: #22C55E;
  color: #0F172A;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 600;
  transition: all 200ms ease;
  cursor: pointer;
  border: none;
}

.btn-primary:hover {
  background: #16A34A;
  transform: translateY(-1px);
}

/* Secondary Button — Light text on dark, visible border */
.btn-secondary {
  background: transparent;
  color: #F8FAFC;
  border: 2px solid #475569;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 600;
  transition: all 200ms ease;
  cursor: pointer;
}

.btn-secondary:hover {
  border-color: #F8FAFC;
  color: #F8FAFC;
}

/* Ghost Button — Minimal, muted text */
.btn-ghost {
  background: transparent;
  color: #94A3B8;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 600;
  transition: all 200ms ease;
  cursor: pointer;
  border: none;
}

.btn-ghost:hover {
  color: #F8FAFC;
}
```

### Cards

```css
.card {
  background: #1E293B;
  border: 1px solid #475569;
  border-radius: 12px;
  padding: 24px;
  box-shadow: var(--shadow-md);
  transition: all 200ms ease;
}

.card:hover {
  box-shadow: var(--shadow-lg);
  transform: translateY(-2px);
  border-color: #94A3B8;
}
```

### Inputs

```css
.input {
  background: #272F42;
  color: #F8FAFC;
  padding: 12px 16px;
  border: 1px solid #475569;
  border-radius: 8px;
  font-size: 16px;
  transition: border-color 200ms ease, box-shadow 200ms ease;
}

.input::placeholder {
  color: #94A3B8;
}

.input:focus {
  border-color: #22C55E;
  outline: none;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.1);
}
```

### Modals

```css
.modal-overlay {
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
}

.modal {
  background: #1E293B;
  border: 1px solid #475569;
  border-radius: 16px;
  padding: 32px;
  box-shadow: var(--shadow-xl);
  max-width: 500px;
  width: 90%;
}

.modal-header {
  color: #F8FAFC;
  font-weight: 600;
}

.modal-body {
  color: #F8FAFC;
}

.modal-footer {
  border-top: 1px solid #475569;
  margin-top: 24px;
  padding-top: 24px;
}
```

---

## Style Guidelines

**Style:** Dark Mode (OLED)

**Keywords:** Dark theme, low light, high contrast, deep black, midnight blue, eye-friendly, OLED, night mode, power efficient

**Best For:** Night-mode apps, coding platforms, entertainment, eye-strain prevention, OLED devices, low-light

**Key Effects:** Minimal glow (text-shadow: 0 0 10px), dark-to-light transitions, low white emission, high readability, visible focus

### Page Pattern

**Pattern Name:** FAQ/Documentation Landing

- **Conversion Strategy:** Reduce support tickets. Track search analytics. Show related articles. Contact escalation path.
- **CTA Placement:** Search bar prominent + Contact CTA for unresolved questions
- **Section Order:** 1. Hero with search bar, 2. Popular categories, 3. FAQ accordion, 4. Contact/support CTA

---

## Anti-Patterns (Do NOT Use)

- ❌ Light mode default
- ❌ Slow performance

### Additional Forbidden Patterns

- ❌ **Emojis as icons** — Use SVG icons (Heroicons, Lucide, Simple Icons)
- ❌ **Missing cursor:pointer** — All clickable elements must have cursor:pointer
- ❌ **Layout-shifting hovers** — Avoid scale transforms that shift layout
- ❌ **Low contrast text** — Maintain 4.5:1 minimum contrast ratio
- ❌ **Instant state changes** — Always use transitions (150-300ms)
- ❌ **Invisible focus states** — Focus states must be visible for a11y

---

## Pre-Delivery Checklist

Before delivering any UI code, verify:

- [ ] No emojis used as icons (use SVG instead)
- [ ] All icons from consistent icon set (Heroicons/Lucide)
- [ ] `cursor-pointer` on all clickable elements
- [ ] Hover states with smooth transitions (150-300ms)
- [ ] Light mode: text contrast 4.5:1 minimum
- [ ] Focus states visible for keyboard navigation
- [ ] `prefers-reduced-motion` respected
- [ ] Responsive: 375px, 768px, 1024px, 1440px
- [ ] No content hidden behind fixed navbars
- [ ] No horizontal scroll on mobile
