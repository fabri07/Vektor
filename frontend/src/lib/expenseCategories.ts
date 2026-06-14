/**
 * Catálogo canónico de categorías de gasto — espejo de
 * backend/app/domain/expense_categories.py (single source of truth backend).
 * Si se agrega un código allá, agregarlo acá con label y variant.
 */

export const CATEGORY_LABELS: Record<string, string> = {
  RENT: "Alquiler",
  UTILITIES: "Servicios",
  PAYROLL: "Sueldos",
  INVENTORY: "Mercadería",
  MARKETING: "Marketing",
  BANK_FEES: "Comisiones bancarias",
  TAXES: "Impuestos",
  INSURANCE: "Seguros",
  LOGISTICS: "Logística",
  MAINTENANCE: "Mantenimiento",
  SUPPLIES: "Insumos",
  PROFESSIONAL_SERVICES: "Profesionales",
  OTHER: "Otros",
};

export type BadgeVariant = "default" | "info" | "warning" | "danger" | "success";

export const CATEGORY_VARIANTS: Record<string, BadgeVariant> = {
  RENT: "info",
  UTILITIES: "warning",
  PAYROLL: "danger",
  INVENTORY: "success",
  MARKETING: "info",
  BANK_FEES: "warning",
  TAXES: "danger",
  INSURANCE: "info",
  LOGISTICS: "info",
  MAINTENANCE: "warning",
  SUPPLIES: "success",
  PROFESSIONAL_SERVICES: "info",
  OTHER: "default",
};

export const ALL_CATEGORIES = Object.keys(CATEGORY_LABELS);

/**
 * Literales viejos que pueden existir en datos previos al deploy del catálogo
 * canónico, hasta que corra el backfill. NO van al dropdown (no son códigos
 * seleccionables), solo evitan mostrar el código crudo en badges/desgloses.
 */
export const LEGACY_CATEGORY_LABELS: Record<string, string> = {
  compra_proveedor: "Compras",
  salida_caja: "Salida de caja",
  importado: "Otros",
};

/** Label visible para una categoría: canónica → label; OTHER con nombre custom → ese nombre. */
export function categoryDisplay(category: string, categoryLabel?: string | null): string {
  if (category === "OTHER" && categoryLabel) return categoryLabel;
  return CATEGORY_LABELS[category] ?? LEGACY_CATEGORY_LABELS[category] ?? category;
}

/**
 * Tipo contable. Se deriva ESTRICTAMENTE de `expense_type` (COGS/OPEX), nunca
 * de la categoría ni de un label custom. Un gasto sin `expense_type` definido se
 * trata como Operativo (OPEX) por convención del backend.
 *  - COGS → "Mercadería" (costo de mercadería vendida) — variant success (verde)
 *  - OPEX → "Operativo"  (gasto operativo)             — variant info    (azul)
 */
export type ExpenseAccountingType = "COGS" | "OPEX";

export function accountingTypeDisplay(expenseType?: string | null): {
  label: string;
  variant: BadgeVariant;
} {
  return expenseType === "COGS"
    ? { label: "Mercadería", variant: "success" }
    : { label: "Operativo", variant: "info" };
}
