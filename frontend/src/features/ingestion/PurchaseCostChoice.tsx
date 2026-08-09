"use client";

import type { PurchaseCostBase, PurchaseLineShipping } from "@/services/ingestion.service";

/**
 * F-H6.c — cómo se calcula el costo de las líneas de esta hoja de compras.
 *
 * Aparece sólo cuando la hoja mapea alguna columna que ajusta el costo. Sin esas
 * columnas no hay nada que preguntar, y preguntar de más en una pantalla que ya
 * es larga es su propio problema.
 *
 * **Los defaults no cambian ningún número**, igual que el remito manual: el monto
 * de la fila se toma como final y el flete de línea queda como gasto aparte.
 * Aplicar ajustes o capitalizar el flete son decisiones explícitas. Lo que el
 * backend NO hace es callarse: si mapeaste un descuento y no declarás nada, el
 * confirm avisa que ese valor no movió el costo.
 *
 * Son dos ejes independientes y se muestran separados a propósito. El primero
 * dice si al monto hay que aplicarle los ajustes; el segundo, si el flete que ya
 * viene asignado a la línea se capitaliza en la mercadería o queda como gasto.
 * Fusionarlos obligaría a elegir las dos cosas juntas cuando son preguntas
 * distintas.
 */

const BASES: Array<{ value: PurchaseCostBase; title: string; desc: string }> = [
  {
    value: "monto_incluye",
    title: "El monto ya es el final",
    desc: "La columna de monto ya tiene el descuento y los impuestos adentro. No se recalcula nada.",
  },
  {
    value: "monto_sin_ajustes",
    title: "El monto es el bruto",
    desc: "Al monto de cada fila se le resta el descuento y se le suman los impuestos para saber qué costó.",
  },
];

const FLETES: Array<{ value: PurchaseLineShipping; title: string; desc: string }> = [
  {
    value: "gasto_aparte",
    title: "Es un gasto de logística",
    desc: "El envío de cada línea se registra como gasto y no cambia el costo del producto.",
  },
  {
    value: "al_costo",
    title: "Es parte de lo que costó",
    desc: "El envío de cada línea se suma al costo de esa mercadería. Sube el costo unitario y la valuación del stock.",
  },
];

function Eje<T extends string>({
  titulo,
  ayuda,
  opciones,
  value,
  onChange,
}: {
  titulo: string;
  ayuda: string;
  opciones: Array<{ value: T; title: string; desc: string }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div>
      <p className="text-xs font-semibold text-vk-text-primary">{titulo}</p>
      <p className="mb-2 text-[11px] text-vk-text-muted">{ayuda}</p>
      <div className="flex flex-col gap-1">
        {opciones.map((o) => (
          <label
            key={o.value}
            className={`flex cursor-pointer items-start gap-2 rounded border px-2 py-1.5 ${
              value === o.value
                ? "border-vk-blue bg-vk-blue/5"
                : "border-vk-border-w hover:border-vk-blue/40"
            }`}
          >
            <input
              type="radio"
              className="mt-0.5"
              checked={value === o.value}
              onChange={() => onChange(o.value)}
            />
            <span className="min-w-0">
              <span className="block text-[11px] font-medium text-vk-text-primary">
                {o.title}
              </span>
              <span className="block text-[11px] leading-snug text-vk-text-muted">
                {o.desc}
              </span>
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

export function PurchaseCostChoice({
  base,
  lineShipping,
  onBaseChange,
  onLineShippingChange,
  mostrarAjustes,
  mostrarFleteDeLinea,
  className,
}: {
  base: PurchaseCostBase;
  lineShipping: PurchaseLineShipping;
  onBaseChange: (v: PurchaseCostBase) => void;
  onLineShippingChange: (v: PurchaseLineShipping) => void;
  /** La hoja mapea descuento o impuestos. */
  mostrarAjustes: boolean;
  /** La hoja mapea el envío ya asignado a cada línea. */
  mostrarFleteDeLinea: boolean;
  className?: string;
}) {
  if (!mostrarAjustes && !mostrarFleteDeLinea) return null;
  return (
    <div className={className}>
      <div className="flex flex-col gap-3">
        {mostrarAjustes && (
          <Eje
            titulo="¿El monto de cada fila ya incluye el descuento y los impuestos?"
            ayuda="Restarle un descuento a un total que ya lo tiene descontado lo contaría dos veces, y eso no se puede deducir del archivo."
            opciones={BASES}
            value={base}
            onChange={onBaseChange}
          />
        )}
        {mostrarFleteDeLinea && (
          <Eje
            titulo="El envío que ya viene asignado a cada línea, ¿entra al costo?"
            ayuda="No se reparte nada: el reparto ya lo hizo quien armó la planilla. Lo único que falta decidir es si ese costo se capitaliza en la mercadería."
            opciones={FLETES}
            value={lineShipping}
            onChange={onLineShippingChange}
          />
        )}
      </div>
    </div>
  );
}
