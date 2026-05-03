import "./globals.css";
import type { Metadata } from "next";
import { Fraunces, Space_Grotesk, Space_Mono } from "next/font/google";
import Header from "./Header";

// Phase 11 typography: replaces Inter / JetBrains Mono with a
// celestial trio that ties the constellation imagery into the type.
//
//   Fraunces       → headlines + serif copy. Variable serif with soft
//                    swooping italics; reads as "warm observatory."
//                    Used for hero headlines, the "Sky empty." state,
//                    Polaris's "Awaiting first sighting," etc.
//   Space Grotesk  → body sans. Modern geometric sans designed for
//                    space / planetary use; replaces Inter everywhere
//                    a `font-sans` class hits, which is most of the app.
//   Space Mono     → monospace data. Replaces JetBrains Mono so agent
//                    names + run IDs read like astronomical readouts.
//
// All three are self-hosted via next/font so prod traffic doesn't pay
// the round-trip to fonts.googleapis.com on first paint.

const serif = Fraunces({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-serif",
  // Variable axis defaults — we only really care about a touch of
  // SOFT and a slightly darker weight for hero copy. Italics inherit.
  axes: ["SOFT", "WONK", "opsz"],
});

const sans = Space_Grotesk({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-sans",
});

const mono = Space_Mono({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-mono",
  weight: ["400", "700"],
});

export const metadata: Metadata = {
  title: "Lightsei",
  description: "Drop-in observability and guardrails for AI agents",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      className={`${sans.variable} ${serif.variable} ${mono.variable}`}
    >
      <body className="bg-white text-gray-900 antialiased font-sans">
        <Header />
        {children}
      </body>
    </html>
  );
}
