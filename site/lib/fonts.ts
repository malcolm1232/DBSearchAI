import { Inter, Instrument_Serif, JetBrains_Mono } from "next/font/google";

export const sans = Inter({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
  variable: "--font-sans",
  display: "swap",
});

/**
 * Display face, used for h1/h2 only. It ships a single weight, so hierarchy
 * comes from size rather than from weight.
 */
export const serif = Instrument_Serif({
  subsets: ["latin"],
  weight: ["400"],
  style: ["normal", "italic"],
  variable: "--font-serif",
  display: "swap",
});

export const mono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono",
  display: "swap",
});
