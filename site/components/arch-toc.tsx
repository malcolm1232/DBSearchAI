"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

export interface ArchTocChild {
  id: string;
  label: string;
}

export interface ArchTocItem {
  id: string;
  label: string;
  children?: readonly ArchTocChild[];
}

/* The law rows are static; only the subsection list is a wheel. Fixed child
   row height keeps the glide math exact: the child list translates so the
   active subsection's center sits at the center of a small masked viewport. */
const CHILD_ROW_H = 22;
const CHILD_VIEW_H = CHILD_ROW_H * 3;

function childFontSize(distance: number): number {
  if (distance === 0) return 10;
  if (distance === 1) return 8.5;
  return 8;
}

function childOpacity(distance: number): number {
  if (distance === 0) return 1;
  if (distance === 1) return 0.6;
  return 0.25;
}

function wheelOffset(count: number, activeIdx: number): number {
  const listH = count * CHILD_ROW_H;
  const viewH = Math.min(CHILD_VIEW_H, listH);
  const centered = viewH / 2 - activeIdx * CHILD_ROW_H - CHILD_ROW_H / 2;
  const min = Math.min(0, viewH - listH);
  return Math.max(min, Math.min(0, centered));
}

function wheelMask(count: number, activeIdx: number) {
  const listH = count * CHILD_ROW_H;
  const viewH = Math.min(CHILD_VIEW_H, listH);
  const offset = wheelOffset(count, activeIdx);
  const clipsTop = offset < 0;
  const clipsBottom = listH + offset > viewH;
  const mask = `linear-gradient(to bottom, ${clipsTop ? "transparent" : "black"}, black 30%, black 70%, ${clipsBottom ? "transparent" : "black"})`;
  return { maskImage: mask, WebkitMaskImage: mask };
}

/**
 * Page-local scroll rail for /architecture. Every law is always visible;
 * the law currently in view expands a small picker-wheel viewport of its
 * subsections (active subsection centered at full size, neighbors tapered,
 * the list gliding as the page scrolls). Crossing into another law collapses
 * the previous wheel and opens that law's.
 *
 * At >=1400px the rail sits in the free margin beside the content column.
 * Below that there is no clear air, so it collapses to a slim edge strip of
 * law numbers (current one marked) that expands into a floating panel on
 * tap - and collapses again on navigation or an outside tap, so it never
 * squats on the reading column.
 */
export function ArchToc({ items }: { items: readonly ArchTocItem[] }) {
  const [active, setActive] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const navRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const ids = Array.from(
      new Set(
        items.flatMap((item) =>
          item.children ? item.children.map((c) => c.id) : [item.id]
        )
      )
    );
    const targets = ids
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null);
    if (targets.length === 0) return;

    // Classic scrollspy: the active target is the LAST one whose top has
    // crossed a line near the top of the viewport. Laws 3-8 are sibling
    // cards in a grid, so a whole row shares one top - default to the row's
    // first card (reading order), but honor a clicked hash within the row.
    let raf = 0;
    const pick = () => {
      raf = 0;
      const line = window.innerHeight * 0.3;
      const crossed = targets.filter(
        (target) => target.getBoundingClientRect().top <= line
      );
      if (crossed.length === 0) {
        setActive(targets[0].id);
        return;
      }
      const rowTop = crossed[crossed.length - 1].getBoundingClientRect().top;
      const row = crossed.filter(
        (target) =>
          Math.abs(target.getBoundingClientRect().top - rowTop) < 8
      );
      const hashId = window.location.hash.slice(1);
      const chosen = row.find((target) => target.id === hashId) ?? row[0];
      setActive(chosen.id);
    };
    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(pick);
    };
    pick();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll, { passive: true });
    window.addEventListener("hashchange", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      window.removeEventListener("hashchange", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [items]);

  // Tapping outside the expanded floating panel dismisses it.
  useEffect(() => {
    if (!expanded) return;
    const onDown = (e: PointerEvent) => {
      if (navRef.current && !navRef.current.contains(e.target as Node)) {
        setExpanded(false);
      }
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [expanded]);

  const activeGroupIdx = items.findIndex((item) =>
    item.children
      ? item.children.some((c) => c.id === active)
      : active === item.id
  );

  return (
    <nav
      ref={navRef}
      aria-label="On this page"
      className="fixed right-0 top-[calc(50%-160px)] z-40 min-[1400px]:right-6 min-[1400px]:w-44"
    >
      {/* Collapsed edge strip - below 1400px only */}
      {!expanded && (
        <button
          type="button"
          aria-label="Open page navigation"
          aria-expanded={false}
          onClick={() => setExpanded(true)}
          className="flex cursor-pointer flex-col gap-1.5 rounded-l-lg border border-r-0 border-border bg-bg/90 py-2.5 pl-2 pr-2.5 backdrop-blur-sm transition-colors hover:border-fg-muted min-[1400px]:hidden"
        >
          {items.map((item, i) => (
            <span
              key={item.label}
              className={cn(
                "border-l-2 pl-1.5 font-mono text-[9px] leading-none",
                i === activeGroupIdx
                  ? "border-fg text-fg"
                  : "border-border text-fg-muted/60"
              )}
            >
              {item.label.replace("Law ", "")}
            </span>
          ))}
        </button>
      )}

      {/* The full rail: always shown >=1400px; a floating panel when
          expanded below that */}
      <div
        className={cn(
          expanded ? "block" : "hidden",
          "rounded-l-lg border border-r-0 border-border bg-bg/95 py-3 pl-2 pr-3 shadow-md backdrop-blur-sm min-[1400px]:block min-[1400px]:rounded-none min-[1400px]:border-0 min-[1400px]:bg-transparent min-[1400px]:p-0 min-[1400px]:shadow-none min-[1400px]:backdrop-blur-none"
        )}
      >
        <ul className="flex flex-col gap-0.5">
          {items.map((item) => {
            const groupActive = item.children
              ? item.children.some((c) => c.id === active)
              : active === item.id;
            const childIdx = item.children
              ? Math.max(
                  0,
                  item.children.findIndex((c) => c.id === active)
                )
              : 0;
            return (
              <li key={item.label}>
                <a
                  href={`#${item.id}`}
                  onClick={() => setExpanded(false)}
                  className={cn(
                    "flex items-center gap-1 border-l-2 py-1 pl-3 font-mono text-[11px] uppercase tracking-widest transition-colors",
                    groupActive
                      ? "border-fg text-fg"
                      : "border-border text-fg-muted hover:text-fg"
                  )}
                >
                  {item.label}
                  {item.children && (
                    <ChevronRight
                      className={cn(
                        "h-3 w-3 shrink-0 transition-transform duration-200",
                        groupActive && "rotate-90"
                      )}
                      aria-hidden="true"
                    />
                  )}
                </a>
                {item.children && (
                  /* The subsection wheel: a small masked viewport; the list
                     glides toward keeping the active subsection centered, but
                     CLAMPED so no blank space ever shows at either end.
                     Edges only fade when they actually clip content. */
                  <div
                    className="overflow-hidden transition-[max-height] duration-300"
                    style={{
                      maxHeight: groupActive
                        ? Math.min(
                            CHILD_VIEW_H,
                            item.children.length * CHILD_ROW_H
                          )
                        : 0,
                      ...wheelMask(item.children.length, childIdx),
                    }}
                  >
                    {/* ml-3: the subsection spine sits indented from the law
                        spine, so the hierarchy reads at a glance */}
                    <ul
                      className="ml-3 flex flex-col transition-transform duration-300 ease-out motion-reduce:transition-none"
                      style={{
                        transform: `translateY(${wheelOffset(item.children.length, childIdx)}px)`,
                      }}
                    >
                      {item.children.map((child, i) => {
                        const d = Math.abs(i - childIdx);
                        return (
                          <li
                            key={child.label + child.id}
                            style={{ height: CHILD_ROW_H }}
                          >
                            <a
                              href={`#${child.id}`}
                              tabIndex={groupActive ? undefined : -1}
                              onClick={() => setExpanded(false)}
                              className={cn(
                                // the subsection tick is fg-muted grey, one
                                // step lighter than the law's black tick, so
                                // the spine itself shows the hierarchy
                                "flex h-full items-center border-l-2 pl-3 font-mono tracking-wider transition-[font-size,opacity,color,border-color] duration-300 motion-reduce:transition-none",
                                active === child.id && groupActive
                                  ? "border-fg-muted text-fg"
                                  : "border-border/70 text-fg-muted/70 hover:text-fg"
                              )}
                              style={{
                                fontSize: childFontSize(d),
                                opacity: childOpacity(d),
                              }}
                            >
                              {child.label}
                            </a>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </nav>
  );
}
