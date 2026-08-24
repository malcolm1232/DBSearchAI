# DBSearch.AI marketing site — "Vault" design

Next.js (App Router) marketing site for DBSearch.AI, built to the **Vault** dark
design system.

## Stack

- Next.js 16 (App Router), TypeScript, ESLint
- Tailwind CSS v4 (`@theme inline` tokens in `app/globals.css` — no
  `tailwind.config.ts` needed under v4)
- Fonts via `next/font/google`: Inter (sans, 300–700) + JetBrains Mono (mono, 400/500)

## Design tokens (Vault palette — locked)

| Tailwind class | Hex |
|---|---|
| `bg-bg` | `#0F172A` |
| `bg-surface` | `#1E293B` |
| `bg-surface-muted` | `#272F42` |
| `text-fg` | `#F8FAFC` |
| `text-fg-muted` | `#94A3B8` |
| `border-border` | `#475569` |
| `bg-accent` | `#22C55E` |
| `bg-accent-hover` | `#16A34A` |
| `bg-destructive` | `#EF4444` |

Dark-only for v1 (no light mode). See `design-system/MASTER.md` for the full
system (typography, spacing, component specs, anti-patterns).

## Getting started

```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## Build

```bash
npm run build
```
