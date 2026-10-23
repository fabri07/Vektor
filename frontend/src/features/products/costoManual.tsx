import type { TratamientoEnvio } from "@/services/products.service";

/**
 * M·P2c: lo que se carga a mano tiene costo, igual que un dato importado
 * (decisión del dueño, 2026-10-14). El precio de compra es obligatorio cuando
 * entra stock; aclarar el envío, no.
 */
export const COSTO_REQUERIDO_CON_STOCK =
  "Para cargar stock hace falta el precio de compra (el costo por unidad). Si incluye el envío o no, es opcional.";

/**
 * M·2d: el stock inicial dice desde cuándo lo tiene el negocio. La fecha la da
 * el usuario: el costo de las ventas sigue a esa fecha, no a la de la carga.
 */
export const FECHA_STOCK_INICIAL_REQUERIDA =
  "Indicá desde qué fecha tenés este stock inicial.";

/**
 * B·5c: qué es el stock inicial, con las palabras del dueño (2026-10-20). Lo
 * comparten la tabla, el onboarding y la edición del producto.
 */
export const QUE_ES_STOCK_INICIAL =
  "Es la mercadería que compraron vos o tus socios: se carga al costo; el precio de venta va aparte.";

/** "YYYY-MM-DD" de hoy en hora local: el `max` de la fecha del stock inicial. */
export function hoyLocal(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Un día "YYYY-MM-DD" del input → lo que espera el servidor (hora local, sin zona). */
export function fechaDeStockInicial(dia: string): string {
  return `${dia}T00:00:00`;
}

/** Clave de idempotencia nueva por intento. */
export function nuevaClaveIdempotencia(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export const OPCIONES_ENVIO:{ value: TratamientoEnvio | ""; label: string }[] = [
  { value: "", label: "No sé" },
  { value: "incluido", label: "Sí, el precio incluye el envío" },
  { value: "no_aplica", label: "No hubo envío" },
  { value: "adicional", label: "El envío lo pagué aparte" },
];

const selectClass =
  "h-9 w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-3 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/15 focus:border-vk-blue/40 disabled:opacity-40";

export function EnvioSelect({
  value,
  onChange,
}: {
  value: TratamientoEnvio | "";
  onChange: (v: TratamientoEnvio | "") => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor="costo-envio" className="text-xs font-medium text-vektor-body">
        ¿El precio de compra incluye el envío? (opcional)
      </label>
      <select
        id="costo-envio"
        value={value}
        onChange={(e) => onChange(e.target.value as TratamientoEnvio | "")}
        className={selectClass}
      >
        {OPCIONES_ENVIO.map((o) => (
          <option key={o.value || "desconocido"} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {value === "" && (
        <p className="text-xs text-vk-text-secondary">
          Sin este dato el margen se muestra como estimado.
        </p>
      )}
    </div>
  );
}
