import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PROTECTED_PATHS = [
  "/dashboard",
  "/onboarding",
  "/products",
  "/sales",
  "/expenses",
  "/settings",
  "/ingestion",
];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const isProtected = PROTECTED_PATHS.some((p) => pathname.startsWith(p));

  if (!isProtected) return NextResponse.next();

  // NOTE: Véktor actualmente usa localStorage para el token JWT, no cookies httpOnly.
  // La protección real de rutas es client-side (ProtectedLayout + AuthHydrationBoundary).
  // Este middleware establece la estructura para una futura migración a httpOnly cookies,
  // que permitirá verificación real en el edge sin exponer el token a JS.
  // TODO post-MVP: migrar token a cookie httpOnly y agregar verificación aquí.

  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
