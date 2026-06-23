import type { Config } from "tailwindcss";

// Tailwind color entries using rgb() with <alpha-value> for opacity modifier support
// e.g. bg-vk-blue/20, text-vk-text-primary/80, etc.
const vkColors = {
  "vektor-night":       "rgb(5 9 19 / <alpha-value>)",
  "vektor-ink":         "rgb(13 22 39 / <alpha-value>)",
  "vektor-surface":     "rgb(22 34 54 / <alpha-value>)",
  "vektor-border":      "rgb(36 50 70 / <alpha-value>)",
  "vektor-white":       "rgb(247 251 255 / <alpha-value>)",
  "vektor-body":        "rgb(214 226 240 / <alpha-value>)",
  "vektor-muted":       "rgb(144 162 188 / <alpha-value>)",
  "vektor-blue":        "rgb(58 134 255 / <alpha-value>)",
  "vektor-blue-strong": "rgb(37 99 235 / <alpha-value>)",
  "vektor-teal":        "rgb(39 199 184 / <alpha-value>)",
  "vektor-amber":       "rgb(241 182 72 / <alpha-value>)",
  "vektor-red":         "rgb(240 108 121 / <alpha-value>)",
  "vektor-red-deep":    "rgb(61 17 22 / <alpha-value>)",
  "vk-navy":            "rgb(26 39 68 / <alpha-value>)",
  "vk-blue":            "rgb(43 127 212 / <alpha-value>)",
  "vk-blue-light":      "rgb(91 163 232 / <alpha-value>)",
  "vk-blue-hover":      "rgb(30 107 184 / <alpha-value>)",
  // Dark surfaces
  "vk-bg-dark":         "rgb(15 22 35 / <alpha-value>)",
  "vk-surface-1":       "rgb(26 36 56 / <alpha-value>)",
  "vk-surface-2":       "rgb(36 48 80 / <alpha-value>)",
  "vk-border-dark":     "rgb(45 58 79 / <alpha-value>)",
  // Light surfaces
  "vk-bg-light":        "rgb(247 248 250 / <alpha-value>)",
  "vk-surface-w":       "rgb(255 255 255 / <alpha-value>)",
  "vk-border-w":        "rgb(229 233 240 / <alpha-value>)",
  "vk-border-w-hover":  "rgb(203 213 225 / <alpha-value>)",
  // Typography
  "vk-text-primary":    "rgb(15 22 35 / <alpha-value>)",
  "vk-text-secondary":  "rgb(74 85 104 / <alpha-value>)",
  "vk-text-muted":      "rgb(100 116 139 / <alpha-value>)",
  "vk-text-light":      "rgb(232 237 244 / <alpha-value>)",
  "vk-text-placeholder":"rgb(160 174 192 / <alpha-value>)",
  // Semantic
  "vk-success":         "rgb(22 163 74 / <alpha-value>)",
  "vk-success-bg":      "rgb(240 253 244 / <alpha-value>)",
  "vk-warning":         "rgb(217 119 6 / <alpha-value>)",
  "vk-warning-bg":      "rgb(255 251 235 / <alpha-value>)",
  "vk-danger":          "rgb(220 38 38 / <alpha-value>)",
  "vk-danger-bg":       "rgb(254 242 242 / <alpha-value>)",
  "vk-info":            "rgb(43 127 212 / <alpha-value>)",
  "vk-info-bg":         "rgb(235 245 251 / <alpha-value>)",
};

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/features/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: vkColors,
      fontFamily: {
        sans: ["var(--font-inter)", "system-ui", "-apple-system", "sans-serif"],
        display: ["var(--font-barlow)", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Fira Code", "monospace"],
      },
      borderRadius: {
        sm: "6px",
        md: "10px",
        lg: "14px",
        xl: "1rem",
        "2xl": "1.25rem",
      },
      boxShadow: {
        "vk-sm": "0 1px 3px rgba(0,0,0,0.08)",
        "vk-md": "0 4px 12px rgba(0,0,0,0.10)",
        "vk-lg": "0 8px 24px rgba(0,0,0,0.12)",
        glow: "0 18px 45px rgba(58, 134, 255, 0.18)",
      },
      keyframes: {
        marquee: {
          "0%": { transform: "translateX(0)" },
          "100%": { transform: "translateX(-50%)" },
        },
        mesh: {
          "0%": { transform: "translate3d(0,0,0) scale(1)", opacity: "0.75" },
          "100%": { transform: "translate3d(2%,-2%,0) scale(1.04)", opacity: "1" },
        },
        "fade-slide-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        marquee: "marquee 28s linear infinite",
        mesh: "mesh 18s ease-in-out infinite alternate",
        "fade-slide-up": "fade-slide-up 150ms ease-out",
      },
    },
  },
  plugins: [],
};

export default config;
