/**
 * Rótulos legibles de los roles y de los permisos de caja (B5).
 *
 * El backend manda el código crudo (`CASHIER`, `OWNER`…); mostrarlo tal cual en
 * la barra lateral o en Ajustes deja ver "CASHIER" a quien no sabe inglés.
 */

export const CASHIER_ROLE = "CASHIER";

const ROLE_LABELS: Record<string, string> = {
  OWNER: "Dueño",
  ADMIN: "Administrador",
  ANALYST: "Analista",
  VIEWER: "Solo lectura",
  CASHIER: "Cajero",
  SUPERADMIN: "Soporte Véktor",
};

export function roleLabel(role: string | null | undefined): string {
  if (!role) return "—";
  return ROLE_LABELS[role] ?? role;
}

/** Espejo de `PosPermission` (backend/app/domain/pos_permissions.py). */
export const POS_PERMISSIONS = [
  { key: "discount", label: "Aplicar descuentos" },
  { key: "fiado", label: "Vender fiado" },
  { key: "learn_barcode", label: "Vincular códigos de barras" },
  { key: "void_ticket", label: "Anular sus tickets (con su PIN)" },
] as const;

export type PosPermissionKey = (typeof POS_PERMISSIONS)[number]["key"];

/** Roles que pueden cobrar en la caja (espejo del `require_role` de `POST /pos/operations`). */
export const POS_ROLES: ReadonlySet<string> = new Set(["OWNER", "ADMIN", CASHIER_ROLE]);

/**
 * ¿El usuario tiene este permiso de caja? Lee `pos_permissions`, que `/auth/me`
 * devuelve YA EFECTIVO (OWNER/ADMIN con todos). Es sólo para mostrar u ocultar
 * controles: quien decide es el servidor.
 */
export function hasPosPermission(
  user: { role?: string; pos_permissions?: string[] } | null | undefined,
  permission: PosPermissionKey,
): boolean {
  // OWNER/ADMIN los tienen todos, como en `effective_pos_permissions`: una
  // sesión guardada antes de B5, abierta sin red, todavía no trae la lista.
  if (user?.role === "OWNER" || user?.role === "ADMIN") return true;
  return Boolean(user?.pos_permissions?.includes(permission));
}
