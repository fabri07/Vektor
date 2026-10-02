/**
 * Las unidades de venta que se pueden elegir para un producto (B3).
 *
 * Espejo de `FACTORES_ADMITIDOS` (backend/app/domain/sale_unit.py): unidad ×1,
 * kilo = gramos ×1000, litro = mililitros ×1000. El stock se guarda en la
 * unidad BASE (g, ml); precio y costo, por unidad de venta (kg, L).
 */

export interface UnidadDeVenta {
  key: "unit" | "kg" | "l";
  label: string;
  saleUnit: "unit" | "gram" | "milliliter";
  factor: number;
  /** Cómo se llama la unidad en la que se escribe la cantidad. */
  etiqueta: string;
  /** Lo que acompaña a "Precio" y "Costo" en el formulario. */
  sufijo: string;
}

export const UNIDADES_DE_VENTA: readonly UnidadDeVenta[] = [
  { key: "unit", label: "Unidad", saleUnit: "unit", factor: 1, etiqueta: "unidades", sufijo: "" },
  { key: "kg", label: "Kilo (se pesa)", saleUnit: "gram", factor: 1000, etiqueta: "kg", sufijo: " por kg" },
  { key: "l", label: "Litro", saleUnit: "milliliter", factor: 1000, etiqueta: "L", sufijo: " por litro" },
];

export function unidadDeVentaDe(
  saleUnit: string | null | undefined,
  factor: number | null | undefined,
): UnidadDeVenta {
  return (
    UNIDADES_DE_VENTA.find((u) => u.saleUnit === saleUnit && u.factor === (factor ?? 1)) ??
    UNIDADES_DE_VENTA[0]!
  );
}
