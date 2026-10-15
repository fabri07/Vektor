import type { TratamientoEnvio } from "@/services/products.service";

/**
 * M·P2c: lo que se carga a mano tiene costo, igual que un dato importado
 * (decisión del dueño, 2026-10-14). El precio de compra es obligatorio cuando
 * entra stock; aclarar el envío, no.
 */
export const COSTO_REQUERIDO_CON_STOCK =
  "Para cargar stock hace falta el precio de compra (el costo por unidad). Si incluye el envío o no, es opcional.";

export const OPCIONES_ENVIO: { value: TratamientoEnvio | ""; label: string }[] = [
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
