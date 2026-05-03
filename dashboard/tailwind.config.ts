import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        // Phase 11 typography: Fraunces is the editorial serif used for
        // hero copy + empty-state headlines. System fallback chain
        // keeps anything rendering before the font loads from looking
        // like Times New Roman in the wrong way.
        serif: ["var(--font-serif)", "ui-serif", "Georgia", "serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        // accent palette = Tailwind indigo, aliased so swapping the brand
        // color later is one place
        accent: {
          50: "#eef2ff",
          100: "#e0e7ff",
          200: "#c7d2fe",
          400: "#818cf8",
          500: "#6366f1",
          600: "#4f46e5",
          700: "#4338ca",
          800: "#3730a3",
        },
      },
    },
  },
  plugins: [],
};

export default config;
