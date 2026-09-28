import type { NextConfig } from "next";
import { withSentryConfig } from "@sentry/nextjs";

const nextConfig: NextConfig = {
  output: "standalone",
  experimental: {},
  images: {
    remotePatterns: [
      {
        protocol: "https",
        hostname: "picsum.photos",
      },
    ],
  },
  env: {
    // Vercel corre preview Y producción con NODE_ENV=production — VERCEL_ENV
    // ("production" | "preview" | "development") es lo que realmente
    // distingue un error de un PR de prueba de uno de un cliente real.
    NEXT_PUBLIC_SENTRY_ENVIRONMENT: process.env.VERCEL_ENV ?? "development",
  },
  async headers() {
    return [
      {
        // El service worker de la caja (B7b) NO se puede cachear: si el
        // navegador sirviera una copia vieja de `sw.js`, una versión con un
        // bug quedaría viva en esa PC hasta que alguien borre el sitio a mano.
        // `Service-Worker-Allowed` porque su alcance es `/`, no `/pos`.
        source: "/sw.js",
        headers: [
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Service-Worker-Allowed", value: "/" },
        ],
      },
    ];
  },
};

export default withSentryConfig(nextConfig, {
  org: process.env.SENTRY_ORG,
  project: process.env.SENTRY_PROJECT,
  authToken: process.env.SENTRY_AUTH_TOKEN,
  // Asocia los source maps subidos al commit real — habilita "suspect
  // commits" junto con la integración de GitHub configurada en el
  // dashboard de Sentry (ver plan, A0.3).
  release: { name: process.env.VERCEL_GIT_COMMIT_SHA },
  silent: !process.env.CI,
  widenClientFileUpload: true,
});
