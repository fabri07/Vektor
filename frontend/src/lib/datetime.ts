/**
 * Formatea un timestamp ISO como "05 jun 2026 14:30".
 * Oculta la hora cuando es medianoche (datos legacy / importados sin hora).
 */
export function formatDateTime(value: unknown): string {
  if (value == null || value === "") return "—";
  const d = new Date(String(value));
  if (isNaN(d.getTime())) return String(value);
  const datePart = d.toLocaleDateString("es-AR", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
  if (d.getHours() === 0 && d.getMinutes() === 0) return datePart;
  const timePart = d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
  return `${datePart} ${timePart}`;
}

/**
 * Parsea un string date-only "YYYY-MM-DD" como fecha LOCAL.
 * `new Date("YYYY-MM-DD")` interpreta medianoche UTC: en UTC-3 el día (y en el
 * borde de año, el año) retrocede uno al formatear/leer en hora local.
 */
export function parseDateOnly(value: string): Date {
  const [y, m, d] = value.slice(0, 10).split("-").map(Number);
  return new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1);
}

/** Valor para un <input type="datetime-local"> (YYYY-MM-DDTHH:mm) en hora local. */
export function toDatetimeLocal(value?: string | Date | null): string {
  const d = value ? new Date(value) : new Date();
  if (isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}
