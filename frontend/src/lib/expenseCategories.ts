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

/** Label visible para una categoría: canónica → label; OTHER con nombre custom → ese nombre. */
export function categoryDisplay(category: string, categoryLabel?: string | null): string {
  if (category === "OTHER" && categoryLabel) return categoryLabel;
  return CATEGORY_LABELS[category] ?? category;
}
