import { api } from "@/lib/api";

/** M·P2c: lo que se declara del envío en un precio de compra cargado a mano.
 *  Mismo vocabulario que el contrato de costo del backend (M·1). */
export type TratamientoEnvio = "incluido" | "no_aplica" | "adicional" | "desconocido";

export interface CreateProductPayload {
  name: string;
  sale_price_ars: number;
  unit_cost_ars?: number | null;
  /** Precio de lista/sugerido por proveedor. Informativo: no entra al margen. */
  list_price_ars?: number | null;
  stock_units?: number;
  /** M·P2c: opcional; sin respuesta el margen sale estimado. */
  costo_envio?: TratamientoEnvio;
  // null = usar DEFAULT_LOW_STOCK_THRESHOLD_UNITS del servidor; 0 = umbral explícito
  low_stock_threshold_units?: number | null;
  category?: string | null;
  sku?: string | null;
  description?: string | null;
  acquired_at?: string | null;
  custom_fields?: Record<string, unknown>;
  /** Unidad de venta (B3): stock y umbral en unidades base; precio y costo
   *  por unidad de venta. Van juntos. */
  sale_unit?: "unit" | "gram" | "milliliter";
  base_units_per_sale_unit?: number;
}

export type UpdateProductPayload = Partial<CreateProductPayload> & {
  is_active?: boolean;
  /** M·P2c: el stock que se veía al editar. Si cambió en el medio, el servidor
   *  responde 409 STOCK_CAMBIO en vez de pisar una venta. */
  stock_units_esperado?: number;
};

export interface ProductResponse {
  id: string;
  tenant_id: string;
  name: string;
  /** El código que aporta el archivo o el proveedor. Puede faltar. */
  sku: string | null;
  /**
   * El código propio de Véktor (`VKT-…`), generado al crear el producto e
   * inmutable. Es lo que se muestra cuando el archivo no trajo `sku`.
   * `null`/ausente sólo en productos anteriores a la migración que lo agregó.
   */
  internal_sku?: string | null;
  description: string | null;
  category: string | null;
  /** B·3: categoría SUGERIDA por el nombre (sólo si `category` está vacía y la
   *  inferencia es de confianza alta). No está guardada: se fija aceptándola con
   *  el PATCH de siempre. Ausente en servidores viejos. */
  category_suggestion?: { code: string; label: string } | null;
  sale_price_ars: number;
  unit_cost_ars: number | null;
  /** M·P4: costo promedio ponderado. DATO, no el costo principal (el margen y
   *  el valor de stock siguen con `unit_cost_ars`, el de reposición). `null` en
   *  cuentas sin habilitar o en productos sin historial. Es el último publicado:
   *  nunca dice «recalculando». */
  avg_cost_ars?: number | string | null;
  avg_cost_estado?: "medido" | "incompleto" | "sin_medicion" | null;
  avg_cost_motivo?: string | null;
  /** Precio de lista/sugerido por proveedor. Informativo: no entra al margen. */
  list_price_ars: number | null;
  /** Unidad de venta (B3). El stock está en unidades BASE; precio y costo, por
   *  unidad de venta: toda cuenta stock × costo divide por el factor. */
  sale_unit?: "unit" | "gram" | "milliliter";
  base_units_per_sale_unit?: number;
  /** El stock ya formateado por el servidor ("1,250 kg", "12 un."). */
  stock_display?: string;
  stock_units: number;
  // null = umbral no configurado (servidor aplica DEFAULT_LOW_STOCK_THRESHOLD_UNITS = 5)
  // 0   = umbral explícito: solo sin-stock aplica
  low_stock_threshold_units: number | null;
  is_active: boolean;
  // FASE 3 (B2): producto auto-creado por import al que le faltan precio/costo.
  requires_completion?: boolean;
  margin_pct: number | null;
  /** M·3-2: por qué no hay margen de lista (costo 0, sin costo, sin precio). */
  margin_motivo?: string | null;
  is_low_stock: boolean;
  stock_status: "in_stock" | "low_stock" | "out_of_stock";
  // F6-B4: estado de vencimiento a nivel producto (null = sin vencimiento conocido).
  expiry_status?: "expired" | "expiring_soon" | "ok" | null;
  expiry_date?: string | null;
  custom_fields?: Record<string, unknown>;
  acquired_at?: string | null;
  created_at?: string;
}

export interface ProductsListParams {
  is_active?: boolean;
  limit?: number;
  offset?: number;
}

/** Categoría canónica del catálogo del vertical del tenant. */
export interface ProductCategoryOption {
  code: string;
  label: string;
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;
// Corrección C4 (revisión externa 2026-08-19): `getAllProducts` acumula hasta
// esta cantidad y corta en silencio — el caller necesita saber el techo para
// poder avisar si lo alcanzó de verdad (ver `countProducts`).
export const PRODUCTS_MAX_ACCUMULATED = PAGE_SIZE * MAX_PAGES;

export const productsService = {
  async createProduct(
    payload: CreateProductPayload,
    idempotencyKey?: string,
  ): Promise<ProductResponse> {
    const res = await api.post<ProductResponse>(
      "/products",
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },

  async updateProduct(id: string, payload: UpdateProductPayload): Promise<ProductResponse> {
    const res = await api.patch<ProductResponse>(`/products/${id}`, payload);
    return res.data;
  },

  async deleteProduct(id: string): Promise<void> {
    await api.delete(`/products/${id}`);
  },

  async getCategories(): Promise<ProductCategoryOption[]> {
    const res = await api.get<ProductCategoryOption[]>("/products/categories");
    return res.data;
  },

  // Crea una categoría de producto personalizada del tenant (reutilizable).
  async createCategory(label: string): Promise<ProductCategoryOption> {
    const res = await api.post<ProductCategoryOption>("/products/custom-categories", { label });
    return res.data;
  },

  async getProducts(params?: ProductsListParams): Promise<ProductResponse[]> {
    const res = await api.get<ProductResponse[]>("/products", { params });
    return res.data;
  },

  async getAllProducts(
    params?: Omit<ProductsListParams, "limit" | "offset">,
  ): Promise<ProductResponse[]> {
    const items: ProductResponse[] = [];

    for (let page = 0; page < MAX_PAGES; page += 1) {
      const batch = await productsService.getProducts({
        ...params,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
      items.push(...batch);

      if (batch.length < PAGE_SIZE) {
        break;
      }
    }

    return items;
  },

  // Corrección C4: total real del catálogo (mismos filtros que `getProducts`/
  // `getAllProducts`, sin `limit`/`offset`) — permite avisar si `getAllProducts`
  // cortó en `PRODUCTS_MAX_ACCUMULATED` sin haber traído todo.
  async countProducts(
    params?: Omit<ProductsListParams, "limit" | "offset">,
  ): Promise<number> {
    const res = await api.get<{ total: number }>("/products/count", { params });
    return res.data.total;
  },
};
