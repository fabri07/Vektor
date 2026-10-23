"use client";

import type {
  PurchaseCostBase,
  PurchaseLineShipping,
  PurchaseSharedShipping,
} from "@/services/ingestion.service";

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
 * Son TRES ejes independientes y se muestran separados a propósito. El primero
 * dice si al monto hay que aplicarle los ajustes; el segundo, qué hacer con el
 * envío que el comprobante cobra UNA vez y hay que repartir entre sus líneas; el
 * tercero, si el flete que YA viene asignado a cada línea se capitaliza en la
 * mercadería o queda como gasto. Fusionarlos obligaría a elegir varias cosas
 * juntas cuando son preguntas distintas: un mismo remito puede traer un envío
 * global para repartir y además un flete por línea, y son dos plata distintas.
 *
 * El eje del envío compartido aparece sólo cuando el SERVIDOR dice que esa hoja
 * se puede repartir (`puede_distribuir`). No hay una regla propia acá: si la
 * pantalla ofreciera repartir donde el importador no puede, el usuario elegiría
 * algo que no va a pasar.
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

// El default es `no_distribuir` y no se toca: cambiarlo movería el costo de
// todos los imports que ya se hicieron con el comportamiento anterior.
const COMPARTIDOS: Array<{
  value: PurchaseSharedShipping;
  title: string;
  desc: string;
}> = [
  {
    value: "no_distribuir",
    title: "Queda como gasto aparte",
    desc: "El envío del comprobante se registra como gasto y no cambia el costo de ningún producto.",
  },
  {
    value: "por_subtotal",
    title: "Se reparte entre los productos del comprobante",
    desc: "El envío se divide entre las líneas en proporción a lo que costó cada una, y pasa a formar parte del costo de esa mercadería.",
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
  sharedShipping,
  lineShipping,
  onBaseChange,
  onSharedShippingChange,
  onLineShippingChange,
  mostrarAjustes,
  mostrarEnvioCompartido,
  mostrarFleteDeLinea,
  className,
}: {
  base: PurchaseCostBase;
  sharedShipping: PurchaseSharedShipping;
  lineShipping: PurchaseLineShipping;
  onBaseChange: (v: PurchaseCostBase) => void;
  onSharedShippingChange: (v: PurchaseSharedShipping) => void;
  onLineShippingChange: (v: PurchaseLineShipping) => void;
  /** La hoja mapea descuento o impuestos. */
  mostrarAjustes: boolean;
  /**
   * La hoja mapea el envío del comprobante Y el servidor dice que se puede
   * repartir. Lo decide el padre con la respuesta de `/purchase-groups`: acá no
   * hay ninguna regla propia sobre cuándo un reparto es posible.
   */
  mostrarEnvioCompartido: boolean;
  /** La hoja mapea el envío ya asignado a cada línea. */
  mostrarFleteDeLinea: boolean;
  className?: string;
}) {
  if (!mostrarAjustes && !mostrarEnvioCompartido && !mostrarFleteDeLinea) return null;
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
        {mostrarEnvioCompartido && (
          <Eje
            titulo="El envío que el comprobante cobra una sola vez, ¿se reparte entre sus productos?"
            ayuda="Es una decisión, no un dato del archivo: el mismo remito puede tratarse como gasto de logística o como parte de lo que costó la mercadería."
            opciones={COMPARTIDOS}
            value={sharedShipping}
            onChange={onSharedShippingChange}
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
