import type { Metadata } from "next";
import "@/styles/globals.css";
import { Providers } from "./providers";
import { ToastContainer } from "@/components/ui/Toast";

export const metadata: Metadata = {
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
    <html lang="es">
      <body>
        <Providers>{children}</Providers>
        <ToastContainer />
      </body>
    </html>
  );
}
