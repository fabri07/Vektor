"use client";

import type { ShippingAction } from "@/services/ingestion.service";

/**
 * F-H6.b — qué hacer con los envíos de esta hoja que no traen comprobante.
 *
 * Véktor no puede deducirlo: un «Envío 2.000» repetido en diez filas es
 * indistinguible de diez envíos de 2.000, y las dos suposiciones cambian los
 * costos del negocio. Quien armó la planilla sí lo sabe, así que lo declara acá.
 *
 * **Sin elegir no se cobra nada**, y eso no es un bug: es el default seguro. El
 * control aparece sólo cuando la hoja mapea una columna de envío y NO mapea el
 * número de comprobante — con comprobante, Véktor agrupa solo y no hay nada que
 * preguntar.
 */
const OPCIONES: Array<{ value: ShippingAction; title: string; desc: string }> = [
  {
    value: "una_por_hoja",
    title: "Es un solo envío",
    desc: "El flete de esta hoja se cobra una vez, aunque figure en cada línea. Si hay dos importes distintos, se cobra uno por importe.",
  },
  {
    value: "una_por_fila",
    title: "Cada fila es un envío",
    desc: "Cada línea con envío genera su propio gasto. Diez filas de $2.000 son $20.000 de logística.",
  },
];

export function ShippingDecisionChoice({
  value,
  onChange,
  className,
}: {
  /** `null` = todavía no eligió: no se cobra ningún envío de esta hoja. */
  value: ShippingAction | null;
  onChange: (v: ShippingAction | null) => void;
  className?: string;
}) {
  return (
    <div className={className}>
      <p className="text-xs font-semibold text-vk-text-primary">
        Esta hoja trae envíos sin número de comprobante
      </p>
      <p className="mb-2 text-[11px] text-vk-text-muted">
        Sin el comprobante no podemos saber si es un envío repetido en varias
        líneas o varios envíos distintos. Si no elegís, no se registra ninguno.
      </p>
      <div className="flex flex-col gap-2 sm:flex-row">
        {OPCIONES.map((opt) => {
          const active = value === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              // Volver a tocar la opción activa la desmarca: es la única forma de
              // volver al default (no registrar) sin recargar la pantalla.
              onClick={() => onChange(active ? null : opt.value)}
              aria-pressed={active}
              className={`flex-1 rounded-lg border px-3 py-2 text-left transition-colors ${
                active
                  ? "border-vk-blue bg-vk-info-bg"
                  : "border-vk-border-w hover:bg-vk-bg-light"
              }`}
            >
              <span
                className={`block text-xs font-medium ${
                  active ? "text-vk-blue" : "text-vk-text-primary"
                }`}
              >
                {opt.title}
              </span>
              <span className="mt-0.5 block text-[11px] text-vk-text-muted">
                {opt.desc}
              </span>
            </button>
          );
        })}
      </div>
      {value === null && (
        <p className="mt-2 text-[11px] text-vk-text-muted">
          También podés mapear la columna del número de comprobante y volver a
          intentar: con ese dato el envío se agrupa solo.
        </p>
      )}
    </div>
  );
}
