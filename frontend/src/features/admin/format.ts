/** Formatos y rótulos del panel de plataforma. */

export const ESTADOS_DE_CUENTA: { value: string; label: string }[] = [
  { value: "ACTIVE", label: "Activa" },
  { value: "TRIAL", label: "En prueba" },
  { value: "READ_ONLY", label: "Solo lectura" },
  { value: "SUSPENDED", label: "Suspendida" },
];

const ROTULO_CUENTA: Record<string, string> = Object.fromEntries(
  ESTADOS_DE_CUENTA.map((e) => [e.value, e.label]),
);

export function rotuloDeCuenta(estado: string): string {
  return ROTULO_CUENTA[estado] ?? `Desconocido (${estado})`;
}

const ROTULO_SUSCRIPCION: Record<string, string> = {
  TRIAL: "En prueba",
  ACTIVE: "Activa",
  GRACE: "En gracia",
  READ_ONLY: "Solo lectura",
  CANCELLED: "Cancelada",
};

export function rotuloDeSuscripcion(estado: string | null): string {
  if (!estado) return "Sin suscripción viva";
  return ROTULO_SUSCRIPCION[estado] ?? estado;
}

export function fechaCorta(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit", year: "numeric" });
}

export function fechaYHora(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("es-AR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function entero(valor: number): string {
  return (Number.isFinite(valor) ? valor : 0).toLocaleString("es-AR");
}

/** Qué pasa con la cuenta al pasar a cada estado (se muestra antes de confirmar). */
export const CONSECUENCIAS: Record<string, string> = {
  ACTIVE: "La cuenta vuelve a operar con normalidad.",
  TRIAL: "La cuenta opera como en período de prueba.",
  READ_ONLY:
    "Los usuarios pueden entrar y consultar sus datos, pero no cargar ni modificar nada. " +
    "Las ventas de caja cobradas sin conexión antes de este momento se siguen aceptando " +
    "(hasta 72 h atrás); las posteriores quedan en la cola de la caja.",
  SUSPENDED:
    "Nadie de la cuenta puede entrar ni renovar su sesión, y las sesiones abiertas se " +
    "cortan en el siguiente pedido. Los datos se conservan. Para que una caja con ventas " +
    "pendientes pueda enviarlas, usá «Solo lectura» en lugar de suspender.",
};
