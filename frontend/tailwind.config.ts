import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/features/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: "#1A1A2E",
          50: "#f0f0f7",
          100: "#d9d9ed",
          200: "#b3b3db",
          300: "#8080c0",
          400: "#5a5a9e",
          500: "#1A1A2E",
          600: "#151526",
          700: "#10101c",
          800: "#0a0a13",
          900: "#050509",
        },
        accent: {
          DEFAULT: "#E63946",
          50: "#fef2f3",
          100: "#fde2e4",
          200: "#fcc9cd",
          300: "#f9a3a9",
          400: "#f47079",
          500: "#E63946",
          600: "#d42433",
          700: "#b21a28",
          800: "#941926",
          900: "#7c1a25",
        },
        mid: {
          DEFAULT: "#4A4E69",
          50: "#f3f3f7",
          100: "#e4e5ef",
          200: "#cccde1",
          300: "#adb0cc",
          400: "#8b8fb5",
          500: "#4A4E69",
          600: "#3f4359",
          700: "#34374b",
          800: "#2b2d3d",
          900: "#212332",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};

export default config;
