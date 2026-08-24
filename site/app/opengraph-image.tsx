import { ImageResponse } from "next/og";

export const alt = "DBSearch.AI - Talk to your databases. Ask your company anything.";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";
// Required by output: "export" - the card is generated once at build time
// rather than per-request.
export const dynamic = "force-static";

/*
 * Satori renders this image outside the Tailwind pipeline, so it cannot read
 * the design tokens. These four constants mirror app/globals.css by hand and
 * are the only literal hexes permitted outside that file. If the palette
 * changes there, change it here too.
 */
const BG = "#FAF9F7";
const FG = "#16161A";
const MUTED = "#6B6B73";
const ACCENT = "#15803D";
const RULE = "#E4E2DD";

export default async function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: BG,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 4,
            fontSize: 84,
            fontWeight: 700,
            letterSpacing: "-0.02em",
            color: FG,
          }}
        >
          <span>DBSearch</span>
          <span style={{ color: ACCENT }}>.AI</span>
        </div>

        <div
          style={{
            display: "flex",
            marginTop: 28,
            fontSize: 32,
            color: MUTED,
            textAlign: "center",
            maxWidth: 900,
          }}
        >
          Talk to your databases. Ask your company anything.
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            marginTop: 48,
            padding: "10px 20px",
            borderRadius: 9999,
            border: `1px solid ${RULE}`,
            color: MUTED,
            fontSize: 22,
            fontFamily: "monospace",
          }}
        >
          <span style={{ display: "flex", color: ACCENT }}>●</span>
          <span style={{ display: "flex" }}>
            self-host free or managed on Azure
          </span>
        </div>
      </div>
    ),
    { ...size }
  );
}
