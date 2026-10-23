"use client";

/**
 * Cierre del onboarding. NO muestra puntaje, a propósito.
 *
 * Véktor no depura ni organiza los datos de un archivo en la primera pasada:
 * el usuario tiene que definir qué significa cada columna de sus registros
 * antes de que se importe nada. Hasta esa confirmación no hay ventas, gastos
 * ni productos en la base — y un puntaje de salud sin movimientos reales no
 * mide el negocio, mide lo que la persona escribió en un formulario.
 *
 * Antes este paso polleaba `/health-scores/latest` y pintaba lo que viniera.
 * Recién terminado el alta eso es `{status: "NO_DATA"}`, que se casteaba a
 * score: `Math.round(Number(undefined))` → **`NaN/100`** en la cara del
 * usuario, y 2,5s después un redirect al dashboard.
 */

import { useRouter } from "next/navigation";

interface Step4LoadingProps {
  /** Archivo subido en el paso 2, si hubo. Define a dónde sigue el usuario. */
  uploadedFileId: string | null;
}

function BotonPrimario({
  children,
  onClick,
}: {
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="mt-6 h-11 rounded-xl bg-vk-navy px-8 text-sm font-semibold text-white transition-opacity hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-vk-navy/30"
    >
      {children}
    </button>
  );
}

export function Step4Loading({ uploadedFileId }: Step4LoadingProps) {
  const router = useRouter();

  if (uploadedFileId) {
    return (
      <div className="flex flex-col items-center py-8 text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-vk-blue/10">
          <svg
            className="h-7 w-7 text-vk-blue"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
            />
          </svg>
        </div>

        <h2 className="mt-5 text-xl font-semibold text-vk-text-primary">
          Listo. Ahora falta que revises tu archivo
        </h2>
        <p className="mt-2 max-w-md text-sm leading-6 text-vk-text-muted">
          Ya lo leímos, pero no lo importamos todavía: necesitamos que nos
          digas qué es cada columna de tus registros. Cuando lo confirmes, se
          cargan tus ventas y gastos y ahí sí calculamos tu salud financiera.
        </p>

        <BotonPrimario
          onClick={() => router.replace(`/ingestion?file=${uploadedFileId}`)}
        >
          Revisar mi archivo
        </BotonPrimario>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center py-8 text-center">
      <h2 className="text-xl font-semibold text-vk-text-primary">
        Todavía no hay datos para analizar
      </h2>
      <p className="mt-2 max-w-md text-sm leading-6 text-vk-text-muted">
        Lo que respondiste quedó guardado, pero un puntaje de salud necesita
        movimientos reales de tu negocio. Cargá tus ventas y gastos —por
        archivo o a mano— y lo calculamos.
      </p>

      <BotonPrimario onClick={() => router.replace("/ingestion")}>
        Cargar mis datos
      </BotonPrimario>
    </div>
  );
}
