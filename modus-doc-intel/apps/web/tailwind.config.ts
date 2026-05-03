import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#f0f4ff",
          100: "#dce4fd",
          500: "#4f6ef7",
          600: "#3b55e6",
          700: "#2c42c7",
          900: "#1a2870",
        },
      },
    },
  },
  plugins: [],
};

export default config;
