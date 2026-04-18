import Link from "next/link";

export function EmptyState() {
  return (
    <div className="flex flex-1 items-center justify-center">
      <div className="max-w-md rounded-lg border border-vk-border-w bg-vk-surface-w p-10 text-center shadow-vk-sm">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-vk-bg-light">
          <svg
            className="h-7 w-7 text-vk-text-muted"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z"
            />
          </svg>
        </div>
        <h2 className="mb-2 text-base font-semibold text-vk-text-primary">
          Tu dashboard está vacío
        </h2>
        <p className="mb-5 text-sm text-vk-text-muted">
          Aún no hay datos suficientes para calcular el score. Podés agregar
          información de tres formas:
        </p>
        <div className="mb-6 space-y-2 text-left">
          <div className="flex items-start gap-2.5 rounded-lg bg-vk-bg-light px-4 py-2.5">
            <span className="mt-0.5 text-base">💬</span>
            <div>
              <p className="text-xs font-medium text-vk-text-primary">Chat</p>
              <p className="text-xs text-vk-text-muted">
                Escribí &ldquo;vendí 80 mil&rdquo; o &ldquo;pagué alquiler 200
                mil&rdquo;
              </p>
            </div>
          </div>
          <div className="flex items-start gap-2.5 rounded-lg bg-vk-bg-light px-4 py-2.5">
            <span className="mt-0.5 text-base">📁</span>
            <div>
              <p className="text-xs font-medium text-vk-text-primary">Cargar archivo</p>
              <p className="text-xs text-vk-text-muted">
                Subí un Excel o CSV con ventas desde la sección de carga
              </p>
            </div>
          </div>
          <div className="flex items-start gap-2.5 rounded-lg bg-vk-bg-light px-4 py-2.5">
            <span className="mt-0.5 text-base">🚀</span>
            <div>
              <p className="text-xs font-medium text-vk-text-primary">Onboarding</p>
              <p className="text-xs text-vk-text-muted">
                Completá el cuestionario inicial si todavía no lo hiciste
              </p>
            </div>
          </div>
        </div>
        <div className="flex gap-2 justify-center">
          <Link
            href="/chat"
            className="inline-flex h-9 items-center justify-center rounded-lg bg-vk-blue px-4 text-sm font-medium text-white transition-colors hover:bg-vk-blue-hover"
          >
            Ir al chat
          </Link>
          <Link
            href="/ingestion"
            className="inline-flex h-9 items-center justify-center rounded-lg border border-vk-border-w bg-vk-surface-w px-4 text-sm font-medium text-vk-text-secondary transition-colors hover:bg-vk-bg-light"
          >
            Cargar archivo
          </Link>
        </div>
      </div>
    </div>
  );
}
