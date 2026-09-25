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
