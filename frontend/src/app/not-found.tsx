import Link from "next/link";

export default function NotFoundPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-vk-bg-light px-6">
      <div className="max-w-md w-full text-center">
        {/* 404 — visible pero no prominente */}
        <p
          className="mb-2 font-bold leading-none select-none text-vk-border-w"
          style={{ fontSize: "96px" }}
        >
          404
        </p>

        <h1 className="mb-2 text-xl font-semibold text-vk-text-primary">
          Esta página no existe
        </h1>
        <p className="mb-8 text-sm text-vk-text-muted">
          La URL que ingresaste no corresponde a ninguna sección de Véktor.
        </p>

        <Link
          href="/dashboard"
          className="inline-flex items-center justify-center rounded-lg bg-vk-blue px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40"
        >
          Ir al dashboard
        </Link>
      </div>
    </div>
  );
}
