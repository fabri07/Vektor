"use client";

import Link from "next/link";

interface ErrorPageProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function ErrorPage({ reset }: ErrorPageProps) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-vk-bg-light px-6">
      <div className="max-w-md w-full text-center">
        {/* Ícono */}
        <div className="mx-auto mb-6 flex h-16 w-16 items-center justify-center rounded-full bg-vk-warning-bg">
          <svg
            className="h-8 w-8 text-vk-warning"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
            />
          </svg>
        </div>

        <h1 className="mb-2 text-xl font-semibold text-vk-text-primary">
          Algo salió mal
        </h1>
        <p className="mb-8 text-sm text-vk-text-muted">
          Ocurrió un error inesperado. Podés intentar de nuevo o volver al chat.
        </p>

        <div className="flex flex-col gap-3 sm:flex-row sm:justify-center">
          <button
            type="button"
            onClick={reset}
            className="inline-flex items-center justify-center rounded-lg bg-vk-blue px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-vk-blue-hover focus:outline-none focus:ring-2 focus:ring-vk-blue/40"
          >
            Reintentar
          </button>
          <Link
            href="/chat"
            className="inline-flex items-center justify-center rounded-lg border border-vk-border-w bg-vk-surface-w px-5 py-2.5 text-sm font-medium text-vk-text-secondary transition-colors hover:border-vk-border-w-hover hover:text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
          >
            Volver al chat
          </Link>
        </div>
      </div>
    </div>
  );
}
