import type { Metadata } from "next";
import { Barlow_Condensed, Inter } from "next/font/google";
import "@/styles/globals.css";
import { Providers } from "./providers";
import { ToastContainer } from "@/components/ui/Toast";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  weight: ["300", "400", "500", "600"],
  display: "swap",
});

const barlowCondensed = Barlow_Condensed({
  subsets: ["latin"],
  variable: "--font-barlow",
  weight: ["500", "600", "700"],
  display: "swap",
});

const appUrl =
  process.env.NEXT_PUBLIC_APP_URL ??
  (process.env.VERCEL_PROJECT_PRODUCTION_URL
    ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}`
    : process.env.VERCEL_URL
      ? `https://${process.env.VERCEL_URL}`
      : "http://localhost:3000");

export const metadata: Metadata = {
  metadataBase: new URL(appUrl),
  title: "Véktor — Salud financiera para PYMEs argentinas",
  description:
    "Controlá caja, margen y stock en tiempo real. Sin contabilidad, sin hojas de cálculo.",
  openGraph: {
    title: "Véktor — Salud financiera para PYMEs argentinas",
    description:
      "Controlá caja, margen y stock en tiempo real. Sin contabilidad, sin hojas de cálculo.",
    siteName: "Véktor",
    locale: "es_AR",
    type: "website",
    images: [
      {
        url: "/og-image.png",
        width: 1200,
        height: 630,
        alt: "Véktor — Salud financiera para PYMEs",
      },
    ],
  },
  icons: {
    icon: "/favicon.ico",
  },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="es" className={`${inter.variable} ${barlowCondensed.variable}`}>
      <body>
        <Providers>{children}</Providers>
        <ToastContainer />
      </body>
    </html>
  );
}
